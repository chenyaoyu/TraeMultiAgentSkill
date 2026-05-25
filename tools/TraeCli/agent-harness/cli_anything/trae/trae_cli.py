from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
import textwrap
import time
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import click

from cli_anything.trae.core.state import SessionState
from cli_anything.trae.utils.trae_backend import (
    CDPBridgeError,
    RPC_CHAT_SESSION_TYPES,
    TraeBackend,
)


@dataclass
class AppContext:
    backend: TraeBackend
    state: SessionState
    json_output: bool
    no_alt_screen: bool = False
    auto_trust_workspace: bool = False

    def rebuild_backend(self) -> None:
        self.backend = TraeBackend(
            app_path=self.state.app_path,
            support_dir=self.state.support_dir,
            user_data_dir=self.state.user_data_dir,
        )


@dataclass
class InteractiveViewState:
    notice: Optional[str] = None
    panel_title: str = "Activity"
    panel_body: str = "Send a prompt to begin."
    mode: str = "chat"
    history: list[str] = field(default_factory=list)
    output_title: str = "Output"
    output_body: str = "No command output yet."
    sessions_payload: Optional[dict[str, Any]] = None
    session_list_index: int = 0
    session_detail_payload: Optional[dict[str, Any]] = None
    session_detail_title: str = "Session Detail"
    input_buffer: str = ""
    input_cursor: int = 0
    slash_palette_index: int = 0
    chat_scroll_offset: int = 0


def normalize(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: normalize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [normalize(item) for item in value]
    return value


def redact_sensitive_json(value: Any) -> Any:
    sensitive_keys = {
        "token",
        "refreshToken",
        "refresh_token",
        "access_token",
        "id_token",
        "session_token",
        "sessionToken",
        "encrypted_model_params",
        "requestPin",
        "RequestPin",
        "encryptedStr",
        "EncryptedStr",
        "ak",
        "sk",
        "custom_config",
    }
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if key in sensitive_keys and item not in (None, ""):
                redacted[key] = "[redacted]"
            else:
                redacted[key] = redact_sensitive_json(item)
        return redacted
    if isinstance(value, list):
        return [redact_sensitive_json(item) for item in value]
    return value


def emit(ctx: AppContext, *, data: Any, text: Optional[str] = None) -> None:
    if ctx.json_output:
        click.echo(
            json.dumps(
                normalize(redact_sensitive_json(data)),
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    if text is not None:
        click.echo(text)
        return
    click.echo(json.dumps(normalize(data), indent=2, ensure_ascii=False))


def parse_json_option(
    raw: Optional[str],
    *,
    option_name: str,
    expect_object: bool = False,
    default: Optional[Any] = None,
) -> Any:
    if raw is None:
        return default
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise click.BadParameter(
            f"{option_name} must be valid JSON: {exc.msg}"
        ) from exc
    if expect_object and not isinstance(payload, dict):
        raise click.BadParameter(f"{option_name} must decode to a JSON object.")
    return payload


def parse_csv_option(raw: Optional[str]) -> Optional[list[str]]:
    if raw is None:
        return None
    items = [item.strip() for item in raw.split(",")]
    parsed = [item for item in items if item]
    return parsed or None


ROOT_COMMAND_NAMES = {
    "bridge",
    "chat",
    "doctor",
    "exec",
    "gui",
    "headless",
    "init",
    "logs",
    "mode",
    "models",
    "mcp",
    "open",
    "repl",
    "resume",
    "sessions",
    "rpc",
    "sandbox",
    "state",
    "trace",
}
ROOT_OPTIONS_WITH_VALUE = {
    "-C",
    "--cd",
    "--workspace",
    "--app-path",
    "--support-dir",
    "--user-data-dir",
}
ROOT_FLAG_OPTIONS = {"--json", "--no-alt-screen", "--trust-workspace"}
MODEL_AGENT_TYPE_CHOICES = ["dev_builder", "solo_coder", "solo_builder"]


def parse_repl_model_command_options(
    parts: list[str],
    *,
    require_model: bool = False,
    start_index: int = 2,
) -> dict[str, Any]:
    agent_type: Optional[str] = None
    reload = True
    model: Optional[str] = None
    index = start_index
    while index < len(parts):
        token = parts[index]
        if token == "--agent-type":
            if index + 1 >= len(parts):
                raise ValueError("--agent-type requires a value.")
            agent_type = parts[index + 1]
            index += 2
            continue
        if token.startswith("--agent-type="):
            agent_type = token.split("=", 1)[1]
            index += 1
            continue
        if token == "--reload":
            reload = True
            index += 1
            continue
        if token == "--no-reload":
            reload = False
            index += 1
            continue
        if token.startswith("--"):
            raise ValueError(f"Unsupported option: {token}")
        if require_model and model is None:
            model = token
            index += 1
            continue
        raise ValueError(f"Unexpected argument: {token}")
    if require_model and not model:
        raise ValueError("A model name is required.")
    return {
        "agent_type": agent_type,
        "reload": reload,
        "model": model,
    }


def rewrite_argv_for_prompt(argv: list[str]) -> list[str]:
    if len(argv) <= 1:
        return argv

    rewritten = [argv[0]]
    index = 1
    saw_json = False
    while index < len(argv):
        token = argv[index]
        if token in {"-h", "--help"}:
            return argv
        if token in ROOT_FLAG_OPTIONS:
            rewritten.append(token)
            saw_json = saw_json or token == "--json"
            index += 1
            continue
        if token in ROOT_OPTIONS_WITH_VALUE:
            if index + 1 >= len(argv):
                return argv
            rewritten.extend(argv[index : index + 2])
            index += 2
            continue
        if any(
            name.startswith("--") and token.startswith(f"{name}=")
            for name in ROOT_OPTIONS_WITH_VALUE
        ):
            rewritten.append(token)
            index += 1
            continue
        if token.startswith("-") or token in ROOT_COMMAND_NAMES:
            return argv
        prompt = " ".join(argv[index:]).strip()
        if not prompt:
            return argv
        return [*rewritten, "exec" if saw_json else "repl", prompt]

    return argv


def is_simple_agent_chat(
    *,
    mode: str,
    add_files: tuple[str, ...],
    new_window: bool,
    reuse_window: bool,
    maximize: bool,
) -> bool:
    return (
        mode == "agent"
        and not add_files
        and not new_window
        and not reuse_window
        and not maximize
    )


def resolve_chat_dispatch_method(
    requested_dispatch_method: str,
    *,
    mode: str,
    add_files: tuple[str, ...],
    new_window: bool,
    reuse_window: bool,
    maximize: bool,
) -> str:
    if requested_dispatch_method != "auto":
        return requested_dispatch_method
    if is_simple_agent_chat(
        mode=mode,
        add_files=add_files,
        new_window=new_window,
        reuse_window=reuse_window,
        maximize=maximize,
    ):
        return "cdp"
    return "cli"


def configured_headless_app(app: AppContext) -> bool:
    return app.backend.paths.app_path.stem.endswith("-headless")


def format_headless_unavailable_message(app: AppContext) -> str:
    workspace = current_workspace(app)
    status_command = "traecli headless status --ping"
    if (
        app.backend.read_bridge_states()
        and app.backend.read_bridge_state(workspace=workspace) is None
    ):
        return (
            "Headless dispatch is unavailable because no running Trae CN window is "
            f"bound to workspace `{workspace}`. Open that folder in Trae, then run "
            f"`{status_command}`."
        )
    if configured_headless_app(app):
        launch_command = f"open -na {shlex.quote(str(app.backend.paths.app_path))}"
        return (
            "Headless dispatch is unavailable for the configured headless app. "
            f"Launch it with `{launch_command}`, then run `{status_command}`."
        )
    return (
        "Headless dispatch is unavailable. Run `traecli init`, then verify the "
        f"bridge with `{status_command}`."
    )


def model_entry_payload(data: dict[str, Any], agent_type: str) -> dict[str, Any]:
    current_models = data.get("current_models") or {}
    entry = current_models.get(agent_type)
    return entry if isinstance(entry, dict) else {}


def model_payload_from_entry(entry: dict[str, Any]) -> dict[str, Any]:
    model = entry.get("model")
    return model if isinstance(model, dict) else {}


def model_display_name(entry: dict[str, Any]) -> str:
    model = model_payload_from_entry(entry)
    return (
        model.get("display_name")
        or model.get("name")
        or entry.get("model_key")
        or "unknown"
    )


def format_current_model_summary(
    data: dict[str, Any],
    *,
    include_legacy: bool = False,
) -> list[str]:
    lines = ["Current models:"]
    current_models = data.get("current_models") or {}
    if current_models:
        for agent_type in MODEL_AGENT_TYPE_CHOICES:
            entry = model_entry_payload(data, agent_type)
            if not entry:
                continue
            lines.append(f"  {agent_type}: {model_display_name(entry)}")
            if entry.get("model_key"):
                lines.append(
                    f"    key={entry.get('model_key')} source={entry.get('source') or 'unknown'}"
                )
    else:
        lines.append("  none")
    if include_legacy:
        selected = data.get("selected_model") or data.get("legacy_selected_model") or {}
        lines.append("Legacy selected model:")
        lines.append(
            f"  {selected.get('display_name') or selected.get('name') or 'unknown'}"
        )
    return lines


def format_doctor(data: dict[str, Any]) -> str:
    app = data["app"]
    auth = data.get("auth") or {}
    state = data.get("state") or {}
    config = data.get("config") or {}
    runtime = data.get("runtime") or {}
    dev_builder_model = model_entry_payload(state, "dev_builder")
    solo_mode = state.get("solo_mode") or {}
    lines = [
        f"App: {app.get('display_name') or 'Trae'} {app.get('bundle_version') or 'unknown'}",
        f"Bundle: {app.get('path')}",
        f"CLI: {app.get('cli_script')}",
        f"Protocol: {app.get('url_protocol')}",
        f"Workspace: {runtime.get('workspace') or 'unknown'}",
        f"Config: {config.get('path') or 'unknown'} ({'present' if config.get('exists') else 'not found'})",
        f"Configured bundle: {config.get('app_path') or 'not set'}",
        f"Support: {data['paths'].get('support_dir')}",
        f"Socket: {data['paths'].get('main_socket') or 'not found'}",
        f"Latest logs: {data['paths'].get('latest_log_session') or 'not found'}",
        f"Auth host: {auth.get('host') or 'unknown'}",
        f"Region: {auth.get('region') or 'unknown'}",
        f"User: {auth.get('username') or 'unknown'}",
        f"Selected model: {model_display_name(dev_builder_model)}",
        f"Mode: {'SOLO' if solo_mode.get('mode') == 'solo' else 'IDE'}",
        f"MCP gallery entries: {data.get('mcp_gallery_count', 0)}",
        f"Sandbox snapshots: {data.get('sandbox_count', 0)}",
        "Data stores:",
        f"  ai-agent: {data['data_stores']['ai_agent_db']['kind']}",
        f"  ckg: {data['data_stores']['ckg_env_db']['kind']}",
    ]
    cache_counts = data.get("model_cache_counts") or {}
    if cache_counts:
        lines.append("Model cache counts:")
        for key, value in sorted(cache_counts.items()):
            lines.append(f"  {key}: {value}")
    return "\n".join(lines)


def format_auth(data: dict[str, Any]) -> str:
    if not data:
        return "No auth record found."
    return "\n".join(
        [
            f"User: {data.get('username') or 'unknown'}",
            f"Email: {data.get('email') or 'unknown'}",
            f"Region: {data.get('region') or 'unknown'}",
            f"Host: {data.get('host') or 'unknown'}",
            f"User ID: {data.get('user_id') or 'unknown'}",
            f"Token expires: {data.get('token_expires_at') or 'unknown'}",
        ]
    )


def format_models(data: dict[str, Any]) -> str:
    selected = data.get("selected_model") or {}
    global_map = data.get("global_model_map") or {}
    agent_mode = data.get("agent_mode") or []
    lines = format_current_model_summary(data, include_legacy=True)
    lines.extend(
        [
            f"  provider={selected.get('provider') or 'unknown'}",
            f"  type={selected.get('model_type') or 'unknown'}",
            "Global model map:",
        ]
    )
    if global_map:
        for key, value in sorted(global_map.items()):
            lines.append(f"  {key}: {value}")
    else:
        lines.append("  none")
    counts = data.get("available_model_counts") or {}
    lines.append("Available model counts:")
    if counts:
        for key, value in sorted(counts.items()):
            lines.append(f"  {key}: {value}")
    else:
        lines.append("  none")
    lines.append("Agent modes:")
    if agent_mode:
        for index, item in enumerate(agent_mode, start=1):
            lines.append(
                f"  #{index}: type={item.get('type')} status={item.get('status')} default={item.get('defaultStatus')}"
            )
    else:
        lines.append("  none")
    return "\n".join(lines)


def format_model_current(data: dict[str, Any]) -> str:
    return "\n".join(format_current_model_summary(data, include_legacy=True))


def format_model_list(data: dict[str, Any]) -> str:
    agents = data.get("agents") or {}
    if not agents:
        return "No available model inventory found."
    lines: list[str] = []
    for index, agent_type in enumerate(MODEL_AGENT_TYPE_CHOICES, start=1):
        payload = agents.get(agent_type)
        if not isinstance(payload, dict):
            continue
        if lines:
            lines.append("")
        current = payload.get("current_model") or {}
        lines.append(
            f"{index}. {agent_type} ({payload.get('source_function') or 'unknown'}) current={model_display_name(current)}"
        )
        models = payload.get("models") or []
        if not models:
            lines.append("   none")
            continue
        for item in models:
            marker = "*" if item.get("current") else " "
            default = " default" if item.get("is_default") else ""
            lines.append(
                f" {marker} {item.get('display_name') or item.get('name') or 'unknown'}"
                f" | key={item.get('model_key') or 'unknown'}"
                f" | type={item.get('model_type') or 'unknown'}{default}"
            )
    return "\n".join(lines)


def format_model_switch(data: dict[str, Any]) -> str:
    lines = [
        f"Agent: {data.get('agent_type') or 'unknown'}",
        f"Requested model: {data.get('requested_model') or 'unknown'}",
        f"Previous model: {(data.get('previous_model') or {}).get('display_name') or (data.get('previous_model') or {}).get('name') or data.get('previous_model_key') or 'unknown'}",
        f"Current model: {(data.get('current_model') or {}).get('display_name') or (data.get('current_model') or {}).get('name') or data.get('current_model_key') or 'unknown'}",
        f"Status: {data.get('status') or 'unknown'}",
    ]
    applied_via = data.get("applied_via") or []
    if applied_via:
        lines.append(f"Applied via: {', '.join(applied_via)}")
    if data.get("reload"):
        lines.append(
            "Reload dispatch: "
            + ("ok" if data["reload"].get("ok") else "failed")
        )
    lines.append(f"Reload required: {'yes' if data.get('reload_required') else 'no'}")
    notes = data.get("notes") or []
    if notes:
        lines.append("")
        lines.append("Notes:")
        lines.extend(f"- {item}" for item in notes)
    return "\n".join(lines)


def collect_model_picker_payload(
    app: AppContext,
    *,
    agent_type: Optional[str] = None,
) -> dict[str, Any]:
    current_payload = app.backend.current_models(agent_type=agent_type)
    list_payload = app.backend.list_models(agent_type=agent_type)
    agents = list_payload.get("agents") or {}
    focus_agent_type = (
        current_payload.get("requested_agent_type")
        or list_payload.get("requested_agent_type")
        or "dev_builder"
    )
    focus_agent = agents.get(focus_agent_type)
    if not isinstance(focus_agent, dict) and agents:
        focus_agent_type, focus_agent = next(iter(agents.items()))
    return {
        **current_payload,
        "agents": agents,
        "focus_agent_type": focus_agent_type,
        "focus_agent": focus_agent if isinstance(focus_agent, dict) else {},
    }


def format_model_picker(data: dict[str, Any]) -> str:
    lines = format_current_model_summary(data, include_legacy=True)
    focus_agent_type = str(data.get("focus_agent_type") or "dev_builder")
    focus_agent = data.get("focus_agent") or {}
    models = focus_agent.get("models") or []
    lines.append("")
    lines.append(f"Selectable models for {focus_agent_type}:")
    if models:
        for item in models:
            marker = "*" if item.get("current") else " "
            label = (
                item.get("display_name")
                or item.get("name")
                or item.get("model_key")
                or "unknown"
            )
            details = [f"key={item.get('model_key') or 'unknown'}"]
            if item.get("model_type"):
                details.append(f"type={item['model_type']}")
            if item.get("is_default"):
                details.append("default")
            lines.append(f" {marker} {label} | {' | '.join(details)}")
    else:
        lines.append("  none")
    lines.extend(
        [
            "",
            f"Type `/model <name>` to switch the {focus_agent_type} model.",
            "Type `/model <name> --agent-type solo_coder` or `solo_builder` to switch another agent.",
            "Type `/models list` to inspect the full inventory.",
        ]
    )
    return "\n".join(lines)


def format_sessions(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return "No session badges found."
    lines = []
    for item in entries:
        lines.append(
            f"{item['session_id']} badge_payload={item['has_badge_payload']} value_length={item['value_length']}"
        )
    return "\n".join(lines)


def format_mode_status(data: dict[str, Any]) -> str:
    lines = [
        f"Current mode: {'SOLO' if data.get('mode') == 'solo' else 'IDE'}",
        f"SOLO enabled: {'yes' if data.get('enabled') else 'no'}",
        f"Current solo tab: {data.get('current_solo_tab_id') or 'none'}",
        f"Visible extension view: {'yes' if data.get('is_visible_extension_view') else 'no'}",
        f"State DB: {data.get('state_db_path') or 'unknown'}",
    ]
    return "\n".join(lines)


def format_mode_switch(data: dict[str, Any]) -> str:
    state = data.get("mode_state") or {}
    lines = [
        f"Target mode: {'SOLO' if data.get('target_mode') == 'solo' else 'IDE'}",
        f"Previous mode: {'SOLO' if data.get('previous_mode') == 'solo' else 'IDE'}",
        f"Current mode: {'SOLO' if data.get('mode') == 'solo' else 'IDE'}",
        f"Status: {data.get('status') or 'unknown'}",
    ]
    applied_via = data.get("applied_via") or []
    if applied_via:
        lines.append(f"Applied via: {', '.join(applied_via)}")
    command_dispatch = data.get("command_dispatch") or {}
    if command_dispatch:
        lines.append(
            "Command dispatch: "
            + ("ok" if command_dispatch.get("ok") else "failed")
        )
        lines.append(f"Command URI: {command_dispatch.get('command_uri') or 'unknown'}")
    reload_dispatch = data.get("reload") or {}
    if reload_dispatch:
        lines.append(
            "Reload dispatch: "
            + ("ok" if reload_dispatch.get("ok") else "failed")
        )
    if data.get("storage"):
        lines.append(f"State DB: {state.get('state_db_path') or 'unknown'}")
        lines.append(f"Reload required: {'yes' if data.get('reload_required') else 'no'}")
    notes = data.get("notes") or []
    if notes:
        lines.append("")
        lines.append("Notes:")
        lines.extend(f"- {item}" for item in notes)
    return "\n".join(lines)


def format_gui_invoke(data: dict[str, Any]) -> str:
    command_id = data.get("command_id") or "unknown"
    lines = [
        f"Command: {command_id}",
        f"Status: {'ok' if data.get('ok') else 'failed'}",
    ]
    if data.get("dispatch"):
        lines.append(f"Dispatch: {data.get('dispatch')}")
    if data.get("command_uri"):
        lines.append(f"Command URI: {data.get('command_uri')}")
    path = data.get("path")
    if path:
        lines.append(f"Path: {path}")
    uri = data.get("uri")
    if uri:
        lines.append(f"URI: {uri}")
    recent_index = data.get("recent_index")
    if recent_index:
        lines.append(f"Recent entry: {recent_index}")
    label = str(data.get("label") or "").strip()
    if label:
        lines.append(f"Label: {label}")
    command = data.get("command") or []
    if command:
        lines.append("CLI dispatch: " + " ".join(str(item) for item in command))
    if data.get("returncode") is not None:
        lines.append(f"Return code: {data.get('returncode')}")
    stderr = str(data.get("stderr") or "").strip()
    if stderr:
        lines.append(f"stderr: {stderr}")
    return "\n".join(lines)


def format_gui_commands(data: dict[str, Any]) -> str:
    commands = data.get("commands") or []
    if not commands:
        return "No GUI commands discovered."
    lines = [
        f"GUI commands: {data.get('count') or len(commands)}"
        + (
            f" / total {data.get('total_count')}"
            if data.get("total_count") is not None
            else ""
        )
    ]
    for item in commands:
        sources = ", ".join(item.get("sources") or [])
        if sources:
            lines.append(f"{item['command_id']} [{sources}]")
        else:
            lines.append(str(item["command_id"]))
    return "\n".join(lines)


def format_gui_recent(data: dict[str, Any]) -> str:
    entries = data.get("entries") or []
    if not entries:
        return "No recently opened GUI entries found."
    lines = [
        f"Recent GUI entries: {data.get('count') or len(entries)}"
        + (
            f" / total {data.get('total_count')}"
            if data.get("total_count") is not None
            else ""
        )
    ]
    for item in entries:
        target = item.get("path") or item.get("uri") or "unknown"
        lines.append(f"[{item['index']}] {item.get('label') or target} [{item.get('kind') or 'unknown'}]")
        lines.append(f"    {target}")
    return "\n".join(lines)


def format_mcp(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return "No MCP gallery entries found."
    lines = []
    for item in entries:
        run = item.get("run") or []
        run_repr = ""
        if run:
            command = run[0].get("command", "")
            args = run[0].get("args", [])
            run_repr = " ".join([command, *args]).strip()
        lines.append(f"{item['display_name']} ({item['id']})")
        lines.append(f"  repo={item.get('repository') or 'unknown'}")
        lines.append(f"  run={run_repr.strip() or 'unknown'}")
    return "\n".join(lines)


def format_sandboxes(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return "No sandbox snapshots found."
    lines = []
    for item in entries:
        sample = ", ".join(item.get("sample_inherited_paths") or [])
        lines.append(f"{item['name']} permissions={item['permission_count']}")
        if sample:
            lines.append(f"  sample={sample}")
    return "\n".join(lines)


def format_log_listing(entries: list[str]) -> str:
    if not entries:
        return "No log files found."
    return "\n".join(entries)


def format_log_tail(data: dict[str, Any]) -> str:
    lines = [
        f"Session: {data['session_dir']}",
        f"File: {data['relative_path']}",
        "",
        *data["lines"],
    ]
    return "\n".join(lines)


def format_rpc_services(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return "No bundled RPC services found."
    lines = []
    for item in entries:
        apply_suffix = f" apply={item['apply_service']}" if item.get("apply_service") else ""
        lines.append(f"{item['service']} methods={item['method_count']}{apply_suffix}")
    return "\n".join(lines)


def format_rpc_methods(data: dict[str, Any]) -> str:
    methods = data.get("methods") or []
    if not methods:
        return f"No methods found for service {data['service']}."
    lines = [f"Service: {data['service']}"]
    if data.get("apply_service"):
        lines.append(f"Apply alias: {data['apply_service']}")
    for item in methods:
        lines.append(f"{item['symbol']} -> {item['method']}")
    return "\n".join(lines)


def format_rpc_activity(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return "No RPC activity found in the latest Trae log session."
    lines = []
    for item in entries:
        avg_cost = item["avg_success_cost_ms"]
        avg_repr = f"{avg_cost}ms" if avg_cost is not None else "n/a"
        lines.append(
            f"{item['service']} {item['method']} requests={item['request_count']} "
            f"success={item['success_count']} avg_success={avg_repr}"
        )
    return "\n".join(lines)


def format_rpc_context(data: dict[str, Any]) -> str:
    lines = [f"Latest logs: {data.get('latest_log_session') or 'not found'}"]
    projects = data.get("projects") or []
    sessions = data.get("sessions") or []
    messages = data.get("messages") or []
    lines.append("Projects:")
    if projects:
        for item in projects:
            lines.append(f"  {item['project_id']} ({item['source_log']})")
    else:
        lines.append("  none")
    lines.append("Sessions:")
    if sessions:
        for item in sessions:
            lines.append(f"  {item['session_id']} via {item['source']}")
    else:
        lines.append("  none")
    lines.append("Messages:")
    if messages:
        for item in messages:
            lines.append(
                f"  {item['message_id']} session={item['session_id']} event={item['event']}"
            )
    else:
        lines.append("  none")
    return "\n".join(lines)


def format_rpc_description(data: dict[str, Any]) -> str:
    lines = [
        f"Service: {data['service']}",
        f"Method: {data['method']}",
        f"Verified in bundle: {'yes' if data.get('bundle_verified') else 'no'}",
    ]
    if data.get("symbol"):
        lines.append(f"Symbol: {data['symbol']}")
    if data.get("apply_service"):
        lines.append(f"Apply alias: {data['apply_service']}")
    hint = data.get("payload_hint") or {}
    if hint.get("source"):
        lines.append(f"Hint source: {hint['source']}")
    fields = hint.get("fields") or hint.get("base_fields") or []
    if fields:
        lines.append("Base fields:")
        for field in fields:
            lines.append(f"  {field}")
    derived_fields = hint.get("derived_fields") or []
    if derived_fields:
        lines.append("Derived fields:")
        for field in derived_fields:
            lines.append(f"  {field}")
    notes = hint.get("notes") or []
    if notes:
        lines.append("Notes:")
        for note in notes:
            lines.append(f"  {note}")
    if data.get("recent_samples"):
        lines.append("Recent samples:")
        for key, value in data["recent_samples"].items():
            lines.append(f"  {key}={value}")
    return "\n".join(lines)


def format_rpc_transport(data: dict[str, Any]) -> str:
    app_rpc = data.get("app_rpc") or {}
    ckg = data.get("ckg") or {}
    oauth_callback = data.get("oauth_callback") or {}
    sample_request = data.get("sample_request") or {}
    live = data.get("live") or {}
    local_impl = app_rpc.get("local_impl") or {}
    node_socket_rule = app_rpc.get("node_socket_rule") or {}
    observed_ipc_addresses = app_rpc.get("observed_ipc_addresses") or []

    def bool_repr(value: Optional[bool]) -> str:
        if value is None:
            return "unknown"
        return "yes" if value else "no"

    lines = [
        f"Latest logs: {data.get('latest_log_session') or 'not found'}",
        f"Main socket: {data.get('main_socket') or 'not found'}",
        "App RPC:",
        f"  kind={app_rpc.get('kind') or 'unknown'}",
        f"  connected_service={app_rpc.get('client_connected_service') or 'unknown'}",
        f"  server_name={app_rpc.get('service_name') or 'unknown'}",
        f"  server_enabled={bool_repr(app_rpc.get('server_enabled'))}",
        f"  jsonrpsee_started={bool_repr(app_rpc.get('jsonrpsee_server_started'))}",
        f"  ffi_connection={bool_repr(app_rpc.get('ffi_connection_accepted'))}",
        f"  requests_seen={data.get('request_count', 0)}",
        f"  local_client_api={local_impl.get('client_api') or 'unknown'}",
        f"  local_server_api={local_impl.get('server_api') or 'unknown'}",
    ]
    if sample_request:
        lines.append("Sample request:")
        lines.append(
            f"  {sample_request.get('service')}.{sample_request.get('method')} "
            f"channel_id={sample_request.get('channel_id') or 'unknown'} "
            f"connect_session_id={sample_request.get('connect_session_id') or '<empty>'}"
        )
        response = sample_request.get("response") or {}
        if response:
            lines.append(
                f"  response code={response.get('code')} message={response.get('message')}"
            )
        matched = sample_request.get("matched_ai_agent_request") or {}
        if matched:
            lines.append(
                f"  matched_in={matched.get('source_log')} "
                f"service={matched.get('service')} method={matched.get('method')}"
            )
    if node_socket_rule:
        lines.append("Node socket rule:")
        lines.append(
            f"  runtime_dir={node_socket_rule.get('runtime_dir') or 'unknown'} "
            f"aha_dir={node_socket_rule.get('aha_dir') or 'unknown'} "
            f"socket_path={node_socket_rule.get('socket_path') or 'unknown'}"
        )
        lines.append(
            f"  marker_path={node_socket_rule.get('marker_path') or 'unknown'}"
        )
    if observed_ipc_addresses:
        lines.append("Observed ipc addresses:")
        for item in observed_ipc_addresses:
            lines.append(f"  {item.get('address')} ({item.get('source_log')})")
    lines.extend(
        [
            "CKG:",
            f"  kind={ckg.get('kind') or 'unknown'}",
            f"  addr={(ckg.get('host') or 'unknown')}:{ckg.get('port') or 'unknown'}",
            f"  grpc_content_type={bool_repr(ckg.get('saw_grpc_content_type'))}",
            "OAuth callback:",
            f"  kind={oauth_callback.get('kind') or 'unknown'}",
            f"  port={oauth_callback.get('port') or 'unknown'}",
        ]
    )
    if live:
        lines.extend(
            [
                "Live runtime:",
                f"  ps_available={bool_repr(live.get('ps_available'))}",
                f"  ps_probe={live.get('ps_probe_status') or 'unknown'}",
                f"  lsof_available={bool_repr(live.get('lsof_available'))}",
                f"  lsof_probe={live.get('lsof_probe_status') or 'unknown'}",
            ]
        )
        if live.get("ps_error"):
            lines.append(f"  ps_error={live['ps_error']}")
        if live.get("lsof_error_count"):
            lines.append(f"  lsof_error_count={live['lsof_error_count']}")
        lsof_errors = live.get("lsof_errors") or []
        if lsof_errors:
            lines.append(f"  lsof_errors={', '.join(lsof_errors)}")
        for process in live.get("processes") or []:
            sockets = process.get("sockets") or {}
            lines.append(
                f"  pid={process.get('pid')} ppid={process.get('ppid')} "
                f"role={process.get('role') or 'unknown'} "
                f"process_type={process.get('process_type') or 'none'}"
            )
            lines.append(f"    command={process.get('command') or 'unknown'}")
            listeners = sockets.get("tcp_listeners") or []
            lines.append(
                f"    tcp_listeners={', '.join(listeners) if listeners else 'none'}"
            )
            named_sockets = sockets.get("named_unix_sockets") or []
            lines.append(
                "    named_unix_sockets="
                + (", ".join(named_sockets) if named_sockets else "none")
            )
            lines.append(
                f"    anonymous_unix_sockets={sockets.get('anonymous_unix_socket_count', 0)}"
            )
            errors = sockets.get("errors") or []
            if errors:
                lines.append(f"    probe_errors={', '.join(errors)}")
        live_notes = live.get("notes") or []
        if live_notes:
            lines.append("Live notes:")
            for note in live_notes:
                lines.append(f"  {note}")
    notes = data.get("notes") or []
    if notes:
        lines.append("Notes:")
        for note in notes:
            lines.append(f"  {note}")
    return "\n".join(lines)


def format_rpc_request(data: dict[str, Any]) -> str:
    envelope = data.get("envelope") or {}
    params = envelope.get("params") or {}
    response = rpc_response_payload(data.get("response"))
    lines = [
        f"Target: {data.get('service_name') or 'ai-agent'} runtime_dir={data.get('runtime_dir') or 'unknown'}",
        f"RPC: {params.get('service') or 'unknown'}.{params.get('method') or 'unknown'}",
        f"channel_id={envelope.get('channel_id') or 'unknown'}",
        f"connect_session_id={params.get('client_info', {}).get('connect_session_id') or '<empty>'}",
    ]
    if response:
        lines.append(
            f"response code={response.get('code')} message={response.get('message')}"
        )
        response_data = response.get("data")
        if response_data is not None:
            lines.append(json.dumps(response_data, indent=2, ensure_ascii=False))
    return "\n".join(lines)


def rpc_response_payload(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict):
        return {}
    params = response.get("params")
    if isinstance(params, dict):
        return params
    return response


def format_rpc_chat(data: dict[str, Any]) -> str:
    client_info = data.get("client_info") or {}
    chat_response = rpc_response_payload(data.get("chat_response"))
    messages_response = rpc_response_payload(data.get("messages_response"))
    connect_session_id = (
        data.get("connect_session_id")
        or client_info.get("connect_session_id")
        or "<empty>"
    )
    lines = [
        f"Workspace: {data.get('workspace') or 'unknown'}",
        f"Project: {data.get('project_id') or 'unknown'}",
        f"Session: {data.get('session_id') or 'unknown'} ({data.get('session_type') or 'unknown'})",
        f"Message: {data.get('message_id') or 'unknown'}",
        "connect_session_id="
        f"{connect_session_id} ({data.get('connect_session_source') or 'unknown'})",
    ]
    guessed = data.get("guessed_connect_session") or {}
    if guessed and guessed.get("trace_id"):
        lines.append(f"guessed from trace={guessed['trace_id']}")
    if chat_response:
        lines.append(
            f"chat response code={chat_response.get('code')} message={chat_response.get('message')}"
        )
    if messages_response:
        lines.append(
            "get_messages response code="
            f"{messages_response.get('code')} message={messages_response.get('message')}"
        )
    if data.get("message_count") is not None:
        lines.append(f"messages={data.get('message_count')}")
    if data.get("assistant_status"):
        lines.append(f"assistant_status={data.get('assistant_status')}")
    if data.get("answer_source"):
        lines.append(f"answer_source={data.get('answer_source')}")
    answer_text = data.get("answer_text")
    if isinstance(answer_text, str) and answer_text.strip():
        lines.extend(["", answer_text.strip()])
    elif data.get("latest_assistant_message"):
        lines.append(
            json.dumps(
                data.get("latest_assistant_message"),
                indent=2,
                ensure_ascii=False,
            )
        )
    return "\n".join(lines)


def format_export_chat(data: dict[str, Any], *, include_content: bool = False) -> str:
    lines = [
        f"Session: {data.get('session_id') or 'unknown'}",
        f"Connect session: {data.get('connect_session_id') or '<empty>'} ({data.get('connect_session_source') or 'unknown'})",
        f"Export path: {data.get('export_path') or 'unknown'}",
    ]
    response = rpc_response_payload(data.get("response"))
    if response:
        lines.append(
            f"response code={response.get('code')} message={response.get('message')}"
        )
    guessed = data.get("guessed_connect_session") or {}
    if guessed and guessed.get("trace_id"):
        lines.append(f"guessed from trace={guessed['trace_id']}")
    content = data.get("content")
    if include_content and content is not None:
        lines.extend(["", content])
    return "\n".join(lines)


def format_rpc_traces(data: dict[str, Any]) -> str:
    traces = data.get("traces") or []
    filters = data.get("filters") or {}
    lines = [f"Latest logs: {data.get('latest_log_session') or 'not found'}"]
    filter_bits = []
    if filters.get("service"):
        filter_bits.append(f"service={filters['service']}")
    if filters.get("method"):
        filter_bits.append(f"method={filters['method']}")
    if filter_bits:
        lines.append("Filters: " + " ".join(filter_bits))
    if not traces:
        lines.append("No ai-agent RPC traces matched in the latest Trae log session.")
        return "\n".join(lines)
    for index, item in enumerate(traces, start=1):
        lines.append(f"#{index}")
        lines.append(
            f"{item.get('timestamp') or item.get('last_seen_at') or 'unknown'} "
            f"{item.get('service') or 'unknown'}.{item.get('method') or 'unknown'} "
            f"trace={item.get('trace_id') or 'none'} "
            f"channel={item.get('channel_id') or 'unknown'}"
        )
        lines.append(
            f"connect_session={item.get('connect_session_id') or '<empty>'} "
            f"session={item.get('session_id') or 'unknown'} "
            f"response_size={item.get('response_size_bytes') or 'unknown'}"
        )
        counts = []
        if item.get("message_model_count") is not None:
            counts.append(
                f"message_models={item.get('message_model_count')}/{item.get('message_model_actual')}"
            )
        if item.get("message_count") is not None:
            counts.append(f"messages={item.get('message_count')}")
        if item.get("turn_count") is not None:
            counts.append(f"turns={item.get('turn_count')}")
        if counts:
            lines.append(" ".join(counts))
        history_count = item.get("prepared_server_history_ids_count")
        if history_count is not None:
            history_line = (
                f"history_ids={history_count} "
                f"query_history_state={item.get('query_history_state_status') or 'unknown'}"
            )
            if item.get("query_history_state_request_count") is not None:
                history_line += (
                    f" request_ids={item.get('query_history_state_request_count')}"
                )
            if item.get("query_history_state_response_content_length") is not None:
                history_line += (
                    " response_content_length="
                    f"{item.get('query_history_state_response_content_length')}"
                )
            lines.append(history_line)
            sample_ids = item.get("prepared_server_history_ids_sample") or []
            if sample_ids:
                lines.append("history_id_sample=" + ", ".join(sample_ids))
        if item.get("query_history_state_response_request_id"):
            lines.append(
                "query_request_id="
                + str(item.get("query_history_state_response_request_id"))
            )
        if item.get("query_history_state_error"):
            lines.append("history_error=" + str(item["query_history_state_error"]))
    return "\n".join(lines)


def format_trace_turn_brief(turn: dict[str, Any]) -> str:
    preview = turn.get("first_token_preview")
    if preview:
        preview = " ".join(str(preview).split())
        if len(preview) > 96:
            preview = preview[:93] + "..."
    lines = [
        f"{turn.get('started_at') or turn.get('last_seen_at') or 'unknown'} "
        f"status={turn.get('status') or 'unknown'} "
        f"model={turn.get('chat_model') or 'unknown'}",
        f"session={turn.get('session_id') or 'unknown'} "
        f"frontend_message={turn.get('frontend_message_id') or 'unknown'}",
    ]
    trace_bits = [
        f"trace={turn.get('trace_id') or 'unknown'}",
        f"task={turn.get('task_id') or 'unknown'}",
        f"backend_message={turn.get('backend_message_id') or 'unknown'}",
    ]
    lines.append(" ".join(trace_bits))
    count_bits = [
        f"tools={turn.get('tool_call_count', 0)}",
        f"run_scripts={turn.get('run_script_success_count', 0)}/{turn.get('run_script_count', 0)}",
    ]
    if turn.get("request_round_count") is not None:
        count_bits.append(f"rounds={turn.get('request_round_count')}")
    if turn.get("progress_notice_count"):
        count_bits.append(f"progress={turn.get('progress_notice_count')}")
    if turn.get("tool_run_count"):
        count_bits.append(f"tool_runs={turn.get('tool_run_count')}")
    lines.append(" ".join(count_bits))
    if preview:
        lines.append(f"preview={json.dumps(preview, ensure_ascii=False)}")
    if turn.get("error_code") is not None:
        lines.append(f"error_code={turn.get('error_code')}")
    if turn.get("error_message"):
        lines.append(f"error={turn.get('error_message')}")
    if turn.get("cancel_reason"):
        lines.append(f"cancel_reason={turn.get('cancel_reason')}")
    return "\n".join(lines)


def format_trace_turns(data: dict[str, Any]) -> str:
    turns = data.get("turns") or []
    lines = [f"Latest logs: {data.get('latest_log_session') or 'not found'}"]
    if not turns:
        lines.append("No chat turns found in the latest Trae log session.")
        return "\n".join(lines)
    for index, turn in enumerate(turns, start=1):
        lines.append(f"#{index}")
        lines.append(format_trace_turn_brief(turn))
    return "\n".join(lines)


def format_tool_runs(tool_runs: list[dict[str, Any]]) -> list[str]:
    lines = ["Tool runs:"]
    for item in tool_runs:
        exit_code = item.get("exit_code")
        exit_repr = exit_code if exit_code is not None else "unknown"
        header = [
            f"  {item.get('tool_id') or 'unknown'}",
            f"exit={exit_repr}",
        ]
        if item.get("terminal_type"):
            header.append(f"terminal={item['terminal_type']}")
        if item.get("detection_strategy"):
            header.append(f"strategy={item['detection_strategy']}")
        lines.append(" ".join(header))

        command = item.get("command")
        if command:
            compact_command = " ".join(str(command).split())
            if len(compact_command) > 160:
                compact_command = compact_command[:157] + "..."
            lines.append(f"    command={compact_command}")

        excerpt = item.get("result_log_excerpt") or []
        if excerpt:
            lines.append("    output:")
            for excerpt_line in excerpt:
                lines.append(f"      {excerpt_line}")
    return lines


def format_trace_turn(data: dict[str, Any]) -> str:
    turn = data.get("turn") or {}
    if not turn:
        return "Chat turn not found."
    log_session = data.get("log_session") or data.get("latest_log_session")
    latest_log_session = data.get("latest_log_session")
    lines = [
        f"Log session: {log_session or 'not found'}",
        f"Status: {turn.get('status') or 'unknown'}",
        f"Started: {turn.get('started_at') or 'unknown'}",
        f"Last seen: {turn.get('last_seen_at') or 'unknown'}",
        f"Session: {turn.get('session_id') or 'unknown'}",
        f"Frontend message: {turn.get('frontend_message_id') or 'unknown'}",
        f"Backend message: {turn.get('backend_message_id') or 'unknown'}",
        f"Trace: {turn.get('trace_id') or 'unknown'}",
        f"Task: {turn.get('task_id') or 'unknown'}",
        f"Model: {turn.get('chat_model') or 'unknown'}",
        f"Agent type: {turn.get('agent_type') or 'unknown'}",
        f"Terminal event: {turn.get('terminal_event') or 'none'}",
        f"Trace match: {turn.get('trace_match') or 'none'}",
        f"Tool calls: {turn.get('tool_call_count', 0)}",
        f"Run scripts: {turn.get('run_script_success_count', 0)}/{turn.get('run_script_count', 0)}",
        f"Progress notices: {turn.get('progress_notice_count', 0)}",
    ]
    if latest_log_session and latest_log_session != log_session:
        lines.append(f"Latest logs: {latest_log_session}")
    if turn.get("first_token_preview"):
        lines.append(
            "First token preview: "
            + json.dumps(turn.get("first_token_preview"), ensure_ascii=False)
        )
    if turn.get("first_token_preview_source"):
        lines.append(f"First token source: {turn.get('first_token_preview_source')}")
    if turn.get("request_round_count") is not None:
        lines.append(f"Request rounds: {turn.get('request_round_count')}")
    if turn.get("renderer_reported_tool_count") is not None:
        lines.append(f"Renderer tool count: {turn.get('renderer_reported_tool_count')}")
    if turn.get("plan_final_token_cost_ms") is not None:
        lines.append(f"Plan final token cost: {turn.get('plan_final_token_cost_ms')}ms")
    if turn.get("error_code") is not None:
        lines.append(f"Error code: {turn.get('error_code')}")
    if turn.get("error_message"):
        lines.append(f"Error: {turn.get('error_message')}")
    if turn.get("cancel_reason"):
        lines.append(f"Cancel reason: {turn.get('cancel_reason')}")
    source_logs = turn.get("source_logs") or []
    if source_logs:
        lines.append("Source logs:")
        for item in source_logs:
            lines.append(f"  {item}")
    events = turn.get("events") or []
    if events:
        lines.append("Events:")
        for item in events:
            summary = f"  {item.get('timestamp') or 'unknown'} {item.get('event') or 'unknown'}"
            if item.get("tool_type"):
                summary += f" tool_type={item['tool_type']}"
            if item.get("tool_id"):
                summary += f" tool_id={item['tool_id']}"
            if item.get("duration_ms") is not None:
                summary += f" duration={item['duration_ms']}ms"
            if item.get("runtime_duration_ms") is not None:
                summary += f" runtime={item['runtime_duration_ms']}ms"
            if item.get("error_code") is not None:
                summary += f" error_code={item['error_code']}"
            lines.append(summary)
    tool_runs = turn.get("tool_runs") or []
    if tool_runs:
        lines.extend(format_tool_runs(tool_runs))
    return "\n".join(lines)


def format_cdp_websocket_probe(data: dict[str, Any]) -> str:
    endpoint = data.get("endpoint") or {}
    target = data.get("target") or {}
    probe_install = data.get("probe_install") or {}
    probe = data.get("websocket_probe") or {}
    page = probe.get("page") or {}
    network = probe.get("network") or {}

    lines = [
        "CDP WebSocket probe",
        f"Endpoint: {endpoint.get('host') or '127.0.0.1'}:{endpoint.get('port') or 'unknown'} "
        f"source={endpoint.get('source') or endpoint.get('availability_source') or 'unknown'}",
        f"Target: {target.get('title') or 'unknown'} url={target.get('url') or 'unknown'}",
    ]
    if probe_install:
        lines.append(
            f"Reload before run: {bool(probe_install.get('reload_before_run'))}"
        )

    page_records = page.get("records") or []
    if page_records:
        lines.append("Page sockets:")
        for index, record in enumerate(page_records, start=1):
            lines.append(f"  #{index} url={record.get('url') or 'unknown'}")
            protocols = record.get("protocols") or []
            if protocols:
                lines.append(
                    "    protocols="
                    + json.dumps(protocols, ensure_ascii=False)
                )
            if record.get("selectedProtocol"):
                lines.append(
                    f"    selected_protocol={record.get('selectedProtocol')}"
                )
            lines.append(
                f"    sends={record.get('sendCount', 0)} "
                f"receives={record.get('receiveCount', 0)} "
                f"ready_state={record.get('readyState') or 'unknown'}"
            )
            sent_frames = record.get("sentFrames") or []
            if sent_frames and sent_frames[0].get("preview"):
                lines.append(
                    "    first_sent="
                    + json.dumps(sent_frames[0].get("preview"), ensure_ascii=False)
                )

    handshakes = network.get("handshakes") or []
    responses = network.get("responses") or []
    if handshakes:
        lines.append("Network handshakes:")
        for item in handshakes:
            lines.append(
                f"  {item.get('requestId') or 'unknown'} url={item.get('url') or 'unknown'}"
            )
            protocol = item.get("secWebSocketProtocol")
            if protocol:
                lines.append(f"    Sec-WebSocket-Protocol={protocol}")
    if responses:
        lines.append("Network handshake responses:")
        for item in responses:
            lines.append(
                f"  {item.get('requestId') or 'unknown'} status={item.get('status') or 'unknown'} "
                f"url={item.get('url') or 'unknown'}"
            )
            protocol = item.get("secWebSocketProtocol")
            if protocol:
                lines.append(f"    Sec-WebSocket-Protocol={protocol}")

    if not page_records and not handshakes and not responses:
        lines.append("No WebSocket activity captured.")

    return "\n".join(lines)


def format_chat_dispatch(data: dict[str, Any]) -> str:
    dispatch_method = data.get("dispatch_method") or "cli"
    requested_dispatch_method = data.get("requested_dispatch_method") or dispatch_method
    label_map = {
        "auto": "Auto dispatch",
        "cli": "CLI proxy",
        "headless": "Headless dispatch",
        "uri": "URI dispatch",
        "command": "Command URI dispatch",
        "cdp": "CDP dispatch",
    }
    label = label_map.get(dispatch_method, dispatch_method)
    lines = [f"{label} exit_code={data.get('exit_code')}"]
    if requested_dispatch_method != dispatch_method:
        lines.append(
            f"Requested dispatch: {requested_dispatch_method} -> {dispatch_method}"
        )
    if dispatch_method == "uri" and data.get("deep_link"):
        lines.append(f"Deep link: {data['deep_link']}")
    if dispatch_method == "command" and data.get("command_uri"):
        lines.append(f"Command URI: {data['command_uri']}")
    if dispatch_method == "headless":
        if data.get("session_id"):
            lines.append(f"Session: {data['session_id']}")
        if data.get("request_message_id"):
            lines.append(f"Request message: {data['request_message_id']}")
        answer_text = data.get("answer_text")
        if answer_text:
            lines.extend(["", answer_text])
    if dispatch_method == "cdp":
        endpoint = data.get("cdp_endpoint") or {}
        if endpoint:
            lines.append(
                f"CDP endpoint: {endpoint.get('host') or '127.0.0.1'}:{endpoint.get('port') or 'unknown'} "
                f"source={endpoint.get('source') or 'unknown'}"
            )
        target = data.get("dispatch_target") or {}
        if target:
            lines.append(
                f"Target: {target.get('title') or 'unknown'} "
                f"url={target.get('url') or 'unknown'}"
            )
        answer_text = data.get("answer_text")
        if answer_text:
            lines.extend(["", answer_text])
    inspection = data.get("inspection") or {}
    turn = inspection.get("turn") or {}
    if turn:
        lines.append(format_trace_turn_brief(turn))
    else:
        lines.append(
            f"Latest logs: {inspection.get('latest_log_session') or 'not found'}"
        )
        lines.append(
            inspection.get("note") or "No matching chat turn was found after dispatch."
        )
        latest_turn = inspection.get("latest_turn") or {}
        if latest_turn:
            lines.append("Latest observed local turn:")
            lines.append(format_trace_turn_brief(latest_turn))
    return "\n".join(lines)


def format_bridge_install(data: dict[str, Any]) -> str:
    lines = [
        f"Bridge extension: {data.get('extension_id') or 'unknown'}@{data.get('version') or 'unknown'}",
        f"Status: {data.get('status') or 'unknown'}",
        f"Install dir: {data.get('install_dir')}",
        f"Registry: {data.get('registry_path')}",
    ]
    reload_payload = data.get("reload") or {}
    if reload_payload:
        lines.append(f"Reload exit_code={reload_payload.get('exit_code')}")
        if reload_payload.get("command"):
            lines.append(
                "Reload command: " + " ".join(str(item) for item in reload_payload["command"])
            )
    elif data.get("reload_required"):
        lines.append("Reload Trae once so the bridge extension can activate.")
    return "\n".join(lines)


def format_bridge_status(data: dict[str, Any]) -> str:
    lines = [
        f"Bridge extension: {data.get('extension_id') or 'unknown'}@{data.get('version') or 'unknown'}",
        f"Installed: {'yes' if data.get('installed') else 'no'}",
        f"Install dir: {data.get('install_dir')}",
        f"State file: {data.get('state_path')}",
    ]
    if data.get("workspace"):
        lines.append(f"Requested workspace: {data.get('workspace')}")
    if data.get("state_count") is not None:
        lines.append(f"Bridge states: {data.get('state_count')}")
    if data.get("matched_state_count") is not None and data.get("workspace"):
        lines.append(f"Matched bridge states: {data.get('matched_state_count')}")
    state = data.get("state") or {}
    if state:
        lines.append(
            f"Bridge endpoint: {state.get('host') or '127.0.0.1'}:{state.get('port') or 'unknown'}"
        )
        lines.append(f"Bridge pid: {state.get('pid') or 'unknown'}")
        workspaces = state.get("workspace_folders") or []
        if workspaces:
            lines.append(
                "Selected workspace: "
                + ", ".join(str(item.get("fsPath") or item.get("uri") or "") for item in workspaces[:3])
            )
    else:
        lines.append("Bridge endpoint: not active")
        candidate_workspaces = data.get("candidate_workspaces") or []
        if candidate_workspaces:
            lines.append("Active bridge workspaces: " + ", ".join(candidate_workspaces[:3]))
    if data.get("health"):
        health = data["health"].get("bridge") or {}
        lines.append(
            f"Health: ok app={health.get('app_name') or 'unknown'} workspace_folders={len(health.get('workspace_folders') or [])}"
        )
    elif data.get("health_error"):
        lines.append(f"Health: {data['health_error']}")
    runtime = data.get("runtime_diagnostics") or {}
    if runtime:
        lines.append(
            "Runtime: "
            f"renderer_windows={runtime.get('renderer_window_count') or 0} "
            f"scanner_hits={runtime.get('extension_scanner_input_count') or 0} "
            f"started_exthost={runtime.get('started_local_extension_host_count') or 0} "
            f"live_exthost={runtime.get('extension_host_process_count') or 0}"
        )
        if runtime.get("bridge_output_log_count"):
            lines.append(f"Bridge output logs: {runtime.get('bridge_output_log_count')}")
        listeners = runtime.get("bridge_output_listeners") or []
        if listeners:
            last_listener = listeners[-1]
            lines.append(
                f"Bridge output listener: {last_listener.get('host')}:{last_listener.get('port')}"
            )
    notes = data.get("notes") or []
    if notes:
        lines.append("Notes:")
        lines.extend(str(note) for note in notes[:5])
    return "\n".join(lines)


def format_bridge_commands(data: dict[str, Any]) -> str:
    commands = data.get("commands") or []
    if not commands:
        return "No bridge commands returned."
    lines = [f"Commands: {data.get('count') or len(commands)}"]
    lines.extend(commands)
    return "\n".join(lines)


def format_bridge_invoke(data: dict[str, Any]) -> str:
    lines = [
        f"Command: {data.get('command') or 'unknown'}",
        f"Args: {data.get('arg_count') or 0}",
    ]
    if "result" in data:
        lines.append("")
        lines.append(json.dumps(data.get("result"), indent=2, ensure_ascii=False))
    return "\n".join(lines)


def format_headless_install(data: dict[str, Any]) -> str:
    lines = [
        f"Headless command: {data.get('command_id') or 'unknown'}",
        f"Status: {data.get('status') or 'unknown'}",
        f"App: {data.get('app_path') or 'unknown'}",
        f"Bundle: {data.get('bundle_path')}",
        f"Backup: {data.get('backup_path')}",
        f"Patch installed: {'yes' if data.get('patch_installed') else 'no'}",
    ]
    reload_payload = data.get("reload") or {}
    if reload_payload:
        lines.append(f"Reload exit_code={reload_payload.get('exit_code')}")
    elif data.get("reload_required"):
        lines.append("Reload Trae once so the patched command can register.")
    return "\n".join(lines)


def format_headless_prepare(data: dict[str, Any]) -> str:
    clone = data.get("clone") or {}
    patch = data.get("patch") or {}
    signature = data.get("signature") or {}
    writable = data.get("writable") or {}
    lines = [
        f"Status: {data.get('status') or 'unknown'}",
        f"Source app: {data.get('source_app_path') or clone.get('source_app_path') or 'unknown'}",
        f"Prepared app: {data.get('app_path') or clone.get('target_app_path') or 'unknown'}",
        f"Clone status: {clone.get('status') or 'unknown'}",
        f"Patch status: {patch.get('status') or 'unknown'}",
        f"Patch installed: {'yes' if patch.get('patch_installed') else 'no'}",
        f"Signature status: {signature.get('status') or 'unknown'}",
        f"Signature verified: {'yes' if signature.get('verified') else 'no'}",
        f"Bundle writable: {'yes' if writable.get('writable') else 'no'}",
    ]
    if writable.get("error"):
        lines.append(f"Writable check: {writable['error']}")
    next_steps = data.get("next_steps") or []
    if next_steps:
        lines.append("")
        lines.append("Next steps:")
        lines.extend(next_steps)
    return "\n".join(lines)


def format_headless_status(data: dict[str, Any]) -> str:
    writable = data.get("writable") or {}
    lines = [
        f"Headless command: {data.get('command_id') or 'unknown'}",
        f"App: {data.get('app_path') or 'unknown'}",
        f"Bundle: {data.get('bundle_path')}",
        f"Patch installed: {'yes' if data.get('patch_installed') else 'no'}",
        f"Backup exists: {'yes' if data.get('backup_exists') else 'no'}",
        f"Bundle writable: {'yes' if writable.get('writable') else 'no'}",
        f"Bridge state: {'present' if data.get('bridge_state_present') else 'missing'}",
    ]
    if data.get("workspace"):
        lines.append(f"Requested workspace: {data.get('workspace')}")
    if data.get("bridge_state_count") is not None:
        lines.append(f"Bridge states: {data.get('bridge_state_count')}")
    bridge_state = data.get("bridge_state") or {}
    if bridge_state:
        workspaces = bridge_state.get("workspace_folders") or []
        if workspaces:
            lines.append(
                "Selected bridge workspace: "
                + ", ".join(str(item.get("fsPath") or item.get("uri") or "") for item in workspaces[:3])
            )
    else:
        candidate_workspaces = data.get("candidate_workspaces") or []
        if candidate_workspaces:
            lines.append("Active bridge workspaces: " + ", ".join(candidate_workspaces[:3]))
    if writable.get("error"):
        lines.append(f"Writable check: {writable['error']}")
    if data.get("command_available") is not None:
        lines.append(
            f"Bridge command visible: {'yes' if data.get('command_available') else 'no'}"
        )
    elif data.get("command_error"):
        lines.append(f"Bridge command check: {data['command_error']}")
    runtime = data.get("runtime_diagnostics") or {}
    if runtime:
        lines.append(
            "Runtime: "
            f"renderer_windows={runtime.get('renderer_window_count') or 0} "
            f"scanner_hits={runtime.get('extension_scanner_input_count') or 0} "
            f"started_exthost={runtime.get('started_local_extension_host_count') or 0} "
            f"live_exthost={runtime.get('extension_host_process_count') or 0}"
        )
        if runtime.get("bridge_output_log_count"):
            lines.append(f"Bridge output logs: {runtime.get('bridge_output_log_count')}")
        listeners = runtime.get("bridge_output_listeners") or []
        if listeners:
            last_listener = listeners[-1]
            lines.append(
                f"Bridge output listener: {last_listener.get('host')}:{last_listener.get('port')}"
            )
    notes = data.get("notes") or []
    if notes:
        lines.append("Notes:")
        lines.extend(str(note) for note in notes[:5])
    return "\n".join(lines)


def format_init(data: dict[str, Any]) -> str:
    lines = [
        f"Status: {data.get('status') or 'unknown'}",
        f"Source app: {data.get('source_app_path') or 'unknown'}",
        f"Selected app: {data.get('selected_app_path') or 'unknown'}",
        f"Mode: {data.get('mode') or 'unknown'}",
        f"Ready: {'yes' if data.get('ready') else 'no'}",
    ]
    bridge = data.get("bridge") or {}
    if bridge:
        lines.append(f"Bridge: {bridge.get('status') or 'unknown'}")
    headless = data.get("headless") or {}
    if headless:
        lines.append(f"Headless: {headless.get('status') or 'unknown'}")
    config = data.get("config") or {}
    if config.get("written"):
        lines.append(f"Config: {config.get('path')}")
    verification = data.get("verification") or {}
    if verification.get("status"):
        lines.append(f"Verification: {verification.get('status')}")
    if verification.get("detail"):
        lines.append(f"Verification detail: {verification.get('detail')}")
    next_steps = data.get("next_steps") or []
    if next_steps:
        lines.append("")
        lines.append("Next steps:")
        lines.extend(next_steps)
    return "\n".join(lines)


def workspace_root(app: AppContext) -> Path:
    return Path(current_workspace(app)).expanduser().resolve()


def codex_init_agents_template(workspace: Path) -> str:
    workspace_name = workspace.name.strip() or str(workspace)
    return "\n".join(
        [
            "# AGENTS.md",
            "",
            "## Scope",
            f"- Workspace: {workspace_name}",
            "",
            "## Working Rules",
            "- Keep changes focused and validate relevant behavior before handoff.",
            "- Record every project progress update in the repository-root `memory.md`.",
            "- When CLI behavior changes, update tests and docs in the same batch when practical.",
            "",
            "## Progress Log",
            "- Add one short entry to `memory.md` after each small, coherent batch of work.",
        ]
    )


def codex_init_memory_template() -> str:
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    return "\n".join(
        [
            "# Memory",
            "",
            f"### {timestamp}",
            "",
            "- Summary: Initialized workspace progress tracking.",
            "- Touched areas: `AGENTS.md`, `memory.md`",
            "- Validation: Not run",
            "- Notes: Append one entry after each coherent batch of work.",
        ]
    )


def bootstrap_workspace_docs(app: AppContext) -> dict[str, Any]:
    workspace = workspace_root(app)
    if not workspace.exists():
        raise click.ClickException(f"Workspace not found: {workspace}")
    if not workspace.is_dir():
        raise click.ClickException(f"Workspace is not a directory: {workspace}")

    agents_path = workspace / "AGENTS.md"
    memory_path = workspace / "memory.md"

    agents_created = False
    memory_created = False
    if not agents_path.exists():
        agents_path.write_text(
            codex_init_agents_template(workspace),
            encoding="utf-8",
        )
        agents_created = True
    if not memory_path.exists():
        memory_path.write_text(
            codex_init_memory_template(),
            encoding="utf-8",
        )
        memory_created = True

    notes = [
        "不会覆盖已存在的 AGENTS.md 或 memory.md。",
        "后续每推进一小批改动，都应在根目录 memory.md 追加记录。",
    ]
    next_steps = [
        "补充仓库约束到 AGENTS.md。",
        "每次完成一个小批次后，把变更、验证结果和备注追加到 memory.md。",
    ]
    return {
        "workspace": str(workspace),
        "agents": {
            "path": str(agents_path),
            "exists": agents_path.exists(),
            "created": agents_created,
        },
        "memory": {
            "path": str(memory_path),
            "exists": memory_path.exists(),
            "created": memory_created,
        },
        "ready": agents_path.exists() and memory_path.exists(),
        "notes": notes,
        "next_steps": next_steps,
    }


def format_workspace_bootstrap(data: dict[str, Any]) -> str:
    agents = data.get("agents") or {}
    memory = data.get("memory") or {}
    lines = [
        f"Workspace: {data.get('workspace') or 'unknown'}",
        f"AGENTS.md: {'created' if agents.get('created') else 'existing'}",
        f"memory.md: {'created' if memory.get('created') else 'existing'}",
        f"Ready: {'yes' if data.get('ready') else 'no'}",
    ]
    if agents.get("path"):
        lines.append(f"AGENTS path: {agents['path']}")
    if memory.get("path"):
        lines.append(f"Memory path: {memory['path']}")
    notes = data.get("notes") or []
    if notes:
        lines.append("")
        lines.append("Notes:")
        lines.extend(str(note) for note in notes)
    next_steps = data.get("next_steps") or []
    if next_steps:
        lines.append("")
        lines.append("Next steps:")
        lines.extend(str(step) for step in next_steps)
    return "\n".join(lines)


def chat_result_text(payload: dict[str, Any]) -> Optional[str]:
    answer_text = payload.get("answer_text")
    if isinstance(answer_text, str) and answer_text.strip():
        return answer_text.strip()
    stdout = str(payload.get("stdout") or "").strip()
    return stdout or None


def current_workspace(app: AppContext) -> str:
    return app.state.workspace or str(Path.cwd().resolve())


def collect_repl_status_payload(
    app: AppContext,
    record: dict[str, Any],
    view: InteractiveViewState,
) -> dict[str, Any]:
    workspace = workspace_root(app)
    trusted = app.backend.read_trusted_workspace(workspace=str(workspace))
    state_summary = app.backend.read_state_summary()
    selected_model = state_summary.get("selected_model") or {}
    sessions_payload = list_cli_sessions_payload(app, all_workspaces=False, limit=200)
    return {
        "workspace": str(workspace),
        "session_id": record.get("id"),
        "view_mode": view.mode,
        "message_count": record_message_count(record),
        "saved_session_count": sessions_payload.get("total_count") or 0,
        "hidden_session_id": app.state.last_headless_session_id,
        "current_cli_session_id": app.state.current_cli_session_id,
        "current_cdp_target_id": app.state.current_cdp_target_id,
        "selected_model": {
            "name": selected_model.get("name"),
            "display_name": selected_model.get("display_name"),
            "provider": selected_model.get("provider"),
            "model_type": selected_model.get("model_type"),
        },
        "trusted_workspace": trusted,
        "auto_trust_workspace": app.auto_trust_workspace,
        "agents_exists": (workspace / "AGENTS.md").exists(),
        "memory_exists": (workspace / "memory.md").exists(),
        "last_backend_command": list(app.state.last_backend_command),
        "undo_depth": app.state.to_dict().get("undo_depth") or 0,
        "redo_depth": app.state.to_dict().get("redo_depth") or 0,
        "app_path": str(app.state.app_path) if app.state.app_path else None,
    }


def format_repl_status(data: dict[str, Any]) -> str:
    model = data.get("selected_model") or {}
    model_name = model.get("display_name") or model.get("name") or "unknown"
    lines = [
        f"Workspace: {data.get('workspace') or 'unknown'}",
        f"Session: {data.get('session_id') or 'unknown'}",
        f"View: {data.get('view_mode') or 'unknown'}",
        f"Messages: {data.get('message_count') or 0}",
        f"Saved CLI sessions: {data.get('saved_session_count') or 0}",
        f"Hidden session: {data.get('hidden_session_id') or 'none'}",
        f"Model: {model_name}",
        f"Workspace trusted: {'yes' if data.get('trusted_workspace') else 'no'}",
        f"Auto trust flag: {'on' if data.get('auto_trust_workspace') else 'off'}",
        f"AGENTS.md: {'present' if data.get('agents_exists') else 'missing'}",
        f"memory.md: {'present' if data.get('memory_exists') else 'missing'}",
        f"Undo/redo: {data.get('undo_depth') or 0}/{data.get('redo_depth') or 0}",
    ]
    if model.get("provider") or model.get("model_type"):
        lines.append(
            "Model detail: "
            f"provider={model.get('provider') or 'unknown'} "
            f"type={model.get('model_type') or 'unknown'}"
        )
    last_backend_command = data.get("last_backend_command") or []
    if last_backend_command:
        lines.append("Last backend command: " + " ".join(str(item) for item in last_backend_command))
    if data.get("current_cdp_target_id"):
        lines.append(f"CDP target: {data['current_cdp_target_id']}")
    if data.get("current_cli_session_id"):
        lines.append(f"CLI session state: {data['current_cli_session_id']}")
    return "\n".join(lines)


def run_git_text_command(workspace: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(workspace), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def collect_workspace_diff_payload(app: AppContext) -> dict[str, Any]:
    workspace = workspace_root(app)
    try:
        repo_result = run_git_text_command(workspace, "rev-parse", "--show-toplevel")
    except FileNotFoundError:
        return {
            "workspace": str(workspace),
            "is_repo": False,
            "error": "Git is not available on PATH.",
        }
    if repo_result.returncode != 0:
        detail = repo_result.stderr.strip() or repo_result.stdout.strip()
        return {
            "workspace": str(workspace),
            "is_repo": False,
            "error": detail or "Current workspace is not inside a git repository.",
        }

    repo_root = repo_result.stdout.strip()
    branch_result = run_git_text_command(workspace, "rev-parse", "--abbrev-ref", "HEAD")
    status_result = run_git_text_command(workspace, "status", "--short")
    staged_result = run_git_text_command(workspace, "diff", "--cached", "--stat", "--no-ext-diff")
    unstaged_result = run_git_text_command(workspace, "diff", "--stat", "--no-ext-diff")

    status_lines = [line.rstrip() for line in status_result.stdout.splitlines() if line.strip()]
    staged_count = 0
    unstaged_count = 0
    untracked_count = 0
    for line in status_lines:
        if line.startswith("??"):
            untracked_count += 1
            continue
        prefix = line[:2].ljust(2)
        if prefix[0] not in {" ", "?"}:
            staged_count += 1
        if prefix[1] not in {" ", "?"}:
            unstaged_count += 1

    return {
        "workspace": str(workspace),
        "repo_root": repo_root,
        "branch": branch_result.stdout.strip() or "unknown",
        "is_repo": True,
        "has_changes": bool(status_lines),
        "status_lines": status_lines[:12],
        "status_remaining": max(0, len(status_lines) - 12),
        "staged_count": staged_count,
        "unstaged_count": unstaged_count,
        "untracked_count": untracked_count,
        "staged_stat": staged_result.stdout.strip(),
        "unstaged_stat": unstaged_result.stdout.strip(),
    }


def format_workspace_diff(data: dict[str, Any]) -> str:
    if not data.get("is_repo"):
        return (
            f"Workspace: {data.get('workspace') or 'unknown'}\n"
            f"{data.get('error') or 'Current workspace is not inside a git repository.'}"
        )
    lines = [
        f"Workspace: {data.get('workspace') or 'unknown'}",
        f"Repository: {data.get('repo_root') or 'unknown'}",
        f"Branch: {data.get('branch') or 'unknown'}",
    ]
    if not data.get("has_changes"):
        lines.append("Working tree: clean")
        return "\n".join(lines)

    lines.extend(
        [
            "Working tree: dirty",
            f"Staged paths: {data.get('staged_count') or 0}",
            f"Unstaged paths: {data.get('unstaged_count') or 0}",
            f"Untracked paths: {data.get('untracked_count') or 0}",
        ]
    )
    status_lines = data.get("status_lines") or []
    if status_lines:
        lines.append("")
        lines.append("Status:")
        lines.extend(str(line) for line in status_lines)
        if data.get("status_remaining"):
            lines.append(f"...and {data['status_remaining']} more paths")
    if data.get("staged_stat"):
        lines.append("")
        lines.append("Staged diffstat:")
        lines.extend(str(line) for line in str(data["staged_stat"]).splitlines())
    if data.get("unstaged_stat"):
        lines.append("")
        lines.append("Unstaged diffstat:")
        lines.extend(str(line) for line in str(data["unstaged_stat"]).splitlines())
    return "\n".join(lines)


def prompt_guide_text(app: AppContext) -> str:
    workspace_name = workspace_root(app).name.strip() or "current workspace"
    lines = [
        f"Prompt ideas for {workspace_name}:",
        "- 用中文总结当前仓库结构、入口文件和高风险区域。",
        "- 基于当前 git diff 只列潜在回归和缺失测试。",
        "- 检查交互式 `/` 菜单的 UX，并给出最小可行修复方案。",
        "- 找出当前 workspace 最适合先自动化的 CLI 流程。",
        "",
        "Useful slash commands:",
        "- /status 查看当前会话、模型和工作区状态。",
        "- /diff 先看改动摘要，再决定是否继续编码。",
        "- /model 查看当前模型并直接切换。",
        "- /approvals 查看当前 trust/approval 上下文。",
    ]
    return "\n".join(lines)


def collect_model_summary_payload(app: AppContext) -> dict[str, Any]:
    data = app.backend.read_state_summary()
    return {
        "selected_model": data.get("selected_model"),
        "global_model_map": data.get("global_model_map"),
        "agent_mode": data.get("agent_mode"),
        "model_cache_counts": app.backend.parse_model_cache_counts(),
    }


def collect_approval_payload(app: AppContext) -> dict[str, Any]:
    workspace = workspace_root(app)
    trusted = app.backend.read_trusted_workspace(workspace=str(workspace))
    return {
        "workspace": str(workspace),
        "workspace_trusted": trusted is not None,
        "trust_record": trusted,
        "auto_trust_workspace": app.auto_trust_workspace,
        "json_output": app.json_output,
        "alt_screen": should_use_alt_screen(app),
        "per_command_approvals": False,
    }


def format_approval_payload(data: dict[str, Any]) -> str:
    record = data.get("trust_record") or {}
    lines = [
        f"Workspace: {data.get('workspace') or 'unknown'}",
        f"Workspace trusted: {'yes' if data.get('workspace_trusted') else 'no'}",
        f"Auto trust flag: {'on' if data.get('auto_trust_workspace') else 'off'}",
        "Per-command approvals: unavailable in traecli",
        f"Alternate screen: {'on' if data.get('alt_screen') else 'off'}",
        f"JSON mode: {'on' if data.get('json_output') else 'off'}",
    ]
    if record:
        lines.append(f"Trust source: {record.get('source') or 'unknown'}")
        lines.append(f"Trusted at: {record.get('trusted_at') or 'unknown'}")
    lines.append("")
    lines.append("Notes:")
    lines.append("- 当前最接近 Codex approvals 的本地能力是 workspace trust。")
    lines.append("- 新工作区可通过 `--trust-workspace` 自动写入信任记录。")
    return "\n".join(lines)


def resolve_existing_path(target: Path | str, *, purpose: str) -> Path:
    resolved = Path(target).expanduser().resolve()
    if not resolved.exists():
        raise click.ClickException(f"{purpose} not found: {resolved}")
    return resolved


def build_gui_cli_path_payload(
    app: AppContext,
    *,
    command_id: str,
    target: Path | str,
) -> dict[str, Any]:
    resolved_target = Path(target).expanduser().resolve()
    result = app.backend.open_path_via_cli(resolved_target)
    command = result.args
    if isinstance(command, tuple):
        command = list(command)
    elif not isinstance(command, list):
        command = [str(command)]
    return {
        "ok": result.returncode == 0,
        "dispatch": "cli-path",
        "command_id": command_id,
        "path": str(resolved_target),
        "command": command,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def command_requires_workspace_trust(command_name: Optional[str]) -> bool:
    return command_name in {None, "chat", "exec", "open", "repl"}


def workspace_trust_help(workspace: str) -> str:
    return (
        f"Workspace `{workspace}` is not trusted by Trae. "
        "Re-run with `--trust-workspace` or answer yes at the prompt."
    )


def ensure_workspace_trusted(
    app: AppContext,
    workspace: Optional[Path | str],
    *,
    auto_trust: bool = False,
) -> Optional[dict[str, Any]]:
    normalized_workspace = app.backend.normalize_workspace_path(workspace)
    if not normalized_workspace:
        return None
    trusted = app.backend.read_trusted_workspace(workspace=normalized_workspace)
    if trusted is not None:
        return trusted
    if auto_trust:
        try:
            return app.backend.trust_workspace(
                workspace=normalized_workspace,
                source="flag",
            )
        except RuntimeError as exc:
            raise click.ClickException(str(exc)) from exc
    if app.json_output:
        raise click.ClickException(workspace_trust_help(normalized_workspace))
    click.echo("首次进入该工作区，需要先确认是否信任该文件夹。")
    click.echo("该决定会写入 Trae GUI 共享的工作区信任状态。")
    click.echo(normalized_workspace)
    try:
        confirmed = click.confirm("Trust this workspace?", default=False)
    except (EOFError, click.Abort) as exc:
        raise click.ClickException(workspace_trust_help(normalized_workspace)) from exc
    if not confirmed:
        raise click.ClickException(f"Workspace trust declined: {normalized_workspace}")
    try:
        return app.backend.trust_workspace(
            workspace=normalized_workspace,
            source="prompt",
        )
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc


def workspace_title_hints(app: AppContext) -> list[str]:
    workspace = current_workspace(app)
    name = Path(workspace).name.strip()
    return [name] if name else []


def session_preview_text(value: Any, *, limit: int = 80) -> str:
    text = str(value or "").strip().replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def append_cli_session_message(
    record: dict[str, Any],
    *,
    role: str,
    content: str,
) -> None:
    text = str(content or "").strip()
    if not text:
        return
    messages = record.get("messages")
    if not isinstance(messages, list):
        messages = []
    messages.append(
        {
            "role": role,
            "content": text,
            "timestamp": datetime.now().astimezone().isoformat(),
        }
    )
    record["messages"] = messages[-40:]


def append_cli_session_event(
    record: dict[str, Any],
    *,
    status: str,
    headline: str,
    details: Optional[list[str]] = None,
) -> None:
    title = str(headline or "").strip()
    if not title:
        return
    messages = record.get("messages")
    if not isinstance(messages, list):
        messages = []
    messages.append(
        {
            "role": "event",
            "status": status,
            "headline": title,
            "details": [str(item).rstrip() for item in (details or []) if str(item).strip()],
            "timestamp": datetime.now().astimezone().isoformat(),
        }
    )
    record["messages"] = messages[-40:]


def compact_shell_command(command: Any, *, limit: int = 96) -> str:
    if isinstance(command, (list, tuple)):
        text = shlex.join(str(part) for part in command)
    else:
        text = str(command or "").strip()
    return session_preview_text(" ".join(text.split()), limit=limit)


def summarize_event_detail_lines(
    lines: list[str],
    *,
    max_lines: int = 4,
    limit: int = 120,
) -> list[str]:
    normalized = [
        session_preview_text(" ".join(str(line).split()), limit=limit)
        for line in lines
        if str(line).strip()
    ]
    if len(normalized) <= max_lines:
        return normalized
    visible = normalized[: max_lines - 1]
    visible.append(f"... +{len(normalized) - len(visible)} lines")
    return visible


def update_pending_working_event(
    record: dict[str, Any],
    *,
    status: str,
    headline: str,
    details: Optional[list[str]] = None,
) -> bool:
    messages = record.get("messages")
    if not isinstance(messages, list):
        return False
    for item in reversed(messages):
        if not isinstance(item, dict):
            continue
        if item.get("role") != "event" or item.get("status") != "working":
            continue
        item["status"] = status
        item["headline"] = str(headline or "").strip()
        item["details"] = [
            str(detail).rstrip()
            for detail in (details or [])
            if str(detail).strip()
        ]
        item["timestamp"] = datetime.now().astimezone().isoformat()
        return True
    return False


def build_interactive_execution_events(payload: dict[str, Any]) -> list[dict[str, Any]]:
    inspection = payload.get("inspection") or {}
    turn = inspection.get("turn") or {}
    events: list[dict[str, Any]] = []
    tool_runs = turn.get("tool_runs") or []
    for item in tool_runs[:2]:
        command = item.get("command") or payload.get("dispatch_command") or payload.get("command")
        excerpt = summarize_event_detail_lines(item.get("result_log_excerpt") or [], max_lines=4)
        if not excerpt and item.get("exit_code") is not None:
            excerpt = [f"exit={item.get('exit_code')}"]
        events.append(
            {
                "status": "success" if item.get("exit_code") == 0 else "error",
                "headline": f"Ran `{compact_shell_command(command)}`",
                "details": excerpt,
            }
        )
    if events:
        return events

    dispatch_command = payload.get("dispatch_command") or payload.get("command")
    if not dispatch_command:
        return events

    details: list[str] = []
    dispatch_method = payload.get("dispatch_method")
    if dispatch_method:
        details.append(f"dispatch={dispatch_method}")
    if turn:
        summary_bits = []
        if turn.get("chat_model"):
            summary_bits.append(f"model={turn.get('chat_model')}")
        if turn.get("status"):
            summary_bits.append(f"status={turn.get('status')}")
        if turn.get("request_round_count") is not None:
            summary_bits.append(f"rounds={turn.get('request_round_count')}")
        if turn.get("progress_notice_count"):
            summary_bits.append(f"progress={turn.get('progress_notice_count')}")
        if turn.get("tool_run_count"):
            summary_bits.append(f"tool_runs={turn.get('tool_run_count')}")
        if summary_bits:
            details.append(" ".join(summary_bits))
        preview = turn.get("first_token_preview")
        if preview:
            details.append(session_preview_text(str(preview), limit=120))
    else:
        stdout = str(payload.get("stdout") or "").strip()
        if stdout and stdout != (chat_result_text(payload) or ""):
            details.extend(summarize_event_detail_lines(stdout.splitlines(), max_lines=4))

    events.append(
        {
            "status": "success" if int(payload.get("exit_code") or 0) == 0 else "error",
            "headline": f"Ran `{compact_shell_command(dispatch_command)}`",
            "details": details,
        }
    )
    return events


def format_cli_sessions(data: dict[str, Any]) -> str:
    sessions = data.get("sessions") or []
    if not sessions:
        scope = "all workspaces" if data.get("all_workspaces") else "this workspace"
        return f"No CLI sessions found for {scope}."
    lines = [
        f"CLI sessions ({len(sessions)})"
        + (" [all workspaces]" if data.get("all_workspaces") else ""),
    ]
    for index, item in enumerate(sessions, start=1):
        title = item.get("title") or "Interactive session"
        session_id = item.get("id") or "unknown"
        updated_at = item.get("updated_at") or item.get("created_at") or "unknown"
        resumable = "yes" if item.get("resumable") else "no"
        workspace = item.get("workspace") or "unknown"
        lines.append(
            f"{index}. {title} [{session_id}] resumable={resumable} updated={updated_at}"
        )
        lines.append(f"   workspace={workspace}")
        preview = item.get("preview")
        if preview:
            lines.append(f"   preview={preview}")
    return "\n".join(lines)


def format_cli_session_detail(data: dict[str, Any]) -> str:
    record = data.get("session") or {}
    if not isinstance(record, dict) or not record:
        return "CLI session not found."
    lines = [
        f"CLI session: {record.get('id') or 'unknown'}",
        f"Title: {record.get('title') or 'Interactive session'}",
        f"Workspace: {record.get('workspace') or 'unknown'}",
        f"Updated: {record.get('updated_at') or record.get('created_at') or 'unknown'}",
        f"Resumable: {'yes' if record.get('resumable') else 'no'}",
        f"Hidden session: {record.get('last_headless_session_id') or 'none'}",
        f"Dispatch: {record.get('last_dispatch_method') or 'unknown'}",
        "Messages:",
    ]
    messages = record.get("messages")
    if not isinstance(messages, list) or not messages:
        lines.append("  none")
        return "\n".join(lines)
    for item in messages[-20:]:
        if not isinstance(item, dict):
            continue
        role = message_prefix(str(item.get("role") or "system"))
        content = session_preview_text(item.get("content") or "", limit=120)
        timestamp = item.get("timestamp") or "unknown"
        lines.append(f"  {role} [{timestamp}] {content}")
    return "\n".join(lines)


def list_cli_sessions_payload(
    app: AppContext,
    *,
    all_workspaces: bool = False,
    resumable_only: bool = False,
    limit: int = 20,
) -> dict[str, Any]:
    sessions = app.backend.read_cli_sessions()
    workspace_filter = None if all_workspaces else current_workspace(app)
    filtered: list[dict[str, Any]] = []
    for item in sessions:
        workspace = item.get("workspace")
        if workspace_filter is not None and workspace != workspace_filter:
            continue
        if resumable_only and not item.get("resumable"):
            continue
        messages = item.get("messages") if isinstance(item.get("messages"), list) else []
        preview_source = None
        if messages:
            last = messages[-1]
            if isinstance(last, dict):
                preview_source = last.get("content")
        preview = session_preview_text(
            preview_source or item.get("last_prompt") or item.get("initial_prompt") or ""
        )
        filtered.append(
            {
                **item,
                "preview": preview or None,
            }
        )
    filtered.sort(
        key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
        reverse=True,
    )
    return {
        "path": str(app.backend.paths.cli_sessions_path),
        "workspace": workspace_filter,
        "all_workspaces": all_workspaces,
        "resumable_only": resumable_only,
        "sessions": filtered[:limit],
        "total_count": len(filtered),
    }


def cli_session_detail_payload(
    app: AppContext,
    *,
    session_id: str,
) -> dict[str, Any]:
    record = app.backend.load_cli_session(session_id)
    return {
        "path": str(app.backend.paths.cli_sessions_path),
        "session": record,
        "found": record is not None,
    }


def should_use_alt_screen(app: AppContext) -> bool:
    return (
        not app.no_alt_screen
        and sys.stdin.isatty()
        and sys.stdout.isatty()
        and not app.json_output
    )


@contextmanager
def maybe_alt_screen(enabled: bool):
    if not enabled:
        yield
        return
    click.echo("\x1b[?1049h", nl=False)
    try:
        yield
    finally:
        click.echo("\x1b[?1049l", nl=False)


def wrap_block_lines(text: str, *, width: int = 76) -> list[str]:
    effective_width = max(8, width)
    cleaned = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    raw_lines = cleaned.split("\n")
    lines: list[str] = []
    for raw in raw_lines:
        line = raw.rstrip()
        if not line.strip():
            lines.append("")
            continue
        wrapped = textwrap.wrap(
            line,
            width=effective_width,
            replace_whitespace=False,
            drop_whitespace=False,
            break_long_words=True,
            break_on_hyphens=False,
        )
        lines.extend(wrapped or [""])
    return lines


def wrap_block(text: str, *, width: int = 76, max_lines: int = 14) -> list[str]:
    lines = wrap_block_lines(text, width=width)
    if len(lines) <= max_lines:
        return lines
    truncated = lines[: max_lines - 1]
    truncated.append("...")
    return truncated


def terminal_screen_size() -> tuple[int, int]:
    size = shutil.get_terminal_size((100, 32))
    return max(60, int(size.columns)), max(20, int(size.lines))


def render_screen_rule(title: Optional[str] = None, *, width: int = 80) -> str:
    if width <= 0:
        return ""
    label = session_preview_text(str(title or "").strip(), limit=max(1, width - 1))
    if not label:
        return "-" * width
    prefix = f"{label} "
    if len(prefix) >= width:
        return prefix[:width]
    return prefix + ("-" * (width - len(prefix)))


def render_screen_section(
    title: str,
    body: str,
    *,
    width: int = 80,
    max_lines: int = 8,
    anchor: str = "top",
    scroll_offset: int = 0,
) -> list[str]:
    section_lines = [render_screen_rule(title, width=width)]
    body_lines = wrap_block_lines(
        (body or "").strip() or "No output.",
        width=width,
    )
    if not body_lines:
        body_lines = ["No output."]
    visible_lines = max(1, max_lines)
    body_count = len(body_lines)
    max_offset = max(0, body_count - visible_lines)
    normalized_offset = max(0, min(scroll_offset, max_offset))
    if body_count <= visible_lines:
        if anchor == "bottom":
            body_lines = ([""] * (visible_lines - body_count)) + body_lines
    elif anchor == "bottom":
        end = body_count - normalized_offset
        start = max(0, end - visible_lines)
        body_lines = list(body_lines[start:end])
        hidden_top = start
        hidden_bottom = body_count - end
        if hidden_top > 0 and body_lines:
            body_lines[0] = f"...上面还有 {hidden_top} 行。"
        if hidden_bottom > 0 and body_lines:
            body_lines[-1] = f"...下面还有 {hidden_bottom} 行。"
    else:
        start = normalized_offset
        end = min(body_count, start + visible_lines)
        body_lines = list(body_lines[start:end])
        hidden_bottom = body_count - end
        if hidden_bottom > 0 and body_lines:
            body_lines[-1] = f"...下面还有 {hidden_bottom} 行。"
    section_lines.extend(body_lines)
    return section_lines


def pad_screen_line(text: str, *, width: int) -> str:
    clipped = session_preview_text(text, limit=max(1, width))
    return clipped + (" " * max(0, width - len(clipped)))


def normalize_screen_lines(
    lines: list[str],
    *,
    width: int,
    height: int,
) -> list[str]:
    padded = [pad_screen_line(line, width=width) for line in lines[:height]]
    if len(padded) < height:
        padded.extend([" " * width] * (height - len(padded)))
    return padded


def render_split_screen_sections(
    left_title: str,
    left_body: str,
    right_title: str,
    right_body: str,
    *,
    width: int,
    height: int,
    left_anchor: str = "top",
    left_scroll_offset: int = 0,
    right_anchor: str = "top",
    right_scroll_offset: int = 0,
) -> list[str]:
    if width < 88:
        main_budget = max(5, int(height * 0.65))
        side_budget = max(3, height - main_budget)
        if main_budget + side_budget > height:
            side_budget = max(3, height - main_budget)
        lines = render_screen_section(
            left_title,
            left_body,
            width=width,
            max_lines=max(1, main_budget - 1),
            anchor=left_anchor,
            scroll_offset=left_scroll_offset,
        )
        lines.append("")
        lines.extend(
            render_screen_section(
                right_title,
                right_body,
                width=width,
                max_lines=max(1, side_budget - 1),
                anchor=right_anchor,
                scroll_offset=right_scroll_offset,
            )
        )
        return lines

    gap = 3
    right_width = min(38, max(28, int(width * 0.32)))
    left_width = max(40, width - gap - right_width)
    panel_height = max(6, height)
    left_lines = normalize_screen_lines(
        render_screen_section(
            left_title,
            left_body,
            width=left_width,
            max_lines=max(1, panel_height - 1),
            anchor=left_anchor,
            scroll_offset=left_scroll_offset,
        ),
        width=left_width,
        height=panel_height,
    )
    right_lines = normalize_screen_lines(
        render_screen_section(
            right_title,
            right_body,
            width=right_width,
            max_lines=max(1, panel_height - 1),
            anchor=right_anchor,
            scroll_offset=right_scroll_offset,
        ),
        width=right_width,
        height=panel_height,
    )
    return [
        f"{left_lines[index]}{' ' * gap}{right_lines[index]}"
        for index in range(panel_height)
    ]


ESCAPE_SEQUENCE_KEYS = {
    "\x1b[A": "UP",
    "\x1b[B": "DOWN",
    "\x1b[C": "RIGHT",
    "\x1b[D": "LEFT",
    "\x1b[H": "HOME",
    "\x1b[F": "END",
    "\x1bOH": "HOME",
    "\x1bOF": "END",
    "\x1b[3~": "DELETE",
}


STARTUP_WORDMARK_LINES = [
    "  █████  ████    ███   █████   ████  █      █████",
    "  █    █   █  █   █  █      █      █        █    ",
    "  █    ████   █████  ███    █      █        █    ",
    "  █    █  █   █   █  █      █      █        █    ",
    "  █    █   █  █   █  █████   ████  █████  █████  ",
]
STARTUP_BANNER_COLOR = "\x1b[32m"
INTERACTIVE_CURSOR_MARKER = "\ue000"
SLASH_COMMAND_SPECS = [
    {
        "command": "/help",
        "insert": "/help",
        "category": "功能",
        "summary": "显示 CLI 帮助和命令说明。",
    },
    {
        "command": "/init",
        "insert": "/init",
        "category": "Codex",
        "summary": "初始化当前 workspace 的 AGENTS.md 和 memory.md。",
    },
    {
        "command": "/status",
        "insert": "/status",
        "category": "Codex",
        "summary": "查看当前会话、模型、记录文件和工作区状态。",
    },
    {
        "command": "/diff",
        "insert": "/diff",
        "category": "Codex",
        "summary": "查看当前 git 工作区的改动摘要。",
    },
    {
        "command": "/prompts",
        "insert": "/prompts",
        "category": "Codex",
        "summary": "显示一组可直接复用的提示词模板。",
    },
    {
        "command": "/model",
        "insert": "/model",
        "category": "Codex",
        "summary": "查看当前模型和可选项。",
    },
    {
        "command": "/model <model>",
        "insert": "/model ",
        "category": "Codex",
        "summary": "切换当前 Trae 模型；可再补 --agent-type。",
    },
    {
        "command": "/approvals",
        "insert": "/approvals",
        "category": "Codex",
        "summary": "查看当前 trust/approval 上下文。",
    },
    {
        "command": "/sessions",
        "insert": "/sessions",
        "category": "历史",
        "summary": "浏览当前 workspace 的已保存 CLI 会话。",
    },
    {
        "command": "/session detail",
        "insert": "/session detail",
        "category": "历史",
        "summary": "查看当前交互会话的元数据。",
    },
    {
        "command": "/doctor",
        "insert": "/doctor",
        "category": "查看",
        "summary": "检查运行时、认证和本地 Trae 配置。",
    },
    {
        "command": "/state auth",
        "insert": "/state auth",
        "category": "查看",
        "summary": "显示脱敏后的本地认证信息。",
    },
    {
        "command": "/state models",
        "insert": "/state models",
        "category": "查看",
        "summary": "显示当前模型选择和缓存状态。",
    },
    {
        "command": "/models current",
        "insert": "/models current",
        "category": "查看",
        "summary": "显示当前生效模型，而不只看 legacy selected_model。",
    },
    {
        "command": "/models list",
        "insert": "/models list",
        "category": "查看",
        "summary": "按 agent 类型列出可切换模型。",
    },
    {
        "command": "/models set <model>",
        "insert": "/models set ",
        "category": "设置",
        "summary": "切换 dev_builder 当前模型；可再补 --agent-type。",
    },
    {
        "command": "/logs tail <match>",
        "insert": "/logs tail ",
        "category": "查看",
        "summary": "查看最新日志，或继续补全匹配文件名。",
    },
    {
        "command": "/rpc services",
        "insert": "/rpc services",
        "category": "查看",
        "summary": "列出逆向整理出的内部 RPC 服务。",
    },
    {
        "command": "/trace chats",
        "insert": "/trace chats",
        "category": "查看",
        "summary": "汇总本地日志中的最近聊天轮次。",
    },
    {
        "command": "/set workspace <path>",
        "insert": "/set workspace ",
        "category": "设置",
        "summary": "切换当前交互会话的工作目录。",
    },
    {
        "command": "/set app <path>",
        "insert": "/set app ",
        "category": "设置",
        "summary": "改用其他 Trae 应用 bundle。",
    },
    {
        "command": "/set support <path>",
        "insert": "/set support ",
        "category": "设置",
        "summary": "覆盖 Trae support 目录。",
    },
    {
        "command": "/set user-data <path>",
        "insert": "/set user-data ",
        "category": "设置",
        "summary": "覆盖 CLI user-data 目录。",
    },
    {
        "command": "/undo",
        "insert": "/undo",
        "category": "设置",
        "summary": "回退上一次本地路径或 workspace 变更。",
    },
    {
        "command": "/redo",
        "insert": "/redo",
        "category": "设置",
        "summary": "重做刚刚撤销的本地配置变更。",
    },
    {
        "command": "/chat",
        "insert": "/chat",
        "category": "导航",
        "summary": "返回实时对话视图。",
    },
    {
        "command": "/back",
        "insert": "/back",
        "category": "导航",
        "summary": "返回上一个交互视图。",
    },
    {
        "command": "/exit",
        "insert": "/exit",
        "category": "导航",
        "summary": "退出当前交互会话。",
    },
]


def set_view_mode(
    view: InteractiveViewState,
    mode: str,
    *,
    push_history: bool = True,
) -> None:
    target_mode = mode or "chat"
    if push_history and view.mode != target_mode:
        view.history.append(view.mode)
    view.mode = target_mode


def pop_view_mode(view: InteractiveViewState) -> str:
    view.mode = view.history.pop() if view.history else "chat"
    return view.mode


def show_chat_view(
    view: InteractiveViewState,
    *,
    notice: Optional[str] = None,
    clear_history: bool = False,
) -> None:
    view.mode = "chat"
    view.chat_scroll_offset = 0
    if clear_history:
        view.history.clear()
    if notice is not None:
        view.notice = notice


def clamp_input_cursor(view: InteractiveViewState) -> None:
    view.input_cursor = max(0, min(view.input_cursor, len(view.input_buffer)))


def clear_interactive_input(view: InteractiveViewState) -> None:
    view.input_buffer = ""
    view.input_cursor = 0
    view.slash_palette_index = 0


def insert_interactive_input(view: InteractiveViewState, text: str) -> None:
    if not text:
        return
    clamp_input_cursor(view)
    view.input_buffer = (
        view.input_buffer[: view.input_cursor]
        + text
        + view.input_buffer[view.input_cursor :]
    )
    view.input_cursor += len(text)
    view.slash_palette_index = 0


def backspace_interactive_input(view: InteractiveViewState) -> None:
    clamp_input_cursor(view)
    if view.input_cursor <= 0:
        return
    view.input_buffer = (
        view.input_buffer[: view.input_cursor - 1]
        + view.input_buffer[view.input_cursor :]
    )
    view.input_cursor -= 1
    view.slash_palette_index = 0


def delete_interactive_input(view: InteractiveViewState) -> None:
    clamp_input_cursor(view)
    if view.input_cursor >= len(view.input_buffer):
        return
    view.input_buffer = (
        view.input_buffer[: view.input_cursor]
        + view.input_buffer[view.input_cursor + 1 :]
    )
    view.slash_palette_index = 0


def move_interactive_cursor(view: InteractiveViewState, delta: int) -> None:
    clamp_input_cursor(view)
    view.input_cursor += delta
    clamp_input_cursor(view)


def set_interactive_cursor(view: InteractiveViewState, position: int) -> None:
    view.input_cursor = position
    clamp_input_cursor(view)


def render_interactive_input(view: InteractiveViewState, *, width: int = 68) -> str:
    clamp_input_cursor(view)
    marker = INTERACTIVE_CURSOR_MARKER
    raw = view.input_buffer
    if not raw:
        return marker
    available = max(8, width - 1)
    start = 0
    end = len(raw)
    if len(raw) > available:
        start = max(0, view.input_cursor - (available // 2))
        end = min(len(raw), start + available)
        start = max(0, end - available)
    visible = raw[start:end]
    cursor = max(0, min(view.input_cursor - start, len(visible)))
    rendered = visible[:cursor] + marker + visible[cursor:]
    if start > 0:
        rendered = "..." + rendered
    if end < len(raw):
        rendered = rendered + "..."
    return session_preview_text(rendered, limit=width)


def render_chat_input(view: InteractiveViewState, *, width: int = 68) -> str:
    if view.input_buffer:
        return render_interactive_input(view, width=width)
    placeholder = "输入消息或 / 打开命令"
    return session_preview_text(f"{INTERACTIVE_CURSOR_MARKER}{placeholder}", limit=width)


def display_cell_width(text: str) -> int:
    width = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
    return width


def extract_cursor_from_lines(lines: list[str]) -> tuple[list[str], Optional[int], Optional[int]]:
    updated = list(lines)
    for row, line in enumerate(updated, start=1):
        column_index = line.find(INTERACTIVE_CURSOR_MARKER)
        if column_index < 0:
            continue
        updated[row - 1] = line[:column_index] + line[column_index + len(INTERACTIVE_CURSOR_MARKER) :]
        return updated, row, display_cell_width(line[:column_index]) + 1
    return updated, None, None


def active_slash_query(view: InteractiveViewState) -> Optional[str]:
    draft = view.input_buffer.lstrip()
    if not draft.startswith("/"):
        return None
    return draft[1:].strip().lower()


def matching_slash_commands(query: str) -> list[dict[str, str]]:
    if not query:
        return list(SLASH_COMMAND_SPECS)
    prefix_matches: list[dict[str, str]] = []
    contains_matches: list[dict[str, str]] = []
    for item in SLASH_COMMAND_SPECS:
        command = item["command"][1:].lower()
        if command.startswith(query):
            prefix_matches.append(item)
        elif query in command:
            contains_matches.append(item)
    return [*prefix_matches, *contains_matches]


def slash_palette_matches(view: InteractiveViewState) -> list[dict[str, str]]:
    query = active_slash_query(view)
    if query is None:
        return []
    return matching_slash_commands(query)


def clamp_slash_palette_index(view: InteractiveViewState) -> None:
    matches = slash_palette_matches(view)
    if not matches:
        view.slash_palette_index = 0
        return
    view.slash_palette_index = max(0, min(view.slash_palette_index, len(matches) - 1))


def move_slash_palette_index(view: InteractiveViewState, delta: int) -> None:
    matches = slash_palette_matches(view)
    if not matches:
        view.slash_palette_index = 0
        return
    clamp_slash_palette_index(view)
    view.slash_palette_index += delta
    clamp_slash_palette_index(view)


def selected_slash_command(view: InteractiveViewState) -> Optional[dict[str, str]]:
    matches = slash_palette_matches(view)
    if not matches:
        return None
    clamp_slash_palette_index(view)
    return matches[view.slash_palette_index]


def slash_command_needs_arguments(item: dict[str, str]) -> bool:
    return item.get("insert") != item.get("command")


def slash_command_ready_to_run(
    current_input: str,
    item: dict[str, str],
) -> bool:
    current = current_input.strip()
    insert = str(item.get("insert") or item.get("command") or "").strip()
    command = str(item.get("command") or "").strip()
    if not current or not insert:
        return False
    if not slash_command_needs_arguments(item):
        return current == command
    return current.startswith(insert) and len(current) > len(insert)


def activate_selected_slash_command(
    view: InteractiveViewState,
) -> tuple[Optional[str], Optional[str]]:
    selected = selected_slash_command(view)
    if selected is None:
        return (None, None)
    current_input = view.input_buffer
    if slash_command_ready_to_run(current_input, selected):
        return (current_input.strip(), None)
    inserted = str(selected.get("insert") or selected.get("command") or "").rstrip()
    if slash_command_needs_arguments(selected):
        inserted = str(selected.get("insert") or "")
        view.input_buffer = inserted
        view.input_cursor = len(inserted)
        view.notice = (
            f"已填入 {selected['command']}，请继续补全参数后回车。"
        )
        clamp_input_cursor(view)
        return (None, "fill")
    return (str(selected.get("command") or "").strip(), None)


def format_slash_command_palette(
    view: InteractiveViewState,
    *,
    width: int = 80,
) -> Optional[str]:
    query = active_slash_query(view)
    if query is None:
        return None
    matches = slash_palette_matches(view)
    clamp_slash_palette_index(view)
    if not matches:
        return "\n".join(
            [
                session_preview_text("No matching slash commands.", limit=width),
                session_preview_text(
                    "Try /status, /diff, /sessions, or /models.",
                    limit=width,
                ),
            ]
        )
    max_visible = 8
    start = max(0, view.slash_palette_index - (max_visible // 2))
    start = min(start, max(0, len(matches) - max_visible))
    end = min(len(matches), start + max_visible)
    visible = matches[start:end]
    command_width = min(
        max(len(item["command"]) for item in visible) + 2,
        max(18, width // 3),
    )
    summary_width = max(12, width - command_width - 3)
    lines: list[str] = []
    for offset, item in enumerate(visible):
        absolute_index = start + offset
        marker = ">" if absolute_index == view.slash_palette_index else " "
        command = session_preview_text(item["command"], limit=command_width).ljust(
            command_width
        )
        summary = session_preview_text(item["summary"], limit=summary_width)
        lines.append(
            f"{marker} {command} {summary}".rstrip()
        )
    return "\n".join(lines)


def should_show_startup_banner(record: dict[str, Any], view: InteractiveViewState) -> bool:
    messages = record.get("messages") if isinstance(record.get("messages"), list) else []
    return view.mode == "chat" and not messages


def center_ascii_block(lines: list[str], *, width: int) -> list[str]:
    rendered: list[str] = []
    for raw in lines:
        text = str(raw)
        if not text:
            rendered.append("")
            continue
        if len(text) >= width:
            rendered.append(text[:width])
            continue
        padding = max(0, (width - len(text)) // 2)
        rendered.append((" " * padding) + text)
    return rendered


def colorize_startup_lines(lines: list[str], *, color: bool) -> list[str]:
    if not color:
        return list(lines)
    rendered: list[str] = []
    for line in lines:
        if not line.strip():
            rendered.append(line)
            continue
        rendered.append(f"{STARTUP_BANNER_COLOR}{line}\x1b[0m")
    return rendered


def startup_landing_lines(*, width: int = 80, color: bool = False) -> list[str]:
    if width < 16:
        return colorize_startup_lines(["TRAECLI"], color=color)
    if width < max(len(line) for line in STARTUP_WORDMARK_LINES):
        return colorize_startup_lines(
            center_ascii_block(["TRAECLI"], width=width),
            color=color,
        )
    return colorize_startup_lines(
        center_ascii_block(STARTUP_WORDMARK_LINES, width=width),
        color=color,
    )


def startup_banner_lines(*, width: int = 80) -> list[str]:
    return startup_landing_lines(width=width, color=True)


def abbreviate_home_path(path: str) -> str:
    raw = str(path or "").strip()
    if not raw:
        return "."
    try:
        home = str(Path.home())
    except RuntimeError:
        return raw
    if raw == home:
        return "~"
    prefix = home + "/"
    if raw.startswith(prefix):
        return "~/" + raw[len(prefix) :]
    return raw


def compact_display_path(path: str, *, limit: int = 48) -> str:
    display = abbreviate_home_path(path)
    if len(display) <= limit:
        return display
    parts = [part for part in Path(path).parts if part not in {"/", ""}]
    if not parts:
        return session_preview_text(display, limit=limit)
    if len(parts) >= 2:
        tail = "/".join(parts[-2:])
        shortened = f".../{tail}"
        if len(shortened) <= limit:
            return shortened
    return session_preview_text(display, limit=limit)


def startup_model_label(app: AppContext) -> str:
    try:
        state = app.backend.read_model_state()
    except Exception:
        return "unknown"
    entry = model_entry_payload(state, "dev_builder")
    if not entry:
        return "unknown"
    return model_display_name(entry)


def startup_card_lines(app: AppContext, *, width: int = 80) -> list[str]:
    model = session_preview_text(startup_model_label(app), limit=max(12, width - 18))
    directory = session_preview_text(
        compact_display_path(current_workspace(app)),
        limit=max(16, width - 18),
    )
    if width < 40:
        return [
            "TraeCLI",
            f"model: {model}",
            f"directory: {directory}",
        ]

    inner_lines = [
        "TraeCLI",
        f"model: {model}   /model",
        f"directory: {directory}",
    ]
    inner_width = min(
        max(len(line) for line in inner_lines) + 2,
        min(max(20, width - 6), 56),
    )
    indent = " "
    top = indent + "╭" + ("─" * (inner_width + 2)) + "╮"
    bottom = indent + "╰" + ("─" * (inner_width + 2)) + "╯"
    lines = [top]
    for raw in inner_lines:
        clipped = str(raw).replace("\n", " ")
        if len(clipped) > inner_width:
            clipped = clipped[: inner_width - 3].rstrip() + "..."
        lines.append(f"{indent}│ {clipped.ljust(inner_width)} │")
    lines.append(bottom)
    return lines


def startup_status_line(app: AppContext, *, width: int = 80) -> str:
    return session_preview_text(
        f"{startup_model_label(app)} · {compact_display_path(current_workspace(app))}",
        limit=width,
    )


def chat_intro_lines(app: AppContext, *, width: int = 80) -> list[str]:
    lines = startup_card_lines(app, width=width)
    lines.append("")
    lines.extend(
        wrap_block_lines(
            "Tip: 输入 / 打开命令；使用 /model 查看或切换当前模型。",
            width=max(20, width),
        )
    )
    lines.append("")
    return lines


def parse_terminal_keypress(raw: str) -> list[str]:
    keys: list[str] = []
    index = 0
    while index < len(raw):
        if raw[index] == "\x1b":
            matched = False
            for sequence, name in sorted(
                ESCAPE_SEQUENCE_KEYS.items(),
                key=lambda item: len(item[0]),
                reverse=True,
            ):
                if raw.startswith(sequence, index):
                    keys.append(name)
                    index += len(sequence)
                    matched = True
                    break
            if matched:
                continue
            keys.append("ESC")
            index += 1
            continue
        char = raw[index]
        if char in {"\r", "\n"}:
            keys.append("ENTER")
        elif char in {"\x7f", "\b"}:
            keys.append("BACKSPACE")
        else:
            keys.append(char)
        index += 1
    return keys


def session_entries_from_payload(payload: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    sessions = payload.get("sessions")
    if not isinstance(sessions, list):
        return []
    return [item for item in sessions if isinstance(item, dict)]


def clamp_session_list_index(view: InteractiveViewState) -> None:
    sessions = session_entries_from_payload(view.sessions_payload)
    if not sessions:
        view.session_list_index = 0
        return
    view.session_list_index = max(0, min(view.session_list_index, len(sessions) - 1))


def selected_session_entry(view: InteractiveViewState) -> Optional[dict[str, Any]]:
    sessions = session_entries_from_payload(view.sessions_payload)
    if not sessions:
        return None
    clamp_session_list_index(view)
    return sessions[view.session_list_index]


def format_saved_sessions_view(
    payload: Optional[dict[str, Any]],
    *,
    selected_index: int = 0,
) -> str:
    if not isinstance(payload, dict):
        return "No saved sessions loaded."
    sessions = session_entries_from_payload(payload)
    if not sessions:
        return format_cli_sessions(payload)
    lines: list[str] = []
    max_visible = 8
    start = max(0, selected_index - (max_visible // 2))
    start = min(start, max(0, len(sessions) - max_visible))
    end = min(len(sessions), start + max_visible)
    visible = sessions[start:end]
    if start > 0:
        lines.append(f"...前面还有 {start} 个会话。")
    for offset, item in enumerate(visible, start=1):
        absolute_index = start + offset
        marker = ">" if absolute_index - 1 == selected_index else " "
        title = session_preview_text(item.get("title") or "Interactive session", limit=56)
        if item.get("resumable"):
            title = f"{title} [resumable]"
        preview = item.get("preview")
        if preview:
            title = f"{title} | {session_preview_text(preview, limit=28)}"
        lines.append(f"{marker} {absolute_index}. {title}")
    if end < len(sessions):
        lines.append(f"...后面还有 {len(sessions) - end} 个会话。")
    return "\n".join(lines)


def format_selected_session_summary(item: Optional[dict[str, Any]]) -> str:
    if not item:
        return "No saved session selected."
    messages = item.get("messages")
    message_count = len(messages) if isinstance(messages, list) else int(item.get("message_count") or 0)
    lines = [
        f"Session: {item.get('id') or 'unknown'}",
        f"Title: {item.get('title') or 'Interactive session'}",
        f"Workspace: {item.get('workspace') or 'unknown'}",
        f"Hidden session: {item.get('last_headless_session_id') or 'none'}",
        f"Last dispatch: {item.get('last_dispatch_method') or 'unknown'}",
        f"Messages: {message_count}",
    ]
    preview = item.get("preview")
    if preview:
        lines.append(f"Preview: {preview}")
    return "\n".join(lines)


def message_prefix(role: str) -> str:
    return {
        "user": "You",
        "assistant": "Trae",
        "system": "System",
    }.get(role, role.title())


def conversation_message_prefix(role: str) -> str:
    return {
        "user": "›",
        "assistant": "•",
        "system": "·",
    }.get(role, "·")


def conversation_event_prefix(status: str) -> str:
    return {
        "working": "●",
        "success": "✓",
        "error": "✕",
        "muted": "·",
    }.get(status, "·")


def conversation_event_lines(
    item: dict[str, Any],
    *,
    width: int,
) -> list[str]:
    prefix = conversation_event_prefix(str(item.get("status") or "muted"))
    headline = str(item.get("headline") or item.get("content") or "").strip()
    if not headline:
        return []
    content_width = max(12, width - 3)
    rendered: list[str] = []
    wrapped_headline = wrap_block_lines(headline, width=content_width)
    rendered.append(f"{prefix} {wrapped_headline[0]}")
    indent = " " * (len(prefix) + 1)
    for extra in wrapped_headline[1:]:
        rendered.append(f"{indent}{extra}")

    details = item.get("details") or []
    if not isinstance(details, list):
        details = []
    detail_width = max(12, width - 6)
    visible_details = [
        str(detail).rstrip()
        for detail in details
        if str(detail).strip()
    ]
    for index, detail in enumerate(visible_details):
        wrapped = wrap_block_lines(detail, width=detail_width)
        if not wrapped:
            continue
        connector = "│" if index < len(visible_details) - 1 else "└"
        rendered.append(f"  {connector} {wrapped[0]}")
        follow_indent = "  │ " if index < len(visible_details) - 1 else "    "
        for extra in wrapped[1:]:
            rendered.append(f"{follow_indent}{extra}")
    return rendered


def main_panel_width(total_width: int) -> int:
    if total_width < 88:
        return total_width
    right_width = min(38, max(28, int(total_width * 0.32)))
    return max(40, total_width - 3 - right_width)


def main_panel_body_lines(total_width: int, content_height: int) -> int:
    if total_width < 88:
        main_budget = max(5, int(content_height * 0.65))
        return max(1, main_budget - 1)
    return max(1, max(6, content_height) - 1)


def conversation_body_lines(
    record: dict[str, Any],
    *,
    width: int = 74,
) -> list[str]:
    messages = record.get("messages") if isinstance(record.get("messages"), list) else []
    message_lines: list[str] = []
    if not messages:
        return startup_landing_lines(width=width, color=True)
    content_width = max(12, width - 3)
    for item in messages:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "system")
        if role == "event":
            rendered_event = conversation_event_lines(item, width=width)
            if rendered_event:
                message_lines.extend(rendered_event)
                message_lines.append("")
            continue
        prefix = conversation_message_prefix(role)
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        wrapped = wrap_block_lines(content, width=content_width)
        if not wrapped:
            continue
        indent = " " * (len(prefix) + 1)
        message_lines.append(f"{prefix} {wrapped[0]}")
        for extra in wrapped[1:]:
            message_lines.append(f"{indent}{extra}")
        message_lines.append("")
    if message_lines and not message_lines[-1]:
        message_lines.pop()
    return message_lines or ["No messages yet."]


def format_conversation_body(
    record: dict[str, Any],
    *,
    width: int = 74,
) -> str:
    return "\n".join(conversation_body_lines(record, width=width))


def record_message_count(record: dict[str, Any]) -> int:
    messages = record.get("messages") if isinstance(record.get("messages"), list) else []
    if messages:
        return len(messages)
    return int(record.get("message_count") or 0)


def format_interactive_context(
    app: AppContext,
    record: dict[str, Any],
    view: InteractiveViewState,
) -> str:
    lines = [
        f"Workspace: {current_workspace(app)}",
        f"Session: {record.get('id') or 'unknown'}",
        f"Mode: {view.mode}",
        f"Messages: {record_message_count(record)}",
        f"Hidden session: {app.state.last_headless_session_id or 'none'}",
        f"Status: {view.notice or 'ready'}",
    ]
    panel_title = (view.panel_title or "").strip()
    panel_body = (view.panel_body or "").strip()
    if panel_title or panel_body:
        lines.append("")
        lines.append(panel_title or "Latest")
        lines.append(panel_body or "No output.")
    return "\n".join(lines)


def clamp_chat_scroll_offset(
    view: InteractiveViewState,
    record: dict[str, Any],
    *,
    panel_width: int,
    visible_lines: int,
) -> None:
    body_lines = conversation_body_lines(record, width=panel_width)
    max_offset = max(0, len(body_lines) - max(1, visible_lines))
    view.chat_scroll_offset = max(0, min(view.chat_scroll_offset, max_offset))


def move_chat_scroll_offset(
    view: InteractiveViewState,
    record: dict[str, Any],
    *,
    panel_width: int,
    visible_lines: int,
    delta: int,
) -> None:
    clamp_chat_scroll_offset(
        view,
        record,
        panel_width=panel_width,
        visible_lines=visible_lines,
    )
    body_lines = conversation_body_lines(record, width=panel_width)
    max_offset = max(0, len(body_lines) - max(1, visible_lines))
    view.chat_scroll_offset = max(0, min(view.chat_scroll_offset + delta, max_offset))


def render_chat_view_lines(
    record: dict[str, Any],
    *,
    width: int,
    visible_lines: int,
    scroll_offset: int,
) -> list[str]:
    body_lines = conversation_body_lines(record, width=width)
    body_count = len(body_lines)
    visible = max(1, visible_lines)
    max_offset = max(0, body_count - visible)
    normalized_offset = max(0, min(scroll_offset, max_offset))
    if body_count <= visible:
        return body_lines
    end = body_count - normalized_offset
    start = max(0, end - visible)
    lines = list(body_lines[start:end])
    hidden_top = start
    hidden_bottom = body_count - end
    if hidden_top > 0 and lines:
        lines[0] = f"...上面还有 {hidden_top} 行。"
    if hidden_bottom > 0 and lines:
        lines[-1] = f"...下面还有 {hidden_bottom} 行。"
    return lines


def interactive_header_lines(
    app: AppContext,
    record: dict[str, Any],
    view: InteractiveViewState,
    *,
    width: int = 80,
) -> list[str]:
    workspace = current_workspace(app)
    workspace_name = Path(workspace).name.strip() or workspace
    lines = [
        session_preview_text(f"traecli | {workspace_name}", limit=width),
        session_preview_text(workspace, limit=width),
    ]
    metadata = [
        f"session={record.get('id') or 'unknown'}",
        f"mode={view.mode}",
        f"messages={record_message_count(record)}",
    ]
    if app.state.last_headless_session_id:
        metadata.append(f"hidden={app.state.last_headless_session_id}")
    metadata.append(f"status={view.notice or 'ready'}")
    lines.append(session_preview_text(" | ".join(metadata), limit=width))
    return lines


def interactive_footer_lines(view: InteractiveViewState, *, width: int = 80) -> list[str]:
    slash_query = active_slash_query(view)
    if slash_query is not None:
        return [
            session_preview_text(
                "slash: 输入以筛选命令和设置 | 上下键选择 | Enter 执行",
                limit=width,
            ),
            session_preview_text(
                "可尝试 /status、/diff、/model、/approvals、/sessions。",
                limit=width,
            ),
            f"> {render_interactive_input(view, width=max(8, width - 2))}",
        ]
    if view.mode == "sessions":
        return [
            session_preview_text(
                "history: Up/Down or j/k move | Enter/o open | Esc back | /exit",
                limit=width,
            ),
            session_preview_text(
                "/chat returns to the live conversation.",
                limit=width,
            ),
            f"> {render_interactive_input(view, width=max(8, width - 2))}",
        ]
    if view.mode == "detail":
        return [
            session_preview_text(
                "inspector: Esc back | /chat conversation | /exit",
                limit=width,
            ),
            session_preview_text(
                "Send a new prompt or run a slash command.",
                limit=width,
            ),
            f"> {render_interactive_input(view, width=max(8, width - 2))}",
        ]
    if view.mode == "output":
        return [
            session_preview_text(
                "output: Esc back | /chat conversation | /exit",
                limit=width,
            ),
            session_preview_text(
                "Send a new prompt or run a slash command.",
                limit=width,
            ),
            f"> {render_interactive_input(view, width=max(8, width - 2))}",
        ]
    return [
        session_preview_text(
            "prompt: Enter 发送 | Up/Down 滚动聊天 | / 打开命令 | /exit",
            limit=width,
        ),
        session_preview_text(
            "最新回复贴近底部显示；向上滚动查看更早内容。",
            limit=width,
        ),
        f"> {render_interactive_input(view, width=max(8, width - 2))}",
    ]


def interactive_chat_footer_lines(
    app: AppContext,
    record: dict[str, Any],
    view: InteractiveViewState,
    *,
    width: int = 80,
) -> list[str]:
    lines = [f"› {render_chat_input(view, width=max(8, width - 2))}"]
    slash_palette = format_slash_command_palette(view, width=width)
    if slash_palette:
        lines.append("")
        lines.extend(slash_palette.splitlines())
    lines.append(startup_status_line(app, width=width))
    return lines


def interactive_screen_sections(
    app: AppContext,
    record: dict[str, Any],
    view: InteractiveViewState,
    *,
    width: int = 80,
) -> tuple[str, str, str, str]:
    slash_palette = format_slash_command_palette(view)
    if slash_palette:
        if view.mode == "sessions":
            clamp_session_list_index(view)
            return (
                "命令面板",
                slash_palette,
                "Inspector",
                format_selected_session_summary(selected_session_entry(view)),
            )
        if view.mode == "detail":
            payload = view.session_detail_payload or {"session": record, "found": True}
            return (
                "命令面板",
                slash_palette,
                view.session_detail_title or "Session Detail",
                format_cli_session_detail(payload),
            )
        if view.mode == "output":
            return (
                "命令面板",
                slash_palette,
                view.output_title,
                view.output_body,
            )
        return (
            "命令面板",
            slash_palette,
            "Context",
            format_interactive_context(app, record, view),
        )
    if view.mode == "sessions":
        clamp_session_list_index(view)
        return (
            "History",
            format_saved_sessions_view(
                view.sessions_payload,
                selected_index=view.session_list_index,
            ),
            "Inspector",
            format_selected_session_summary(selected_session_entry(view)),
        )
    if view.mode == "detail":
        payload = view.session_detail_payload or {"session": record, "found": True}
        return (
            view.session_detail_title or "Session Detail",
            format_cli_session_detail(payload),
            view.panel_title,
            view.panel_body,
        )
    if view.mode == "output":
        return (
            view.output_title,
            view.output_body,
            view.panel_title,
            view.panel_body,
        )
    return (
        "Chat",
        format_conversation_body(record, width=max(24, width)),
        "Context",
        format_interactive_context(app, record, view),
    )


def build_interactive_screen(
    app: AppContext,
    record: dict[str, Any],
    view: InteractiveViewState,
) -> str:
    width, height = terminal_screen_size()
    single_panel_chat = view.mode == "chat"
    if single_panel_chat:
        footer_lines = interactive_chat_footer_lines(app, record, view, width=width)
        intro_lines = chat_intro_lines(app, width=width)
        messages = record.get("messages") if isinstance(record.get("messages"), list) else []
        if not messages:
            lines = list(intro_lines)
        else:
            content_budget = max(4, height - len(intro_lines) - len(footer_lines) - 1)
            clamp_chat_scroll_offset(
                view,
                record,
                panel_width=width,
                visible_lines=content_budget,
            )
            lines = [*intro_lines]
            lines.extend(
                render_chat_view_lines(
                    record,
                    width=width,
                    visible_lines=content_budget,
                    scroll_offset=view.chat_scroll_offset,
                )
            )
        lines.append("")
        lines.extend(footer_lines)
        rendered_lines, cursor_row, cursor_col = extract_cursor_from_lines(lines)
        output = "\x1b[2J\x1b[H" + "\n".join(rendered_lines)
        if cursor_row is not None and cursor_col is not None:
            output += f"\x1b[{cursor_row};{cursor_col}H"
        return output

    header_lines = interactive_header_lines(app, record, view, width=width)
    footer_lines = interactive_footer_lines(view, width=width)
    content_budget = max(10, height - (len(header_lines) + len(footer_lines) + 4))
    chat_panel_width = width if single_panel_chat else main_panel_width(width)
    chat_visible_lines = (
        max(1, content_budget - 1)
        if single_panel_chat
        else main_panel_body_lines(width, content_budget)
    )
    if single_panel_chat:
        clamp_chat_scroll_offset(
            view,
            record,
            panel_width=chat_panel_width,
            visible_lines=chat_visible_lines,
        )
    main_title, main_body, side_title, side_body = interactive_screen_sections(
        app,
        record,
        view,
        width=chat_panel_width,
    )
    left_anchor = "bottom" if single_panel_chat else "top"
    left_scroll_offset = view.chat_scroll_offset if left_anchor == "bottom" else 0

    lines = [*header_lines, ""]
    if single_panel_chat:
        lines.extend(
            render_screen_section(
                main_title,
                main_body,
                width=width,
                max_lines=max(1, content_budget - 1),
                anchor="bottom",
                scroll_offset=view.chat_scroll_offset,
            )
        )
    else:
        lines.extend(
            render_split_screen_sections(
                main_title,
                main_body,
                side_title,
                side_body,
                width=width,
                height=content_budget,
                left_anchor=left_anchor,
                left_scroll_offset=left_scroll_offset,
            )
        )
    lines.append("")
    lines.append(render_screen_rule(width=width))
    lines.extend(footer_lines)
    return "\x1b[2J\x1b[H" + "\n".join(lines)


def render_interactive_session(
    app: AppContext,
    record: dict[str, Any],
    view: InteractiveViewState,
) -> None:
    if not should_use_alt_screen(app):
        return
    click.echo(build_interactive_screen(app, record, view), nl=False)


def set_interactive_panel(
    view: InteractiveViewState,
    *,
    title: str,
    body: Optional[str],
    notice: Optional[str] = None,
) -> None:
    view.panel_title = title
    view.panel_body = (body or "").strip() or "No output."
    if notice is not None:
        view.notice = notice


def show_repl_output(
    app: AppContext,
    record: dict[str, Any],
    view: InteractiveViewState,
    *,
    alt_screen: bool,
    title: str,
    data: Any = None,
    text: Optional[str] = None,
    notice: Optional[str] = None,
) -> None:
    body = text
    if body is None and data is not None:
        body = json.dumps(normalize(data), indent=2, ensure_ascii=False)
    if alt_screen:
        set_view_mode(view, "output", push_history=view.mode != "output")
        view.output_title = title
        view.output_body = (body or "").strip() or "No output."
        set_interactive_panel(
            view,
            title="Status",
            body="Use /back to return to the previous view.",
            notice=notice,
        )
        render_interactive_session(app, record, view)
        return
    if data is None:
        click.echo(body or "")
        return
    emit(app, data=data, text=body)


def show_repl_notice(
    app: AppContext,
    record: dict[str, Any],
    view: InteractiveViewState,
    *,
    alt_screen: bool,
    message: str,
) -> None:
    if alt_screen:
        set_interactive_panel(view, title="Status", body=message, notice=message)
        render_interactive_session(app, record, view)
        return
    click.echo(message)


def show_saved_sessions_view(
    app: AppContext,
    record: dict[str, Any],
    view: InteractiveViewState,
    *,
    limit: int = 10,
    all_workspaces: bool = False,
    notice: Optional[str] = None,
) -> None:
    payload = list_cli_sessions_payload(
        app,
        all_workspaces=all_workspaces,
        limit=limit,
    )
    view.sessions_payload = payload
    clamp_session_list_index(view)
    set_view_mode(view, "sessions", push_history=view.mode != "sessions")
    set_interactive_panel(
        view,
        title="Status",
        body="Browse saved sessions. Use j/k to move, o to inspect, /back to return.",
        notice=notice or "saved sessions",
    )
    render_interactive_session(app, record, view)


def show_session_detail_view(
    app: AppContext,
    record: dict[str, Any],
    view: InteractiveViewState,
    *,
    payload: dict[str, Any],
    title: str,
    notice: Optional[str] = None,
    status_body: Optional[str] = None,
) -> None:
    view.session_detail_payload = payload
    view.session_detail_title = title
    set_view_mode(view, "detail", push_history=view.mode != "detail")
    set_interactive_panel(
        view,
        title="Status",
        body=status_body or "Use /back to return to the previous view.",
        notice=notice or "session detail",
    )
    render_interactive_session(app, record, view)


def handle_interactive_view_input(
    app: AppContext,
    record: dict[str, Any],
    view: InteractiveViewState,
    line: str,
) -> bool:
    command = line.strip().lower()
    if not command:
        return False
    if command in {"chat", "/chat"}:
        show_chat_view(view, notice="chat", clear_history=True)
        render_interactive_session(app, record, view)
        return True
    if command in {"back", "/back"} or (command in {"b", "q"} and view.mode != "chat"):
        show_chat_view(view, notice="chat") if view.mode == "chat" else None
        target_mode = pop_view_mode(view) if view.mode != "chat" else "chat"
        view.notice = f"returned to {target_mode}"
        render_interactive_session(app, record, view)
        return True
    if view.mode != "sessions":
        return False
    if command in {"j", "down", "n", "next"}:
        sessions = session_entries_from_payload(view.sessions_payload)
        if sessions:
            view.session_list_index = min(len(sessions) - 1, view.session_list_index + 1)
        view.notice = "moved selection down"
        render_interactive_session(app, record, view)
        return True
    if command in {"k", "up", "p", "prev", "previous"}:
        view.session_list_index = max(0, view.session_list_index - 1)
        view.notice = "moved selection up"
        render_interactive_session(app, record, view)
        return True
    if command in {"o", "open", "s", "select", "detail"}:
        selected = selected_session_entry(view)
        if selected is None:
            show_repl_notice(
                app,
                record,
                view,
                alt_screen=True,
                message="No saved session is selected.",
            )
            return True
        session_id = str(selected.get("id") or "")
        payload = cli_session_detail_payload(app, session_id=session_id)
        if not payload.get("found"):
            show_repl_notice(
                app,
                record,
                view,
                alt_screen=True,
                message=f"CLI session not found: {session_id}",
            )
            return True
        show_session_detail_view(
            app,
            record,
            view,
            payload=payload,
            title=f"Session Detail: {session_id}",
            notice="session detail",
            status_body="Use /back to return to saved sessions.",
        )
        return True
    return False


def pick_cli_session(
    app: AppContext,
    *,
    all_workspaces: bool = False,
    alt_screen: bool = False,
    view: Optional[InteractiveViewState] = None,
    record: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    payload = list_cli_sessions_payload(
        app,
        all_workspaces=all_workspaces,
        resumable_only=True,
        limit=20,
    )
    sessions = payload.get("sessions") or []
    if not sessions:
        return None
    index = 0

    def picker_body() -> str:
        lines = []
        for offset, item in enumerate(sessions, start=1):
            marker = ">" if offset - 1 == index else " "
            title = item.get("title") or "Interactive session"
            lines.append(f"{marker} {offset}. {title}")
            lines.append(
                f"   resumable={'yes' if item.get('resumable') else 'no'} updated={item.get('updated_at') or item.get('created_at') or 'unknown'}"
            )
            preview = item.get("preview")
            if preview:
                lines.append(f"   {preview}")
        selected = sessions[index]
        lines.append("")
        lines.append("Selected:")
        lines.append(f"  id={selected.get('id') or 'unknown'}")
        lines.append(f"  workspace={selected.get('workspace') or 'unknown'}")
        lines.append(f"  hidden_session={selected.get('last_headless_session_id') or 'none'}")
        return "\n".join(lines)

    def render_picker(notice: str) -> None:
        body = picker_body()
        if alt_screen and view is not None and record is not None:
            set_interactive_panel(
                view,
                title="Resume Picker",
                body=body,
                notice=notice,
            )
            render_interactive_session(app, record, view)
            return
        click.echo(body)

    render_picker("Select number, use j/k to move, s to select, q to cancel.")
    while True:
        raw = input("Select session (j/k/s/q or number): ").strip()
        if not raw:
            render_picker("Use j/k to move, s to select, q to cancel.")
            continue
        if raw.isdigit():
            target = int(raw)
            if 1 <= target <= len(sessions):
                return sessions[target - 1]
            render_picker(f"Choose a number between 1 and {len(sessions)}.")
            continue
        if raw.lower() in {"j", "down", "n", "next"}:
            index = min(len(sessions) - 1, index + 1)
            render_picker("Moved selection down.")
            continue
        if raw.lower() in {"k", "up", "p", "prev", "previous"}:
            index = max(0, index - 1)
            render_picker("Moved selection up.")
            continue
        if raw.lower() in {"s", "select"}:
            return sessions[index]
        if raw.lower() in {"q", "quit", "cancel"}:
            return None
        render_picker("Use j/k to move, s to select, q to cancel, or enter a number.")


def new_cli_session_record(
    app: AppContext,
    *,
    session_id: Optional[str] = None,
    existing: Optional[dict[str, Any]] = None,
    initial_prompt: Optional[str] = None,
) -> dict[str, Any]:
    now = datetime.now().astimezone().isoformat()
    record = dict(existing or {})
    record_id = str(record.get("id") or session_id or uuid.uuid4())
    created_at = str(record.get("created_at") or now)
    prompt_preview = (initial_prompt or record.get("initial_prompt") or "").strip()
    record.update(
        {
            "id": record_id,
            "created_at": created_at,
            "updated_at": now,
            "workspace": current_workspace(app),
            "app_path": app.state.app_path,
            "support_dir": app.state.support_dir,
            "user_data_dir": app.state.user_data_dir,
            "last_headless_session_id": app.state.last_headless_session_id,
            "message_count": int(record.get("message_count") or 0),
            "initial_prompt": prompt_preview or None,
            "last_prompt": record.get("last_prompt"),
            "last_dispatch_method": record.get("last_dispatch_method"),
            "resumable": bool(app.state.last_headless_session_id),
            "title": (prompt_preview or record.get("title") or "Interactive session")[:80],
            "messages": record.get("messages")
            if isinstance(record.get("messages"), list)
            else [],
        }
    )
    return record


def save_cli_session_record(
    app: AppContext,
    record: dict[str, Any],
) -> dict[str, Any]:
    record.update(
        {
            "updated_at": datetime.now().astimezone().isoformat(),
            "workspace": current_workspace(app),
            "app_path": app.state.app_path,
            "support_dir": app.state.support_dir,
            "user_data_dir": app.state.user_data_dir,
            "last_headless_session_id": app.state.last_headless_session_id,
            "current_cdp_target_id": app.state.current_cdp_target_id,
            "resumable": bool(app.state.last_headless_session_id),
        }
    )
    app.backend.save_cli_session(record)
    app.state.current_cli_session_id = str(record.get("id") or "")
    return record


def apply_cli_session_record(app: AppContext, record: dict[str, Any]) -> None:
    workspace = record.get("workspace")
    app_path = record.get("app_path")
    support_dir = record.get("support_dir")
    user_data_dir = record.get("user_data_dir")
    app.state.workspace = workspace if isinstance(workspace, str) and workspace else None
    app.state.app_path = app_path if isinstance(app_path, str) and app_path else app.state.app_path
    app.state.support_dir = (
        support_dir if isinstance(support_dir, str) and support_dir else app.state.support_dir
    )
    app.state.user_data_dir = (
        user_data_dir
        if isinstance(user_data_dir, str) and user_data_dir
        else app.state.user_data_dir
    )
    headless_session_id = record.get("last_headless_session_id")
    app.state.last_headless_session_id = (
        headless_session_id
        if isinstance(headless_session_id, str) and headless_session_id.strip()
        else None
    )
    app.state.current_cli_session_id = str(record.get("id") or "") or None
    cdp_target_id = record.get("current_cdp_target_id")
    app.state.current_cdp_target_id = (
        cdp_target_id if isinstance(cdp_target_id, str) and cdp_target_id.strip() else None
    )
    app.rebuild_backend()


def apply_cli_session_chat_context(app: AppContext, record: dict[str, Any]) -> None:
    headless_session_id = record.get("last_headless_session_id")
    app.state.last_headless_session_id = (
        headless_session_id
        if isinstance(headless_session_id, str) and headless_session_id.strip()
        else None
    )
    app.state.current_cli_session_id = str(record.get("id") or "") or None
    cdp_target_id = record.get("current_cdp_target_id")
    app.state.current_cdp_target_id = (
        cdp_target_id if isinstance(cdp_target_id, str) and cdp_target_id.strip() else None
    )


def restore_latest_chat_context(
    app: AppContext,
    *,
    resumable_only: bool = False,
) -> Optional[dict[str, Any]]:
    record = app.backend.latest_cli_session(
        workspace=current_workspace(app),
        resumable_only=resumable_only,
    )
    if record is None:
        return None
    apply_cli_session_chat_context(app, record)
    return record


def remember_cli_session_prompt(
    app: AppContext,
    record: dict[str, Any],
    *,
    prompt: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if not record.get("initial_prompt"):
        record["initial_prompt"] = prompt
    if not record.get("title"):
        record["title"] = prompt[:80]
    record["last_prompt"] = prompt
    record["message_count"] = int(record.get("message_count") or 0) + 1
    record["last_dispatch_method"] = payload.get("dispatch_method")
    append_cli_session_message(record, role="user", content=prompt)
    if app.state.last_headless_session_id:
        record["last_headless_session_id"] = app.state.last_headless_session_id
    elif isinstance(payload.get("session_id"), str) and payload.get("session_id"):
        record["last_headless_session_id"] = payload["session_id"]
    result_text = chat_result_text(payload)
    if result_text:
        append_cli_session_message(record, role="assistant", content=result_text)
    return save_cli_session_record(app, record)


def prepare_interactive_cli_prompt(
    record: dict[str, Any],
    *,
    prompt: str,
) -> None:
    if not record.get("initial_prompt"):
        record["initial_prompt"] = prompt
    if not record.get("title"):
        record["title"] = prompt[:80]
    record["last_prompt"] = prompt
    record["message_count"] = int(record.get("message_count") or 0) + 1
    append_cli_session_message(record, role="user", content=prompt)


def interactive_mode_supported(app: AppContext) -> None:
    if app.json_output:
        raise click.UsageError(
            "Interactive mode does not support `--json`; use `traecli exec ...`."
        )


def execute_chat_request(
    app: AppContext,
    *,
    prompt: Optional[str],
    mode: str = "agent",
    add_files: tuple[str, ...] = (),
    new_window: bool = False,
    reuse_window: bool = False,
    maximize: bool = False,
    requested_dispatch_method: str = "auto",
    new_chat: bool = False,
    inspect_turn: bool = False,
    wait_seconds: float = 0.0,
    answer_seconds: float = 30.0,
    cdp_host: Optional[str] = None,
    cdp_port: Optional[int] = None,
    cdp_title_contains: Optional[str] = None,
    cdp_url_contains: Optional[str] = None,
    launch_debug: bool = False,
    restore_saved_context: bool = True,
    force_fresh_dispatch: bool = False,
) -> dict[str, Any]:
    args = ["chat", "--mode", mode]
    for path in add_files:
        args.extend(["--add-file", path])
    if new_window:
        args.append("--new-window")
    if reuse_window:
        args.append("--reuse-window")
    if maximize:
        args.append("--maximize")
    if prompt:
        args.append(prompt)

    simple_agent_chat = is_simple_agent_chat(
        mode=mode,
        add_files=add_files,
        new_window=new_window,
        reuse_window=reuse_window,
        maximize=maximize,
    )
    dispatch_method = resolve_chat_dispatch_method(
        requested_dispatch_method,
        mode=mode,
        add_files=add_files,
        new_window=new_window,
        reuse_window=reuse_window,
        maximize=maximize,
    )
    effective_launch_debug = launch_debug or (
        requested_dispatch_method == "auto"
        and simple_agent_chat
        and dispatch_method == "cdp"
    )
    if (
        requested_dispatch_method == "auto"
        and simple_agent_chat
        and dispatch_method == "headless"
        and not app.backend.headless_dispatch_available(
            timeout_seconds=1.0,
            workspace=current_workspace(app),
        )
    ):
        raise click.ClickException(format_headless_unavailable_message(app))

    effective_new_chat = new_chat or (
        force_fresh_dispatch and dispatch_method in {"uri", "command", "headless", "cdp"}
    )

    if restore_saved_context and not effective_new_chat:
        if dispatch_method == "headless" and not app.state.last_headless_session_id:
            restore_latest_chat_context(app, resumable_only=True)

    if new_chat and dispatch_method not in {"uri", "command", "cdp", "headless"}:
        raise click.UsageError(
            "`--new-chat` currently requires `--dispatch uri`, `--dispatch command`, "
            "`--dispatch headless`, or `--dispatch cdp`."
        )
    if dispatch_method in {"uri", "command", "headless", "cdp"}:
        unsupported: list[str] = []
        if mode != "agent":
            unsupported.append("--mode")
        if add_files:
            unsupported.append("--add-file")
        if new_window:
            unsupported.append("--new-window")
        if reuse_window:
            unsupported.append("--reuse-window")
        if maximize:
            unsupported.append("--maximize")
        if unsupported:
            raise click.UsageError(
                f"`chat --dispatch {dispatch_method}` does not support "
                + ", ".join(sorted(set(unsupported)))
                + "."
            )
        if not prompt:
            raise click.UsageError(
                f"`chat --dispatch {dispatch_method}` requires a prompt."
            )

    known_frontend_ids: set[str] = set()
    if inspect_turn:
        baseline_turns = app.backend.extract_chat_turns(limit=50).get("turns") or []
        known_frontend_ids = {
            turn["frontend_message_id"]
            for turn in baseline_turns
            if turn.get("frontend_message_id")
        }
    started_at = datetime.now().astimezone()
    payload: dict[str, Any] = {
        "command": args,
        "dispatch_method": dispatch_method,
        "requested_dispatch_method": requested_dispatch_method,
        "new_chat": effective_new_chat,
    }

    if dispatch_method == "uri":
        deep_link = app.backend.build_side_chat_deep_link(
            prompt,
            new_chat=effective_new_chat,
        )
        dispatch_command = app.backend.open_command(deep_link)
        result = app.backend.open_target(deep_link)
        app.state.note_backend_command(dispatch_command)
        payload.update(
            {
                "dispatch_command": dispatch_command,
                "deep_link": deep_link,
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
    elif dispatch_method == "command":
        command_uri = app.backend.build_side_chat_command_uri(
            prompt,
            new_chat=effective_new_chat,
        )
        dispatch_command = app.backend.cli_open_url_command(command_uri)
        result = app.backend.open_url_via_cli(
            command_uri,
            cwd=app.state.workspace or None,
        )
        app.state.note_backend_command(dispatch_command)
        payload.update(
            {
                "dispatch_command": dispatch_command,
                "command_uri": command_uri,
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
    elif dispatch_method == "headless":
        if not app.backend.headless_dispatch_available(
            timeout_seconds=1.0,
            workspace=current_workspace(app),
        ):
            raise click.ClickException(format_headless_unavailable_message(app))
        session_id = None if effective_new_chat else app.state.last_headless_session_id
        try:
            headless_payload = app.backend.invoke_headless_chat(
                prompt or "",
                session_id=session_id,
                timeout_seconds=answer_seconds,
                workspace=current_workspace(app),
            )
        except RuntimeError as exc:
            raise click.ClickException(str(exc)) from exc
        dispatch_command = [headless_payload.get("command_id") or "headless"]
        app.state.note_backend_command(dispatch_command)
        answer_text = headless_payload.get("answer_text")
        result = subprocess.CompletedProcess(
            dispatch_command,
            0,
            answer_text or "",
            "",
        )
        if headless_payload.get("session_id"):
            app.state.last_headless_session_id = headless_payload["session_id"]
        payload.update(
            {
                "dispatch_command": dispatch_command,
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "headless": headless_payload,
                "command_result": headless_payload.get("result"),
                "session": headless_payload.get("session"),
                "session_id": headless_payload.get("session_id"),
                "request_message_id": headless_payload.get("request_message_id"),
                "answer_text": answer_text,
            }
        )
    elif dispatch_method == "cdp":
        command_uri = app.backend.build_side_chat_command_uri(
            new_chat=effective_new_chat
        )
        workspace = current_workspace(app)
        bridge_wait_seconds = max(1.0, min(answer_seconds, 8.0))
        title_hints: list[str] = []
        for item in [*workspace_title_hints(app), *(parse_csv_option(cdp_title_contains) or [])]:
            if item and item not in title_hints:
                title_hints.append(item)
        binding = app.backend.read_workspace_cdp_binding(workspace=workspace)
        binding_target_id = str((binding or {}).get("target_id") or "").strip()
        bound_target_id = str(app.state.current_cdp_target_id or "").strip()
        if not bound_target_id and binding:
            bound_target_id = binding_target_id
        bridge_state = app.backend.read_bridge_state(workspace=workspace)
        workspace_open_result = None
        requested_url_contains = parse_csv_option(cdp_url_contains)

        def ensure_workspace_bridge_bound() -> None:
            nonlocal bridge_state, workspace_open_result
            if bridge_state is not None:
                return
            workspace_open_result = app.backend.open_workspace_via_cli(workspace)
            bridge_state = app.backend.wait_for_bridge_state(
                workspace=workspace,
                wait_seconds=bridge_wait_seconds,
            )
            if bridge_state is None:
                raise click.ClickException(
                    "Opened the current workspace in Trae, but that project is not "
                    "bound yet. Wait for the project window to finish loading, then retry."
                )

        if not bound_target_id:
            ensure_workspace_bridge_bound()
        bridge_open_result = None
        open_result = None
        try:
            bridge_open_result = app.backend.invoke_bridge_vscode_api(
                "commands.executeCommand",
                args=[
                    "workbench.action.chat.icube.open",
                    {
                        "keepOpen": True,
                        **({"newChat": True} if effective_new_chat else {}),
                    },
                ],
                workspace=workspace,
            )
            dispatch_command = [
                "bridge:vscode-api",
                "commands.executeCommand",
                "workbench.action.chat.icube.open",
            ]
        except RuntimeError:
            open_result = app.backend.open_url_via_cli(
                command_uri,
                cwd=workspace,
            )
            dispatch_command = app.backend.cli_open_url_command(command_uri)
        app.state.note_backend_command(dispatch_command)

        def invoke_cdp_with_target(target_id: Optional[str]) -> dict[str, Any]:
            return app.backend.invoke_cdp_chat(
                prompt=prompt,
                host=cdp_host,
                port=cdp_port,
                target_id=target_id or None,
                prefer_focused=not bool(target_id),
                title_contains=title_hints,
                url_contains=requested_url_contains,
                timeout_ms=max(1, int(answer_seconds * 1000)),
                launch_if_needed=effective_launch_debug,
            )

        try:
            cdp_payload = invoke_cdp_with_target(bound_target_id)
        except CDPBridgeError as exc:
            if exc.code == "CDP_BOUND_TARGET_NOT_FOUND" and bound_target_id:
                if binding_target_id and binding_target_id == bound_target_id:
                    app.backend.clear_workspace_cdp_binding(workspace=workspace)
                app.state.current_cdp_target_id = None
                bound_target_id = ""
                bridge_state = app.backend.read_bridge_state(workspace=workspace)
                ensure_workspace_bridge_bound()
                try:
                    cdp_payload = invoke_cdp_with_target(None)
                except CDPBridgeError as retry_exc:
                    raise click.ClickException(str(retry_exc)) from retry_exc
                except RuntimeError as retry_exc:
                    raise click.ClickException(str(retry_exc)) from retry_exc
            else:
                raise click.ClickException(str(exc)) from exc
        except RuntimeError as exc:
            raise click.ClickException(str(exc)) from exc
        binding_payload = app.backend.write_workspace_cdp_binding(
            workspace=workspace,
            target=cdp_payload.get("target") or {},
        )
        if binding_payload:
            app.state.current_cdp_target_id = str(binding_payload.get("target_id") or "") or None
        app.state.last_headless_session_id = None
        answer_text = (
            (cdp_payload.get("response") or {}).get("text")
            if isinstance(cdp_payload.get("response"), dict)
            else None
        )
        result = subprocess.CompletedProcess(
            dispatch_command,
            0,
            answer_text or "",
            (open_result.stderr if open_result is not None else ""),
        )
        payload.update(
            {
                "dispatch_command": dispatch_command,
                "command_uri": command_uri,
                "open_via": "bridge" if bridge_open_result is not None else "cli_open_url",
                "open_exit_code": 0 if bridge_open_result is not None else open_result.returncode,
                "open_stdout": (
                    json.dumps(bridge_open_result, ensure_ascii=False)
                    if bridge_open_result is not None
                    else open_result.stdout
                ),
                "open_stderr": "" if bridge_open_result is not None else open_result.stderr,
                "bridge_open": bridge_open_result,
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "cdp": cdp_payload,
                "cdp_endpoint": cdp_payload.get("endpoint"),
                "dispatch_target": cdp_payload.get("target"),
                "workspace_binding": binding_payload,
                "answer_text": answer_text,
            }
        )
        if workspace_open_result is not None:
            payload.update(
                {
                    "workspace_open_exit_code": workspace_open_result.returncode,
                    "workspace_open_stdout": workspace_open_result.stdout,
                    "workspace_open_stderr": workspace_open_result.stderr,
                }
            )
    else:
        result = app.backend.run_cli(args, cwd=app.state.workspace or None)
        dispatch_command = [str(app.backend.paths.cli_script_path), *args]
        app.state.note_backend_command(dispatch_command)
        payload.update(
            {
                "dispatch_command": dispatch_command,
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )

    if inspect_turn:
        payload["inspection"] = app.backend.wait_for_chat_turn(
        started_after=started_at,
        wait_seconds=wait_seconds,
        exclude_frontend_ids=known_frontend_ids,
    )
    return payload


def persist_noninteractive_chat_prompt(
    app: AppContext,
    *,
    prompt: Optional[str],
    payload: dict[str, Any],
    allow_resume: bool,
) -> None:
    if not prompt:
        return
    existing_record = None
    if allow_resume and app.state.current_cli_session_id:
        existing_record = app.backend.load_cli_session(app.state.current_cli_session_id)
    if existing_record is None and allow_resume:
        existing_record = app.backend.latest_cli_session(
            workspace=current_workspace(app),
            resumable_only=False,
        )
    record = new_cli_session_record(
        app,
        session_id=str(existing_record.get("id")) if existing_record else None,
        existing=existing_record,
        initial_prompt=prompt,
    )
    remember_cli_session_prompt(app, record, prompt=prompt, payload=payload)


def emit_chat_result(app: AppContext, payload: dict[str, Any], *, inspect_turn: bool) -> None:
    emit(
        app,
        data=payload,
        text=(
            format_chat_dispatch(payload)
            if inspect_turn
            else (
                chat_result_text(payload)
                or f"Exit code: {payload.get('exit_code')}"
            )
        ),
    )


@click.group(
    context_settings={"help_option_names": ["-h", "--help"]},
    invoke_without_command=True,
    help="启动交互式 Trae CLI 会话，或执行非交互工具命令。",
    epilog='Examples: `traecli`, `traecli "收到回复我"`, `traecli exec "收到回复我"`, `traecli resume --last`',
)
@click.option("--json", "json_output", is_flag=True, help="输出机器可读的 JSON。")
@click.option(
    "--no-alt-screen",
    is_flag=True,
    help="禁用交互模式的备用屏渲染。",
)
@click.option(
    "--trust-workspace",
    is_flag=True,
    help="将当前工作区写入 Trae GUI 共享的信任列表，跳过首次确认提示。",
)
@click.option(
    "-C",
    "--cd",
    "--workspace",
    "workspace",
    type=click.Path(path_type=Path),
    help="为内置 `trae` 命令指定工作目录。",
)
@click.option(
    "--app-path",
    type=click.Path(path_type=Path),
    help="指定 Trae 桌面应用 bundle 路径，默认优先 Trae CN.app。",
)
@click.option(
    "--support-dir",
    type=click.Path(path_type=Path),
    help="指定 `~/Library/Application Support` 下的 Trae 支持目录。",
)
@click.option(
    "--user-data-dir",
    type=click.Path(path_type=Path),
    help="指定 Trae 用户数据目录，默认 `~/.trae`。",
)
@click.pass_context
def cli(
    click_ctx: click.Context,
    json_output: bool,
    no_alt_screen: bool,
    trust_workspace: bool,
    workspace: Optional[Path],
    app_path: Optional[Path],
    support_dir: Optional[Path],
    user_data_dir: Optional[Path],
) -> None:
    resolved_workspace = (workspace or Path.cwd()).expanduser().resolve()
    backend = TraeBackend(
        app_path=app_path,
        support_dir=support_dir,
        user_data_dir=user_data_dir,
    )
    state = SessionState(
        workspace=str(resolved_workspace),
        app_path=str(backend.paths.app_path),
        support_dir=str(backend.paths.support_dir),
        user_data_dir=str(backend.paths.user_data_dir),
        json_output=json_output,
    )
    click_ctx.obj = AppContext(
        backend=backend,
        state=state,
        json_output=json_output,
        no_alt_screen=no_alt_screen,
        auto_trust_workspace=trust_workspace,
    )
    if click_ctx.invoked_subcommand is None:
        interactive_mode_supported(click_ctx.obj)
    if trust_workspace or command_requires_workspace_trust(click_ctx.invoked_subcommand):
        ensure_workspace_trusted(
            click_ctx.obj,
            resolved_workspace,
            auto_trust=trust_workspace,
        )
    if click_ctx.invoked_subcommand is None:
        run_repl(click_ctx.obj)


@cli.command(
    short_help="检查 Trae 安装、配置、认证和本地运行状态。",
    help="检查 Trae 安装、配置、认证和本地运行状态。",
)
@click.pass_obj
def doctor(app: AppContext) -> None:
    data = app.backend.probe()
    data["runtime"] = {
        "workspace": app.state.workspace or str(Path.cwd().resolve()),
    }
    emit(app, data=data, text=format_doctor(data))


@cli.command(
    short_help="查看或切换 SOLO/IDE 模式。",
    help="查看或切换 SOLO/IDE 模式；不给目标值时输出当前模式。",
)
@click.argument(
    "target",
    required=False,
    type=click.Choice(["solo", "ide"], case_sensitive=False),
)
@click.option(
    "--reload/--no-reload",
    default=True,
    show_default=True,
    help="在通过共享 GUI 状态切换时，额外触发一次窗口 reload 以尽快生效。",
)
@click.pass_obj
def mode(app: AppContext, target: Optional[str], reload: bool) -> None:
    workspace = current_workspace(app)
    if not target:
        data = app.backend.read_solo_mode_state()
        emit(app, data=data, text=format_mode_status(data))
        return
    payload = app.backend.switch_mode(
        target,
        cwd=workspace,
        reload_if_needed=reload,
    )
    emit(app, data=payload, text=format_mode_switch(payload))


@cli.group(
    invoke_without_command=True,
    short_help="查看或切换 Trae 模型。",
    help="查看或切换 Trae 模型；不给子命令时输出当前生效模型。",
)
@click.pass_context
def models(click_ctx: click.Context) -> None:
    if click_ctx.invoked_subcommand is not None:
        return
    app = click_ctx.obj
    assert isinstance(app, AppContext)
    payload = app.backend.current_models()
    emit(app, data=payload, text=format_model_current(payload))


@models.command(
    "current",
    short_help="查看当前生效模型。",
    help="查看当前生效模型；默认展示 dev_builder、solo_coder、solo_builder 三类 agent。",
)
@click.option(
    "--agent-type",
    type=click.Choice(MODEL_AGENT_TYPE_CHOICES, case_sensitive=False),
    help="只查看指定 agent 类型的当前模型。",
)
@click.pass_obj
def models_current(app: AppContext, agent_type: Optional[str]) -> None:
    payload = app.backend.current_models(agent_type=agent_type)
    emit(app, data=payload, text=format_model_current(payload))


@models.command(
    "list",
    short_help="列出可切换模型。",
    help="按 agent 类型列出当前 Trae 暴露的可切换模型。",
)
@click.option(
    "--agent-type",
    type=click.Choice(MODEL_AGENT_TYPE_CHOICES, case_sensitive=False),
    help="只列出指定 agent 类型的可用模型。",
)
@click.pass_obj
def models_list(app: AppContext, agent_type: Optional[str]) -> None:
    payload = app.backend.list_models(agent_type=agent_type)
    emit(app, data=payload, text=format_model_list(payload))


@models.command(
    "set",
    short_help="切换模型。",
    help="更新 Trae 本地共享状态中的模型映射，并按需触发窗口 reload。",
)
@click.argument("model")
@click.option(
    "--agent-type",
    type=click.Choice(MODEL_AGENT_TYPE_CHOICES, case_sensitive=False),
    default="dev_builder",
    show_default=True,
    help="切换哪个 agent 类型的当前模型。",
)
@click.option(
    "--reload/--no-reload",
    default=True,
    show_default=True,
    help="写入共享 GUI 状态后，额外触发一次窗口 reload 以尽快生效。",
)
@click.pass_obj
def models_set(
    app: AppContext,
    model: str,
    agent_type: str,
    reload: bool,
) -> None:
    try:
        payload = app.backend.switch_model(
            model,
            agent_type=agent_type,
            cwd=current_workspace(app),
            reload_if_needed=reload,
        )
    except (RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    emit(app, data=payload, text=format_model_switch(payload))


@cli.group(
    short_help="触发 Trae GUI 原生命令。",
    help="通过 Trae 的 command URI 触发 GUI 原生命令，不依赖 bridge 在线。",
)
def gui() -> None:
    pass


@gui.command(
    "commands",
    short_help="列出已发现的 GUI 命令。",
    help="从 Trae 的 product.json 和主 bundle 中扫描已发现的 GUI 命令。",
)
@click.option("--match", type=str, help="只保留包含该子串的命令。")
@click.option("--limit", type=int, default=80, show_default=True)
@click.pass_obj
def gui_commands(app: AppContext, match: Optional[str], limit: int) -> None:
    payload = app.backend.discover_gui_commands(match=match, limit=limit)
    emit(app, data=payload, text=format_gui_commands(payload))


@gui.command(
    "recent",
    short_help="读取 GUI 最近打开列表。",
    help="直接读取 Trae GUI 共享状态中的最近打开列表，而不是 CLI 自己维护副本。",
)
@click.option(
    "--kind",
    type=click.Choice(["folder", "workspace", "file"], case_sensitive=False),
    help="只保留指定类型的最近打开项。",
)
@click.option("--limit", type=int, default=20, show_default=True)
@click.pass_obj
def gui_recent(app: AppContext, kind: Optional[str], limit: int) -> None:
    payload = app.backend.list_recently_opened(kind=kind, limit=limit)
    emit(app, data=payload, text=format_gui_recent(payload))


@gui.command(
    "open-recent",
    short_help="重新打开 GUI 最近打开项。",
    help="根据 Trae GUI 共享的最近打开列表重新打开指定项；索引来自 `gui recent` 输出。",
)
@click.argument("index", type=click.IntRange(min=1))
@click.pass_obj
def gui_open_recent(app: AppContext, index: int) -> None:
    try:
        payload = app.backend.open_recently_opened(index)
    except (RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    emit(app, data=payload, text=format_gui_invoke(payload))


@gui.command(
    "settings",
    short_help="打开 Trae 设置。",
    help="触发 Trae GUI 设置页。",
)
@click.pass_obj
def gui_settings(app: AppContext) -> None:
    payload = app.backend.invoke_command_uri(
        "workbench.action.icube.openSettings",
        cwd=current_workspace(app),
    )
    emit(app, data=payload, text=format_gui_invoke(payload))


@gui.command(
    "about",
    short_help="打开 About 对话框。",
    help="触发 Trae GUI 的 About 对话框。",
)
@click.pass_obj
def gui_about(app: AppContext) -> None:
    payload = app.backend.invoke_command_uri(
        "workbench.action.showAboutDialog",
        cwd=current_workspace(app),
    )
    emit(app, data=payload, text=format_gui_invoke(payload))


@gui.command(
    "check-update",
    short_help="检查更新。",
    help="触发 Trae GUI 的更新检查。",
)
@click.pass_obj
def gui_check_update(app: AppContext) -> None:
    payload = app.backend.invoke_command_uri(
        "update.checkForUpdate",
        cwd=current_workspace(app),
    )
    emit(app, data=payload, text=format_gui_invoke(payload))


@gui.command(
    "solo-guide",
    short_help="打开 SOLO 引导。",
    help="触发 Trae GUI 的 SOLO 引导入口。",
)
@click.pass_obj
def gui_solo_guide(app: AppContext) -> None:
    payload = app.backend.invoke_command_uri(
        "trae.solo.guide.tryShowSoloGuide",
        cwd=current_workspace(app),
    )
    emit(app, data=payload, text=format_gui_invoke(payload))


@gui.command(
    "solo-builder",
    short_help="触发 SOLO Builder 入口。",
    help="触发 Trae GUI 的 SOLO Builder 入口；是否可见仍由 GUI 自身模式和权限控制。",
)
@click.pass_obj
def gui_solo_builder(app: AppContext) -> None:
    payload = app.backend.invoke_command_uri(
        "soloBuilder",
        cwd=current_workspace(app),
    )
    emit(app, data=payload, text=format_gui_invoke(payload))


@gui.command(
    "new-window",
    short_help="新建窗口。",
    help="触发 Trae GUI 新建窗口。",
)
@click.pass_obj
def gui_new_window(app: AppContext) -> None:
    payload = app.backend.invoke_command_uri(
        "workbench.action.newWindow",
        cwd=current_workspace(app),
    )
    emit(app, data=payload, text=format_gui_invoke(payload))


@gui.command(
    "switch-window",
    short_help="打开窗口切换器。",
    help="触发 Trae GUI 的窗口切换入口。",
)
@click.pass_obj
def gui_switch_window(app: AppContext) -> None:
    payload = app.backend.invoke_command_uri(
        "workbench.action.switchWindow",
        cwd=current_workspace(app),
    )
    emit(app, data=payload, text=format_gui_invoke(payload))


@gui.command(
    "new-file",
    short_help="新建未命名文件。",
    help="触发 Trae GUI 新建未命名文件。",
)
@click.pass_obj
def gui_new_file(app: AppContext) -> None:
    payload = app.backend.invoke_command_uri(
        "workbench.action.files.newUntitledFile",
        cwd=current_workspace(app),
    )
    emit(app, data=payload, text=format_gui_invoke(payload))


@gui.command(
    "open-folder",
    short_help="打开文件夹或文件夹选择器。",
    help="不传路径时触发 Trae GUI 的打开文件夹选择器；传路径时通过 Trae 自身 CLI 让 GUI 直接打开该文件夹。",
)
@click.argument("path", required=False, type=click.Path(path_type=Path))
@click.pass_obj
def gui_open_folder(app: AppContext, path: Optional[Path]) -> None:
    if path is None:
        payload = app.backend.invoke_command_uri(
            "workbench.action.files.openFolder",
            cwd=current_workspace(app),
        )
    else:
        resolved = resolve_existing_path(path, purpose="Folder")
        if not resolved.is_dir():
            raise click.ClickException(f"Folder path must be a directory: {resolved}")
        payload = build_gui_cli_path_payload(
            app,
            command_id="workbench.action.files.openFolder",
            target=resolved,
        )
    emit(app, data=payload, text=format_gui_invoke(payload))


@gui.command(
    "open-file-folder",
    short_help="打开文件、文件夹或选择器。",
    help="不传路径时触发 Trae GUI 的文件/文件夹选择器；传路径时通过 Trae 自身 CLI 让 GUI 直接打开该目标。",
)
@click.argument("path", required=False, type=click.Path(path_type=Path))
@click.pass_obj
def gui_open_file_folder(app: AppContext, path: Optional[Path]) -> None:
    if path is None:
        payload = app.backend.invoke_command_uri(
            "workbench.action.files.openFileFolder",
            cwd=current_workspace(app),
        )
    else:
        resolved = resolve_existing_path(path, purpose="Target")
        if not (resolved.is_file() or resolved.is_dir()):
            raise click.ClickException(
                f"Target path must be a regular file or directory: {resolved}"
            )
        payload = build_gui_cli_path_payload(
            app,
            command_id="workbench.action.files.openFileFolder",
            target=resolved,
        )
    emit(app, data=payload, text=format_gui_invoke(payload))


@gui.command(
    "open-workspace",
    short_help="打开工作区或工作区选择器。",
    help="不传路径时触发 Trae GUI 的工作区选择器；传路径时通过 Trae 自身 CLI 让 GUI 直接打开该目录或 .code-workspace 文件。",
)
@click.argument("path", required=False, type=click.Path(path_type=Path))
@click.pass_obj
def gui_open_workspace(app: AppContext, path: Optional[Path]) -> None:
    if path is None:
        payload = app.backend.invoke_command_uri(
            "workbench.action.openWorkspace",
            cwd=current_workspace(app),
        )
    else:
        resolved = resolve_existing_path(path, purpose="Workspace target")
        if not (
            resolved.is_dir()
            or (resolved.is_file() and resolved.suffix == ".code-workspace")
        ):
            raise click.ClickException(
                "Workspace target must be a directory or a .code-workspace file: "
                f"{resolved}"
            )
        payload = build_gui_cli_path_payload(
            app,
            command_id="workbench.action.openWorkspace",
            target=resolved,
        )
    emit(app, data=payload, text=format_gui_invoke(payload))


@gui.command(
    "clear-recent",
    short_help="清空最近打开列表。",
    help="触发 Trae GUI 清空最近打开列表。",
)
@click.pass_obj
def gui_clear_recent(app: AppContext) -> None:
    payload = app.backend.invoke_command_uri(
        "workbench.action.clearRecentFiles",
        cwd=current_workspace(app),
    )
    emit(app, data=payload, text=format_gui_invoke(payload))


@gui.command(
    "toggle-devtools",
    short_help="切换 DevTools。",
    help="触发 Trae GUI 的 DevTools 切换。",
)
@click.pass_obj
def gui_toggle_devtools(app: AppContext) -> None:
    payload = app.backend.invoke_command_uri(
        "workbench.action.toggleDevTools",
        cwd=current_workspace(app),
    )
    emit(app, data=payload, text=format_gui_invoke(payload))


@gui.command(
    "command",
    short_help="触发任意 GUI 命令 ID。",
    help="通过 command URI 触发任意 Trae GUI 命令 ID。",
)
@click.argument("command_id")
@click.option(
    "--args",
    "args_json",
    type=str,
    help="JSON array passed to the command URI as arguments.",
)
@click.pass_obj
def gui_command(app: AppContext, command_id: str, args_json: Optional[str]) -> None:
    args = parse_json_option(args_json, option_name="--args")
    if args is not None and not isinstance(args, list):
        raise click.BadParameter("--args must decode to a JSON array.")
    payload = app.backend.invoke_command_uri(
        command_id,
        args=args,
        cwd=current_workspace(app),
    )
    emit(app, data=payload, text=format_gui_invoke(payload))


@cli.command(
    short_help="安装 bridge/headless 并写入默认配置。",
    help="安装 bridge/headless 并写入默认配置。",
)
@click.option(
    "--reload/--no-reload",
    default=True,
    show_default=True,
    help="Reload the active Trae window after an in-place bridge or headless patch install.",
)
@click.option(
    "--write-config/--no-write-config",
    default=True,
    show_default=True,
    help="Persist the selected app and support paths to ~/.trae/traecli.json for future commands.",
)
@click.option(
    "--ping/--no-ping",
    default=True,
    show_default=True,
    help="Try to verify that the bridge can see the patched headless command when the selected app stays in place.",
)
@click.option("--timeout-seconds", default=5.0, show_default=True, type=float)
@click.pass_obj
def init(
    app: AppContext,
    reload: bool,
    write_config: bool,
    ping: bool,
    timeout_seconds: float,
) -> None:
    payload: dict[str, Any] = {
        "status": "initialized",
        "source_app_path": str(app.backend.paths.app_path),
        "support_dir": str(app.backend.paths.support_dir),
        "user_data_dir": str(app.backend.paths.user_data_dir),
        "ready": False,
    }
    payload["bridge"] = app.backend.install_bridge_extension()
    writable = app.backend.app_bundle_writable()
    payload["writable"] = writable
    selected_app_path = str(app.backend.paths.app_path)
    verification: dict[str, Any] = {"status": "not-run", "detail": None}

    if writable.get("writable"):
        payload["mode"] = "in-place"
        try:
            headless_payload = app.backend.install_headless_patch()
        except (FileNotFoundError, RuntimeError, OSError) as exc:
            raise click.ClickException(str(exc)) from exc
        headless_payload["app_path"] = str(app.backend.paths.app_path)
        payload["headless"] = headless_payload
        if reload:
            result = app.backend.bridge_reload_window(cwd=app.state.workspace or None)
            payload["reload"] = {
                "command": app.backend.cli_open_url_command(
                    app.backend.bridge_extension_command_uri("workbench.action.reloadWindow")
                ),
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        if ping and reload:
            status_payload = app.backend.headless_status(
                ping=True,
                timeout_seconds=timeout_seconds,
                workspace=current_workspace(app),
            )
            payload["verification_payload"] = status_payload
            if status_payload.get("command_available"):
                verification = {"status": "verified", "detail": "bridge command visible"}
                payload["ready"] = True
            elif status_payload.get("command_error"):
                verification = {
                    "status": "partial",
                    "detail": status_payload["command_error"],
                }
            else:
                verification = {
                    "status": "partial",
                    "detail": "bridge state not active yet; launch or reload Trae and rerun `traecli headless status --ping`",
                }
        else:
            verification = {
                "status": "pending",
                "detail": "reload or ping was skipped; rerun `traecli headless status --ping` after Trae restarts",
            }
    else:
        payload["mode"] = "prepared-copy"
        try:
            prepare_payload = app.backend.prepare_headless_app_copy()
        except (FileNotFoundError, RuntimeError, OSError) as exc:
            raise click.ClickException(str(exc)) from exc
        selected_app_path = str(prepare_payload.get("app_path") or app.backend.paths.app_path)
        payload["headless"] = prepare_payload.get("patch") or {}
        payload["prepared_copy"] = prepare_payload
        verification = {
            "status": "pending",
            "detail": "launch the prepared app copy, then run `traecli headless status --ping`",
        }

    config_payload: dict[str, Any] = {
        "written": False,
        "path": str(app.backend.paths.cli_config_path),
    }
    if write_config:
        config_payload = app.backend.write_cli_config(
            app_path=selected_app_path,
            support_dir=app.backend.paths.support_dir,
            user_data_dir=app.backend.paths.user_data_dir,
        )
    payload["config"] = config_payload
    payload["selected_app_path"] = selected_app_path
    payload["verification"] = verification

    selected_arg = "" if write_config else f"--app-path {shlex.quote(selected_app_path)} "
    verify_command = f"traecli {selected_arg}headless status --ping"
    prompt_command = f"traecli {selected_arg}{shlex.quote('收到回复我')}"
    if payload.get("mode") == "prepared-copy":
        payload["next_steps"] = [
            f"Launch the prepared app copy: open -na {shlex.quote(selected_app_path)}",
            f"Verify the bridge sees the headless command: {verify_command}",
            f"Send a hidden prompt after launch: {prompt_command}",
        ]
    elif payload.get("ready"):
        payload["next_steps"] = [
            f"Send a hidden prompt: {prompt_command}",
        ]
    else:
        payload["next_steps"] = [
            f"Verify the bridge sees the headless command: {verify_command}",
            f"Send a hidden prompt after verification: {prompt_command}",
        ]

    emit(app, data=payload, text=format_init(payload))


@cli.command(
    context_settings={"ignore_unknown_options": True},
    short_help="调用 Trae 内置 CLI 打开文件、目录或定位目标。",
    help="调用 Trae 内置 CLI 打开文件、目录或定位目标。",
)
@click.argument("paths", nargs=-1)
@click.option("--new-window", is_flag=True)
@click.option("--reuse-window", is_flag=True)
@click.option("--wait", is_flag=True)
@click.option("--goto", "goto_target", type=str)
@click.pass_obj
def open(
    app: AppContext,
    paths: tuple[str, ...],
    new_window: bool,
    reuse_window: bool,
    wait: bool,
    goto_target: Optional[str],
) -> None:
    args: list[str] = []
    if new_window:
        args.append("--new-window")
    if reuse_window:
        args.append("--reuse-window")
    if wait:
        args.append("--wait")
    if goto_target:
        args.extend(["--goto", goto_target])
    args.extend(paths)
    result = app.backend.run_cli(args, cwd=app.state.workspace or None)
    app.state.note_backend_command(args)
    emit(
        app,
        data={
            "command": args,
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        },
        text=result.stdout.strip() or f"Exit code: {result.returncode}",
    )


@cli.command(
    short_help="发送聊天请求，可续接当前 workspace 的已保存上下文。",
    help="发送聊天请求，支持 CLI、headless、URI、command 和 CDP 分发；默认会续接当前 workspace 的已保存上下文。",
)
@click.argument("prompt", required=False)
@click.option("--mode", type=click.Choice(["ask", "edit", "agent"]), default="agent")
@click.option("--add-file", "add_files", multiple=True, type=click.Path())
@click.option("--new-window", is_flag=True)
@click.option("--reuse-window", is_flag=True)
@click.option("--maximize", is_flag=True)
@click.option(
    "--dispatch",
    "dispatch_method",
    type=click.Choice(["auto", "cli", "headless", "uri", "command", "cdp"]),
    default="auto",
    show_default=True,
    help="Dispatch through auto selection, bundled `trae chat`, the patched headless bridge path, the side-chat deep link, Trae's hidden command URI path, or CDP DOM automation.",
)
@click.option(
    "--new-chat",
    is_flag=True,
    help="With --dispatch uri/command/cdp, create a fresh side-chat session before sending the prompt.",
)
@click.option(
    "--inspect",
    "inspect_turn",
    is_flag=True,
    help="Inspect the latest local chat turn after dispatching the prompt.",
)
@click.option(
    "--wait-seconds",
    default=0.0,
    show_default=True,
    type=float,
    help="When used with --inspect, poll local logs for up to N seconds.",
)
@click.option(
    "--answer-seconds",
    default=30.0,
    show_default=True,
    type=float,
    help="With --dispatch headless/cdp, wait up to N seconds for assistant text to appear.",
)
@click.option(
    "--cdp-host",
    type=str,
    help="CDP host, defaults to TRAE_CDP_HOST or 127.0.0.1.",
)
@click.option(
    "--cdp-port",
    type=int,
    help="CDP port, defaults to TRAE_REMOTE_DEBUGGING_PORT, the running Trae process arg, or 9222.",
)
@click.option(
    "--cdp-title-contains",
    type=str,
    help="Comma-separated CDP target title filters.",
)
@click.option(
    "--cdp-url-contains",
    type=str,
    help="Comma-separated CDP target URL filters.",
)
@click.option(
    "--launch-debug/--no-launch-debug",
    default=False,
    show_default=True,
    help="With `--dispatch cdp`, try to launch Trae with a remote debugging port if no debugger endpoint is reachable.",
)
@click.pass_obj
def chat(
    app: AppContext,
    prompt: Optional[str],
    mode: str,
    add_files: tuple[str, ...],
    new_window: bool,
    reuse_window: bool,
    maximize: bool,
    dispatch_method: str,
    new_chat: bool,
    inspect_turn: bool,
    wait_seconds: float,
    answer_seconds: float,
    cdp_host: Optional[str],
    cdp_port: Optional[int],
    cdp_title_contains: Optional[str],
    cdp_url_contains: Optional[str],
    launch_debug: bool,
) -> None:
    payload = execute_chat_request(
        app,
        prompt=prompt,
        mode=mode,
        add_files=add_files,
        new_window=new_window,
        reuse_window=reuse_window,
        maximize=maximize,
        requested_dispatch_method=dispatch_method,
        new_chat=new_chat,
        inspect_turn=inspect_turn,
        wait_seconds=wait_seconds,
        answer_seconds=answer_seconds,
        cdp_host=cdp_host,
        cdp_port=cdp_port,
        cdp_title_contains=cdp_title_contains,
        cdp_url_contains=cdp_url_contains,
        launch_debug=launch_debug,
        restore_saved_context=True,
        force_fresh_dispatch=False,
    )
    persist_noninteractive_chat_prompt(
        app,
        prompt=prompt,
        payload=payload,
        allow_resume=not payload.get("new_chat"),
    )
    emit_chat_result(
        app,
        payload,
        inspect_turn=inspect_turn,
    )


@cli.command(
    "exec",
    short_help="执行一次性请求，不恢复也不保存会话。",
    help="执行一次性请求；默认不恢复历史上下文，也不会写入 CLI 会话历史。",
)
@click.argument("prompt")
@click.option("--mode", type=click.Choice(["ask", "edit", "agent"]), default="agent")
@click.option("--add-file", "add_files", multiple=True, type=click.Path())
@click.option("--new-window", is_flag=True)
@click.option("--reuse-window", is_flag=True)
@click.option("--maximize", is_flag=True)
@click.option(
    "--dispatch",
    "dispatch_method",
    type=click.Choice(["auto", "cli", "headless", "uri", "command", "cdp"]),
    default="auto",
    show_default=True,
    help="Dispatch through auto selection, bundled `trae chat`, the patched headless bridge path, the side-chat deep link, Trae's hidden command URI path, or CDP DOM automation.",
)
@click.option(
    "--new-chat",
    is_flag=True,
    help="With --dispatch uri/command/cdp/headless, force a fresh chat before sending the prompt.",
)
@click.option(
    "--inspect",
    "inspect_turn",
    is_flag=True,
    help="Inspect the latest local chat turn after dispatching the prompt.",
)
@click.option(
    "--wait-seconds",
    default=0.0,
    show_default=True,
    type=float,
    help="When used with --inspect, poll local logs for up to N seconds.",
)
@click.option(
    "--answer-seconds",
    default=30.0,
    show_default=True,
    type=float,
    help="With --dispatch headless/cdp, wait up to N seconds for assistant text to appear.",
)
@click.option(
    "--cdp-host",
    type=str,
    help="CDP host, defaults to TRAE_CDP_HOST or 127.0.0.1.",
)
@click.option(
    "--cdp-port",
    type=int,
    help="CDP port, defaults to TRAE_REMOTE_DEBUGGING_PORT, the running Trae process arg, or 9222.",
)
@click.option(
    "--cdp-title-contains",
    type=str,
    help="Comma-separated CDP target title filters.",
)
@click.option(
    "--cdp-url-contains",
    type=str,
    help="Comma-separated CDP target URL filters.",
)
@click.option(
    "--launch-debug/--no-launch-debug",
    default=False,
    show_default=True,
    help="With `--dispatch cdp`, try to launch Trae with a remote debugging port if no debugger endpoint is reachable.",
)
@click.pass_obj
def exec_cmd(
    app: AppContext,
    prompt: str,
    mode: str,
    add_files: tuple[str, ...],
    new_window: bool,
    reuse_window: bool,
    maximize: bool,
    dispatch_method: str,
    new_chat: bool,
    inspect_turn: bool,
    wait_seconds: float,
    answer_seconds: float,
    cdp_host: Optional[str],
    cdp_port: Optional[int],
    cdp_title_contains: Optional[str],
    cdp_url_contains: Optional[str],
    launch_debug: bool,
) -> None:
    payload = execute_chat_request(
        app,
        prompt=prompt,
        mode=mode,
        add_files=add_files,
        new_window=new_window,
        reuse_window=reuse_window,
        maximize=maximize,
        requested_dispatch_method=dispatch_method,
        new_chat=new_chat,
        inspect_turn=inspect_turn,
        wait_seconds=wait_seconds,
        answer_seconds=answer_seconds,
        cdp_host=cdp_host,
        cdp_port=cdp_port,
        cdp_title_contains=cdp_title_contains,
        cdp_url_contains=cdp_url_contains,
        launch_debug=launch_debug,
        restore_saved_context=False,
        force_fresh_dispatch=True,
    )
    emit_chat_result(
        app,
        payload,
        inspect_turn=inspect_turn,
    )


@cli.group(
    short_help="管理 bridge 扩展并调用已注册命令。",
    help="管理 bridge 扩展并调用已注册命令。",
)
def bridge() -> None:
    pass


@bridge.command(
    "install",
    short_help="安装 bridge 扩展。",
    help="安装 bridge 扩展，可选自动重载窗口。",
)
@click.option(
    "--reload/--no-reload",
    default=False,
    show_default=True,
    help="Reload the active Trae window after installing the bridge extension.",
)
@click.pass_obj
def bridge_install(app: AppContext, reload: bool) -> None:
    payload = app.backend.install_bridge_extension()
    if reload:
        result = app.backend.bridge_reload_window(cwd=app.state.workspace or None)
        payload["reload"] = {
            "command": app.backend.cli_open_url_command(
                app.backend.bridge_extension_command_uri("workbench.action.reloadWindow")
            ),
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    else:
        payload["reload_required"] = True
    emit(app, data=payload, text=format_bridge_install(payload))


@bridge.command(
    "status",
    short_help="查看 bridge 安装与连接状态。",
    help="查看 bridge 安装与连接状态。",
)
@click.option(
    "--ping/--no-ping",
    default=False,
    show_default=True,
    help="Probe the running bridge endpoint if its state file exists.",
)
@click.option("--timeout-seconds", default=5.0, show_default=True, type=float)
@click.pass_obj
def bridge_status(app: AppContext, ping: bool, timeout_seconds: float) -> None:
    payload = app.backend.bridge_status(
        ping=ping,
        timeout_seconds=timeout_seconds,
        workspace=current_workspace(app),
    )
    emit(app, data=payload, text=format_bridge_status(payload))


@bridge.command(
    "commands",
    short_help="列出 bridge 可见命令。",
    help="列出 bridge 可见的命令，并支持按关键字过滤。",
)
@click.option("--match", type=str, help="Filter commands by substring.")
@click.option(
    "--internal/--no-internal",
    "include_internal",
    default=True,
    show_default=True,
    help="Include internal command ids returned by vscode.commands.getCommands(true).",
)
@click.option("--timeout-seconds", default=5.0, show_default=True, type=float)
@click.pass_obj
def bridge_commands(
    app: AppContext,
    match: Optional[str],
    include_internal: bool,
    timeout_seconds: float,
) -> None:
    try:
        payload = app.backend.list_bridge_commands(
            match=match,
            include_internal=include_internal,
            timeout_seconds=timeout_seconds,
            workspace=current_workspace(app),
        )
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    emit(app, data=payload, text=format_bridge_commands(payload))


@bridge.command(
    "invoke",
    short_help="通过 bridge 调用指定命令。",
    help="通过 bridge 直接调用指定命令。",
)
@click.argument("command_id")
@click.option(
    "--args-json",
    type=str,
    help="JSON array of executeCommand arguments.",
)
@click.option("--timeout-seconds", default=5.0, show_default=True, type=float)
@click.pass_obj
def bridge_invoke(
    app: AppContext,
    command_id: str,
    args_json: Optional[str],
    timeout_seconds: float,
) -> None:
    args_payload = parse_json_option(
        args_json,
        option_name="--args-json",
        default=[],
    )
    if not isinstance(args_payload, list):
        raise click.BadParameter("--args-json must decode to a JSON array.")
    try:
        payload = app.backend.execute_bridge_command(
            command_id,
            args=args_payload,
            timeout_seconds=timeout_seconds,
            workspace=current_workspace(app),
        )
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    emit(app, data=payload, text=format_bridge_invoke(payload))


@cli.group(
    short_help="管理 headless 补丁和无界面发送能力。",
    help="管理 headless 补丁和无界面发送能力。",
)
def headless() -> None:
    pass


@headless.command(
    "prepare",
    short_help="准备可写的 headless app 副本。",
    help="复制并修补一个可写的 Trae app 副本。",
)
@click.option(
    "--target-app-path",
    type=click.Path(path_type=Path),
    help="Target path for a user-writable Trae desktop app copy.",
)
@click.option(
    "--overwrite/--no-overwrite",
    default=True,
    show_default=True,
    help="Replace the target app copy when it already exists.",
)
@click.pass_obj
def headless_prepare(
    app: AppContext,
    target_app_path: Optional[Path],
    overwrite: bool,
) -> None:
    try:
        payload = app.backend.prepare_headless_app_copy(
            target_app_path=target_app_path,
            overwrite=overwrite,
        )
    except (FileNotFoundError, RuntimeError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    prepared_app_path = str(payload.get("app_path") or "")
    quoted_prepared_app_path = shlex.quote(prepared_app_path)
    payload["next_steps"] = [
        f"Quit the currently running Trae app, then launch the prepared copy: open -na {quoted_prepared_app_path}",
        f"Verify the patched command is visible: traecli --app-path {quoted_prepared_app_path} headless status --ping",
        f"Send a hidden prompt through the copy: traecli --app-path {quoted_prepared_app_path} chat --dispatch headless \"收到回复我\"",
    ]
    emit(app, data=payload, text=format_headless_prepare(payload))


@headless.command(
    "install",
    short_help="安装 headless 补丁。",
    help="在当前 app 上安装 headless 补丁。",
)
@click.option(
    "--reload/--no-reload",
    default=False,
    show_default=True,
    help="Reload the active Trae window after patching the ai chat bundle.",
)
@click.pass_obj
def headless_install(app: AppContext, reload: bool) -> None:
    try:
        payload = app.backend.install_headless_patch()
    except (FileNotFoundError, RuntimeError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    payload["app_path"] = str(app.backend.paths.app_path)
    if reload:
        result = app.backend.bridge_reload_window(cwd=app.state.workspace or None)
        payload["reload"] = {
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    else:
        payload["reload_required"] = True
    emit(app, data=payload, text=format_headless_install(payload))


@headless.command(
    "uninstall",
    short_help="卸载 headless 补丁。",
    help="恢复 headless 补丁修改过的原始文件。",
)
@click.option(
    "--reload/--no-reload",
    default=False,
    show_default=True,
    help="Reload the active Trae window after restoring the original ai chat bundle.",
)
@click.pass_obj
def headless_uninstall(app: AppContext, reload: bool) -> None:
    try:
        payload = app.backend.uninstall_headless_patch()
    except (FileNotFoundError, RuntimeError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    payload["app_path"] = str(app.backend.paths.app_path)
    if reload:
        result = app.backend.bridge_reload_window(cwd=app.state.workspace or None)
        payload["reload"] = {
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    else:
        payload["reload_required"] = True
    emit(app, data=payload, text=format_headless_install(payload))


@headless.command(
    "status",
    short_help="检查 headless 补丁状态。",
    help="检查 headless 补丁和命令可见性。",
)
@click.option(
    "--ping/--no-ping",
    default=False,
    show_default=True,
    help="When the bridge is active, verify that the patched command is registered.",
)
@click.option("--timeout-seconds", default=5.0, show_default=True, type=float)
@click.pass_obj
def headless_status(app: AppContext, ping: bool, timeout_seconds: float) -> None:
    payload = app.backend.headless_status(
        ping=ping,
        timeout_seconds=timeout_seconds,
        workspace=current_workspace(app),
    )
    emit(app, data=payload, text=format_headless_status(payload))


@cli.group(
    short_help="读取 Trae 本地状态与配置摘要。",
    help="读取 Trae 本地状态与配置摘要。",
)
def state() -> None:
    pass


@state.command(
    "auth",
    short_help="查看本地认证信息。",
    help="查看本地认证信息。",
)
@click.pass_obj
def state_auth(app: AppContext) -> None:
    data = app.backend.load_auth_info() or {}
    emit(app, data=data, text=format_auth(data))


@state.command(
    "models",
    short_help="查看当前模型选择和模型映射。",
    help="查看当前模型选择和模型映射。",
)
@click.pass_obj
def state_models(app: AppContext) -> None:
    data = app.backend.read_state_summary()
    payload = {
        "selected_model": data.get("selected_model"),
        "current_models": data.get("current_models"),
        "global_model_map": data.get("global_model_map"),
        "agent_mode": data.get("agent_mode"),
        "available_model_counts": data.get("available_model_counts"),
        "model_cache_counts": app.backend.parse_model_cache_counts(),
    }
    emit(app, data=payload, text=format_models(payload))


@state.command(
    "sessions",
    short_help="查看 Trae 应用本地会话摘要。",
    help="查看 Trae 应用本地会话摘要，不是 CLI 保存的会话历史。",
)
@click.option("--limit", default=20, show_default=True, type=int)
@click.pass_obj
def state_sessions(app: AppContext, limit: int) -> None:
    data = app.backend.list_sessions(limit=limit)
    emit(app, data=data, text=format_sessions(data))


@cli.group(
    short_help="浏览 Trae 本地日志。",
    help="浏览 Trae 本地日志。",
)
def logs() -> None:
    pass


@logs.command(
    "list",
    short_help="列出可用日志文件。",
    help="列出可用日志文件。",
)
@click.pass_obj
def logs_list(app: AppContext) -> None:
    data = app.backend.list_log_files()
    emit(app, data=data, text=format_log_listing(data))


@logs.command(
    "tail",
    short_help="查看最新日志内容。",
    help="查看最新日志内容。",
)
@click.option("--match", default="main.log", show_default=True)
@click.option("--lines", default=40, show_default=True, type=int)
@click.pass_obj
def logs_tail(app: AppContext, match: str, lines: int) -> None:
    data = app.backend.tail_log(match=match, lines=lines)
    emit(app, data=data, text=format_log_tail(data))


@cli.group(
    short_help="查看 sandbox 快照。",
    help="查看 sandbox 快照。",
)
def sandbox() -> None:
    pass


@sandbox.command(
    "list",
    short_help="列出 sandbox 快照。",
    help="列出 sandbox 快照。",
)
@click.option("--limit", default=20, show_default=True, type=int)
@click.pass_obj
def sandbox_list(app: AppContext, limit: int) -> None:
    data = app.backend.list_sandboxes(limit=limit)
    emit(app, data=data, text=format_sandboxes(data))


@sandbox.command(
    "show",
    short_help="查看指定 sandbox 详情。",
    help="查看指定 sandbox 的详细内容。",
)
@click.argument("name")
@click.pass_obj
def sandbox_show(app: AppContext, name: str) -> None:
    data = app.backend.read_sandbox(name)
    emit(app, data=data)


@cli.group(
    short_help="查看或添加 MCP 配置。",
    help="查看或添加 MCP 配置。",
)
def mcp() -> None:
    pass


@mcp.command(
    "gallery",
    short_help="列出 MCP gallery 条目。",
    help="列出 MCP gallery 条目。",
)
@click.pass_obj
def mcp_gallery(app: AppContext) -> None:
    data = app.backend.list_mcp_gallery()
    emit(app, data=data, text=format_mcp(data))


@mcp.command(
    "add",
    short_help="添加一个 MCP 配置负载。",
    help="向 Trae 添加一个 MCP 配置负载。",
)
@click.argument("payload")
@click.pass_obj
def mcp_add(app: AppContext, payload: str) -> None:
    result = app.backend.add_mcp(payload)
    emit(
        app,
        data={
            "payload": payload,
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        },
        text=result.stdout.strip() or f"Exit code: {result.returncode}",
    )


@cli.group(
    short_help="检查内部 RPC 服务、流量和桥接调用。",
    help="检查 Trae 内部 RPC 服务、流量和桥接调用。",
)
def rpc() -> None:
    pass


@rpc.command(
    "services",
    short_help="列出 bundle 中的 RPC 服务。",
    help="列出 bundle 中识别到的 RPC 服务。",
)
@click.pass_obj
def rpc_services(app: AppContext) -> None:
    data = app.backend.list_rpc_services()
    emit(app, data=data, text=format_rpc_services(data))


@rpc.command(
    "methods",
    short_help="列出指定 RPC 服务的方法。",
    help="列出指定 RPC 服务的方法。",
)
@click.argument("service")
@click.pass_obj
def rpc_methods(app: AppContext, service: str) -> None:
    data = app.backend.get_rpc_methods(service)
    emit(app, data=data, text=format_rpc_methods(data))


@rpc.command(
    "activity",
    short_help="汇总最近的 RPC 调用活动。",
    help="汇总最近日志里的 RPC 调用活动。",
)
@click.option("--limit", default=50, show_default=True, type=int)
@click.pass_obj
def rpc_activity(app: AppContext, limit: int) -> None:
    data = app.backend.list_rpc_activity(limit=limit)
    emit(app, data=data, text=format_rpc_activity(data))


@rpc.command(
    "traces",
    short_help="重建 ai-agent RPC 调用轨迹。",
    help="重建 ai-agent RPC 调用轨迹。",
)
@click.option("--service", type=str, help="Filter by ai-agent RPC service.")
@click.option("--method", type=str, help="Filter by ai-agent RPC method.")
@click.option("--limit", default=20, show_default=True, type=int)
@click.pass_obj
def rpc_traces(
    app: AppContext,
    service: Optional[str],
    method: Optional[str],
    limit: int,
) -> None:
    data = app.backend.list_rpc_traces(limit=limit, service=service, method=method)
    emit(app, data=data, text=format_rpc_traces(data))


@rpc.command(
    "context",
    short_help="提取最近 RPC 上下文。",
    help="提取最近 RPC 上下文中的项目、会话和消息。",
)
@click.option("--limit", default=10, show_default=True, type=int)
@click.pass_obj
def rpc_context(app: AppContext, limit: int) -> None:
    data = app.backend.extract_recent_rpc_context(limit=limit)
    emit(app, data=data, text=format_rpc_context(data))


@rpc.command(
    "describe",
    short_help="描述指定 RPC 方法。",
    help="描述指定 RPC 方法的元数据和样例。",
)
@click.argument("service")
@click.argument("method")
@click.pass_obj
def rpc_describe(app: AppContext, service: str, method: str) -> None:
    data = app.backend.describe_rpc_method(service, method)
    emit(app, data=data, text=format_rpc_description(data))


@rpc.command(
    "transport",
    short_help="分析本地 RPC 传输拓扑。",
    help="分析本地 RPC 传输拓扑，并可选实时探测进程。",
)
@click.option(
    "--live",
    "include_live",
    is_flag=True,
    help="Probe current Trae processes with ps/lsof.",
)
@click.pass_obj
def rpc_transport(app: AppContext, include_live: bool) -> None:
    data = app.backend.extract_transport_topology(include_live=include_live)
    emit(app, data=data, text=format_rpc_transport(data))


@rpc.command(
    "request",
    short_help="通过 AHA bridge 直接发起 RPC 请求。",
    help="通过 AHA RPC bridge 直接发起一次请求。",
)
@click.argument("service")
@click.argument("method")
@click.option("--data", "raw_data", type=str, help="Plain string for params.data.")
@click.option("--data-json", type=str, help="JSON value for params.data.")
@click.option("--connect-session-id", default="", help="AHA client connect_session_id.")
@click.option("--session-id", type=str, help="Envelope session_id override.")
@click.option("--channel-id", type=str, help="Envelope channel_id override.")
@click.option(
    "--client-info-json",
    type=str,
    help="JSON object for params.client_info.",
)
@click.option(
    "--user-info-json",
    type=str,
    help="JSON object for params.user_info.",
)
@click.option(
    "--common-params-json",
    type=str,
    help="JSON object for params.common_params.",
)
@click.option(
    "--streamlined-common-params-json",
    type=str,
    help="JSON object for params.streamlined_common_params.",
)
@click.option(
    "--runtime-dir",
    type=click.Path(path_type=Path),
    help="Override the AHA runtime dir instead of auto-detecting it.",
)
@click.option(
    "--service-name",
    default="ai-agent",
    show_default=True,
    help="AHA IPC service name.",
)
@click.option("--timeout-ms", default=10000, show_default=True, type=int)
@click.pass_obj
def rpc_request(
    app: AppContext,
    service: str,
    method: str,
    raw_data: Optional[str],
    data_json: Optional[str],
    connect_session_id: str,
    session_id: Optional[str],
    channel_id: Optional[str],
    client_info_json: Optional[str],
    user_info_json: Optional[str],
    common_params_json: Optional[str],
    streamlined_common_params_json: Optional[str],
    runtime_dir: Optional[Path],
    service_name: str,
    timeout_ms: int,
) -> None:
    if raw_data is not None and data_json is not None:
        raise click.UsageError("Choose either `--data` or `--data-json`, not both.")

    data = (
        raw_data
        if data_json is None
        else parse_json_option(data_json, option_name="--data-json")
    )
    client_info = parse_json_option(
        client_info_json,
        option_name="--client-info-json",
        expect_object=True,
        default={},
    )
    user_info = parse_json_option(
        user_info_json,
        option_name="--user-info-json",
        expect_object=True,
        default={},
    )
    common_params = parse_json_option(
        common_params_json,
        option_name="--common-params-json",
        expect_object=True,
        default={},
    )
    streamlined_common_params = parse_json_option(
        streamlined_common_params_json,
        option_name="--streamlined-common-params-json",
        expect_object=True,
        default={},
    )

    try:
        payload = app.backend.invoke_aha_rpc(
            service=service,
            method=method,
            data=data,
            connect_session_id=connect_session_id,
            client_info=client_info,
            session_id=session_id,
            channel_id=channel_id,
            user_info=user_info,
            common_params=common_params,
            streamlined_common_params=streamlined_common_params,
            runtime_dir=str(runtime_dir) if runtime_dir else None,
            service_name=service_name,
            timeout_ms=timeout_ms,
            workspace=current_workspace(app),
        )
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc

    emit(app, data=payload, text=format_rpc_request(payload))


@rpc.command(
    "chat",
    short_help="通过原始 RPC 发送实验性的无头 chat 请求。",
    help="自动串起 project/create_session/chat/get_messages，直接验证 Trae CN 的原始 RPC 聊天链路。",
)
@click.argument("prompt")
@click.option("--project-id", type=str, help="Reuse an existing Trae project_id.")
@click.option("--session-id", type=str, help="Reuse an existing Trae session_id.")
@click.option(
    "--session-type",
    type=click.Choice(sorted(RPC_CHAT_SESSION_TYPES)),
    default="inline_chat",
    show_default=True,
)
@click.option(
    "--agent-type",
    type=str,
    help="Override params.data.agent_type. Defaults follow the session type mapping used by Trae CN.",
)
@click.option("--connect-session-id", default="", help="AHA client connect_session_id.")
@click.option("--message-id", type=str, help="Override the generated chat message_id.")
@click.option("--model-name", type=str, help="Override the selected model config name.")
@click.option("--scene-location", type=int, help="Override scene_location in params.data.")
@click.option(
    "--client-info-json",
    type=str,
    help="Extra JSON object merged into params.client_info.",
)
@click.option(
    "--chat-data-json",
    type=str,
    help="Extra JSON object merged into the outgoing chat payload.",
)
@click.option(
    "--custom-model-json",
    type=str,
    help="JSON object merged into params.data.custom_model.",
)
@click.option(
    "--runtime-dir",
    type=click.Path(path_type=Path),
    help="Override the AHA runtime dir instead of auto-detecting it.",
)
@click.option("--timeout-ms", default=10000, show_default=True, type=int)
@click.option("--wait-seconds", default=20.0, show_default=True, type=float)
@click.option("--poll-interval", default=1.0, show_default=True, type=float)
@click.option("--page-size", default=20, show_default=True, type=int)
@click.pass_obj
def rpc_chat(
    app: AppContext,
    prompt: str,
    project_id: Optional[str],
    session_id: Optional[str],
    session_type: str,
    agent_type: Optional[str],
    connect_session_id: str,
    message_id: Optional[str],
    model_name: Optional[str],
    scene_location: Optional[int],
    client_info_json: Optional[str],
    chat_data_json: Optional[str],
    custom_model_json: Optional[str],
    runtime_dir: Optional[Path],
    timeout_ms: int,
    wait_seconds: float,
    poll_interval: float,
    page_size: int,
) -> None:
    client_info = parse_json_option(
        client_info_json,
        option_name="--client-info-json",
        expect_object=True,
        default={},
    )
    chat_data = parse_json_option(
        chat_data_json,
        option_name="--chat-data-json",
        expect_object=True,
        default={},
    )
    custom_model = parse_json_option(
        custom_model_json,
        option_name="--custom-model-json",
        expect_object=True,
        default={},
    )

    try:
        payload = app.backend.send_rpc_chat(
            prompt,
            project_id=project_id,
            session_id=session_id,
            session_type=session_type,
            agent_type=agent_type,
            connect_session_id=connect_session_id,
            client_info=client_info,
            message_id=message_id,
            runtime_dir=str(runtime_dir) if runtime_dir else None,
            timeout_ms=timeout_ms,
            workspace=current_workspace(app),
            wait_seconds=wait_seconds,
            poll_interval=poll_interval,
            page_size=page_size,
            model_name=model_name,
            custom_model=custom_model,
            chat_data=chat_data,
            scene_location=scene_location,
        )
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc

    emit(app, data=payload, text=format_rpc_chat(payload))


@rpc.command(
    "export-chat",
    short_help="通过内部 RPC 导出聊天记录。",
    help="调用内部 RPC 导出聊天记录。",
)
@click.argument("session_id")
@click.option(
    "--connect-session-id",
    type=str,
    help="Override the transport connect_session_id. Defaults to the newest chat trace in local logs.",
)
@click.option(
    "--output",
    "export_path",
    type=click.Path(path_type=Path),
    help="Markdown export target path.",
)
@click.option(
    "--header-extra",
    type=str,
    help="Optional Markdown header prefix sent to Trae's export RPC.",
)
@click.option(
    "--runtime-dir",
    type=click.Path(path_type=Path),
    help="Override the AHA runtime dir instead of auto-detecting it.",
)
@click.option("--timeout-ms", default=10000, show_default=True, type=int)
@click.option(
    "--print-content/--no-print-content",
    default=False,
    show_default=True,
    help="Print the exported markdown content in text mode.",
)
@click.pass_obj
def rpc_export_chat(
    app: AppContext,
    session_id: str,
    connect_session_id: Optional[str],
    export_path: Optional[Path],
    header_extra: Optional[str],
    runtime_dir: Optional[Path],
    timeout_ms: int,
    print_content: bool,
) -> None:
    try:
        payload = app.backend.export_chat_session(
            session_id,
            connect_session_id=connect_session_id,
            export_path=export_path,
            header_extra=header_extra,
            runtime_dir=str(runtime_dir) if runtime_dir else None,
            timeout_ms=timeout_ms,
            workspace=current_workspace(app),
        )
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc

    emit(
        app,
        data=payload,
        text=format_export_chat(payload, include_content=print_content),
    )


@cli.group(
    short_help="查看聊天轮次和工具执行轨迹。",
    help="查看聊天轮次和工具执行轨迹。",
)
def trace() -> None:
    pass


@trace.command(
    "chats",
    short_help="列出最近聊天轮次摘要。",
    help="列出最近聊天轮次摘要。",
)
@click.option("--limit", default=10, show_default=True, type=int)
@click.pass_obj
def trace_chats(app: AppContext, limit: int) -> None:
    data = app.backend.extract_chat_turns(limit=limit)
    emit(app, data=data, text=format_trace_turns(data))


@trace.command(
    "show",
    short_help="查看指定目标的详细轨迹。",
    help="查看指定消息、会话或 trace 的详细轨迹。",
)
@click.argument("target")
@click.pass_obj
def trace_show(app: AppContext, target: str) -> None:
    data = app.backend.describe_chat_turn(target)
    emit(app, data=data, text=format_trace_turn(data))


@trace.command(
    "cdp-ws",
    short_help="通过 CDP 抓取渲染层 WebSocket 握手与构造参数。",
    help="通过 CDP 在 Trae 渲染层发送一次 prompt，并抓取 WebSocket URL、protocol 与握手信息。",
)
@click.argument("prompt")
@click.option(
    "--cdp-host",
    type=str,
    help="CDP host, defaults to TRAE_CDP_HOST or 127.0.0.1.",
)
@click.option(
    "--cdp-port",
    type=int,
    help="CDP port, defaults to TRAE_REMOTE_DEBUGGING_PORT, the running Trae process arg, or 9222.",
)
@click.option(
    "--target-id",
    type=str,
    help="Bind to a specific CDP target id instead of auto-selecting.",
)
@click.option(
    "--prefer-focused/--no-prefer-focused",
    default=True,
    show_default=True,
    help="Prefer the focused Trae window when auto-selecting a target.",
)
@click.option(
    "--cdp-title-contains",
    type=str,
    help="Comma-separated CDP target title filters.",
)
@click.option(
    "--cdp-url-contains",
    type=str,
    help="Comma-separated CDP target URL filters.",
)
@click.option("--ready-timeout-ms", default=15000, show_default=True, type=int)
@click.option("--timeout-ms", default=30000, show_default=True, type=int)
@click.option("--response-poll-interval-ms", default=350, show_default=True, type=int)
@click.option("--response-idle-ms", default=1200, show_default=True, type=int)
@click.option("--command-timeout-ms", default=5000, show_default=True, type=int)
@click.option(
    "--reload-before-run/--no-reload-before-run",
    default=False,
    show_default=True,
    help="Register the probe for new documents, reload the workbench, then submit the prompt.",
)
@click.option(
    "--launch/--no-launch",
    "launch_if_needed",
    default=False,
    show_default=True,
    help="Try to launch Trae with a remote debugging port if no debugger endpoint is reachable.",
)
@click.option("--launch-timeout-ms", default=12000, show_default=True, type=int)
@click.pass_obj
def trace_cdp_ws(
    app: AppContext,
    prompt: str,
    cdp_host: Optional[str],
    cdp_port: Optional[int],
    target_id: Optional[str],
    prefer_focused: bool,
    cdp_title_contains: Optional[str],
    cdp_url_contains: Optional[str],
    ready_timeout_ms: int,
    timeout_ms: int,
    response_poll_interval_ms: int,
    response_idle_ms: int,
    command_timeout_ms: int,
    reload_before_run: bool,
    launch_if_needed: bool,
    launch_timeout_ms: int,
) -> None:
    try:
        data = app.backend.invoke_cdp_websocket_probe(
            prompt=prompt,
            host=cdp_host,
            port=cdp_port,
            target_id=target_id,
            prefer_focused=prefer_focused,
            title_contains=parse_csv_option(cdp_title_contains),
            url_contains=parse_csv_option(cdp_url_contains),
            ready_timeout_ms=ready_timeout_ms,
            timeout_ms=timeout_ms,
            response_poll_interval_ms=response_poll_interval_ms,
            response_idle_ms=response_idle_ms,
            command_timeout_ms=command_timeout_ms,
            reload_before_run=reload_before_run,
            launch_if_needed=launch_if_needed,
            launch_timeout_ms=launch_timeout_ms,
        )
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc

    emit(app, data=data, text=format_cdp_websocket_probe(data))


def run_interactive_prompt(
    app: AppContext,
    session_record: dict[str, Any],
    prompt: str,
    view: Optional[InteractiveViewState] = None,
) -> Optional[str]:
    requested_dispatch_method = (
        "headless"
        if isinstance(session_record.get("last_headless_session_id"), str)
        and session_record.get("last_headless_session_id")
        else "auto"
    )
    prepare_interactive_cli_prompt(session_record, prompt=prompt)
    append_cli_session_event(
        session_record,
        status="working",
        headline="Working",
        details=[f"Sending prompt via {requested_dispatch_method} dispatch."],
    )
    save_cli_session_record(app, session_record)
    if view is not None:
        show_chat_view(view, clear_history=True)
        view.notice = "working"
        if should_use_alt_screen(app):
            render_interactive_session(app, session_record, view)
    started_at = time.monotonic()
    try:
        payload = execute_chat_request(
            app,
            prompt=prompt,
            requested_dispatch_method=requested_dispatch_method,
            inspect_turn=True,
            wait_seconds=2.0,
        )
    except (click.ClickException, click.UsageError) as exc:
        elapsed = max(0.0, time.monotonic() - started_at)
        update_pending_working_event(
            session_record,
            status="error",
            headline=f"Failed after {elapsed:.1f}s",
            details=[f"dispatch={requested_dispatch_method}"],
        )
        error_text = f"Error: {exc.format_message()}"
        append_cli_session_message(session_record, role="system", content=error_text)
        save_cli_session_record(app, session_record)
        if view is not None:
            show_chat_view(view, notice="dispatch failed", clear_history=True)
            set_interactive_panel(
                view,
                title="Error",
                body=error_text,
            )
        if not should_use_alt_screen(app):
            click.echo(error_text)
        return error_text
    elapsed = max(0.0, time.monotonic() - started_at)
    update_pending_working_event(
        session_record,
        status="muted",
        headline=f"Completed in {elapsed:.1f}s",
        details=[f"dispatch={payload.get('dispatch_method') or requested_dispatch_method}"],
    )
    session_record["last_dispatch_method"] = payload.get("dispatch_method")
    if app.state.last_headless_session_id:
        session_record["last_headless_session_id"] = app.state.last_headless_session_id
    elif isinstance(payload.get("session_id"), str) and payload.get("session_id"):
        session_record["last_headless_session_id"] = payload["session_id"]
    for event in build_interactive_execution_events(payload):
        append_cli_session_event(
            session_record,
            status=str(event.get("status") or "muted"),
            headline=str(event.get("headline") or ""),
            details=event.get("details") if isinstance(event.get("details"), list) else None,
        )
    result_text = chat_result_text(payload)
    if result_text:
        append_cli_session_message(session_record, role="assistant", content=result_text)
    save_cli_session_record(app, session_record)
    if view is not None:
        show_chat_view(view, clear_history=True)
        dispatch_method = payload.get("dispatch_method") or "unknown"
        set_interactive_panel(
            view,
            title=f"Dispatch: {dispatch_method}",
            body=result_text or f"Exit code: {payload.get('exit_code')}",
            notice=f"completed via {dispatch_method}",
        )
    if not should_use_alt_screen(app):
        emit_chat_result(app, payload, inspect_turn=False)
    return result_text


@cli.command(
    short_help="显式进入交互式 REPL。",
    help="显式进入交互式 REPL，会话可带初始提示词。",
)
@click.argument("prompt", required=False)
@click.pass_obj
def repl(app: AppContext, prompt: Optional[str]) -> None:
    interactive_mode_supported(app)
    run_repl(app, initial_prompt=prompt)


@cli.command(
    short_help="恢复最近或指定的 headless CLI 会话。",
    help="恢复最近或指定的 headless CLI 会话；只有带 hidden session id 的记录才能恢复。",
)
@click.argument("args", nargs=-1)
@click.option(
    "--last",
    is_flag=True,
    help="恢复当前 workspace 最近一个可恢复的 headless CLI 会话。",
)
@click.option(
    "--all",
    "all_workspaces",
    is_flag=True,
    help="跨所有已保存 workspace 搜索，而不是只看当前 workspace。",
)
@click.pass_obj
def resume(
    app: AppContext,
    args: tuple[str, ...],
    last: bool,
    all_workspaces: bool,
) -> None:
    interactive_mode_supported(app)
    alt_screen = should_use_alt_screen(app)
    picker_view = InteractiveViewState(
        notice="Select a saved session to resume."
    )
    picker_record = new_cli_session_record(app)
    session_id: Optional[str] = None
    prompt: Optional[str] = None
    if last:
        if len(args) > 1:
            raise click.UsageError("`resume --last` accepts at most one prompt argument.")
        prompt = args[0] if args else None
    else:
        if args:
            session_id = args[0]
            prompt = " ".join(args[1:]).strip() or None

    record = (
        app.backend.latest_cli_session(
            workspace=None if all_workspaces else app.state.workspace or Path.cwd(),
            resumable_only=True,
        )
        if last
        else (
            pick_cli_session(
                app,
                all_workspaces=all_workspaces,
                alt_screen=alt_screen,
                view=picker_view,
                record=picker_record,
            )
            if session_id is None
            else app.backend.load_cli_session(session_id or "")
        )
    )
    if record is None:
        detail = (
            "No resumable local session was found for the current workspace."
            if last
            else (
                f"CLI session not found: {session_id}"
                if session_id
                else "Resume cancelled."
            )
        )
        raise click.ClickException(f"{detail} Start one with `traecli`, then try again.")

    if not isinstance(record.get("last_headless_session_id"), str) or not record.get(
        "last_headless_session_id"
    ):
        raise click.ClickException(
            "The selected CLI session is not resumable because it has no recorded hidden Trae session id."
        )

    apply_cli_session_record(app, record)
    ensure_workspace_trusted(
        app,
        current_workspace(app),
        auto_trust=app.auto_trust_workspace,
    )
    run_repl(app, initial_prompt=prompt, session_record=record, resumed=True)


@cli.command(
    "sessions",
    short_help="列出或查看已保存的 CLI 会话历史。",
    help="列出或查看已保存的 CLI 会话历史，不是 Trae 应用本地 session 摘要。",
)
@click.option("--limit", default=20, show_default=True, type=int)
@click.option(
    "--all",
    "all_workspaces",
    is_flag=True,
    help="列出所有 workspace 的 CLI 会话历史。",
)
@click.option(
    "--resumable-only",
    is_flag=True,
    help="只显示带 hidden Trae session id 的可恢复会话。",
)
@click.option(
    "--show",
    "show_session_id",
    type=str,
    help="查看指定 CLI 会话历史记录的详情。",
)
@click.pass_obj
def sessions_cmd(
    app: AppContext,
    limit: int,
    all_workspaces: bool,
    resumable_only: bool,
    show_session_id: Optional[str],
) -> None:
    if show_session_id:
        payload = cli_session_detail_payload(app, session_id=show_session_id)
        if not payload.get("found"):
            raise click.ClickException(
                f"CLI session not found: {show_session_id}"
            )
        emit(app, data=payload, text=format_cli_session_detail(payload))
        return
    payload = list_cli_sessions_payload(
        app,
        all_workspaces=all_workspaces,
        resumable_only=resumable_only,
        limit=limit,
    )
    emit(app, data=payload, text=format_cli_sessions(payload))


def process_repl_line(
    app: AppContext,
    record: dict[str, Any],
    view: InteractiveViewState,
    *,
    alt_screen: bool,
    line: str,
) -> bool:
    line = line.strip()
    if not line:
        return False
    if alt_screen and handle_interactive_view_input(app, record, view, line):
        return False
    if line in {"exit", "quit", "/exit", "/quit"}:
        return True
    if line in {"help", "/help"}:
        show_repl_output(
            app,
            record,
            view,
            alt_screen=alt_screen,
            title="Help",
            text=cli.get_help(click.Context(cli)),
            notice="help",
        )
        return False
    if line in {"init", "/init"}:
        payload = bootstrap_workspace_docs(app)
        show_repl_output(
            app,
            record,
            view,
            alt_screen=alt_screen,
            title="Init",
            data=payload,
            text=format_workspace_bootstrap(payload),
            notice="init",
        )
        return False
    if line in {"status", "/status"}:
        payload = collect_repl_status_payload(app, record, view)
        show_repl_output(
            app,
            record,
            view,
            alt_screen=alt_screen,
            title="Status",
            data=payload,
            text=format_repl_status(payload),
            notice="status",
        )
        return False
    if line in {"diff", "/diff"}:
        payload = collect_workspace_diff_payload(app)
        show_repl_output(
            app,
            record,
            view,
            alt_screen=alt_screen,
            title="Diff",
            data=payload,
            text=format_workspace_diff(payload),
            notice="diff",
        )
        return False
    if line in {"prompts", "/prompts"}:
        text = prompt_guide_text(app)
        show_repl_output(
            app,
            record,
            view,
            alt_screen=alt_screen,
            title="Prompts",
            data={
                "workspace": current_workspace(app),
                "body": text,
            },
            text=text,
            notice="prompts",
        )
        return False
    if line in {"model", "/model"}:
        payload = collect_model_picker_payload(app)
        show_repl_output(
            app,
            record,
            view,
            alt_screen=alt_screen,
            title="Model",
            data=payload,
            text=format_model_picker(payload),
            notice="model",
        )
        return False
    if line in {"approval", "/approval", "approvals", "/approvals"}:
        payload = collect_approval_payload(app)
        show_repl_output(
            app,
            record,
            view,
            alt_screen=alt_screen,
            title="Approvals",
            data=payload,
            text=format_approval_payload(payload),
            notice="approvals",
        )
        return False
    if line in {"session", "/session"}:
        show_repl_output(
            app,
            record,
            view,
            alt_screen=alt_screen,
            title="Session State",
            data=app.state.to_dict(),
            notice="session state",
        )
        return False
    if line in {"session detail", "/session detail"}:
        payload = {"session": record, "found": True}
        if alt_screen:
            show_session_detail_view(
                app,
                record,
                view,
                payload=payload,
                title="Current Session Detail",
                notice="current session detail",
                status_body="Viewing the current interactive session.",
            )
        else:
            show_repl_output(
                app,
                record,
                view,
                alt_screen=alt_screen,
                title="Current Session Detail",
                data=payload,
                text=format_cli_session_detail(payload),
                notice="current session detail",
            )
        return False
    if line in {"sessions", "/sessions"}:
        if alt_screen:
            show_saved_sessions_view(
                app,
                record,
                view,
                limit=10,
                all_workspaces=False,
                notice="saved sessions",
            )
        else:
            payload = list_cli_sessions_payload(app, all_workspaces=False, limit=10)
            show_repl_output(
                app,
                record,
                view,
                alt_screen=alt_screen,
                title="Saved Sessions",
                data=payload,
                text=format_cli_sessions(payload),
                notice="saved sessions",
            )
        return False
    if line in {"undo", "/undo"}:
        message = "ok" if app.state.undo() else "nothing to undo"
        app.rebuild_backend()
        save_cli_session_record(app, record)
        show_repl_notice(
            app,
            record,
            view,
            alt_screen=alt_screen,
            message=message,
        )
        return False
    if line in {"redo", "/redo"}:
        message = "ok" if app.state.redo() else "nothing to redo"
        app.rebuild_backend()
        save_cli_session_record(app, record)
        show_repl_notice(
            app,
            record,
            view,
            alt_screen=alt_screen,
            message=message,
        )
        return False

    command_line = line[1:].strip() if line.startswith("/") else line
    parts = shlex.split(command_line)
    if not parts:
        return False
    if parts[0] == "model":
        try:
            implicit_model = next(
                (
                    token
                    for token in parts[1:]
                    if token and not token.startswith("--")
                ),
                None,
            )
            options = parse_repl_model_command_options(
                parts,
                require_model=implicit_model is not None,
                start_index=1,
            )
            if options["model"]:
                payload = app.backend.switch_model(
                    str(options["model"]),
                    agent_type=options["agent_type"] or "dev_builder",
                    cwd=current_workspace(app),
                    reload_if_needed=bool(options["reload"]),
                )
                title = "Model Switch"
                text = format_model_switch(payload)
                notice = "model switch"
            else:
                payload = collect_model_picker_payload(
                    app,
                    agent_type=options["agent_type"],
                )
                title = "Model"
                text = format_model_picker(payload)
                notice = "model"
        except (RuntimeError, ValueError) as exc:
            show_repl_notice(
                app,
                record,
                view,
                alt_screen=alt_screen,
                message=str(exc),
            )
            return False
        show_repl_output(
            app,
            record,
            view,
            alt_screen=alt_screen,
            title=title,
            data=payload,
            text=text,
            notice=notice,
        )
        return False
    if len(parts) >= 3 and parts[:2] == ["set", "workspace"]:
        target_workspace = str(Path(parts[2]).expanduser().resolve())
        try:
            ensure_workspace_trusted(
                app,
                target_workspace,
                auto_trust=app.auto_trust_workspace,
            )
        except click.ClickException as exc:
            show_repl_notice(
                app,
                record,
                view,
                alt_screen=alt_screen,
                message=str(exc),
            )
            return False
        workspace = app.state.set_workspace(target_workspace)
        save_cli_session_record(app, record)
        show_repl_notice(
            app,
            record,
            view,
            alt_screen=alt_screen,
            message=workspace,
        )
        return False
    if len(parts) >= 3 and parts[:2] == ["set", "app"]:
        app.state.set_paths(app_path=parts[2])
        app.rebuild_backend()
        save_cli_session_record(app, record)
        show_repl_notice(
            app,
            record,
            view,
            alt_screen=alt_screen,
            message=str(app.state.app_path),
        )
        return False
    if len(parts) >= 3 and parts[:2] == ["set", "support"]:
        app.state.set_paths(support_dir=parts[2])
        app.rebuild_backend()
        save_cli_session_record(app, record)
        show_repl_notice(
            app,
            record,
            view,
            alt_screen=alt_screen,
            message=str(app.state.support_dir),
        )
        return False
    if len(parts) >= 3 and parts[:2] == ["set", "user-data"]:
        app.state.set_paths(user_data_dir=parts[2])
        app.rebuild_backend()
        save_cli_session_record(app, record)
        show_repl_notice(
            app,
            record,
            view,
            alt_screen=alt_screen,
            message=str(app.state.user_data_dir),
        )
        return False

    if parts == ["doctor"]:
        data = app.backend.probe()
        data["runtime"] = {"workspace": current_workspace(app)}
        show_repl_output(
            app,
            record,
            view,
            alt_screen=alt_screen,
            title="Doctor",
            data=data,
            text=format_doctor(data),
            notice="doctor",
        )
        return False
    if parts == ["state", "auth"]:
        data = app.backend.load_auth_info() or {}
        show_repl_output(
            app,
            record,
            view,
            alt_screen=alt_screen,
            title="Auth",
            data=data,
            text=format_auth(data),
            notice="auth",
        )
        return False
    if parts == ["state", "models"]:
        data = app.backend.read_state_summary()
        payload = {
            "selected_model": data.get("selected_model"),
            "current_models": data.get("current_models"),
            "global_model_map": data.get("global_model_map"),
            "agent_mode": data.get("agent_mode"),
            "available_model_counts": data.get("available_model_counts"),
            "model_cache_counts": app.backend.parse_model_cache_counts(),
        }
        show_repl_output(
            app,
            record,
            view,
            alt_screen=alt_screen,
            title="Models",
            data=payload,
            text=format_models(payload),
            notice="models",
        )
        return False
    if parts == ["models"] or parts[:2] == ["models", "current"]:
        try:
            options = parse_repl_model_command_options(parts)
            payload = app.backend.current_models(agent_type=options["agent_type"])
        except (RuntimeError, ValueError) as exc:
            show_repl_notice(
                app,
                record,
                view,
                alt_screen=alt_screen,
                message=str(exc),
            )
            return False
        show_repl_output(
            app,
            record,
            view,
            alt_screen=alt_screen,
            title="Current Models",
            data=payload,
            text=format_model_current(payload),
            notice="current models",
        )
        return False
    if parts[:2] == ["models", "list"]:
        try:
            options = parse_repl_model_command_options(parts)
            payload = app.backend.list_models(agent_type=options["agent_type"])
        except (RuntimeError, ValueError) as exc:
            show_repl_notice(
                app,
                record,
                view,
                alt_screen=alt_screen,
                message=str(exc),
            )
            return False
        show_repl_output(
            app,
            record,
            view,
            alt_screen=alt_screen,
            title="Available Models",
            data=payload,
            text=format_model_list(payload),
            notice="available models",
        )
        return False
    if parts[:2] == ["models", "set"]:
        try:
            options = parse_repl_model_command_options(parts, require_model=True)
            payload = app.backend.switch_model(
                str(options["model"]),
                agent_type=options["agent_type"] or "dev_builder",
                cwd=current_workspace(app),
                reload_if_needed=bool(options["reload"]),
            )
        except (RuntimeError, ValueError) as exc:
            show_repl_notice(
                app,
                record,
                view,
                alt_screen=alt_screen,
                message=str(exc),
            )
            return False
        show_repl_output(
            app,
            record,
            view,
            alt_screen=alt_screen,
            title="Model Switch",
            data=payload,
            text=format_model_switch(payload),
            notice="model switch",
        )
        return False
    if parts == ["state", "sessions"]:
        data = app.backend.list_sessions()
        show_repl_output(
            app,
            record,
            view,
            alt_screen=alt_screen,
            title="Trae Sessions",
            data=data,
            text=format_sessions(data),
            notice="trae sessions",
        )
        return False
    if parts[:2] == ["sessions", "show"] and len(parts) > 2:
        payload = cli_session_detail_payload(app, session_id=parts[2])
        if not payload.get("found"):
            show_repl_notice(
                app,
                record,
                view,
                alt_screen=alt_screen,
                message=f"CLI session not found: {parts[2]}",
            )
            return False
        if alt_screen:
            show_session_detail_view(
                app,
                record,
                view,
                payload=payload,
                title=f"Session Detail: {parts[2]}",
                notice="session detail",
                status_body="Use /back to return.",
            )
        else:
            show_repl_output(
                app,
                record,
                view,
                alt_screen=alt_screen,
                title=f"Session Detail: {parts[2]}",
                data=payload,
                text=format_cli_session_detail(payload),
                notice="session detail",
            )
        return False
    if parts == ["logs", "list"]:
        data = app.backend.list_log_files()
        show_repl_output(
            app,
            record,
            view,
            alt_screen=alt_screen,
            title="Logs",
            data=data,
            text=format_log_listing(data),
            notice="logs",
        )
        return False
    if parts[:2] == ["logs", "tail"]:
        match = parts[2] if len(parts) > 2 else "main.log"
        data = app.backend.tail_log(match=match, lines=40)
        show_repl_output(
            app,
            record,
            view,
            alt_screen=alt_screen,
            title=f"Log Tail: {match}",
            data=data,
            text=format_log_tail(data),
            notice="log tail",
        )
        return False
    if parts == ["sandbox", "list"]:
        data = app.backend.list_sandboxes()
        show_repl_output(
            app,
            record,
            view,
            alt_screen=alt_screen,
            title="Sandboxes",
            data=data,
            text=format_sandboxes(data),
            notice="sandboxes",
        )
        return False
    if parts[:2] == ["sandbox", "show"] and len(parts) > 2:
        data = app.backend.read_sandbox(parts[2])
        show_repl_output(
            app,
            record,
            view,
            alt_screen=alt_screen,
            title=f"Sandbox: {parts[2]}",
            data=data,
            notice="sandbox detail",
        )
        return False
    if parts == ["mcp", "gallery"]:
        data = app.backend.list_mcp_gallery()
        show_repl_output(
            app,
            record,
            view,
            alt_screen=alt_screen,
            title="MCP Gallery",
            data=data,
            text=format_mcp(data),
            notice="mcp gallery",
        )
        return False
    if parts[:2] == ["mcp", "add"] and len(parts) > 2:
        payload = " ".join(parts[2:])
        result = app.backend.add_mcp(payload)
        show_repl_output(
            app,
            record,
            view,
            alt_screen=alt_screen,
            title="MCP Add",
            data={
                "payload": payload,
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            },
            text=result.stdout.strip() or f"Exit code: {result.returncode}",
            notice="mcp add",
        )
        return False
    if parts == ["rpc", "services"]:
        data = app.backend.list_rpc_services()
        show_repl_output(
            app,
            record,
            view,
            alt_screen=alt_screen,
            title="RPC Services",
            data=data,
            text=format_rpc_services(data),
            notice="rpc services",
        )
        return False
    if parts[:2] == ["rpc", "methods"] and len(parts) > 2:
        data = app.backend.get_rpc_methods(parts[2])
        show_repl_output(
            app,
            record,
            view,
            alt_screen=alt_screen,
            title=f"RPC Methods: {parts[2]}",
            data=data,
            text=format_rpc_methods(data),
            notice="rpc methods",
        )
        return False
    if parts == ["rpc", "activity"]:
        data = app.backend.list_rpc_activity()
        show_repl_output(
            app,
            record,
            view,
            alt_screen=alt_screen,
            title="RPC Activity",
            data=data,
            text=format_rpc_activity(data),
            notice="rpc activity",
        )
        return False
    if parts == ["rpc", "context"]:
        data = app.backend.extract_recent_rpc_context()
        show_repl_output(
            app,
            record,
            view,
            alt_screen=alt_screen,
            title="RPC Context",
            data=data,
            text=format_rpc_context(data),
            notice="rpc context",
        )
        return False
    if parts[:2] == ["rpc", "describe"] and len(parts) > 3:
        data = app.backend.describe_rpc_method(parts[2], parts[3])
        show_repl_output(
            app,
            record,
            view,
            alt_screen=alt_screen,
            title=f"RPC Describe: {parts[2]}.{parts[3]}",
            data=data,
            text=format_rpc_description(data),
            notice="rpc describe",
        )
        return False
    if parts == ["rpc", "transport"]:
        data = app.backend.extract_transport_topology()
        show_repl_output(
            app,
            record,
            view,
            alt_screen=alt_screen,
            title="RPC Transport",
            data=data,
            text=format_rpc_transport(data),
            notice="rpc transport",
        )
        return False
    if parts == ["trace", "chats"]:
        data = app.backend.extract_chat_turns()
        show_repl_output(
            app,
            record,
            view,
            alt_screen=alt_screen,
            title="Trace Chats",
            data=data,
            text=format_trace_turns(data),
            notice="trace chats",
        )
        return False
    if parts[:2] == ["trace", "show"] and len(parts) > 2:
        data = app.backend.describe_chat_turn(parts[2])
        show_repl_output(
            app,
            record,
            view,
            alt_screen=alt_screen,
            title=f"Trace Show: {parts[2]}",
            data=data,
            text=format_trace_turn(data),
            notice="trace show",
        )
        return False
    if parts and parts[0] == "open":
        result = app.backend.run_cli(parts[1:], cwd=app.state.workspace or None)
        app.state.note_backend_command(parts[1:])
        show_repl_output(
            app,
            record,
            view,
            alt_screen=alt_screen,
            title="Open",
            data={
                "command": parts[1:],
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            },
            text=result.stdout.strip() or f"Exit code: {result.returncode}",
            notice="open",
        )
        return False
    if parts and parts[0] == "chat":
        prompt = " ".join(parts[1:]).strip()
        if not prompt:
            show_repl_notice(
                app,
                record,
                view,
                alt_screen=alt_screen,
                message="`chat` requires a prompt.",
            )
            return False
        result_text = run_interactive_prompt(app, record, prompt, view=view)
        if alt_screen:
            if result_text:
                view.notice = session_preview_text(result_text)
            render_interactive_session(app, record, view)
        return False
    if not line.startswith("/"):
        result_text = run_interactive_prompt(app, record, line, view=view)
        if alt_screen:
            if result_text:
                view.notice = session_preview_text(result_text)
            render_interactive_session(app, record, view)
        return False
    show_repl_notice(
        app,
        record,
        view,
        alt_screen=alt_screen,
        message=f"Unknown command: {line}",
    )
    return False


def process_alt_screen_key(
    app: AppContext,
    record: dict[str, Any],
    view: InteractiveViewState,
    key: str,
) -> bool:
    slash_query = active_slash_query(view)
    width, height = terminal_screen_size()
    content_budget = max(10, height - (3 + 3 + 4))
    panel_width = main_panel_width(width)
    visible_lines = main_panel_body_lines(width, content_budget)
    if key == "ESC":
        if view.input_buffer:
            clear_interactive_input(view)
            view.notice = "cleared input"
            render_interactive_session(app, record, view)
            return False
        if view.mode != "chat":
            handle_interactive_view_input(app, record, view, "/back")
        return False
    if key == "ENTER":
        if slash_query is not None:
            submitted, action = activate_selected_slash_command(view)
            if action == "fill":
                render_interactive_session(app, record, view)
                return False
            if submitted:
                clear_interactive_input(view)
                return process_repl_line(
                    app,
                    record,
                    view,
                    alt_screen=True,
                    line=submitted,
                )
        if view.mode == "sessions" and not view.input_buffer:
            handle_interactive_view_input(app, record, view, "o")
            return False
        submitted = view.input_buffer.strip()
        clear_interactive_input(view)
        if not submitted:
            render_interactive_session(app, record, view)
            return False
        return process_repl_line(
            app,
            record,
            view,
            alt_screen=True,
            line=submitted,
        )
    if key == "BACKSPACE":
        backspace_interactive_input(view)
        render_interactive_session(app, record, view)
        return False
    if key == "DELETE":
        delete_interactive_input(view)
        render_interactive_session(app, record, view)
        return False
    if key == "LEFT":
        move_interactive_cursor(view, -1)
        render_interactive_session(app, record, view)
        return False
    if key == "RIGHT":
        move_interactive_cursor(view, 1)
        render_interactive_session(app, record, view)
        return False
    if key == "HOME":
        set_interactive_cursor(view, 0)
        render_interactive_session(app, record, view)
        return False
    if key == "END":
        set_interactive_cursor(view, len(view.input_buffer))
        render_interactive_session(app, record, view)
        return False
    if key == "UP" and slash_query is not None:
        move_slash_palette_index(view, -1)
        render_interactive_session(app, record, view)
        return False
    if key == "DOWN" and slash_query is not None:
        move_slash_palette_index(view, 1)
        render_interactive_session(app, record, view)
        return False
    if key == "UP" and not view.input_buffer and view.mode == "chat":
        move_chat_scroll_offset(
            view,
            record,
            panel_width=panel_width,
            visible_lines=visible_lines,
            delta=1,
        )
        render_interactive_session(app, record, view)
        return False
    if key == "DOWN" and not view.input_buffer and view.mode == "chat":
        move_chat_scroll_offset(
            view,
            record,
            panel_width=panel_width,
            visible_lines=visible_lines,
            delta=-1,
        )
        render_interactive_session(app, record, view)
        return False
    if key == "UP" and not view.input_buffer and view.mode == "sessions":
        handle_interactive_view_input(app, record, view, "k")
        return False
    if key == "DOWN" and not view.input_buffer and view.mode == "sessions":
        handle_interactive_view_input(app, record, view, "j")
        return False
    if len(key) == 1 and key.isprintable():
        if not view.input_buffer:
            command = key.lower()
            if view.mode == "sessions" and command in {"j", "k", "o", "s", "q", "b"}:
                handle_interactive_view_input(app, record, view, command)
                return False
            if view.mode in {"detail", "output"} and command in {"q", "b"}:
                handle_interactive_view_input(app, record, view, command)
                return False
        insert_interactive_input(view, key)
        render_interactive_session(app, record, view)
        return False
    return False


def run_alt_screen_repl(
    app: AppContext,
    record: dict[str, Any],
    view: InteractiveViewState,
) -> None:
    render_interactive_session(app, record, view)
    while True:
        try:
            raw = click.getchar()
        except (EOFError, KeyboardInterrupt):
            click.echo()
            return
        for key in parse_terminal_keypress(raw):
            if process_alt_screen_key(app, record, view, key):
                return


def run_repl(
    app: AppContext,
    *,
    initial_prompt: Optional[str] = None,
    session_record: Optional[dict[str, Any]] = None,
    resumed: bool = False,
) -> None:
    record = new_cli_session_record(
        app,
        session_id=str(session_record.get("id")) if session_record else None,
        existing=session_record,
        initial_prompt=initial_prompt,
    )
    save_cli_session_record(app, record)
    alt_screen = should_use_alt_screen(app)
    view = InteractiveViewState(
        notice=(
            f"resumed hidden session {app.state.last_headless_session_id}"
            if resumed and app.state.last_headless_session_id
            else "interactive session ready"
        )
    )
    with maybe_alt_screen(alt_screen):
        if not alt_screen:
            width, _ = terminal_screen_size()
            for line in startup_banner_lines(width=width):
                click.echo(line, color=True)
            click.echo()
            click.echo(f"workspace: {current_workspace(app)}")
            click.echo(f"session: {record['id']}")
            if resumed and app.state.last_headless_session_id:
                click.echo(f"hidden: {app.state.last_headless_session_id}")
        else:
            render_interactive_session(app, record, view)
        if initial_prompt:
            result_text = run_interactive_prompt(app, record, initial_prompt, view=view)
            if alt_screen:
                if result_text:
                    view.notice = session_preview_text(result_text)
                render_interactive_session(app, record, view)
        if alt_screen:
            run_alt_screen_repl(app, record, view)
            return
        while True:
            try:
                line = input("traecli> ").strip()
            except EOFError:
                click.echo()
                return
            if process_repl_line(
                app,
                record,
                view,
                alt_screen=False,
                line=line,
            ):
                return


def main() -> None:
    argv = rewrite_argv_for_prompt(sys.argv)
    cli.main(
        args=argv[1:],
        prog_name=Path(argv[0]).name if argv else "traecli",
        standalone_mode=True,
    )
