from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
import uuid
import http.client
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, unquote, urlencode, urlparse


MODEL_CACHE_RE = re.compile(
    r"Updated model config cache .* function: (?P<function>[^,]+), configs count: (?P<count>\d+)"
)
SESSION_DIR_RE = re.compile(r"^\d{8}T\d{6}$")
TRANSPORT_REQUEST_RE = re.compile(
    r"\[TransportManager\] executeRequest(?P<success> success)?, "
    r"(?P<service>[a-zA-Z0-9_]+) (?P<method>[a-zA-Z0-9_]+), "
    r"(?P<request_id>[0-9a-f-]+), cost: (?P<cost>\d+)"
)
AHA_IPC_CONNECT_RE = re.compile(
    r"\[AhaRpcClient\] ahaIpc connect serverName: (?P<service>[A-Za-z0-9_-]+)"
)
AHA_IPC_OPTIONS_RE = re.compile(
    r"ai-agent start options .*?(?P<payload>\{.*\})"
)
AI_AGENT_IPC_SERVER_ENABLED_RE = re.compile(r'"ENABLE_IPC_SERVER":"?(?P<value>true|false)', re.I)
AI_AGENT_IPC_SERVICE_RE = re.compile(r'"AHA_IPC_SERVICE_NAME":"(?P<service>[^"]+)"')
DO_REQUEST_DATA_RE = re.compile(r"doRequest, data:\s*(?P<payload>\{.*\})")
DO_REQUEST_RESPONSE_RE = re.compile(r"doRequest, response:\s*(?P<payload>\{.*\})")
OAUTH_PORT_RE = re.compile(
    r"SupabaseOAuthLocalServer#Found available port:\s*(?P<port>\d+)"
)
ELECTRON_AHA_IPC_IMPORT_RE = re.compile(
    r'import\{ahaIpc as (?P<alias>[A-Za-z0-9_]+)\}from"electron";'
)
ELECTRON_AHA_IPC_CONNECT_RE = re.compile(r"\.ahaIpc\.connect\(")
ELECTRON_AHA_IPC_SERVER_CLASS_RE = re.compile(r'super\("(?P<name>ElectronAhaIpcServer)"')
RPC_SERVICE_RE = re.compile(
    r'SERVER_NAME(?:=|:)"(?P<service>[^"]+)"\}'
    r'(?:static\{this\.SERVER_NAME_APPLY="(?P<apply>[^"]+)"\})?'
    r'static\{this\.METHODS=\{(?P<methods>[^}]*)\}',
    re.S,
)
RPC_METHOD_ENTRY_RE = re.compile(r'([A-Za-z0-9_]+):"([^"]+)"')
PROJECT_RESULT_RE = re.compile(r"createProject result: (?P<payload>\{.*\})")
SESSION_SWITCH_RE = re.compile(r"switchToSession .* (?P<payload>\{.*\})")
SESSION_STREAM_RE = re.compile(
    r"chatStream handleStream sessionId: (?P<session_id>[A-Za-z0-9-]+)"
)
PROCESS_IPC_REQUEST_RE = re.compile(
    r"process_ipc_request called!.*?channel_id:\s*(?P<channel_id>[A-Za-z0-9-]+).*?"
    r'(?:trace_id:\s*(?P<trace_id>[A-Za-z0-9-]+).*?)?'
    r'service:\s*"(?P<service>[^"]+)".*?method:\s*"(?P<method>[^"]+)"'
)
PROCESS_IPC_ROUTE_RE = re.compile(
    r'route:\s*service:"(?P<service>[^"]+)",\s*method:"(?P<method>[^"]+)",\s*'
    r'connect_session_id:"(?P<connect_session_id>[^"]*)"\s*trace_id="(?P<trace_id>[A-Za-z0-9-]+)"'
)
FAST_CONVERT_CHAT_MESSAGE_MODELS_RE = re.compile(
    r"fast_convert_chat_message_models: count=(?P<count>\d+), actual=(?P<actual>\d+).*?"
    r'trace_id="(?P<trace_id>[A-Za-z0-9-]+)"'
)
CHAT_SERVICE_MESSAGES_WITH_TRACE_RE = re.compile(
    r"\[ChatService\] get messages (?P<count>\d+).*?trace_id=\"(?P<trace_id>[A-Za-z0-9-]+)\""
)
CHAT_SERVICE_TURNS_WITH_TRACE_RE = re.compile(
    r"\[ChatService\] get turns (?P<count>\d+).*?trace_id=\"(?P<trace_id>[A-Za-z0-9-]+)\""
)
SERVER_HISTORY_BUILD_START_RE = re.compile(
    r"\[build_server_history_ids_cache\] START: session_id=(?P<session_id>[A-Za-z0-9-]+)\s+"
    r'trace_id="(?P<trace_id>[A-Za-z0-9-]+)"'
)
PREPARED_SERVER_HISTORY_IDS_RE = re.compile(
    r"Prepared server history ids:\s*(?P<payload>\[.*\])"
)
GET_HISTORY_STATE_BODY_RE = re.compile(
    r"REQUEST BODY: GetHistoryStateRequest \{ history_id_list:\s*(?P<payload>\[.*\]) \}"
)
QUERY_HISTORY_STATE_REQUEST_RE = re.compile(
    r"\[HTTPClient\] request url (?P<url>https://\S+/api/agent/v3/query_history_state)"
)
QUERY_HISTORY_STATE_RESPONSE_HEADERS_RE = re.compile(
    r"\[AhaNetHTTPClient\] url (?P<url>https://\S+/api/agent/v3/query_history_state), "
    r"response_headers:\s*(?P<payload>\{.*\})"
)
GET_HISTORY_STATE_ERROR_RE = re.compile(r"Get history state error:\s*(?P<error>.+)$")
CKG_SERVER_START_RE = re.compile(r"\bserver start at (?P<port>\d+)")
CKG_LOOKUP_ADDR_RE = re.compile(
    r'\[CKGClient\] Lookup CKG Server Addr : "(?P<host>[^:"]+):(?P<port>\d+)"'
)
GRPC_CONTENT_TYPE_RE = re.compile(r"application/grpc", re.I)
AHA_IPC_ADDRESS_RE = re.compile(
    r"\[AhaIpcServer\].*?\[AhaIPC\] \[client\] start, .*? server:\s+(?P<service>[A-Za-z0-9._-]+)\s*, "
    r"ipc address:\s+(?P<address>ipc://\S+)"
)
PS_PROCESS_ROW_RE = re.compile(
    r"^\s*(?P<pid>\d+)\s+(?P<ppid>\d+)\s+(?P<command>.+)$"
)
REPORTER_PROCESS_TYPE_RE = re.compile(
    r"--vscode-crash-reporter-process-type=(?P<kind>[A-Za-z0-9_-]+)"
)
REMOTE_DEBUGGING_PORT_RE = re.compile(
    r"--remote-debugging-port(?:=|\s+)(?P<port>\d+)\b"
)
PARAMS_EVENT_RE = re.compile(
    r"event:\s+(?P<event>[A-Za-z0-9_]+)\s*;\s*params:\s+(?P<payload>\{.*\})"
)

DEFAULT_RPC_USER_INFO = {
    "name": "",
    "token": "",
    "region": "",
    "is_internal": False,
    "user_id": "",
    "scope": "",
}
AUTH_STORAGE_KEY = "iCubeAuthInfo://icube.cloudide"
RPC_CHAT_SESSION_TYPES = {
    "side_chat",
    "inline_chat",
    "background_chat",
    "proactive_chat",
}
RPC_CHAT_DEFAULT_AGENT_TYPE_BY_SESSION_TYPE = {
    "inline_chat": "inline_chat",
    "side_chat": "chat_v3",
    "background_chat": "builder_v3",
    "proactive_chat": "chat_v3",
}
RPC_CHAT_SCENE_LOCATION_BY_SESSION_TYPE = {
    "inline_chat": 1,
    "side_chat": 2,
}
RPC_CHAT_TERMINAL_STATUSES = {"completed", "failed", "canceled"}
RPC_CHAT_RUNNING_STATUSES = {"running", "pending", "processing", "created"}
HEADLESS_RUNNING_STATUSES = {"running", "pending", "processing", "created", "generating"}
CHAT_REQUEST_OBJECT_RE = re.compile(
    r"createChatRequestObject\([^)]*\)\{(?P<body>.*?)"
    r"return w\.ask_question_config=this\.askQuestionFeatureService\.getAskQuestionConfig\(\),w\}",
    re.S,
)
CHAT_REQUEST_BASE_RE = re.compile(
    r"w=\{(?P<fields>.*?)\};w\.active_text_editor",
    re.S,
)
OBJECT_FIELD_RE = re.compile(r"([a-z_][a-z0-9_]*):")
OBJECT_ASSIGNMENT_RE = re.compile(r"w\.([a-z_][a-z0-9_]*)=")
AI_AGENT_TRACE_CONTEXT_RE = re.compile(
    r'trace_id="(?P<trace_id>[A-Za-z0-9-]+)".*?'
    r"session_id=(?P<session_id>[A-Za-z0-9-]+)"
    r"(?: .*?task_id=(?P<task_id>[A-Za-z0-9-]+))?"
    r"(?: .*?message_id=(?P<message_id>[A-Za-z0-9-]+))?"
)
CHAT_TURN_FINISH_RE = re.compile(
    r"chat_turn_finish session_id:\s*(?P<session_id>[A-Za-z0-9-]+),\s*"
    r'message_id:\s*"(?P<message_id>[A-Za-z0-9-]+)".*?'
    r'trace_id="(?P<trace_id>[A-Za-z0-9-]+)"'
)
CHAT_FINISHED_WITH_ERROR_RE = re.compile(
    r"chat finished with error:\s*(?P<error>.+?)\s+trace_id="
)
TASK_FAILED_ERROR_RE = re.compile(r"task failed:\s*(?P<error>.+?)\s+trace_id=")
PLAN_FINAL_TOKEN_COST_RE = re.compile(r"plan final token cost:\s*(?P<cost>\d+)ms")
TIMESTAMP_PREFIX_RE = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))"
)
FIRST_TOKEN_FLUSHED_RE = re.compile(
    r'first token flushed thought,\s*thought=(?P<thought>"(?:\\.|[^"])*"),\s*'
    r'reasoning=(?P<reasoning>Some\("(?:\\.|[^"])*"\)|None)'
)
TOOLING_TERMINAL_TRACE_RE = re.compile(
    r"\[ToolingTerminalTrace\]toolcall_run_command_tracing (?P<payload>\{.*\})"
)
TOOLING_COMMAND_RESULT_RE = re.compile(
    r"\[tooling\].*?result:\s*exitCode=(?P<exit_code>\S+)\s+commandResult=\s*(?P<payload>\[.*\])$"
)
RENDERER_LOG_NAME_RE = re.compile(r"renderer(?:\.(?P<rotation>\d+))?\.log$")
ANSI_ESCAPE_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])"
)
CARET_OSC_ESCAPE_RE = re.compile(r"\^\[\][^^\r\n]*(?:\^\[\\)")
CARET_CSI_ESCAPE_RE = re.compile(r"\^\[\[[0-?]*[ -/]*[@-~]")
CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
GUI_COMMAND_BUNDLE_RE = re.compile(
    r"\b(?:workbench\.action|update|trae)\.[A-Za-z0-9._-]+\b"
)
EXTENSION_SCANNER_INPUT_RE = re.compile(r"Created extension scanner input for (?P<target>\S+)")
LOCAL_EXTENSION_HOST_STARTED_RE = re.compile(
    r"Started local extension host with pid (?P<pid>\d+)"
)
BRIDGE_OUTPUT_LISTEN_RE = re.compile(r"bridge listening on (?P<host>[^:]+):(?P<port>\d+)")
BRIDGE_OUTPUT_MANAGER_READY_RE = re.compile(
    r"manager exchange ready session=(?P<session>[A-Za-z0-9-]+)"
)

BRIDGE_EXTENSION_SOURCE_DIR_NAME = "bridge_extension"
BRIDGE_EXTENSION_ID = "traecli.headless-bridge"
BRIDGE_EXTENSION_STATE_FILE_NAME = "traecli-headless-bridge.json"
BRIDGE_EXTENSION_STATE_INSTANCES_DIR_NAME = "instances"
BRIDGE_EXTENSION_DEFAULT_TIMEOUT_SECONDS = 5.0
STATE_DB_WRITE_BUSY_TIMEOUT_SECONDS = 1.5
STATE_DB_WRITE_RETRY_ATTEMPTS = 5
STATE_DB_WRITE_RETRY_DELAY_SECONDS = 0.25
HEADLESS_COMMAND_ID = "traecli.headless.send"
HEADLESS_PATCH_BACKUP_SUFFIX = ".traecli-headless.bak"
HEADLESS_PATCH_VERSION = "v9"
HEADLESS_PATCH_MARKER_PREFIX = "traecli-headless-patch:"
HEADLESS_PATCH_MARKER = f"traecli-headless-patch:{HEADLESS_PATCH_VERSION}"
HEADLESS_PATCH_COMMAND_ANCHOR = (
    'i.registerCommand("workbench.action.chat.icube.send.codeReview",async(e,t,r)=>{'
)
HEADLESS_PATCH_MARKED_BLOCK_RE = re.compile(
    r"/\*\s*traecli-headless-patch:[^*]+\*/[\s\S]*?"
    rf"(?={re.escape(HEADLESS_PATCH_COMMAND_ANCHOR)})",
    re.S,
)
HEADLESS_PATCH_COMMAND_RE = re.compile(
    r'i\.registerCommand\("traecli\.headless\.send",async\(e,t,r\)=>\{[\s\S]*?\}\),'
    rf'(?={re.escape(HEADLESS_PATCH_COMMAND_ANCHOR)})',
    re.S,
)
CONTENT_TRUST_STATE_KEY = "content.trust.model.key"
SOLO_MODE_ENABLED_STATE_KEY = "workbench.global.soloMode.enabled"
SOLO_MODE_INFO_STATE_KEY = "workbench.global.soloMode.info"
RECENTLY_OPENED_PATHS_STATE_KEY = "history.recentlyOpenedPathsList"
SOLO_MODE_COMMAND_ID = "soloMode"
RELOAD_WINDOW_COMMAND_ID = "workbench.action.reloadWindow"
MODEL_FRONT_AGENT_TO_FUNCTION = {
    "dev_builder": "builder",
    "solo_coder": "solo_coder",
    "solo_builder": "solo_builder",
}
MODEL_FRONT_AGENT_ALIASES = {
    "builder": "dev_builder",
    "chat": "dev_builder",
    "dev-builder": "dev_builder",
    "dev_builder": "dev_builder",
    "solo-coder": "solo_coder",
    "solo_coder": "solo_coder",
    "solo-builder": "solo_builder",
    "solo_builder": "solo_builder",
    "ui-builder": "solo_builder",
    "ui_builder": "solo_builder",
}


@dataclass
class TraePaths:
    app_path: Path
    support_dir: Path
    user_data_dir: Path

    @property
    def _app_resources_app_dir(self) -> Path:
        if os.name == "nt":
            return self.app_path / "resources" / "app"
        return self.app_path / "Contents" / "Resources" / "app"

    @property
    def info_plist_path(self) -> Path:
        if os.name == "nt":
            return self._app_resources_app_dir / "product.json"
        return self.app_path / "Contents" / "Info.plist"

    @property
    def product_json_path(self) -> Path:
        return self._app_resources_app_dir / "product.json"

    @property
    def electron_main_bundle_path(self) -> Path:
        return self._app_resources_app_dir / "out" / "main.js"

    @property
    def cli_script_path(self) -> Path:
        if os.name == "nt":
            bin_dir = self.app_path / "bin"
            candidates = ["trae-cn.cmd", "trae.cmd", "trae-cn", "trae"] if self.app_path.stem == "Trae CN" else ["trae.cmd", "trae-cn.cmd", "trae", "trae-cn"]
            for name in candidates:
                candidate = bin_dir / name
                if candidate.exists():
                    return candidate
            return bin_dir / candidates[0]
        bin_dir = self.app_path / "Contents" / "Resources" / "app" / "bin"
        candidates = ["trae-cn", "trae"] if self.app_path.stem == "Trae CN" else ["trae", "trae-cn"]
        for name in candidates:
            candidate = bin_dir / name
            if candidate.exists():
                return candidate
        return bin_dir / candidates[0]

    @property
    def global_storage_dir(self) -> Path:
        return self.support_dir / "User" / "globalStorage"

    @property
    def state_db_path(self) -> Path:
        return self.global_storage_dir / "state.vscdb"

    @property
    def storage_json_path(self) -> Path:
        return self.global_storage_dir / "storage.json"

    @property
    def user_extensions_dir(self) -> Path:
        return self.user_data_dir / "extensions"

    @property
    def cli_config_path(self) -> Path:
        return self.user_data_dir / "traecli.json"

    @property
    def cli_sessions_path(self) -> Path:
        return self.user_data_dir / "traecli-sessions.json"

    @property
    def cli_bindings_path(self) -> Path:
        return self.user_data_dir / "traecli-bindings.json"

    @property
    def bridge_runtime_dir(self) -> Path:
        return self.global_storage_dir / BRIDGE_EXTENSION_ID

    @property
    def bridge_state_path(self) -> Path:
        return self.bridge_runtime_dir / BRIDGE_EXTENSION_STATE_FILE_NAME

    @property
    def bridge_state_instances_dir(self) -> Path:
        return self.bridge_runtime_dir / BRIDGE_EXTENSION_STATE_INSTANCES_DIR_NAME

    @property
    def logs_root(self) -> Path:
        return self.support_dir / "logs"

    @property
    def modular_data_dir(self) -> Path:
        return self.support_dir / "ModularData"

    @property
    def mcp_gallery_dir(self) -> Path:
        return self.global_storage_dir / ".mcp_gallery_cache"

    @property
    def ai_chat_bundle_path(self) -> Path:
        base = self._app_resources_app_dir / "node_modules" / "@byted-icube" / "ai-modules-chat" / "dist"
        if os.name == "nt":
            return base / "index.mjs"
        return base / "index.js"

    @property
    def aha_ipc_utils_path(self) -> Path:
        if os.name == "nt":
            ipc_module = "ipc-win32-x64"
        else:
            ipc_module = "ipc-darwin-arm64"
        return (
            self._app_resources_app_dir
            / "node_modules"
            / "@aha-kit"
            / ipc_module
            / "dist"
            / "utils.js"
        )


class CDPBridgeError(RuntimeError):
    def __init__(self, message: str, *, code: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code


class TraeBackend:
    def __init__(
        self,
        *,
        app_path: Optional[Path | str] = None,
        support_dir: Optional[Path | str] = None,
        user_data_dir: Optional[Path | str] = None,
    ) -> None:
        self.paths = self._resolve_paths(app_path, support_dir, user_data_dir)

    def _resolve_paths(
        self,
        app_path: Optional[Path | str],
        support_dir: Optional[Path | str],
        user_data_dir: Optional[Path | str],
    ) -> TraePaths:
        explicit_user_data_dir = self._explicit_user_data_dir(user_data_dir)
        if explicit_user_data_dir is not None:
            resolved_user_data = explicit_user_data_dir
            cli_config = self._read_cli_config_from_path(resolved_user_data / "traecli.json")
            resolved_app = self._resolve_app_path(app_path, cli_config=cli_config)
        else:
            provisional_app = self._resolve_app_path(app_path, cli_config=None)
            resolved_user_data = self._default_user_data_dir(provisional_app)
            cli_config = self._read_cli_config_from_path(resolved_user_data / "traecli.json")
            configured_user_data_dir = self._config_path_value(cli_config, "user_data_dir")
            if configured_user_data_dir is not None:
                resolved_user_data = configured_user_data_dir
                cli_config = self._read_cli_config_from_path(resolved_user_data / "traecli.json")
            resolved_app = self._resolve_app_path(app_path, cli_config=cli_config)
            if configured_user_data_dir is None:
                app_scoped_user_data_dir = self._default_user_data_dir(resolved_app)
                if app_scoped_user_data_dir != resolved_user_data:
                    resolved_user_data = app_scoped_user_data_dir
                    cli_config = self._read_cli_config_from_path(
                        resolved_user_data / "traecli.json"
                    )
                    resolved_app = self._resolve_app_path(app_path, cli_config=cli_config)
        resolved_support = (
            Path(support_dir).expanduser().resolve()
            if support_dir
            else self._default_support_dir(resolved_app, cli_config=cli_config)
        )
        return TraePaths(
            app_path=resolved_app,
            support_dir=resolved_support,
            user_data_dir=resolved_user_data,
        )

    def _resolve_app_path(
        self,
        app_path: Optional[Path | str],
        *,
        cli_config: Optional[dict[str, Any]] = None,
    ) -> Path:
        candidates: list[Path] = []
        if app_path:
            candidates.append(Path(app_path))
        env_app_path = self._env_path("TRAECLI_APP_PATH")
        if env_app_path:
            candidates.append(env_app_path)
        config_app_path = self._config_path_value(cli_config, "app_path")
        if config_app_path:
            candidates.append(config_app_path)
        candidates.extend(self._default_app_candidates())
        for candidate in candidates:
            expanded = candidate.expanduser().resolve()
            if expanded.exists():
                return expanded
        raise FileNotFoundError("Unable to locate a Trae desktop app bundle")

    @staticmethod
    def _default_app_candidates() -> list[Path]:
        if os.name == "nt":
            local_app = os.environ.get("LOCALAPPDATA", "")
            candidates: list[Path] = []
            if local_app:
                candidates.append(Path(local_app) / "Programs" / "Trae CN")
                candidates.append(Path(local_app) / "Programs" / "Trae")
            candidates.append(Path("C:/Program Files/Trae CN"))
            candidates.append(Path("C:/Program Files/Trae"))
            candidates.append(Path("C:/Program Files (x86)/Trae CN"))
            candidates.append(Path("C:/Program Files (x86)/Trae"))
            return candidates
        return [
            Path("~/.trae-cn/app-copies/Trae CN-headless.app").expanduser(),
            Path("/Applications/Trae CN.app"),
            Path("~/.trae/app-copies/Trae-headless.app").expanduser(),
            Path("/Applications/Trae.app"),
        ]

    def _default_support_dir(
        self,
        app_path: Path,
        *,
        cli_config: Optional[dict[str, Any]] = None,
    ) -> Path:
        explicit = self._env_path("TRAECLI_SUPPORT_DIR")
        if explicit:
            return explicit
        config_support = self._config_path_value(cli_config, "support_dir")
        if config_support:
            return config_support
        bundle_name = app_path.stem
        support_name = "Trae CN" if bundle_name.startswith("Trae CN") else "Trae"
        if os.name == "nt":
            local_app = os.environ.get("LOCALAPPDATA", "")
            if local_app:
                return Path(local_app) / support_name
            app_data = os.environ.get("APPDATA", "")
            if app_data:
                return Path(app_data) / support_name
        return (
            Path("~/Library/Application Support").expanduser().resolve() / support_name
        )

    @staticmethod
    def _explicit_user_data_dir(
        user_data_dir: Optional[Path | str] = None,
    ) -> Optional[Path]:
        if user_data_dir:
            return Path(user_data_dir).expanduser().resolve()
        explicit = os.environ.get("TRAECLI_USER_DATA_DIR")
        if explicit:
            return Path(explicit).expanduser().resolve()
        return None

    @classmethod
    def _default_user_data_dir(cls, app_path: Optional[Path | str] = None) -> Path:
        explicit = cls._explicit_user_data_dir()
        if explicit is not None:
            return explicit
        bundle_name = Path(app_path).stem if app_path is not None else ""
        if bundle_name.startswith("Trae CN"):
            return Path("~/.trae-cn").expanduser().resolve()
        return Path("~/.trae").expanduser().resolve()

    @staticmethod
    def _env_path(name: str) -> Optional[Path]:
        raw = os.environ.get(name)
        if not raw:
            return None
        return Path(raw).expanduser().resolve()

    @staticmethod
    def _safe_json_object(raw: str) -> dict[str, Any]:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    @classmethod
    def _read_cli_config_from_path(cls, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            return cls._safe_json_object(path.read_text(encoding="utf-8"))
        except OSError:
            return {}

    @staticmethod
    def _config_path_value(
        cli_config: Optional[dict[str, Any]],
        key: str,
    ) -> Optional[Path]:
        if not isinstance(cli_config, dict):
            return None
        raw = cli_config.get(key)
        if not isinstance(raw, str) or not raw.strip():
            return None
        return Path(raw).expanduser().resolve()

    def load_info_plist(self) -> dict[str, Any]:
        if os.name == "nt":
            return self.load_product_json()
        if not self.paths.info_plist_path.exists():
            return {}
        with self.paths.info_plist_path.open("rb") as handle:
            return plistlib.load(handle)

    def load_product_json(self) -> dict[str, Any]:
        if not self.paths.product_json_path.exists():
            return {}
        return json.loads(self.paths.product_json_path.read_text(encoding="utf-8"))

    @staticmethod
    def _extract_product_command_ids(value: Any) -> list[str]:
        discovered: list[str] = []
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "commandId" and isinstance(item, str) and item.strip():
                    discovered.append(item.strip())
                discovered.extend(TraeBackend._extract_product_command_ids(item))
        elif isinstance(value, list):
            for item in value:
                discovered.extend(TraeBackend._extract_product_command_ids(item))
        return discovered

    def discover_gui_commands(
        self,
        *,
        match: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> dict[str, Any]:
        entries: dict[str, dict[str, Any]] = {}

        def record(command_id: str, source: str) -> None:
            normalized = str(command_id or "").strip()
            if not normalized:
                return
            entry = entries.setdefault(
                normalized,
                {
                    "command_id": normalized,
                    "sources": [],
                },
            )
            if source not in entry["sources"]:
                entry["sources"].append(source)

        product = self.load_product_json()
        for command_id in self._extract_product_command_ids(product):
            record(command_id, "product.json")

        if self.paths.electron_main_bundle_path.exists():
            raw = self.paths.electron_main_bundle_path.read_text(errors="ignore")
            for command_id in GUI_COMMAND_BUNDLE_RE.findall(raw):
                record(command_id, "main.js")

        commands = sorted(entries.values(), key=lambda item: str(item["command_id"]))
        filtered_commands = commands
        if match:
            needle = match.strip().lower()
            filtered_commands = [
                item
                for item in commands
                if needle in str(item["command_id"]).lower()
            ]
        if limit is not None and limit >= 0:
            filtered_commands = filtered_commands[:limit]
        for item in filtered_commands:
            item["sources"] = self._ordered_unique(item.get("sources") or [])
        return {
            "match": match,
            "total_count": len(commands),
            "count": len(filtered_commands),
            "commands": filtered_commands,
            "paths": {
                "product_json": str(self.paths.product_json_path),
                "main_bundle": str(self.paths.electron_main_bundle_path),
            },
        }

    def latest_log_session_dir(self) -> Optional[Path]:
        active = self.active_log_session_dir()
        if active is not None:
            return active
        return self.latest_log_session_dir_by_timestamp()

    def latest_log_session_dir_by_timestamp(self) -> Optional[Path]:
        sessions = self.list_log_session_dirs()
        return sessions[-1] if sessions else None

    def list_log_session_dirs(self) -> list[Path]:
        if not self.paths.logs_root.exists():
            return []
        sessions = [
            path
            for path in self.paths.logs_root.iterdir()
            if path.is_dir() and SESSION_DIR_RE.match(path.name)
        ]
        return sorted(sessions)

    @staticmethod
    def _normalized_path(path: Path | str) -> Path:
        try:
            expanded = Path(path).expanduser()
        except (TypeError, ValueError):
            return Path("/")
        return Path(os.path.realpath(str(expanded)))

    def _extract_log_session_dir_from_open_path(
        self, raw_path: Optional[str]
    ) -> Optional[Path]:
        if not raw_path:
            return None
        try:
            path = Path(raw_path).expanduser()
        except (TypeError, ValueError):
            return None
        normalized_logs_root = self._normalized_path(self.paths.logs_root)
        for candidate in (path, *path.parents):
            if not SESSION_DIR_RE.match(candidate.name):
                continue
            if self._normalized_path(candidate.parent) != normalized_logs_root:
                continue
            try:
                return candidate.resolve()
            except OSError:
                return candidate
        return None

    def _collect_process_open_paths(self, pid: int) -> dict[str, Any]:
        snapshot: dict[str, Any] = {
            "paths": [],
            "errors": [],
        }
        result = self._run_probe_command(["lsof", "-nP", "-a", "-p", str(pid), "-F0fn"])
        error = self._probe_error(result)
        if error:
            snapshot["errors"].append(error)
            return snapshot
        paths: list[str] = []
        for entry in self._parse_lsof_field_output(result.stdout):
            name = entry.get("name")
            if not name or name.startswith("->"):
                continue
            paths.append(name)
        snapshot["paths"] = self._ordered_unique(paths)
        return snapshot

    def active_log_session_dir(self) -> Optional[Path]:
        if not self.paths.logs_root.exists():
            return None
        if not self._command_available("ps") or not self._command_available("lsof"):
            return None

        ps_result = self._run_probe_command(["ps", "-axo", "pid=,ppid=,command="])
        if self._probe_error(ps_result):
            return None

        processes = self._parse_trae_processes(ps_result.stdout)
        if not processes:
            return None

        role_weights = {
            "main-electron": 100,
            "extension-host": 80,
            "ai-helper": 50,
            "ai-server-helper": 40,
            "ckg-helper": 20,
        }
        candidates: dict[Path, dict[str, Any]] = {}
        for process in processes:
            opened = self._collect_process_open_paths(process["pid"])
            if opened["errors"]:
                continue
            for open_path in opened["paths"]:
                session_dir = self._extract_log_session_dir_from_open_path(open_path)
                if session_dir is None:
                    continue
                candidate = candidates.setdefault(
                    session_dir,
                    {
                        "score": 0,
                        "hit_count": 0,
                    },
                )
                candidate["score"] += role_weights.get(process["role"], 0) + 1
                candidate["hit_count"] += 1

        if not candidates:
            return None

        ranked = sorted(
            candidates.items(),
            key=lambda item: (item[1]["score"], item[1]["hit_count"], item[0].name),
            reverse=True,
        )
        return ranked[0][0]

    def main_socket(self) -> Optional[Path]:
        sockets = sorted(self.paths.support_dir.glob("*-main.sock"))
        return sockets[-1] if sockets else None

    def run_cli(
        self,
        args: list[str],
        *,
        cwd: Optional[Path | str] = None,
        capture_output: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        command = [str(self.paths.cli_script_path), *args]
        return subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            text=True,
            capture_output=capture_output,
            check=False,
        )

    def deep_link_scheme(self) -> str:
        product = self.load_product_json()
        scheme = product.get("urlProtocol")
        if isinstance(scheme, str) and scheme.strip():
            return scheme.strip()

        plist = self.load_info_plist()
        url_types = plist.get("CFBundleURLTypes")
        if isinstance(url_types, list):
            for entry in url_types:
                if not isinstance(entry, dict):
                    continue
                schemes = entry.get("CFBundleURLSchemes")
                if not isinstance(schemes, list):
                    continue
                for candidate in schemes:
                    if isinstance(candidate, str) and candidate.strip():
                        return candidate.strip()
        return "trae"

    def deep_link_authority(self) -> str:
        product = self.load_product_json()
        authority = product.get("agentShareLinkAuthority")
        if isinstance(authority, str) and authority.strip():
            return authority.strip()
        return "trae.ai-ide"

    def bundle_identifier(self) -> Optional[str]:
        plist = self.load_info_plist()
        bundle_id = plist.get("CFBundleIdentifier")
        if isinstance(bundle_id, str) and bundle_id.strip():
            return bundle_id.strip()
        return None

    def build_side_chat_deep_link(
        self,
        prompt: str,
        *,
        new_chat: bool = False,
    ) -> str:
        query: dict[str, str] = {"query": prompt}
        if new_chat:
            query["newChat"] = "true"
        encoded = urlencode(query)
        return (
            f"{self.deep_link_scheme()}://{self.deep_link_authority()}"
            f"/side-chat?{encoded}"
        )

    def build_command_uri(
        self,
        command_id: str,
        *,
        args: Optional[list[Any]] = None,
    ) -> str:
        uri = f"command:{command_id}"
        if args is None:
            return uri
        encoded_args = quote(json.dumps(args, separators=(",", ":")))
        return f"{uri}?{encoded_args}"

    def build_side_chat_command_uri(
        self,
        prompt: Optional[str] = None,
        *,
        new_chat: bool = False,
    ) -> str:
        payload: dict[str, Any] = {}
        if prompt is not None:
            payload["query"] = prompt
        payload["keepOpen"] = True
        if new_chat:
            payload["newChat"] = True
        return self.build_command_uri(
            "workbench.action.chat.icube.open",
            args=[payload],
        )

    def open_command(self, target: str) -> list[str]:
        if os.name == "nt":
            exe_name = self.paths.app_path.stem + ".exe"
            exe_path = self.paths.app_path / exe_name
            if "://" in target:
                return ["cmd", "/c", "start", target]
            return [str(exe_path), target]
        if "://" in target:
            bundle_id = self.bundle_identifier()
            if bundle_id:
                return ["open", "-b", bundle_id, "-u", target]
            return ["open", "-u", target]
        return ["open", "-a", str(self.paths.app_path), target]

    def cli_open_url_command(self, target: str) -> list[str]:
        return [str(self.paths.cli_script_path), "--open-url", "--", target]

    def cli_open_uri_command(
        self,
        target: str,
        *,
        kind: str,
    ) -> list[str]:
        flag = "--folder-uri" if kind == "folder" else "--file-uri"
        return [str(self.paths.cli_script_path), flag, target]

    def open_target(
        self,
        target: str,
        *,
        capture_output: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.open_command(target),
            text=True,
            capture_output=capture_output,
            check=False,
        )

    def open_url_via_cli(
        self,
        target: str,
        *,
        cwd: Optional[Path | str] = None,
        capture_output: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return self.run_cli(
            ["--open-url", "--", target],
            cwd=cwd,
            capture_output=capture_output,
        )

    def open_uri_via_cli(
        self,
        target: str,
        *,
        kind: str,
        cwd: Optional[Path | str] = None,
        capture_output: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        flag = "--folder-uri" if kind == "folder" else "--file-uri"
        return self.run_cli(
            [flag, target],
            cwd=cwd,
            capture_output=capture_output,
        )

    def invoke_command_uri(
        self,
        command_id: str,
        *,
        args: Optional[list[Any]] = None,
        cwd: Optional[Path | str] = None,
        capture_output: bool = True,
    ) -> dict[str, Any]:
        command_uri = self.build_command_uri(command_id, args=args)
        result = self.open_url_via_cli(
            command_uri,
            cwd=cwd,
            capture_output=capture_output,
        )
        return {
            "ok": result.returncode == 0,
            "command_id": command_id,
            "command_uri": command_uri,
            "command": self.cli_open_url_command(command_uri),
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }

    def open_workspace_via_cli(
        self,
        workspace: Path | str,
        *,
        capture_output: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return self.open_path_via_cli(
            workspace,
            capture_output=capture_output,
        )

    def open_path_via_cli(
        self,
        target: Path | str,
        *,
        capture_output: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        resolved_target = Path(target).expanduser().resolve()
        cwd = resolved_target if resolved_target.is_dir() else resolved_target.parent
        return self.run_cli(
            [str(resolved_target)],
            cwd=cwd,
            capture_output=capture_output,
        )

    def add_mcp(self, payload: str) -> subprocess.CompletedProcess[str]:
        return self.run_cli(["--add-mcp", payload])

    def load_bridge_extension_manifest(self) -> dict[str, Any]:
        manifest_path = self._bridge_extension_source_dir() / "package.json"
        if not manifest_path.exists():
            raise FileNotFoundError(
                f"Bridge extension manifest not found: {manifest_path}"
            )
        return json.loads(manifest_path.read_text(encoding="utf-8"))

    @staticmethod
    def bridge_extension_id_from_manifest(manifest: dict[str, Any]) -> str:
        publisher = str(manifest.get("publisher") or "").strip()
        name = str(manifest.get("name") or "").strip()
        if not publisher or not name:
            raise RuntimeError("Bridge extension manifest must define publisher and name.")
        return f"{publisher}.{name}"

    @staticmethod
    def bridge_extension_version_from_manifest(manifest: dict[str, Any]) -> str:
        version = str(manifest.get("version") or "").strip()
        if not version:
            raise RuntimeError("Bridge extension manifest must define version.")
        return version

    def bridge_extension_install_dir(
        self, manifest: Optional[dict[str, Any]] = None
    ) -> Path:
        manifest = manifest or self.load_bridge_extension_manifest()
        extension_id = self.bridge_extension_id_from_manifest(manifest)
        version = self.bridge_extension_version_from_manifest(manifest)
        return self.paths.user_extensions_dir / f"{extension_id}-{version}-universal"

    def bridge_extension_command_uri(self, command_id: str, *args: Any) -> str:
        return self.build_command_uri(command_id, args=list(args) if args else None)

    def bridge_reload_window(self, *, cwd: Optional[Path | str] = None) -> subprocess.CompletedProcess[str]:
        return self.open_url_via_cli(
            self.bridge_extension_command_uri("workbench.action.reloadWindow"),
            cwd=cwd,
        )

    @staticmethod
    def _bridge_registry_entry(
        *,
        manifest: dict[str, Any],
        extension_id: str,
        version: str,
        install_dir: Path,
        updated: bool = False,
    ) -> dict[str, Any]:
        publisher = str(manifest.get("publisher") or "").strip() or "unknown"
        extension_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"trae-extension:{extension_id}"))
        publisher_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"trae-publisher:{publisher}"))
        return {
            "identifier": {"id": extension_id, "uuid": extension_uuid},
            "version": version,
            "location": {
                "$mid": 1,
                "path": str(install_dir),
                "scheme": "file",
            },
            "relativeLocation": install_dir.name,
            "metadata": {
                "id": extension_uuid,
                "installedTimestamp": int(time.time() * 1000),
                "pinned": False,
                "source": "resource",
                "publisherId": publisher_uuid,
                "publisherDisplayName": publisher,
                "isApplicationScoped": False,
                "isMachineScoped": False,
                "isBuiltin": False,
                "targetPlatform": "universal",
                "updated": bool(updated),
                "private": False,
                "isPreReleaseVersion": False,
                "hasPreReleaseVersion": False,
                "preRelease": False,
            },
        }

    @staticmethod
    def _render_bridge_vsix_manifest(manifest: dict[str, Any]) -> str:
        publisher = str(manifest.get("publisher") or "").strip()
        name = str(manifest.get("name") or "").strip()
        version = str(manifest.get("version") or "").strip()
        display_name = str(manifest.get("displayName") or name)
        description = str(manifest.get("description") or "")
        engines = manifest.get("engines") if isinstance(manifest.get("engines"), dict) else {}
        vscode_engine = str(engines.get("vscode") or "*")
        extension_kind = manifest.get("extensionKind")
        if isinstance(extension_kind, list):
            extension_kind_value = ",".join(str(item) for item in extension_kind)
        else:
            extension_kind_value = ""
        return (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<PackageManifest Version="2.0.0" '
            'xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">\n'
            "  <Metadata>\n"
            f'    <Identity Language="en-US" Id="{name}" Version="{version}" Publisher="{publisher}" />\n'
            f"    <DisplayName>{display_name}</DisplayName>\n"
            f'    <Description xml:space="preserve">{description}</Description>\n'
            "    <Properties>\n"
            f'      <Property Id="Microsoft.VisualStudio.Code.Engine" Value="{vscode_engine}" />\n'
            f'      <Property Id="Microsoft.VisualStudio.Code.ExtensionKind" Value="{extension_kind_value}" />\n'
            "    </Properties>\n"
            "  </Metadata>\n"
            "  <Installation>\n"
            '    <InstallationTarget Id="Microsoft.VisualStudio.Code" />\n'
            "  </Installation>\n"
            "  <Dependencies />\n"
            "  <Assets>\n"
            '    <Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true" />\n'
            '    <Asset Type="Microsoft.VisualStudio.Services.Content.Details" Path="extension/README.md" Addressable="true" />\n'
            "  </Assets>\n"
            "</PackageManifest>\n"
        )

    def install_bridge_extension(self, *, force: bool = True) -> dict[str, Any]:
        manifest = self.load_bridge_extension_manifest()
        extension_id = self.bridge_extension_id_from_manifest(manifest)
        version = self.bridge_extension_version_from_manifest(manifest)
        source_dir = self._bridge_extension_source_dir()
        install_dir = self.bridge_extension_install_dir(manifest)
        install_root = self.paths.user_extensions_dir
        install_root.mkdir(parents=True, exist_ok=True)

        existing_dir = install_dir if install_dir.exists() else None
        status = "already-installed"
        if force or existing_dir is None:
            stage_dir = install_root / f".{install_dir.name}.tmp-{uuid.uuid4().hex}"
            shutil.copytree(source_dir, stage_dir)
            (stage_dir / ".vsixmanifest").write_text(
                self._render_bridge_vsix_manifest(manifest),
                encoding="utf-8",
            )
            if install_dir.exists():
                shutil.rmtree(install_dir)
                status = "updated"
            else:
                status = "installed"
            stage_dir.replace(install_dir)
        for stale_dir in install_root.glob(f"{extension_id}-*"):
            if stale_dir == install_dir or not stale_dir.is_dir():
                continue
            shutil.rmtree(stale_dir)

        registry_path = install_root / "extensions.json"
        registry: list[dict[str, Any]] = []
        if registry_path.exists():
            try:
                loaded = json.loads(registry_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                loaded = []
            if isinstance(loaded, list):
                registry = loaded
        registry = [
            entry
            for entry in registry
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("identifier"), dict)
                or entry["identifier"].get("id") != extension_id
            )
        ]
        registry.append(
            self._bridge_registry_entry(
                manifest=manifest,
                extension_id=extension_id,
                version=version,
                install_dir=install_dir,
                updated=status == "updated",
            )
        )
        registry_path.write_text(json.dumps(registry, ensure_ascii=False), encoding="utf-8")

        return {
            "status": status,
            "extension_id": extension_id,
            "version": version,
            "source_dir": str(source_dir),
            "install_dir": str(install_dir),
            "registry_path": str(registry_path),
            "installed": install_dir.exists(),
        }

    def read_cli_config(self) -> dict[str, Any]:
        return self._read_cli_config_from_path(self.paths.cli_config_path)

    def write_cli_config(
        self,
        *,
        app_path: Optional[Path | str] = None,
        support_dir: Optional[Path | str] = None,
        user_data_dir: Optional[Path | str] = None,
    ) -> dict[str, Any]:
        config_path = self.paths.cli_config_path
        config_path.parent.mkdir(parents=True, exist_ok=True)
        current = self.read_cli_config()
        current.update(
            {
                "app_path": str(Path(app_path).expanduser().resolve())
                if app_path is not None
                else str(self.paths.app_path),
                "support_dir": str(Path(support_dir).expanduser().resolve())
                if support_dir is not None
                else str(self.paths.support_dir),
                "user_data_dir": str(Path(user_data_dir).expanduser().resolve())
                if user_data_dir is not None
                else str(self.paths.user_data_dir),
            }
        )
        config_path.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
        return {
            "path": str(config_path),
            "config": current,
            "written": True,
        }

    def read_cli_sessions(self) -> list[dict[str, Any]]:
        path = self.paths.cli_sessions_path
        if not path.exists():
            return []
        try:
            raw = self._read_text_with_fallback(path)
            payload = self._safe_json_value(raw)
        except OSError:
            return []
        if not isinstance(payload, list):
            return []
        return [item for item in payload if isinstance(item, dict)]

    def write_cli_sessions(self, sessions: list[dict[str, Any]]) -> dict[str, Any]:
        path = self.paths.cli_sessions_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sessions, indent=2, ensure_ascii=False), encoding="utf-8")
        return {
            "path": str(path),
            "count": len(sessions),
            "written": True,
        }

    def save_cli_session(self, session: dict[str, Any]) -> dict[str, Any]:
        session_id = str(session.get("id") or "").strip()
        if not session_id:
            raise ValueError("CLI session record is missing `id`.")
        sessions = [
            item
            for item in self.read_cli_sessions()
            if str(item.get("id") or "").strip() != session_id
        ]
        sessions.append(session)
        sessions.sort(
            key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
            reverse=True,
        )
        self.write_cli_sessions(sessions)
        return session

    def load_cli_session(self, session_id: str) -> Optional[dict[str, Any]]:
        target = session_id.strip()
        if not target:
            return None
        for item in self.read_cli_sessions():
            if str(item.get("id") or "").strip() == target:
                return item
        return None

    @staticmethod
    def normalize_workspace_path(workspace: Optional[Path | str]) -> Optional[str]:
        if workspace is None:
            return None
        text = str(workspace).strip()
        if not text:
            return None
        return str(Path(text).expanduser().resolve())

    @staticmethod
    def _default_cli_bindings_payload() -> dict[str, Any]:
        return {
            "version": 1,
            "cdp_targets": {},
            "trusted_workspaces": {},
        }

    def read_cli_bindings(self) -> dict[str, Any]:
        path = self.paths.cli_bindings_path
        if not path.exists():
            return self._default_cli_bindings_payload()
        try:
            payload = self._safe_json_value(path.read_text(encoding="utf-8"))
        except OSError:
            return self._default_cli_bindings_payload()
        if not isinstance(payload, dict):
            return self._default_cli_bindings_payload()
        bindings = payload.get("cdp_targets")
        if not isinstance(bindings, dict):
            payload["cdp_targets"] = {}
        trusted = payload.get("trusted_workspaces")
        if not isinstance(trusted, dict):
            payload["trusted_workspaces"] = {}
        if "version" not in payload:
            payload["version"] = 1
        return payload

    def write_cli_bindings(self, payload: dict[str, Any]) -> dict[str, Any]:
        path = self.paths.cli_bindings_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return {
            "path": str(path),
            "written": True,
        }

    def _read_legacy_trusted_workspace(
        self,
        *,
        workspace: Optional[str],
    ) -> Optional[dict[str, Any]]:
        if not workspace:
            return None
        payload = self.read_cli_bindings()
        trusted = payload.get("trusted_workspaces")
        if not isinstance(trusted, dict):
            return None
        record = trusted.get(workspace)
        if not isinstance(record, dict):
            return None
        if record.get("trusted") is False:
            return None
        return record

    def _clear_legacy_trusted_workspace(
        self,
        *,
        workspace: Optional[str],
    ) -> bool:
        if not workspace:
            return False
        payload = self.read_cli_bindings()
        trusted = payload.get("trusted_workspaces")
        if not isinstance(trusted, dict) or workspace not in trusted:
            return False
        del trusted[workspace]
        self.write_cli_bindings(payload)
        return True

    def _read_state_item_value(self, key: str) -> Optional[str]:
        if not key or not self.paths.state_db_path.exists():
            return None
        connection = sqlite3.connect(str(self.paths.state_db_path))
        try:
            cursor = connection.execute(
                "select value from ItemTable where key = ?",
                (key,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            value = row[0]
            return value if isinstance(value, str) else None
        finally:
            connection.close()

    def _write_state_item_value(self, key: str, value: str) -> dict[str, Any]:
        path = self.paths.state_db_path
        path.parent.mkdir(parents=True, exist_ok=True)
        last_error: Optional[sqlite3.OperationalError] = None
        for attempt in range(STATE_DB_WRITE_RETRY_ATTEMPTS):
            connection = sqlite3.connect(
                str(path),
                timeout=STATE_DB_WRITE_BUSY_TIMEOUT_SECONDS,
            )
            try:
                connection.execute(
                    f"pragma busy_timeout = {int(STATE_DB_WRITE_BUSY_TIMEOUT_SECONDS * 1000)}"
                )
                connection.execute(
                    "create table if not exists ItemTable(key text primary key, value text)"
                )
                connection.execute(
                    "insert or replace into ItemTable(key, value) values (?, ?)",
                    (key, value),
                )
                connection.commit()
                break
            except sqlite3.OperationalError as exc:
                last_error = exc
                if "database is locked" not in str(exc).lower():
                    raise RuntimeError(
                        f"Unable to update Trae GUI state database {path}: {exc}"
                    ) from exc
                if attempt + 1 >= STATE_DB_WRITE_RETRY_ATTEMPTS:
                    raise RuntimeError(
                        "Timed out while waiting for Trae to release "
                        f"{path}. Close any conflicting Trae window activity and retry."
                    ) from exc
                time.sleep(STATE_DB_WRITE_RETRY_DELAY_SECONDS)
            finally:
                connection.close()
        else:
            detail = str(last_error) if last_error else "unknown sqlite write failure"
            raise RuntimeError(
                f"Unable to update Trae GUI state database {path}: {detail}"
            )
        return {
            "path": str(path),
            "key": key,
            "written": True,
        }

    @staticmethod
    def _default_content_trust_model() -> dict[str, Any]:
        return {
            "uriTrustInfo": [],
        }

    def read_content_trust_model(self) -> dict[str, Any]:
        raw = self._read_state_item_value(CONTENT_TRUST_STATE_KEY)
        payload = self._maybe_json(raw)
        if not isinstance(payload, dict):
            return self._default_content_trust_model()
        trust_info = payload.get("uriTrustInfo")
        if not isinstance(trust_info, list):
            payload["uriTrustInfo"] = []
        return payload

    def write_content_trust_model(self, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(payload or {})
        trust_info = normalized.get("uriTrustInfo")
        if not isinstance(trust_info, list):
            normalized["uriTrustInfo"] = []
        storage = self._write_state_item_value(
            CONTENT_TRUST_STATE_KEY,
            json.dumps(normalized, ensure_ascii=False),
        )
        storage["value"] = normalized
        return storage

    @staticmethod
    def _default_solo_mode_info() -> dict[str, Any]:
        return {
            "currentSoloTabId": "",
            "isVisibleExtensionView": True,
        }

    @staticmethod
    def _default_recently_opened_paths_list() -> dict[str, Any]:
        return {
            "entries": [],
        }

    def read_recently_opened_paths_list(self) -> dict[str, Any]:
        raw = self._read_state_item_value(RECENTLY_OPENED_PATHS_STATE_KEY)
        payload = self._maybe_json(raw)
        if not isinstance(payload, dict):
            return self._default_recently_opened_paths_list()
        entries = payload.get("entries")
        if not isinstance(entries, list):
            payload["entries"] = []
        return payload

    @staticmethod
    def _local_path_from_file_uri(uri: str) -> Optional[str]:
        parsed = urlparse(uri)
        if parsed.scheme != "file":
            return None
        path = unquote(parsed.path or "")
        if parsed.netloc and parsed.netloc not in {"", "localhost"}:
            path = f"//{parsed.netloc}{path}"
        if not path:
            return None
        return str(Path(path))

    @staticmethod
    def _recently_opened_entry_target(item: dict[str, Any]) -> tuple[str, Optional[str]]:
        folder_uri = item.get("folderUri")
        if isinstance(folder_uri, str) and folder_uri.strip():
            return "folder", folder_uri.strip()
        workspace = item.get("workspace")
        if isinstance(workspace, dict):
            config_path = workspace.get("configPath") or workspace.get("workspaceUri")
            if isinstance(config_path, str) and config_path.strip():
                return "workspace", config_path.strip()
        workspace_uri = item.get("workspaceUri")
        if isinstance(workspace_uri, str) and workspace_uri.strip():
            return "workspace", workspace_uri.strip()
        file_uri = item.get("fileUri")
        if isinstance(file_uri, str) and file_uri.strip():
            return "file", file_uri.strip()
        return "unknown", None

    @classmethod
    def _normalize_recently_opened_entry(
        cls,
        item: Any,
        *,
        index: int,
    ) -> Optional[dict[str, Any]]:
        if not isinstance(item, dict):
            return None
        kind, uri = cls._recently_opened_entry_target(item)
        local_path = cls._local_path_from_file_uri(uri or "") if uri else None
        label = str(item.get("label") or "").strip()
        if not label:
            if local_path:
                label = Path(local_path).name or local_path
            else:
                label = uri or f"entry-{index}"
        exists = Path(local_path).exists() if local_path else None
        return {
            "index": index,
            "kind": kind,
            "label": label,
            "uri": uri,
            "path": local_path,
            "local": local_path is not None,
            "exists": exists,
            "remote_authority": str(item.get("remoteAuthority") or "").strip() or None,
        }

    def list_recently_opened(
        self,
        *,
        kind: Optional[str] = None,
        limit: Optional[int] = 20,
    ) -> dict[str, Any]:
        payload = self.read_recently_opened_paths_list()
        normalized = [
            entry
            for index, item in enumerate(payload.get("entries") or [], start=1)
            for entry in [self._normalize_recently_opened_entry(item, index=index)]
            if entry is not None
        ]
        requested_kind = str(kind or "").strip().lower() or None
        if requested_kind:
            normalized = [
                entry for entry in normalized if entry.get("kind") == requested_kind
            ]
        total_count = len(normalized)
        if limit is not None:
            normalized = normalized[: max(0, int(limit))]
        return {
            "entries": normalized,
            "count": len(normalized),
            "total_count": total_count,
            "kind": requested_kind,
            "source": "state.vscdb" if self.paths.state_db_path.exists() else "default",
            "state_db_path": str(self.paths.state_db_path),
            "state_key": RECENTLY_OPENED_PATHS_STATE_KEY,
        }

    def get_recently_opened_entry(self, index: int) -> dict[str, Any]:
        target_index = int(index)
        if target_index < 1:
            raise ValueError("Recent entry index must be >= 1.")
        payload = self.list_recently_opened(limit=None)
        for entry in payload.get("entries") or []:
            if int(entry.get("index") or 0) == target_index:
                return entry
        raise ValueError(f"Recent GUI entry not found: {target_index}")

    def open_recently_opened(
        self,
        index: int,
        *,
        capture_output: bool = True,
    ) -> dict[str, Any]:
        entry = self.get_recently_opened_entry(index)
        kind = str(entry.get("kind") or "").strip() or "unknown"
        local_path = str(entry.get("path") or "").strip()
        uri = str(entry.get("uri") or "").strip()

        if local_path:
            result = self.open_path_via_cli(
                local_path,
                capture_output=capture_output,
            )
            dispatch = "cli-path"
        elif uri:
            uri_kind = "folder" if kind == "folder" else "file"
            result = self.open_uri_via_cli(
                uri,
                kind=uri_kind,
                capture_output=capture_output,
            )
            dispatch = "cli-uri"
        else:
            raise RuntimeError(
                f"Recent GUI entry {index} does not expose a reopenable path or URI."
            )

        command = result.args
        if isinstance(command, tuple):
            command = list(command)
        elif not isinstance(command, list):
            command = [str(command)]

        return {
            "ok": result.returncode == 0,
            "dispatch": dispatch,
            "command_id": "gui.openRecent",
            "recent_index": int(entry.get("index") or index),
            "kind": kind,
            "label": entry.get("label"),
            "path": local_path or None,
            "uri": uri or None,
            "local": entry.get("local"),
            "exists": entry.get("exists"),
            "remote_authority": entry.get("remote_authority"),
            "command": command,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }

    @staticmethod
    def _normalize_solo_mode_info(value: Any) -> dict[str, Any]:
        normalized = dict(value) if isinstance(value, dict) else {}
        current_solo_tab_id = normalized.get("currentSoloTabId")
        normalized["currentSoloTabId"] = (
            current_solo_tab_id if isinstance(current_solo_tab_id, str) else ""
        )
        visible_extension_view = TraeBackend._parse_boolish(
            normalized.get("isVisibleExtensionView")
        )
        normalized["isVisibleExtensionView"] = (
            visible_extension_view
            if visible_extension_view is not None
            else True
        )
        return normalized

    def read_solo_mode_state(self) -> dict[str, Any]:
        raw_enabled = self._read_state_item_value(SOLO_MODE_ENABLED_STATE_KEY)
        enabled = self._parse_boolish(raw_enabled)
        if enabled is None:
            numeric_enabled = self._parse_optional_int(raw_enabled)
            if numeric_enabled is not None:
                enabled = numeric_enabled != 0
        raw_info = self._read_state_item_value(SOLO_MODE_INFO_STATE_KEY)
        info = self._normalize_solo_mode_info(self._maybe_json(raw_info))
        resolved_enabled = enabled if enabled is not None else False
        return {
            "mode": "solo" if resolved_enabled else "ide",
            "enabled": resolved_enabled,
            "raw_enabled": raw_enabled,
            "info": info,
            "current_solo_tab_id": info.get("currentSoloTabId") or "",
            "is_visible_extension_view": bool(info.get("isVisibleExtensionView")),
            "source": "state.vscdb" if self.paths.state_db_path.exists() else "default",
            "state_db_path": str(self.paths.state_db_path),
            "state_keys": {
                "enabled": SOLO_MODE_ENABLED_STATE_KEY,
                "info": SOLO_MODE_INFO_STATE_KEY,
            },
        }

    def write_solo_mode_state(
        self,
        *,
        enabled: bool,
        source: str = "manual",
    ) -> dict[str, Any]:
        normalized_info = self._normalize_solo_mode_info(
            self.read_solo_mode_state().get("info")
        )
        if not enabled:
            normalized_info["currentSoloTabId"] = ""
        enabled_storage = self._write_state_item_value(
            SOLO_MODE_ENABLED_STATE_KEY,
            "1" if enabled else "0",
        )
        info_storage = self._write_state_item_value(
            SOLO_MODE_INFO_STATE_KEY,
            json.dumps(normalized_info, ensure_ascii=False),
        )
        state = self.read_solo_mode_state()
        return {
            "mode": state["mode"],
            "enabled": state["enabled"],
            "info": state["info"],
            "source": str(source or "manual"),
            "updated_at": datetime.now().astimezone().isoformat(),
            "state_db_path": str(self.paths.state_db_path),
            "state_keys": state["state_keys"],
            "storage": {
                "enabled": enabled_storage,
                "info": info_storage,
            },
        }

    def switch_mode(
        self,
        target_mode: str,
        *,
        cwd: Optional[Path | str] = None,
        reload_if_needed: bool = True,
    ) -> dict[str, Any]:
        target = str(target_mode or "").strip().lower()
        if target not in {"solo", "ide"}:
            raise ValueError("Mode target must be `solo` or `ide`.")

        workspace = self.normalize_workspace_path(cwd) if cwd is not None else None
        before = self.read_solo_mode_state()
        previous_mode = str(before.get("mode") or "ide")
        command_dispatch = None
        storage = None
        reload_dispatch = None
        notes: list[str] = []
        applied_via: list[str] = []

        if target == "solo" and previous_mode != "solo":
            command_dispatch = self.invoke_command_uri(
                SOLO_MODE_COMMAND_ID,
                cwd=workspace,
            )
            if command_dispatch.get("ok"):
                applied_via.append("command-uri")
            else:
                notes.append(
                    "SOLO command dispatch failed; fell back to the shared GUI state database."
                )

        current = self.read_solo_mode_state()
        if current.get("mode") != target:
            storage = self.write_solo_mode_state(
                enabled=(target == "solo"),
                source="mode-switch",
            )
            applied_via.append("state.vscdb")
            if target == "ide":
                notes.append(
                    "No confirmed IDE return command ID was found in the app bundle; updated the shared GUI state instead."
                )

        after = self.read_solo_mode_state()
        reload_required = storage is not None
        if reload_required and reload_if_needed:
            reload_dispatch = self.invoke_command_uri(
                RELOAD_WINDOW_COMMAND_ID,
                cwd=workspace,
            )
            if reload_dispatch.get("ok"):
                applied_via.append("reload-window")
                reload_required = False
            else:
                notes.append(
                    "Window reload dispatch failed; reopen or reload Trae manually to apply the mode change immediately."
                )

        status = "switched"
        if previous_mode == target and storage is None and command_dispatch is None:
            status = f"already-{target}"

        return {
            "status": status,
            "workspace": workspace,
            "target_mode": target,
            "previous_mode": previous_mode,
            "mode": after.get("mode"),
            "changed": previous_mode != after.get("mode"),
            "mode_state": after,
            "command_dispatch": command_dispatch,
            "storage": storage,
            "reload": reload_dispatch,
            "reload_requested": bool(reload_if_needed),
            "reload_required": reload_required,
            "applied_via": self._ordered_unique(applied_via),
            "notes": self._ordered_unique(notes),
        }

    @staticmethod
    def _workspace_trust_uri_payload(workspace: str) -> dict[str, Any]:
        resolved_workspace = Path(workspace).expanduser().resolve()
        return {
            "$mid": 1,
            "fsPath": str(resolved_workspace),
            "external": resolved_workspace.as_uri(),
            "path": str(resolved_workspace),
            "scheme": "file",
        }

    @staticmethod
    def _trusted_uri_entry_path(entry: Any) -> Optional[str]:
        if not isinstance(entry, dict):
            return None
        uri = entry.get("uri")
        if not isinstance(uri, dict):
            return None
        if str(uri.get("scheme") or "").strip().lower() != "file":
            return None
        raw = uri.get("fsPath") or uri.get("path")
        if not isinstance(raw, str) or not raw.strip():
            return None
        return str(Path(raw).expanduser().resolve())

    @staticmethod
    def _path_is_same_or_within(path: str, root: str) -> bool:
        try:
            Path(path).relative_to(Path(root))
            return True
        except ValueError:
            return False

    def read_trusted_workspace(
        self,
        *,
        workspace: Optional[Path | str],
    ) -> Optional[dict[str, Any]]:
        normalized_workspace = self.normalize_workspace_path(workspace)
        if not normalized_workspace:
            return None
        model = self.read_content_trust_model()
        trust_info = model.get("uriTrustInfo")
        candidates: list[tuple[int, dict[str, Any], str]] = []
        if isinstance(trust_info, list):
            for item in trust_info:
                matched_path = self._trusted_uri_entry_path(item)
                if not matched_path:
                    continue
                if not self._path_is_same_or_within(normalized_workspace, matched_path):
                    continue
                if not isinstance(item, dict):
                    continue
                candidates.append((len(matched_path), item, matched_path))
        if candidates:
            _, entry, matched_path = sorted(
                candidates,
                key=lambda item: item[0],
                reverse=True,
            )[0]
            if entry.get("trusted") is not True:
                return None
            return {
                "workspace": normalized_workspace,
                "matched_workspace": matched_path,
                "trusted": True,
                "inherited": matched_path != normalized_workspace,
                "source": "state.vscdb",
                "state_key": CONTENT_TRUST_STATE_KEY,
                "state_db_path": str(self.paths.state_db_path),
                "uri": entry.get("uri"),
            }

        legacy_record = self._read_legacy_trusted_workspace(workspace=normalized_workspace)
        if legacy_record is None:
            return None
        migrated = self.trust_workspace(
            workspace=normalized_workspace,
            source="legacy-migration",
        )
        self._clear_legacy_trusted_workspace(workspace=normalized_workspace)
        return migrated

    def trust_workspace(
        self,
        *,
        workspace: Path | str,
        source: str = "manual",
    ) -> dict[str, Any]:
        normalized_workspace = self.normalize_workspace_path(workspace)
        if not normalized_workspace:
            raise ValueError("Workspace path is required.")
        payload = self.read_content_trust_model()
        trust_info = payload.get("uriTrustInfo")
        if not isinstance(trust_info, list):
            trust_info = []
            payload["uriTrustInfo"] = trust_info
        legacy_record = self._read_legacy_trusted_workspace(workspace=normalized_workspace)
        now = datetime.now().astimezone().isoformat()
        trust_info = [
            item
            for item in trust_info
            if self._trusted_uri_entry_path(item) != normalized_workspace
        ]
        trust_info.append(
            {
                "uri": self._workspace_trust_uri_payload(normalized_workspace),
                "trusted": True,
            }
        )
        payload["uriTrustInfo"] = trust_info
        storage = self.write_content_trust_model(payload)
        self._clear_legacy_trusted_workspace(workspace=normalized_workspace)
        return {
            "workspace": normalized_workspace,
            "matched_workspace": normalized_workspace,
            "trusted": True,
            "inherited": False,
            "source": str(source or "manual"),
            "trusted_at": (
                str(legacy_record.get("trusted_at"))
                if isinstance(legacy_record, dict) and legacy_record.get("trusted_at")
                else now
            ),
            "updated_at": now,
            "app_path": str(self.paths.app_path),
            "state_key": CONTENT_TRUST_STATE_KEY,
            "state_db_path": str(self.paths.state_db_path),
            "storage": storage,
        }

    def clear_trusted_workspace(
        self,
        *,
        workspace: Optional[Path | str],
    ) -> bool:
        normalized_workspace = self.normalize_workspace_path(workspace)
        if not normalized_workspace:
            return False
        payload = self.read_content_trust_model()
        trust_info = payload.get("uriTrustInfo")
        if not isinstance(trust_info, list):
            trust_info = []
        filtered = [
            item
            for item in trust_info
            if self._trusted_uri_entry_path(item) != normalized_workspace
        ]
        changed = len(filtered) != len(trust_info)
        payload["uriTrustInfo"] = filtered
        if changed:
            self.write_content_trust_model(payload)
        legacy_cleared = self._clear_legacy_trusted_workspace(workspace=normalized_workspace)
        return changed or legacy_cleared

    def workspace_is_trusted(
        self,
        *,
        workspace: Optional[Path | str],
    ) -> bool:
        return self.read_trusted_workspace(workspace=workspace) is not None

    def read_workspace_cdp_binding(
        self,
        *,
        workspace: Optional[Path | str],
    ) -> Optional[dict[str, Any]]:
        normalized_workspace = self.normalize_workspace_path(workspace)
        if not normalized_workspace:
            return None
        payload = self.read_cli_bindings()
        bindings = payload.get("cdp_targets")
        if not isinstance(bindings, dict):
            return None
        binding = bindings.get(normalized_workspace)
        if not isinstance(binding, dict):
            return None
        if str(binding.get("app_path") or "") != str(self.paths.app_path):
            return None
        return binding

    def write_workspace_cdp_binding(
        self,
        *,
        workspace: Path | str,
        target: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        normalized_workspace = self.normalize_workspace_path(workspace)
        target_id = str((target or {}).get("id") or "").strip()
        if not normalized_workspace or not target_id:
            return None
        payload = self.read_cli_bindings()
        bindings = payload.get("cdp_targets")
        if not isinstance(bindings, dict):
            bindings = {}
            payload["cdp_targets"] = bindings
        binding = {
            "workspace": normalized_workspace,
            "app_path": str(self.paths.app_path),
            "target_id": target_id,
            "target_title": str((target or {}).get("title") or ""),
            "target_url": str((target or {}).get("url") or ""),
            "updated_at": datetime.now().astimezone().isoformat(),
        }
        bindings[normalized_workspace] = binding
        self.write_cli_bindings(payload)
        return binding

    def clear_workspace_cdp_binding(
        self,
        *,
        workspace: Optional[Path | str],
    ) -> bool:
        normalized_workspace = self.normalize_workspace_path(workspace)
        if not normalized_workspace:
            return False
        payload = self.read_cli_bindings()
        bindings = payload.get("cdp_targets")
        if not isinstance(bindings, dict) or normalized_workspace not in bindings:
            return False
        del bindings[normalized_workspace]
        self.write_cli_bindings(payload)
        return True

    def latest_cli_session(
        self,
        *,
        workspace: Optional[Path | str] = None,
        resumable_only: bool = False,
    ) -> Optional[dict[str, Any]]:
        sessions = self.read_cli_sessions()
        resolved_workspace = (
            str(Path(workspace).expanduser().resolve()) if workspace else None
        )
        filtered: list[dict[str, Any]] = []
        for item in sessions:
            if resolved_workspace is not None:
                workspace_value = item.get("workspace")
                if not isinstance(workspace_value, str) or workspace_value != resolved_workspace:
                    continue
            if resumable_only:
                session_value = item.get("last_headless_session_id")
                if not isinstance(session_value, str) or not session_value.strip():
                    continue
            filtered.append(item)
        if not filtered:
            return None
        filtered.sort(
            key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
            reverse=True,
        )
        return filtered[0]

    @staticmethod
    def _bridge_state_sort_key(state: dict[str, Any]) -> str:
        return str(
            state.get("last_updated_at")
            or state.get("started_at")
            or state.get("state_path")
            or ""
        )

    @staticmethod
    def _bridge_state_identity(state: dict[str, Any]) -> tuple[str, ...]:
        instance_id = str(state.get("instance_id") or "").strip()
        pid = str(state.get("pid") or "").strip()
        port = str(state.get("port") or "").strip()
        token = str(state.get("token") or "").strip()
        if instance_id or pid or port or token:
            return ("runtime", instance_id, pid, port, token)
        return ("path", str(state.get("state_path") or "").strip())

    def read_bridge_states(self) -> list[dict[str, Any]]:
        candidates: list[Path] = []
        instances_dir = self.paths.bridge_state_instances_dir
        if instances_dir.exists():
            candidates.extend(sorted(instances_dir.glob("*.json")))
        legacy_path = self.paths.bridge_state_path
        if legacy_path.exists():
            candidates.append(legacy_path)

        states: list[dict[str, Any]] = []
        seen: set[tuple[str, ...]] = set()
        for path in candidates:
            try:
                payload = self._safe_json_value(path.read_text(encoding="utf-8"))
            except OSError:
                continue
            if not isinstance(payload, dict):
                continue
            state = dict(payload)
            state.setdefault("state_path", str(path))
            identity = self._bridge_state_identity(state)
            if identity in seen:
                continue
            seen.add(identity)
            states.append(state)

        states.sort(key=self._bridge_state_sort_key, reverse=True)
        return states

    def _bridge_state_workspace_match_score(
        self,
        state: dict[str, Any],
        *,
        workspace: Optional[Path | str],
    ) -> int:
        normalized_workspace = self.normalize_workspace_path(workspace)
        if not normalized_workspace:
            return 0

        score = 0
        folders = state.get("workspace_folders")
        has_workspace_folders = isinstance(folders, list) and bool(folders)
        if isinstance(folders, list):
            for folder in folders:
                if not isinstance(folder, dict):
                    continue
                folder_path = self.normalize_workspace_path(
                    folder.get("fsPath") or folder.get("path")
                )
                if folder_path == normalized_workspace:
                    score = max(score, 1_000)
        active_editor = state.get("active_editor")
        if isinstance(active_editor, dict):
            active_path = self.normalize_workspace_path(
                active_editor.get("fsPath") or active_editor.get("path")
            )
            if (
                not has_workspace_folders
                and active_path
                and self._path_is_same_or_within(active_path, normalized_workspace)
            ):
                score = max(score, 700)
        return score

    def bridge_state_candidates(
        self,
        *,
        workspace: Optional[Path | str] = None,
    ) -> list[dict[str, Any]]:
        states = self.read_bridge_states()
        if not states:
            return []
        normalized_workspace = self.normalize_workspace_path(workspace)

        def sort_key(state: dict[str, Any]) -> tuple[int, str]:
            return (
                self._bridge_state_workspace_match_score(state, workspace=workspace),
                self._bridge_state_sort_key(state),
            )

        candidates = sorted(states, key=sort_key, reverse=True)
        if normalized_workspace:
            candidates = [
                state
                for state in candidates
                if self._bridge_state_workspace_match_score(
                    state,
                    workspace=normalized_workspace,
                )
                > 0
            ]
        return candidates

    def read_bridge_state(
        self,
        *,
        workspace: Optional[Path | str] = None,
    ) -> Optional[dict[str, Any]]:
        candidates = self.bridge_state_candidates(workspace=workspace)
        return candidates[0] if candidates else None

    def wait_for_bridge_state(
        self,
        *,
        workspace: Optional[Path | str] = None,
        wait_seconds: float = 0.0,
        poll_interval: float = 0.25,
    ) -> Optional[dict[str, Any]]:
        state = self.read_bridge_state(workspace=workspace)
        deadline = time.monotonic() + max(wait_seconds, 0.0)
        effective_poll_interval = max(poll_interval, 0.1)
        while state is None and time.monotonic() < deadline:
            time.sleep(
                min(effective_poll_interval, max(0.0, deadline - time.monotonic()))
            )
            state = self.read_bridge_state(workspace=workspace)
        return state

    def _bridge_state_workspace_paths(self, state: Optional[dict[str, Any]]) -> list[str]:
        if not isinstance(state, dict):
            return []
        folders = state.get("workspace_folders")
        if not isinstance(folders, list):
            return []
        paths: list[str] = []
        for folder in folders:
            if not isinstance(folder, dict):
                continue
            folder_path = self.normalize_workspace_path(
                folder.get("fsPath") or folder.get("path")
            )
            if folder_path and folder_path not in paths:
                paths.append(folder_path)
        return paths

    def _bridge_state_active_text_editor(
        self,
        state: Optional[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        if not isinstance(state, dict):
            return None
        active_editor = state.get("active_editor")
        if not isinstance(active_editor, dict):
            return None
        payload: dict[str, Any] = {}
        for key in ("uri", "fsPath", "path", "languageId"):
            value = active_editor.get(key)
            if value is None:
                continue
            if key in {"fsPath", "path"}:
                normalized = self.normalize_workspace_path(value)
                payload[key] = normalized or str(value)
            else:
                payload[key] = value
        return payload or None

    @staticmethod
    def _rpc_editor_zero_position() -> dict[str, int]:
        return {
            "line": 0,
            "character": 0,
        }

    @classmethod
    def _rpc_editor_empty_range(
        cls,
        *,
        include_active_anchor: bool = False,
        include_text: bool = False,
    ) -> dict[str, Any]:
        zero = cls._rpc_editor_zero_position()
        payload: dict[str, Any] = {
            "start": dict(zero),
            "end": dict(zero),
            "is_empty": True,
            "is_single_line": True,
        }
        if include_active_anchor:
            payload["active"] = dict(zero)
            payload["anchor"] = dict(zero)
        if include_text:
            payload["text"] = ""
        return payload

    @classmethod
    def _rpc_editor_range_for_text(cls, text: str) -> dict[str, Any]:
        if not text:
            return cls._rpc_editor_empty_range()
        lines = text.splitlines()
        if text.endswith(("\n", "\r")):
            lines.append("")
        if not lines:
            lines = [""]
        return {
            "start": cls._rpc_editor_zero_position(),
            "end": {
                "line": len(lines) - 1,
                "character": len(lines[-1]),
            },
            "is_empty": False,
            "is_single_line": len(lines) == 1,
        }

    @staticmethod
    def _rpc_editor_comment_style(language_id: str) -> str:
        language = str(language_id or "").strip().lower()
        if language in {
            "python",
            "shellscript",
            "shell",
            "bash",
            "zsh",
            "yaml",
            "dockerfile",
            "makefile",
            "toml",
            "perl",
            "ruby",
            "r",
        }:
            return "#"
        if language in {"sql", "lua", "haskell"}:
            return "--"
        if language in {"html", "xml"}:
            return "<!--"
        return "//"

    @classmethod
    def _build_rpc_active_text_editor_payload(
        cls,
        active_editor: Optional[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        if not isinstance(active_editor, dict):
            return None
        if isinstance(active_editor.get("document"), dict):
            return active_editor

        uri = str(active_editor.get("uri") or "").strip()
        fs_path_value = active_editor.get("fsPath") or active_editor.get("path")
        fs_path = cls.normalize_workspace_path(fs_path_value)
        language_id = str(
            active_editor.get("languageId")
            or active_editor.get("language_id")
            or (active_editor.get("language") or {}).get("language_id")
            or ""
        ).strip()
        if not uri and fs_path:
            uri = Path(fs_path).as_uri()
        parsed_uri = urlparse(uri) if uri else None
        scheme = (
            parsed_uri.scheme
            if parsed_uri and parsed_uri.scheme
            else ("file" if fs_path else "")
        )
        file_name = ""
        if fs_path:
            file_name = Path(fs_path).name
        elif parsed_uri and parsed_uri.path:
            file_name = Path(unquote(parsed_uri.path)).name

        document_text = ""
        if fs_path:
            try:
                raw_text = Path(fs_path).read_text(errors="replace")
            except OSError:
                raw_text = ""
            if raw_text:
                document_text = raw_text[:524288]

        empty_range = cls._rpc_editor_range_for_text(document_text)
        selection = cls._rpc_editor_empty_range(
            include_active_anchor=True,
            include_text=True,
        )
        return {
            "document": {
                "uri": uri or "",
                "language_id": language_id,
                "file_name": file_name,
                "text": document_text,
                "version": 1 if document_text else 0,
                "scheme": scheme,
                "is_dirty": False,
            },
            "file_indent_info": {
                "insert_spaces": True,
                "tab_size": 4,
            },
            "language": {
                "language_id": language_id,
                "comment_style": cls._rpc_editor_comment_style(language_id),
            },
            "whole_range": dict(empty_range),
            "selection": selection,
            "expanded_range": dict(empty_range),
            "function_range": dict(empty_range),
            "file_name": file_name,
            "visible_ranges": [dict(empty_range)],
            "is_empty_line": not document_text.splitlines()[:1]
            or not document_text.splitlines()[0].strip(),
            "file_size": len(document_text),
        }

    def build_workspace_context(
        self,
        *,
        workspace: Optional[Path | str] = None,
        bridge_state: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        normalized_workspace = self.normalize_workspace_path(workspace)
        resolved_bridge_state = (
            bridge_state
            if isinstance(bridge_state, dict)
            else self.read_bridge_state(workspace=workspace)
        )
        bridge_workspace_folders = self._bridge_state_workspace_paths(resolved_bridge_state)
        active_text_editor = self._bridge_state_active_text_editor(resolved_bridge_state)

        workspace_folders = list(bridge_workspace_folders)
        if normalized_workspace and normalized_workspace not in workspace_folders:
            workspace_folders = [normalized_workspace]
        elif not workspace_folders and normalized_workspace:
            workspace_folders = [normalized_workspace]

        payload: dict[str, Any] = {
            "workspace_folders": workspace_folders,
        }
        if normalized_workspace:
            payload["workspace_folder"] = normalized_workspace
        if bridge_workspace_folders and workspace_folders != bridge_workspace_folders:
            payload["original_workspace_folders"] = bridge_workspace_folders
            payload["is_workspace_folder_changed"] = True
        elif normalized_workspace:
            payload["is_workspace_folder_changed"] = False
        if active_text_editor:
            payload["active_text_editor"] = self._build_rpc_active_text_editor_payload(
                active_text_editor
            )
        return payload

    def _bridge_request_with_state(
        self,
        state: dict[str, Any],
        route: str,
        *,
        payload: Optional[dict[str, Any]] = None,
        timeout_seconds: float = BRIDGE_EXTENSION_DEFAULT_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        host = str(state.get("host") or "127.0.0.1")
        port = self._parse_optional_int(state.get("port"))
        token = str(state.get("token") or "").strip()
        if port is None or not token:
            raise RuntimeError(
                "Bridge state file is missing `port` or `token`; reload Trae so the companion extension can restart."
            )
        normalized_route = route if route.startswith("/") else f"/{route}"
        url = f"http://{host}:{port}{normalized_route}"
        data = None
        headers = {
            "x-traecli-token": token,
            "content-type": "application/json; charset=utf-8",
        }
        method = "GET"
        if payload is not None:
            method = "POST"
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=timeout_seconds) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"Bridge request failed with HTTP {exc.code}: {detail or exc.reason}"
            ) from exc
        except (
            urllib.error.URLError,
            TimeoutError,
            socket.timeout,
            OSError,
            ValueError,
        ) as exc:
            raise RuntimeError(
                f"Bridge request failed for {url}: {exc}"
            ) from exc
        response_payload = self._safe_json_value(raw.strip())
        if not isinstance(response_payload, dict):
            raise RuntimeError(
                f"Bridge returned invalid JSON for {normalized_route}: {raw.strip() or 'empty body'}"
            )
        if response_payload.get("ok") is False:
            error = response_payload.get("error")
            if isinstance(error, dict):
                message = error.get("message") or json.dumps(error, ensure_ascii=False)
            else:
                message = str(error or "unknown bridge error")
            raise RuntimeError(
                f"Bridge returned an error for {normalized_route}: {message}"
            )
        return response_payload

    def bridge_request(
        self,
        route: str,
        *,
        payload: Optional[dict[str, Any]] = None,
        timeout_seconds: float = BRIDGE_EXTENSION_DEFAULT_TIMEOUT_SECONDS,
        workspace: Optional[Path | str] = None,
    ) -> dict[str, Any]:
        states = self.bridge_state_candidates(workspace=workspace)
        if not states:
            normalized_workspace = self.normalize_workspace_path(workspace)
            if normalized_workspace and self.read_bridge_states():
                raise RuntimeError(
                    "No running Trae bridge instance is bound to workspace "
                    f"`{normalized_workspace}`."
                )
            raise RuntimeError(
                f"Bridge state file not found: {self.paths.bridge_state_path}"
            )
        last_error: Optional[RuntimeError] = None
        for state in states:
            try:
                return self._bridge_request_with_state(
                    state,
                    route,
                    payload=payload,
                    timeout_seconds=timeout_seconds,
                )
            except RuntimeError as exc:
                last_error = exc
                continue
        if last_error is not None:
            raise last_error
        normalized_workspace = self.normalize_workspace_path(workspace)
        if normalized_workspace and self.read_bridge_states():
            raise RuntimeError(
                "No running Trae bridge instance is bound to workspace "
                f"`{normalized_workspace}`."
            )
        raise RuntimeError(
            f"Bridge state file not found: {self.paths.bridge_state_path}"
        )

    def bridge_health(
        self,
        *,
        timeout_seconds: float = BRIDGE_EXTENSION_DEFAULT_TIMEOUT_SECONDS,
        workspace: Optional[Path | str] = None,
    ) -> dict[str, Any]:
        return self.bridge_request(
            "/health",
            timeout_seconds=timeout_seconds,
            workspace=workspace,
        )

    def list_bridge_commands(
        self,
        *,
        match: Optional[str] = None,
        include_internal: bool = True,
        timeout_seconds: float = BRIDGE_EXTENSION_DEFAULT_TIMEOUT_SECONDS,
        workspace: Optional[Path | str] = None,
    ) -> dict[str, Any]:
        query: dict[str, str] = {"internal": "true" if include_internal else "false"}
        if match:
            query["match"] = match
        route = "/commands"
        if query:
            route += "?" + urlencode(query)
        return self.bridge_request(
            route,
            timeout_seconds=timeout_seconds,
            workspace=workspace,
        )

    def execute_bridge_command(
        self,
        command_id: str,
        *,
        args: Optional[list[Any]] = None,
        timeout_seconds: float = BRIDGE_EXTENSION_DEFAULT_TIMEOUT_SECONDS,
        workspace: Optional[Path | str] = None,
    ) -> dict[str, Any]:
        return self.bridge_request(
            "/execute",
            payload={
                "command": command_id,
                "args": args or [],
            },
            timeout_seconds=timeout_seconds,
            workspace=workspace,
        )

    def invoke_bridge_aha_rpc(
        self,
        *,
        envelope: dict[str, Any],
        service_name: str = "ai-agent",
        request_method: str = "request",
        timeout_ms: int = 10000,
        workspace: Optional[Path | str] = None,
    ) -> dict[str, Any]:
        timeout_seconds = max(timeout_ms / 1000.0, 1.0) + 1.0
        return self.bridge_request(
            "/aha-rpc",
            payload={
                "service_name": service_name,
                "request_method": request_method,
                "timeout_ms": timeout_ms,
                "envelope": envelope,
            },
            timeout_seconds=timeout_seconds,
            workspace=workspace,
        )

    def bridge_vscode_surface(
        self,
        *,
        path: Optional[str] = None,
        timeout_seconds: float = BRIDGE_EXTENSION_DEFAULT_TIMEOUT_SECONDS,
        workspace: Optional[Path | str] = None,
    ) -> dict[str, Any]:
        route = "/vscode-surface"
        query: dict[str, str] = {}
        if path:
            query["path"] = path
        if query:
            route += "?" + urlencode(query)
        return self.bridge_request(
            route,
            timeout_seconds=timeout_seconds,
            workspace=workspace,
        )

    def invoke_bridge_vscode_api(
        self,
        path: str,
        *,
        args: Optional[list[Any]] = None,
        timeout_seconds: float = BRIDGE_EXTENSION_DEFAULT_TIMEOUT_SECONDS,
        workspace: Optional[Path | str] = None,
    ) -> dict[str, Any]:
        return self.bridge_request(
            "/vscode-api",
            payload={
                "path": path,
                "args": args or [],
            },
            timeout_seconds=timeout_seconds,
            workspace=workspace,
        )

    def invoke_bridge_icube_rpc(
        self,
        *,
        client_url: str,
        method: str,
        args: Optional[list[Any]] = None,
        client_options: Optional[dict[str, Any]] = None,
        close_after: bool = True,
        timeout_seconds: float = BRIDGE_EXTENSION_DEFAULT_TIMEOUT_SECONDS,
        workspace: Optional[Path | str] = None,
    ) -> dict[str, Any]:
        return self.bridge_request(
            "/icube-rpc",
            payload={
                "client_url": client_url,
                "client_options": client_options or {},
                "method": method,
                "args": args or [],
                "close_after": close_after,
            },
            timeout_seconds=timeout_seconds,
            workspace=workspace,
        )

    def inspect_bridge_icube_rpc_client(
        self,
        *,
        client_url: str,
        client_options: Optional[dict[str, Any]] = None,
        method: Optional[str] = None,
        args: Optional[list[Any]] = None,
        wait_ms: int = 100,
        timeout_seconds: float = BRIDGE_EXTENSION_DEFAULT_TIMEOUT_SECONDS,
        workspace: Optional[Path | str] = None,
    ) -> dict[str, Any]:
        return self.bridge_request(
            "/icube-rpc-inspect",
            payload={
                "client_url": client_url,
                "client_options": client_options or {},
                "method": method or "",
                "args": args or [],
                "wait_ms": wait_ms,
            },
            timeout_seconds=timeout_seconds,
            workspace=workspace,
        )

    def _bridge_state_candidate_workspaces(
        self,
        states: list[dict[str, Any]],
    ) -> list[str]:
        workspaces: list[str] = []
        for state in states:
            for path in self._bridge_state_workspace_paths(state):
                if path not in workspaces:
                    workspaces.append(path)
        return workspaces

    @staticmethod
    def _log_session_has_log_files(path: Optional[Path]) -> bool:
        return path is not None and any(path.rglob("*.log"))

    def _collect_bridge_runtime_diagnostics(self) -> dict[str, Any]:
        active_latest = self.latest_log_session_dir()
        latest = active_latest
        if active_latest is not None and not self._log_session_has_log_files(active_latest):
            for candidate in reversed(self.list_log_session_dirs()):
                if candidate == active_latest:
                    continue
                if self._log_session_has_log_files(candidate):
                    latest = candidate
                    break
        diagnostics: dict[str, Any] = {
            "active_log_session": str(active_latest) if active_latest else None,
            "latest_log_session": str(latest) if latest else None,
            "renderer_window_count": 0,
            "renderer_logs": [],
            "extension_scanner_input_count": 0,
            "extension_scanner_windows": [],
            "started_local_extension_host_count": 0,
            "started_local_extension_host_windows": [],
            "started_local_extension_host_pids": [],
            "exthost_dir_count": 0,
            "exthost_windows": [],
            "exthost_log_count": 0,
            "exthost_log_paths": [],
            "bridge_output_log_count": 0,
            "bridge_output_log_paths": [],
            "bridge_output_listeners": [],
            "manager_exchange_ready_count": 0,
            "extension_host_process_count": 0,
            "extension_host_pids": [],
            "live_probe": None,
            "notes": [],
        }
        if latest is None:
            diagnostics["notes"].append("No Trae log session was found.")
            return diagnostics

        renderer_logs = sorted(
            path for path in latest.glob("window*/renderer.log") if path.is_file()
        )
        diagnostics["renderer_window_count"] = len(renderer_logs)
        diagnostics["renderer_logs"] = [
            str(path.relative_to(latest)) for path in renderer_logs
        ]
        scanner_windows: list[str] = []
        started_windows: list[str] = []
        started_pids: list[int] = []
        for path in renderer_logs:
            window_name = path.parent.name
            for line in path.read_text(errors="replace").splitlines():
                if EXTENSION_SCANNER_INPUT_RE.search(line):
                    diagnostics["extension_scanner_input_count"] += 1
                    scanner_windows.append(window_name)
                started_match = LOCAL_EXTENSION_HOST_STARTED_RE.search(line)
                if started_match:
                    diagnostics["started_local_extension_host_count"] += 1
                    started_windows.append(window_name)
                    started_pids.append(int(started_match.group("pid")))
        diagnostics["extension_scanner_windows"] = self._ordered_unique(scanner_windows)
        diagnostics["started_local_extension_host_windows"] = self._ordered_unique(
            started_windows
        )
        diagnostics["started_local_extension_host_pids"] = self._ordered_unique(
            started_pids
        )

        exthost_dirs = sorted(
            path for path in latest.glob("window*/exthost") if path.is_dir()
        )
        diagnostics["exthost_dir_count"] = len(exthost_dirs)
        diagnostics["exthost_windows"] = self._ordered_unique(
            [path.parent.name for path in exthost_dirs]
        )
        exthost_logs = sorted(
            path for path in latest.glob("window*/exthost/exthost.log") if path.is_file()
        )
        diagnostics["exthost_log_count"] = len(exthost_logs)
        diagnostics["exthost_log_paths"] = [
            str(path.relative_to(latest)) for path in exthost_logs
        ]

        bridge_logs = sorted(
            path
            for path in latest.glob(
                "window*/exthost/output_logging_*/1-Trae CLI Headless.log"
            )
            if path.is_file()
        )
        diagnostics["bridge_output_log_count"] = len(bridge_logs)
        diagnostics["bridge_output_log_paths"] = [
            str(path.relative_to(latest)) for path in bridge_logs
        ]
        bridge_output_listeners: list[dict[str, Any]] = []
        manager_exchange_ready_count = 0
        for path in bridge_logs:
            relative_path = str(path.relative_to(latest))
            for line in path.read_text(errors="replace").splitlines():
                listen_match = BRIDGE_OUTPUT_LISTEN_RE.search(line)
                if listen_match:
                    bridge_output_listeners.append(
                        {
                            "path": relative_path,
                            "host": listen_match.group("host"),
                            "port": int(listen_match.group("port")),
                        }
                    )
                if BRIDGE_OUTPUT_MANAGER_READY_RE.search(line):
                    manager_exchange_ready_count += 1
        diagnostics["bridge_output_listeners"] = bridge_output_listeners
        diagnostics["manager_exchange_ready_count"] = manager_exchange_ready_count

        topology = self.extract_transport_topology(include_live=True)
        live = topology.get("live") if isinstance(topology, dict) else None
        if isinstance(live, dict):
            processes = live.get("processes") or []
            extension_host_processes = [
                process
                for process in processes
                if process.get("role") == "extension-host"
            ]
            diagnostics["extension_host_process_count"] = len(extension_host_processes)
            diagnostics["extension_host_pids"] = [
                int(process["pid"])
                for process in extension_host_processes
                if process.get("pid") is not None
            ]
            diagnostics["live_probe"] = {
                "ps_probe_status": live.get("ps_probe_status"),
                "lsof_probe_status": live.get("lsof_probe_status"),
                "process_count": len(processes),
                "notes": list(live.get("notes") or []),
            }

        notes: list[str] = []
        if active_latest is not None and latest is not None and active_latest != latest:
            notes.append(
                f"Active log session {active_latest} is empty; fell back to {latest} for diagnostics."
            )
        if (
            diagnostics["extension_scanner_input_count"]
            and not diagnostics["started_local_extension_host_count"]
            and not diagnostics["extension_host_process_count"]
            and not diagnostics["exthost_log_count"]
        ):
            notes.append(
                "Renderer logs show extension scanning, but no local extension host start was observed."
            )
        if (
            diagnostics["started_local_extension_host_count"]
            or diagnostics["extension_host_process_count"]
            or diagnostics["exthost_log_count"]
        ):
            notes.append(
                "Extension host runtime artifacts are present in the latest Trae session."
            )
        if diagnostics["bridge_output_log_count"]:
            notes.append(
                "The Trae CLI Headless extension wrote output logs in the latest exthost session."
            )
        if diagnostics["bridge_output_listeners"]:
            listener = diagnostics["bridge_output_listeners"][-1]
            notes.append(
                f"Bridge output logs report a listener on {listener['host']}:{listener['port']}."
            )
        if diagnostics["manager_exchange_ready_count"]:
            notes.append(
                "Bridge output logs show the manager exchange reached the ready state."
            )
        elif diagnostics["bridge_output_log_count"] and not diagnostics[
            "manager_exchange_ready_count"
        ]:
            notes.append(
                "Bridge output logs exist, but no manager exchange ready event was seen."
            )
        if not notes:
            notes.append(
                "No extension-host specific bridge diagnostics were inferred from the latest session."
            )
        diagnostics["notes"] = self._ordered_unique(notes)
        return diagnostics

    def bridge_status(
        self,
        *,
        ping: bool = False,
        timeout_seconds: float = BRIDGE_EXTENSION_DEFAULT_TIMEOUT_SECONDS,
        workspace: Optional[Path | str] = None,
    ) -> dict[str, Any]:
        normalized_workspace = self.normalize_workspace_path(workspace)
        manifest = self.load_bridge_extension_manifest()
        extension_id = self.bridge_extension_id_from_manifest(manifest)
        version = self.bridge_extension_version_from_manifest(manifest)
        install_dir = self.bridge_extension_install_dir(manifest)
        registry_path = self.paths.user_extensions_dir / "extensions.json"
        registry_entries: list[dict[str, Any]] = []
        if registry_path.exists():
            loaded = self._safe_json_value(registry_path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                for entry in loaded:
                    if (
                        isinstance(entry, dict)
                        and isinstance(entry.get("identifier"), dict)
                        and entry["identifier"].get("id") == extension_id
                    ):
                        registry_entries.append(entry)
        states = self.read_bridge_states()
        matched_states = (
            self.bridge_state_candidates(workspace=normalized_workspace)
            if normalized_workspace
            else list(states)
        )
        state = matched_states[0] if matched_states else None
        health = None
        health_error = None
        if ping and state is not None:
            try:
                health = self.bridge_health(
                    timeout_seconds=timeout_seconds,
                    workspace=workspace,
                )
            except RuntimeError as exc:
                health_error = str(exc)
        candidate_workspaces = self._bridge_state_candidate_workspaces(states)
        runtime_diagnostics = self._collect_bridge_runtime_diagnostics() if state is None else None
        notes: list[str] = []
        if normalized_workspace and states and state is None:
            notes.append(
                f"Bridge instances exist, but none match the requested workspace {normalized_workspace}."
            )
            if candidate_workspaces:
                notes.append(
                    "Active bridge workspaces: " + ", ".join(candidate_workspaces[:5])
                )
        if isinstance(runtime_diagnostics, dict):
            notes.extend(runtime_diagnostics.get("notes") or [])
        return {
            "extension_id": extension_id,
            "version": version,
            "source_dir": str(self._bridge_extension_source_dir()),
            "install_dir": str(install_dir),
            "installed": install_dir.exists(),
            "workspace": normalized_workspace,
            "registry_path": str(registry_path),
            "registry_entries": registry_entries,
            "state_path": str(self.paths.bridge_state_path),
            "state_count": len(states),
            "matched_state_count": len(matched_states),
            "states": states,
            "state": state,
            "candidate_workspaces": candidate_workspaces,
            "health": health,
            "health_error": health_error,
            "runtime_diagnostics": runtime_diagnostics,
            "notes": self._ordered_unique(notes),
        }

    def default_headless_app_copy_path(self) -> Path:
        return (
            self.paths.user_data_dir
            / "app-copies"
            / f"{self.paths.app_path.stem}-headless.app"
        )

    def app_bundle_writable(self) -> dict[str, Any]:
        probe_dir = self.paths.ai_chat_bundle_path.parent
        probe_path = probe_dir / f".traecli-write-probe-{uuid.uuid4().hex}"
        try:
            probe_path.write_text("probe", encoding="utf-8")
            probe_path.unlink()
            return {
                "writable": True,
                "error": None,
                "probe_dir": str(probe_dir),
            }
        except OSError as exc:
            return {
                "writable": False,
                "error": str(exc),
                "probe_dir": str(probe_dir),
            }

    def clone_app_bundle(
        self,
        *,
        target_app_path: Optional[Path | str] = None,
        overwrite: bool = True,
    ) -> dict[str, Any]:
        source_app_path = self.paths.app_path
        target_path = (
            Path(target_app_path).expanduser().resolve()
            if target_app_path
            else self.default_headless_app_copy_path()
        )
        if source_app_path.resolve() == target_path.resolve():
            raise RuntimeError("The target app copy path must differ from the source app path.")
        if target_path.suffix.lower() != ".app":
            raise RuntimeError("The target app path must end with `.app`.")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        status = "already-exists"
        if target_path.exists():
            if not overwrite:
                return {
                    "status": status,
                    "source_app_path": str(source_app_path),
                    "target_app_path": str(target_path),
                    "target_exists": True,
                }
            shutil.rmtree(target_path)
            status = "updated"
        else:
            status = "created"
        shutil.copytree(source_app_path, target_path, symlinks=True)
        return {
            "status": status,
            "source_app_path": str(source_app_path),
            "target_app_path": str(target_path),
            "target_exists": target_path.exists(),
        }

    def resign_app_bundle(self) -> dict[str, Any]:
        if not self._command_available("codesign"):
            raise RuntimeError("`codesign` is required to re-sign a patched Trae app bundle.")

        sign_command = [
            "codesign",
            "--force",
            "--deep",
            "--sign",
            "-",
            str(self.paths.app_path),
        ]
        sign_result = subprocess.run(
            sign_command,
            text=True,
            capture_output=True,
            check=False,
        )
        if sign_result.returncode != 0:
            stderr = (sign_result.stderr or "").strip()
            stdout = (sign_result.stdout or "").strip()
            detail = stderr or stdout or "codesign returned a non-zero exit status."
            raise RuntimeError(
                "Unable to re-sign the patched Trae app bundle.\n"
                f"App: {self.paths.app_path}\n"
                f"Command: {' '.join(sign_command)}\n"
                f"Detail: {detail}"
            )

        verify_command = [
            "codesign",
            "--verify",
            "--deep",
            "--strict",
            "--verbose=2",
            str(self.paths.app_path),
        ]
        verify_result = subprocess.run(
            verify_command,
            text=True,
            capture_output=True,
            check=False,
        )
        if verify_result.returncode != 0:
            stderr = (verify_result.stderr or "").strip()
            stdout = (verify_result.stdout or "").strip()
            detail = stderr or stdout or "codesign verification failed."
            raise RuntimeError(
                "Patched Trae app bundle was re-signed but verification still failed.\n"
                f"App: {self.paths.app_path}\n"
                f"Command: {' '.join(verify_command)}\n"
                f"Detail: {detail}"
            )

        return {
            "status": "signed",
            "app_path": str(self.paths.app_path),
            "command": sign_command,
            "verify_command": verify_command,
            "stdout": sign_result.stdout,
            "stderr": sign_result.stderr,
            "verified": True,
            "verify_stdout": verify_result.stdout,
            "verify_stderr": verify_result.stderr,
        }

    def prepare_headless_app_copy(
        self,
        *,
        target_app_path: Optional[Path | str] = None,
        overwrite: bool = True,
    ) -> dict[str, Any]:
        clone_payload = self.clone_app_bundle(
            target_app_path=target_app_path,
            overwrite=overwrite,
        )
        prepared_backend = TraeBackend(
            app_path=clone_payload["target_app_path"],
            support_dir=self.paths.support_dir,
            user_data_dir=self.paths.user_data_dir,
        )
        patch_payload = prepared_backend.install_headless_patch()
        signature_payload = prepared_backend.resign_app_bundle()
        writable = prepared_backend.app_bundle_writable()
        return {
            "status": "prepared",
            "source_app_path": str(self.paths.app_path),
            "app_path": str(prepared_backend.paths.app_path),
            "clone": clone_payload,
            "patch": patch_payload,
            "signature": signature_payload,
            "writable": writable,
        }

    def headless_patch_backup_path(self) -> Path:
        return Path(f"{self.paths.ai_chat_bundle_path}{HEADLESS_PATCH_BACKUP_SUFFIX}")

    @staticmethod
    def _build_headless_patch_snippet() -> str:
        snippet = "".join(
            [
                f"/* {HEADLESS_PATCH_MARKER} */(()=>{{",
                'function __traecliHeadlessTrimText(e){if(null==e)return null;if("string"==typeof e){e=e.trim();return e||null}return"number"==typeof e||"boolean"==typeof e?String(e):null}',
                'function __traecliHeadlessPromptSet(e){let t=new Set;return Array.isArray(e)&&e.forEach(e=>{let r=__traecliHeadlessTrimText(e);r&&t.add(r)}),t}',
                'function __traecliHeadlessCandidate(e,t){let r=__traecliHeadlessTrimText(e);return r&&(!t||!t.has(r))?r:null}',
                'function __traecliHeadlessValueText(e,t,r,i){if(void 0===t&&(t=0),void 0===r&&(r=[]),t>6)return null;let n=__traecliHeadlessTrimText(e);if(n)return n;if(Array.isArray(e)){for(let o of e){let a=__traecliHeadlessValueText(o,t+1,r,i);if(a)return a}return null}if(!e||"object"!=typeof e)return null;i||(i=new WeakSet);if(i.has(e))return null;i.add(e);for(let o of r){let a=__traecliHeadlessValueText(e[o],t+1,r,i);if(a)return a}for(let[o,a]of Object.entries(e)){if(r.includes(o))continue;let s=__traecliHeadlessValueText(a,t+1,r,i);if(s)return s}return null}',
                'function __traecliHeadlessBuildResult(e,t){return e?{text:e,source:t}:null}',
                'function __traecliHeadlessPlanItemText(e,t){if(!e||"object"!=typeof e)return null;let r=String(e.toolName||e?.tool_call_info?.name||"").toLowerCase(),i=["summary","content","text","message","answer","response","final_answer","output"],n=["finish","response_to_user","agent_finish"].includes(r);if(n){let o=e.params;if(o&&"object"==typeof o){for(let a of i){let s=__traecliHeadlessCandidate(__traecliHeadlessValueText(o[a],0,i),t);if(s)return __traecliHeadlessBuildResult(s,`plan_item.${r}.params.${a}`)}let a=__traecliHeadlessCandidate(__traecliHeadlessValueText(o,0,i),t);if(a)return __traecliHeadlessBuildResult(a,`plan_item.${r}.params`)}for(let o of["thought","summary","content","text","message","answer","response"]){let a=__traecliHeadlessCandidate(__traecliHeadlessValueText(e[o],0,i),t);if(a)return __traecliHeadlessBuildResult(a,`plan_item.${r}.${o}`)}let o=e.result;if(o&&"object"==typeof o)for(let a of["summary","content","text","message","answer","response","data"]){let s=__traecliHeadlessCandidate(__traecliHeadlessValueText(o[a],0,i),t);if(s)return __traecliHeadlessBuildResult(s,`plan_item.${r}.result.${a}`)}}for(let o of["thought","summary","content","text","message","answer","response"]){let a=__traecliHeadlessCandidate(__traecliHeadlessValueText(e[o],0,i),t);if(a)return __traecliHeadlessBuildResult(a,`plan_item.${r||"unknown"}.${o}`)}return null}',
                'function __traecliHeadlessMessageText(e,t){if(!e||"object"!=typeof e)return null;let r=["summary","content","text","message","answer","response","final_answer","output"];for(let[i,n]of[["content","message.content"],["text","message.text"],["message","message.message"],["summary","message.summary"],["answer","message.answer"]]){let o=__traecliHeadlessCandidate(__traecliHeadlessValueText(e[i],0,r),t);if(o)return __traecliHeadlessBuildResult(o,n)}let i=e.agentTaskContent;if(i&&"object"==typeof i){for(let[e,n]of[["proposal","agentTaskContent.proposal"],["summary","agentTaskContent.summary"],["content","agentTaskContent.content"],["answer","agentTaskContent.answer"]]){let o=__traecliHeadlessCandidate(__traecliHeadlessValueText(i[e],0,r),t);if(o)return __traecliHeadlessBuildResult(o,n)}let n=i.guideline,o=Array.isArray(n?.planItems)?n.planItems:[];for(let a=o.length-1;a>=0;a--){let s=__traecliHeadlessPlanItemText(o[a],t);if(s&&["plan_item.finish","plan_item.response_to_user","plan_item.agent_finish"].some(e=>s.source.startsWith(e)))return s}for(let a=o.length-1;a>=0;a--){let s=__traecliHeadlessPlanItemText(o[a],t);if(s)return s}}return null}',
                'function __traecliHeadlessExtractAnswer(e,t){let r=__traecliHeadlessPromptSet(t),i=e?.unstableFields?.session,n=Array.isArray(i?.messages)?i.messages:[],o=n.filter(e=>e&&"object"==typeof e&&["assistant","model"].includes(String(e.role||"").toLowerCase()));for(let a of(o.length?o:n).slice().reverse()){let s=__traecliHeadlessMessageText(a,r);if(s)return s}return null}',
                f'return i.registerCommand("{HEADLESS_COMMAND_ID}",async(e,t,r)=>{{'
                'if(!t||0===t.length)throw Error("no inputs provided");'
                "let i=ur.getInstance(),n=i.resolve(Sn.IICubeAuthService);"
                'if("not-login"===n.getCurrentLoginStatusSync())throw Error("the headless command only support login user");'
                "let o={enableUnstableFields:!0};"
                "r&&(r.sessionId&&(o.sessionId=r.sessionId),"
                "r.modelName&&(o.modelName=r.modelName),"
                "r.agentName&&(o.agentName=r.agentName),"
                "r.agentId&&(o.agentId=r.agentId),"
                "r.projectId&&(o.projectId=r.projectId),"
                "r.realProjectId&&(o.realProjectId=r.realProjectId),"
                "r.workspaceFolder&&(o.workspaceFolder=r.workspaceFolder),"
                "r.integrations&&(o.integrations=r.integrations),"
                "r.cancelEventKey&&(o.cancelEventKey=r.cancelEventKey));"
                "let a=null,s=[],l=null;"
                "try{if(r?.projectId){let e=i.resolve(Tc);e?.actions?.setProjectId?.(r.projectId,r.realProjectId||'')}}catch(e){}"
                "try{let e=i.resolve(Sn.IViewsService);await e.openViewContainer(zN,!0)}catch(e){}"
                'try{document.dispatchEvent(new CustomEvent("icube.ai-agent.focusInput"))}catch(e){}'
                "a=await z7.sendToAgent(t,o);"
                "try{l=ur.getInstance().resolve(NB);let e=l?.getMessages?.(a?.sessionId);Array.isArray(e)&&(s=e)}catch(e){}"
                "let u=a?.unstableFields?.session,d=s.length?{...(u||{}),sessionId:a?.sessionId||u?.sessionId||'',messages:s}:u,h=u&&a?.unstableFields?{...a,unstableFields:{...a.unstableFields,session:d}}:a,p=__traecliHeadlessExtractAnswer(h,t);"
                'return h&&"object"==typeof h?{...h,traecliPatchVersion:"'
                f"{HEADLESS_PATCH_VERSION}"
                '",answerText:p?.text??h.answerText??null,answerSource:p?.source??h.answerSource??null}:h})})(),'
            ]
        )
        return snippet.replace("for(let", "for(var").replace("let ", "var ")

    def _patch_ai_chat_bundle(self, bundle: str) -> str:
        if HEADLESS_PATCH_MARKER in bundle:
            return bundle
        if HEADLESS_PATCH_MARKER_PREFIX in bundle:
            upgraded_bundle, replacements = HEADLESS_PATCH_MARKED_BLOCK_RE.subn(
                self._build_headless_patch_snippet(),
                bundle,
                count=1,
            )
            if replacements:
                return upgraded_bundle
            raise RuntimeError(
                "Found an existing TraeCLI headless patch marker, but could not upgrade it in place."
            )
        if HEADLESS_COMMAND_ID in bundle:
            upgraded_bundle, replacements = HEADLESS_PATCH_COMMAND_RE.subn(
                self._build_headless_patch_snippet(),
                bundle,
                count=1,
            )
            if replacements:
                return upgraded_bundle
            raise RuntimeError(
                "Found an existing TraeCLI headless patch, but could not upgrade it in place."
            )
        if HEADLESS_PATCH_COMMAND_ANCHOR not in bundle:
            raise RuntimeError(
                "Unable to locate the bundled code-review command anchor in Trae's ai chat bundle."
            )
        return bundle.replace(
            HEADLESS_PATCH_COMMAND_ANCHOR,
            self._build_headless_patch_snippet() + HEADLESS_PATCH_COMMAND_ANCHOR,
            1,
        )

    def _headless_prepare_guidance(self, *, writable: Optional[dict[str, Any]] = None) -> str:
        default_copy = json.dumps(
            str(self.default_headless_app_copy_path()),
            ensure_ascii=False,
        )
        lines = [
            f"Trae app bundle is not writable: {self.paths.app_path}",
        ]
        if writable:
            probe_dir = writable.get("probe_dir")
            if probe_dir:
                lines.append(f"Probe dir: {probe_dir}")
            error = writable.get("error")
            if error:
                lines.append(f"Write check error: {error}")
        lines.extend(
            [
                "Prepare a user-writable app copy first, then retry against that copy:",
                f"  traecli headless prepare --target-app-path {default_copy}",
                f"  traecli --app-path {default_copy} headless install --reload",
            ]
        )
        return "\n".join(lines)

    def install_headless_patch(self) -> dict[str, Any]:
        bundle_path = self.paths.ai_chat_bundle_path
        backup_path = self.headless_patch_backup_path()
        if not bundle_path.exists():
            raise FileNotFoundError(f"AI chat bundle not found: {bundle_path}")
        bundle = bundle_path.read_text(errors="replace")
        if HEADLESS_PATCH_MARKER in bundle:
            status = "already-installed"
        else:
            writable = self.app_bundle_writable()
            if not writable.get("writable"):
                raise RuntimeError(self._headless_prepare_guidance(writable=writable))
            try:
                if not backup_path.exists():
                    shutil.copy2(bundle_path, backup_path)
                legacy_patch_installed = HEADLESS_COMMAND_ID in bundle
                bundle_path.write_text(self._patch_ai_chat_bundle(bundle), encoding="utf-8")
            except OSError as exc:
                guidance = self._headless_prepare_guidance(
                    writable={
                        "writable": False,
                        "error": str(exc),
                        "probe_dir": str(bundle_path.parent),
                    }
                )
                raise RuntimeError(
                    f"Unable to patch {bundle_path}: {exc}\n{guidance}"
                ) from exc
            status = "upgraded" if legacy_patch_installed else "installed"
        return {
            "status": status,
            "command_id": HEADLESS_COMMAND_ID,
            "patch_version": HEADLESS_PATCH_VERSION,
            "bundle_path": str(bundle_path),
            "backup_path": str(backup_path),
            "backup_exists": backup_path.exists(),
            "patch_installed": HEADLESS_COMMAND_ID in bundle_path.read_text(errors="replace"),
            "patch_up_to_date": HEADLESS_PATCH_MARKER
            in bundle_path.read_text(errors="replace"),
        }

    def uninstall_headless_patch(self) -> dict[str, Any]:
        bundle_path = self.paths.ai_chat_bundle_path
        backup_path = self.headless_patch_backup_path()
        if not bundle_path.exists():
            raise FileNotFoundError(f"AI chat bundle not found: {bundle_path}")
        bundle = bundle_path.read_text(errors="replace")
        if backup_path.exists():
            try:
                shutil.copy2(backup_path, bundle_path)
                backup_path.unlink()
            except OSError as exc:
                raise RuntimeError(
                    f"Unable to restore {bundle_path}: {exc}"
                ) from exc
            status = "restored"
        elif HEADLESS_COMMAND_ID in bundle:
            raise RuntimeError(
                "The headless patch marker is present but the backup file is missing."
            )
        else:
            status = "not-installed"
        return {
            "status": status,
            "command_id": HEADLESS_COMMAND_ID,
            "bundle_path": str(bundle_path),
            "backup_path": str(backup_path),
            "backup_exists": backup_path.exists(),
            "patch_installed": HEADLESS_COMMAND_ID
            in bundle_path.read_text(errors="replace"),
        }

    def headless_command_available(
        self,
        *,
        timeout_seconds: float = BRIDGE_EXTENSION_DEFAULT_TIMEOUT_SECONDS,
        workspace: Optional[Path | str] = None,
    ) -> bool:
        payload = self.list_bridge_commands(
            match=HEADLESS_COMMAND_ID,
            include_internal=True,
            timeout_seconds=timeout_seconds,
            workspace=workspace,
        )
        commands = payload.get("commands") if isinstance(payload, dict) else None
        return isinstance(commands, list) and HEADLESS_COMMAND_ID in commands

    def headless_dispatch_available(
        self,
        *,
        timeout_seconds: float = BRIDGE_EXTENSION_DEFAULT_TIMEOUT_SECONDS,
        workspace: Optional[Path | str] = None,
    ) -> bool:
        if self.read_bridge_state(workspace=workspace) is None:
            return False
        try:
            return self.headless_command_available(
                timeout_seconds=timeout_seconds,
                workspace=workspace,
            )
        except RuntimeError:
            return False

    def headless_status(
        self,
        *,
        ping: bool = False,
        timeout_seconds: float = BRIDGE_EXTENSION_DEFAULT_TIMEOUT_SECONDS,
        workspace: Optional[Path | str] = None,
    ) -> dict[str, Any]:
        normalized_workspace = self.normalize_workspace_path(workspace)
        bundle_path = self.paths.ai_chat_bundle_path
        backup_path = self.headless_patch_backup_path()
        bundle = self._load_ai_chat_bundle()
        writable = self.app_bundle_writable()
        bridge_states = self.read_bridge_states()
        bridge_state = self.read_bridge_state(workspace=workspace)
        command_available = None
        command_error = None
        if ping and bridge_state is not None:
            try:
                command_available = self.headless_command_available(
                    timeout_seconds=timeout_seconds,
                    workspace=workspace,
                )
            except RuntimeError as exc:
                command_error = str(exc)
        candidate_workspaces = self._bridge_state_candidate_workspaces(bridge_states)
        runtime_diagnostics = (
            self._collect_bridge_runtime_diagnostics() if bridge_state is None else None
        )
        notes: list[str] = []
        if normalized_workspace and bridge_states and bridge_state is None:
            notes.append(
                f"Bridge instances exist, but none match the requested workspace {normalized_workspace}."
            )
            if candidate_workspaces:
                notes.append(
                    "Active bridge workspaces: " + ", ".join(candidate_workspaces[:5])
                )
        if isinstance(runtime_diagnostics, dict):
            notes.extend(runtime_diagnostics.get("notes") or [])
        return {
            "command_id": HEADLESS_COMMAND_ID,
            "patch_version": HEADLESS_PATCH_VERSION,
            "app_path": str(self.paths.app_path),
            "bundle_path": str(bundle_path),
            "bundle_exists": bundle_path.exists(),
            "backup_path": str(backup_path),
            "backup_exists": backup_path.exists(),
            "patch_installed": HEADLESS_COMMAND_ID in bundle,
            "patch_up_to_date": HEADLESS_PATCH_MARKER in bundle,
            "writable": writable,
            "workspace": normalized_workspace,
            "bridge_state_path": str(self.paths.bridge_state_path),
            "bridge_state_count": len(bridge_states),
            "bridge_state_present": bridge_state is not None,
            "bridge_state": bridge_state,
            "candidate_workspaces": candidate_workspaces,
            "command_available": command_available,
            "command_error": command_error,
            "runtime_diagnostics": runtime_diagnostics,
            "notes": self._ordered_unique(notes),
        }

    @staticmethod
    def _normalize_headless_text(
        value: Any,
        *,
        ignored_texts: Optional[set[str]] = None,
    ) -> Optional[str]:
        if isinstance(value, str):
            text = value.strip()
        elif isinstance(value, (int, float, bool)):
            text = str(value).strip()
        else:
            return None
        if not text:
            return None
        if ignored_texts and text in ignored_texts:
            return None
        return text

    @classmethod
    def _extract_headless_value_text(
        cls,
        value: Any,
        *,
        priority_keys: tuple[str, ...] = (),
        ignored_texts: Optional[set[str]] = None,
        depth: int = 0,
        seen: Optional[set[int]] = None,
    ) -> Optional[str]:
        if depth > 6:
            return None
        text = cls._normalize_headless_text(value, ignored_texts=ignored_texts)
        if text:
            return text
        if isinstance(value, list):
            for item in value:
                text = cls._extract_headless_value_text(
                    item,
                    priority_keys=priority_keys,
                    ignored_texts=ignored_texts,
                    depth=depth + 1,
                    seen=seen,
                )
                if text:
                    return text
            return None
        if not isinstance(value, dict):
            return None
        if seen is None:
            seen = set()
        object_id = id(value)
        if object_id in seen:
            return None
        next_seen = set(seen)
        next_seen.add(object_id)
        for key in priority_keys:
            if key in value:
                text = cls._extract_headless_value_text(
                    value.get(key),
                    priority_keys=priority_keys,
                    ignored_texts=ignored_texts,
                    depth=depth + 1,
                    seen=next_seen,
                )
                if text:
                    return text
        for key, item in value.items():
            if key in priority_keys:
                continue
            text = cls._extract_headless_value_text(
                item,
                priority_keys=priority_keys,
                ignored_texts=ignored_texts,
                depth=depth + 1,
                seen=next_seen,
            )
            if text:
                return text
        return None

    @classmethod
    def _extract_headless_message_role(cls, message: Any) -> str:
        if not isinstance(message, dict):
            return ""
        return str(message.get("role") or "").strip().lower()

    @classmethod
    def _extract_headless_turn_messages(
        cls,
        messages: list[dict[str, Any]],
        *,
        prompt: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        if not isinstance(messages, list):
            return []
        normalized_prompt = cls._normalize_headless_text(prompt)
        latest_user_index: Optional[int] = None
        matching_user_index: Optional[int] = None
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if cls._extract_headless_message_role(message) != "user":
                continue
            if latest_user_index is None:
                latest_user_index = index
            if not normalized_prompt:
                matching_user_index = index
                break
            candidates = [
                cls._normalize_headless_text(message.get("content")),
                cls._normalize_headless_text(message.get("text")),
                cls._normalize_headless_text(message.get("message")),
                cls._normalize_headless_text(message.get("summary")),
            ]
            parsed_query = message.get("parsedQuery")
            if isinstance(parsed_query, list):
                candidates.extend(
                    cls._normalize_headless_text(item)
                    for item in parsed_query
                )
            if normalized_prompt in {candidate for candidate in candidates if candidate}:
                matching_user_index = index
                break
        target_index = (
            matching_user_index
            if matching_user_index is not None
            else latest_user_index
        )
        if target_index is None:
            return list(messages)
        return [
            item
            for item in messages[target_index + 1 :]
            if isinstance(item, dict)
        ]

    @staticmethod
    def _normalize_headless_status(value: Any) -> str:
        return str(value or "").strip().lower()

    @classmethod
    def _extract_headless_message_plan_items(
        cls,
        message: Any,
    ) -> list[dict[str, Any]]:
        if not isinstance(message, dict):
            return []
        plan_items: list[dict[str, Any]] = []
        agent_task = message.get("agentTaskContent")
        if isinstance(agent_task, dict):
            guideline = agent_task.get("guideline")
            raw_plan_items = guideline.get("planItems") if isinstance(guideline, dict) else None
            if isinstance(raw_plan_items, list):
                plan_items.extend(
                    item for item in raw_plan_items if isinstance(item, dict)
                )
        content = message.get("content")
        if isinstance(content, dict):
            raw_messages = content.get("messages")
            if isinstance(raw_messages, list):
                for item in raw_messages:
                    if not isinstance(item, dict):
                        continue
                    candidate = item.get("plan_item") or item.get("planItem")
                    if isinstance(candidate, dict):
                        plan_items.append(candidate)
            raw_plan_items = content.get("planItems")
            if isinstance(raw_plan_items, list):
                plan_items.extend(
                    item for item in raw_plan_items if isinstance(item, dict)
                )
        return plan_items

    @classmethod
    def _headless_plan_item_pending(cls, plan_item: Any) -> bool:
        if not isinstance(plan_item, dict):
            return False
        tool_call_info = plan_item.get("tool_call_info")
        if not isinstance(tool_call_info, dict):
            tool_call_info = plan_item.get("toolCallInfo")
        statuses = [
            plan_item.get("status"),
            plan_item.get("state"),
            tool_call_info.get("status") if isinstance(tool_call_info, dict) else None,
            (tool_call_info.get("result") or {}).get("status")
            if isinstance(tool_call_info, dict)
            and isinstance(tool_call_info.get("result"), dict)
            else None,
            (plan_item.get("result") or {}).get("status")
            if isinstance(plan_item.get("result"), dict)
            else None,
        ]
        return any(
            cls._normalize_headless_status(status) in HEADLESS_RUNNING_STATUSES
            for status in statuses
            if status is not None
        )

    @classmethod
    def _headless_message_pending(cls, message: Any) -> bool:
        if not isinstance(message, dict):
            return True
        if cls._normalize_headless_status(message.get("status")) in HEADLESS_RUNNING_STATUSES:
            return True
        return any(
            cls._headless_plan_item_pending(plan_item)
            for plan_item in cls._extract_headless_message_plan_items(message)
        )

    @classmethod
    def _extract_headless_latest_assistant_message(
        cls,
        messages: list[dict[str, Any]],
        *,
        prompt: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        turn_messages = cls._extract_headless_turn_messages(messages, prompt=prompt)
        assistant_messages = [
            item
            for item in turn_messages
            if cls._extract_headless_message_role(item) in {"assistant", "model"}
        ]
        if assistant_messages:
            return assistant_messages[-1]
        return None

    @classmethod
    def _extract_headless_plan_item_payload(
        cls,
        plan_item: Any,
        *,
        ignored_texts: Optional[set[str]] = None,
    ) -> tuple[Optional[str], Optional[str]]:
        if not isinstance(plan_item, dict):
            return None, None
        tool_name = str(
            plan_item.get("toolName")
            or (plan_item.get("tool_call_info") or {}).get("name")
            or ""
        ).strip()
        normalized_tool_name = tool_name.lower()
        tool_call_info = plan_item.get("tool_call_info")
        if not isinstance(tool_call_info, dict):
            tool_call_info = plan_item.get("toolCallInfo")
        preferred_keys = (
            "summary",
            "content",
            "text",
            "message",
            "answer",
            "response",
            "final_answer",
            "output",
        )
        final_tools = {"finish", "response_to_user", "agent_finish"}

        def candidate(value: Any, source: str) -> tuple[Optional[str], Optional[str]]:
            text = cls._extract_headless_value_text(
                value,
                priority_keys=preferred_keys,
                ignored_texts=ignored_texts,
            )
            if text:
                return text, source
            return None, None

        if normalized_tool_name in final_tools:
            for params_source, params in (
                ("params", plan_item.get("params")),
                ("tool_call_info.params", tool_call_info.get("params") if isinstance(tool_call_info, dict) else None),
            ):
                if isinstance(params, dict):
                    for key in preferred_keys:
                        text, source = candidate(
                            params.get(key),
                            f"plan_item.{normalized_tool_name}.{params_source}.{key}",
                        )
                        if text:
                            return text, source
                    text, source = candidate(
                        params,
                        f"plan_item.{normalized_tool_name}.{params_source}",
                    )
                    if text:
                        return text, source
            for key in ("thought", "summary", "content", "text", "message", "answer", "response"):
                text, source = candidate(
                    plan_item.get(key),
                    f"plan_item.{normalized_tool_name}.{key}",
                )
                if text:
                    return text, source
            for result_source, result in (
                ("result", plan_item.get("result")),
                ("tool_call_info.result", tool_call_info.get("result") if isinstance(tool_call_info, dict) else None),
            ):
                if isinstance(result, dict):
                    for key in ("summary", "content", "text", "message", "answer", "response", "data"):
                        text, source = candidate(
                            result.get(key),
                            f"plan_item.{normalized_tool_name}.{result_source}.{key}",
                        )
                        if text:
                            return text, source

        for key in ("thought", "summary", "content", "text", "message", "answer", "response"):
            text, source = candidate(
                plan_item.get(key),
                f"plan_item.{normalized_tool_name or 'unknown'}.{key}",
            )
            if text:
                return text, source
        return None, None

    @classmethod
    def _extract_headless_message_payload(
        cls,
        message: Any,
        *,
        ignored_texts: Optional[set[str]] = None,
    ) -> tuple[Optional[str], Optional[str]]:
        if not isinstance(message, dict):
            return None, None
        preferred_keys = (
            "summary",
            "content",
            "text",
            "message",
            "answer",
            "response",
            "final_answer",
            "output",
        )

        def candidate(value: Any, source: str) -> tuple[Optional[str], Optional[str]]:
            text = cls._extract_headless_value_text(
                value,
                priority_keys=preferred_keys,
                ignored_texts=ignored_texts,
            )
            if text:
                return text, source
            return None, None

        content = message.get("content")
        if isinstance(content, dict):
            for key in preferred_keys:
                text, source = candidate(content.get(key), f"message.content.{key}")
                if text:
                    return text, source
        plan_items = cls._extract_headless_message_plan_items(message)
        if plan_items:
            for plan_item in reversed(plan_items):
                text, source = cls._extract_headless_plan_item_payload(
                    plan_item,
                    ignored_texts=ignored_texts,
                )
                if text and source and source.startswith(
                    ("plan_item.finish", "plan_item.response_to_user", "plan_item.agent_finish")
                ):
                    return text, source
            for plan_item in reversed(plan_items):
                text, source = cls._extract_headless_plan_item_payload(
                    plan_item,
                    ignored_texts=ignored_texts,
                )
                if text:
                    return text, source
        for key in ("text", "message", "summary", "answer"):
            text, source = candidate(message.get(key), f"message.{key}")
            if text:
                return text, source
        agent_task = message.get("agentTaskContent")
        if isinstance(agent_task, dict):
            for key in ("proposal", "summary", "content", "answer"):
                text, source = candidate(agent_task.get(key), f"agentTaskContent.{key}")
                if text:
                    return text, source
        if not isinstance(content, dict):
            text, source = candidate(content, "message.content")
            if text:
                return text, source
        return None, None

    @classmethod
    def _extract_headless_answer_payload(
        cls,
        result: Any,
        *,
        prompt: Optional[str] = None,
    ) -> tuple[Optional[str], Optional[str]]:
        if not isinstance(result, dict):
            return None, None
        ignored_texts = {
            prompt_text
            for prompt_text in [cls._normalize_headless_text(prompt)]
            if prompt_text
        }
        direct_answer = cls._normalize_headless_text(
            result.get("answerText"),
            ignored_texts=ignored_texts,
        )
        if direct_answer:
            direct_source = cls._normalize_headless_text(result.get("answerSource"))
            return direct_answer, direct_source or "result.answerText"
        unstable_fields = result.get("unstableFields")
        session = unstable_fields.get("session") if isinstance(unstable_fields, dict) else None
        messages = session.get("messages") if isinstance(session, dict) else None
        if not isinstance(messages, list):
            return None, None
        assistant_candidates = [
            item
            for item in cls._extract_headless_turn_messages(messages, prompt=prompt)
            if cls._extract_headless_message_role(item) in {"assistant", "model"}
        ]
        for message in reversed(assistant_candidates):
            text, source = cls._extract_headless_message_payload(
                message,
                ignored_texts=ignored_texts,
            )
            if text:
                return text, source
        return None, None

    @classmethod
    def _extract_headless_answer_text(
        cls,
        result: Any,
        *,
        prompt: Optional[str] = None,
    ) -> Optional[str]:
        text, _source = cls._extract_headless_answer_payload(result, prompt=prompt)
        return text

    def invoke_headless_chat(
        self,
        prompt: str,
        *,
        session_id: Optional[str] = None,
        timeout_seconds: float = 30.0,
        workspace: Optional[Path | str] = None,
    ) -> dict[str, Any]:
        started_at = time.monotonic()
        options: dict[str, Any] = {}
        if session_id:
            options["sessionId"] = session_id
        normalized_workspace = self.normalize_workspace_path(workspace)
        project_payload: Optional[dict[str, Any]] = None
        if normalized_workspace:
            options["workspaceFolder"] = normalized_workspace
            project_payload = self.create_rpc_project(
                normalized_workspace,
                workspace=normalized_workspace,
            )
            options["projectId"] = project_payload["project_id"]
            if project_payload.get("real_project_id"):
                options["realProjectId"] = project_payload["real_project_id"]
        payload = self.execute_bridge_command(
            HEADLESS_COMMAND_ID,
            args=[[prompt], options],
            timeout_seconds=timeout_seconds,
            workspace=workspace,
        )
        result = payload.get("result") if isinstance(payload, dict) else None
        unstable_fields = result.get("unstableFields") if isinstance(result, dict) else None
        session = unstable_fields.get("session") if isinstance(unstable_fields, dict) else None
        session_messages = session.get("messages") if isinstance(session, dict) else None
        answer_text, answer_source = self._extract_headless_answer_payload(
            result,
            prompt=prompt,
        )
        current_turn_assistant = self._extract_headless_latest_assistant_message(
            session_messages if isinstance(session_messages, list) else [],
            prompt=prompt,
        )
        polled_messages_payload = None
        resolved_session_id = result.get("sessionId") if isinstance(result, dict) else None
        should_poll_messages = (
            normalized_workspace is not None
            and isinstance(project_payload, dict)
            and isinstance(resolved_session_id, str)
            and bool(resolved_session_id.strip())
            and (
                answer_text is None
                or current_turn_assistant is None
                or self._headless_message_pending(current_turn_assistant)
            )
        )
        if should_poll_messages:
            remaining_seconds = max(
                0.0,
                timeout_seconds - (time.monotonic() - started_at),
            )
            if remaining_seconds > 0:
                bridge_state = self.read_bridge_state(workspace=normalized_workspace)
                connect_session_id = self._string_or_empty(
                    bridge_state.get("connect_session_id")
                    if isinstance(bridge_state, dict)
                    else ""
                )
                if not connect_session_id and isinstance(bridge_state, dict):
                    manager_exchange = bridge_state.get("manager_exchange")
                    if isinstance(manager_exchange, dict):
                        connect_session_id = self._string_or_empty(
                            manager_exchange.get("connect_session_id")
                        )
                if not connect_session_id and isinstance(bridge_state, dict):
                    connect_session_id = self._string_or_empty(
                        bridge_state.get("ai_session_id")
                    )
                if not connect_session_id:
                    guessed_connect = self.guess_recent_connect_session(service="chat")
                    if isinstance(guessed_connect, dict):
                        connect_session_id = self._string_or_empty(
                            guessed_connect.get("connect_session_id")
                        )
                try:
                    polled_messages_payload = self.wait_for_headless_messages(
                        resolved_session_id,
                        project_id=project_payload["project_id"],
                        connect_session_id=connect_session_id,
                        workspace=normalized_workspace,
                        prompt=prompt,
                        wait_seconds=remaining_seconds,
                    )
                except RuntimeError:
                    polled_messages_payload = None
                if isinstance(polled_messages_payload, dict):
                    polled_answer_text = self._normalize_headless_text(
                        polled_messages_payload.get("answer_text")
                    )
                    polled_answer_source = self._normalize_headless_text(
                        polled_messages_payload.get("answer_source")
                    )
                    if polled_answer_text:
                        answer_text = polled_answer_text
                        answer_source = polled_answer_source or answer_source
                    if isinstance(session, dict):
                        merged_session = dict(session)
                        merged_session["messages"] = polled_messages_payload.get("messages") or []
                        if polled_messages_payload.get("message_count") is not None:
                            merged_session["totalCount"] = polled_messages_payload.get("message_count")
                        session = merged_session
        return {
            "command_id": HEADLESS_COMMAND_ID,
            "bridge": payload,
            "result": result,
            "session": session,
            "session_id": resolved_session_id,
            "request_message_id": (
                result.get("requestMessageId") if isinstance(result, dict) else None
            ),
            "answer_source": answer_source,
            "answer_text": answer_text,
            "messages_poll": polled_messages_payload,
        }

    def _rpc_bridge_script_path(self) -> Path:
        return Path(__file__).with_name("trae_aha_rpc_bridge.js")

    def _cdp_bridge_script_path(self) -> Path:
        return Path(__file__).with_name("trae_cdp_bridge.js")

    def _bridge_extension_source_dir(self) -> Path:
        return Path(__file__).with_name(BRIDGE_EXTENSION_SOURCE_DIR_NAME)

    @staticmethod
    def _extract_runtime_dir_from_ipc_address(address: Optional[str]) -> Optional[str]:
        if not address or not address.startswith("ipc://"):
            return None
        raw_path = address[len("ipc://") :]
        if not raw_path:
            return None
        socket_dir = Path(raw_path).parent
        if socket_dir.name == "aha" and socket_dir.parent != socket_dir:
            return str(socket_dir.parent)
        return str(socket_dir)

    def resolve_aha_runtime_dir(self, *, service_name: str = "ai-agent") -> str:
        topology = self.extract_transport_topology()
        observed = topology.get("app_rpc", {}).get("observed_ipc_addresses") or []
        acceptable_services = {service_name, service_name.replace("-", "_")}
        for item in observed:
            if item.get("service") not in acceptable_services:
                continue
            runtime_dir = self._extract_runtime_dir_from_ipc_address(item.get("address"))
            if runtime_dir and Path(runtime_dir).exists():
                return runtime_dir
        node_socket_rule = topology.get("app_rpc", {}).get("node_socket_rule") or {}
        runtime_dir = node_socket_rule.get("runtime_dir")
        if isinstance(runtime_dir, str) and runtime_dir.strip():
            candidate_runtime_dir = Path(runtime_dir).expanduser()
            candidate_aha_dir = candidate_runtime_dir / "aha"
            socket_name = f"{self._sanitize_ipc_name(service_name)}.sock"
            socket_path = candidate_aha_dir / socket_name
            marker_path = candidate_aha_dir / f"{socket_name}.ready"
            if candidate_aha_dir.exists() and (
                socket_path.exists() or marker_path.exists()
            ):
                return str(candidate_runtime_dir)
        support_aha_dir = self.paths.support_dir / "aha"
        if support_aha_dir.exists():
            return str(self.paths.support_dir)
        return str(Path("/tmp"))

    def guess_recent_connect_session(
        self,
        *,
        service: str = "chat",
        limit: int = 50,
        log_session_limit: int = 20,
    ) -> Optional[dict[str, Any]]:
        candidate_sessions: list[Path] = []
        seen_sessions: set[Path] = set()
        for candidate in [self.latest_log_session_dir(), *reversed(self.list_log_session_dirs())]:
            if candidate is None:
                continue
            resolved_candidate = candidate.expanduser().resolve()
            if resolved_candidate in seen_sessions:
                continue
            seen_sessions.add(resolved_candidate)
            candidate_sessions.append(resolved_candidate)
            if len(candidate_sessions) >= max(log_session_limit, 1):
                break

        def first_connect_from_traces(
            traces: list[dict[str, Any]],
        ) -> Optional[dict[str, Any]]:
            for item in traces:
                connect_session_id = item.get("connect_session_id")
                if not connect_session_id:
                    continue
                return {
                    "connect_session_id": connect_session_id,
                    "trace_id": item.get("trace_id"),
                    "service": item.get("service"),
                    "method": item.get("method"),
                    "source_logs": item.get("source_logs") or [],
                    "timestamp": item.get("timestamp") or item.get("last_seen_at"),
                }
            return None

        for session_dir in candidate_sessions:
            service_traces = (
                self.list_rpc_traces(
                    limit=limit,
                    service=service,
                    log_session_dir=session_dir,
                ).get("traces")
                or []
            )
            resolved = first_connect_from_traces(service_traces)
            if resolved is not None:
                return resolved

            traces = (
                self.list_rpc_traces(
                    limit=limit,
                    log_session_dir=session_dir,
                ).get("traces")
                or []
            )
            resolved = first_connect_from_traces(traces)
            if resolved is not None:
                return resolved

        topology = self.extract_transport_topology()
        sample_request = topology.get("sample_request") or {}
        connect_session_id = sample_request.get("connect_session_id")
        if connect_session_id:
            return {
                "connect_session_id": connect_session_id,
                "trace_id": None,
                "service": sample_request.get("service"),
                "method": sample_request.get("method"),
                "source_logs": [sample_request.get("source_log")]
                if sample_request.get("source_log")
                else [],
                "timestamp": sample_request.get("timestamp"),
            }
        return None

    def build_rpc_request_envelope(
        self,
        *,
        service: str,
        method: str,
        data: Any = "",
        connect_session_id: str = "",
        client_info: Optional[dict[str, Any]] = None,
        session_id: Optional[str] = None,
        channel_id: Optional[str] = None,
        common_params: Optional[dict[str, Any]] = None,
        user_info: Optional[dict[str, Any]] = None,
        streamlined_common_params: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        effective_connect_session_id = connect_session_id or ""
        effective_client_info = self.build_rpc_client_info(
            connect_session_id=effective_connect_session_id,
            client_info=client_info,
        )
        effective_user_info = {
            **DEFAULT_RPC_USER_INFO,
            **(user_info or {}),
        }
        return {
            "packet_type": "request",
            "session_id": session_id
            if session_id is not None
            else effective_connect_session_id,
            "channel_id": channel_id or str(uuid.uuid4()),
            "params": {
                "service": service,
                "method": method,
                "data": "" if data is None else data,
                "common_params": common_params or {},
                "user_info": effective_user_info,
                "streamlined_common_params": streamlined_common_params or {},
                "client_info": effective_client_info,
            },
        }

    def build_rpc_client_info(
        self,
        *,
        connect_session_id: str = "",
        client_info: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        effective_client_info = dict(client_info or {})
        fallback_connect_session_id = self._string_or_empty(
            effective_client_info.get("connect_session_id")
        )
        effective_client_info["connect_session_id"] = (
            connect_session_id or fallback_connect_session_id
        )
        return effective_client_info

    def invoke_aha_rpc(
        self,
        *,
        service: str,
        method: str,
        data: Any = "",
        connect_session_id: str = "",
        client_info: Optional[dict[str, Any]] = None,
        session_id: Optional[str] = None,
        channel_id: Optional[str] = None,
        common_params: Optional[dict[str, Any]] = None,
        user_info: Optional[dict[str, Any]] = None,
        streamlined_common_params: Optional[dict[str, Any]] = None,
        service_name: str = "ai-agent",
        runtime_dir: Optional[str] = None,
        timeout_ms: int = 10000,
        workspace: Optional[Path | str] = None,
    ) -> dict[str, Any]:
        resolved_user_info = self.build_rpc_user_info(user_info)
        envelope = self.build_rpc_request_envelope(
            service=service,
            method=method,
            data=data,
            connect_session_id=connect_session_id,
            client_info=client_info,
            session_id=session_id,
            channel_id=channel_id,
            common_params=common_params,
            user_info=resolved_user_info,
            streamlined_common_params=streamlined_common_params,
        )
        topology = self.extract_transport_topology()
        app_rpc = topology.get("app_rpc") or {}
        local_impl = app_rpc.get("local_impl") or {}
        client_api = str(local_impl.get("client_api") or "").strip().lower()
        resolved_runtime_dir = runtime_dir or self.resolve_aha_runtime_dir(
            service_name=service_name
        )
        bridge_error: Optional[str] = None

        if client_api.startswith("electron.ahaipc.connect"):
            try:
                helper_payload = self.invoke_bridge_aha_rpc(
                    envelope=envelope,
                    service_name=service_name,
                    request_method="request",
                    timeout_ms=timeout_ms,
                    workspace=workspace,
                )
            except RuntimeError as exc:
                bridge_error = str(exc)
            else:
                return {
                    "service_name": service_name,
                    "runtime_dir": resolved_runtime_dir,
                    "request_method": "request",
                    "envelope": envelope,
                    "response": helper_payload.get("result"),
                    "bridge": {
                        "exit_code": 0,
                        "stderr": "",
                        "meta": helper_payload.get("meta"),
                    },
                }

        node_bin = shutil.which("node")
        if node_bin is None:
            message = "`node` is required for AHA RPC bridge calls."
            if bridge_error:
                message += f" Bridge attempt failed first: {bridge_error}"
            raise RuntimeError(message)

        bridge_path = self._rpc_bridge_script_path()
        if not bridge_path.exists():
            raise RuntimeError(f"AHA RPC bridge script not found: {bridge_path}")

        bridge_payload = {
            "app_path": str(self.paths.app_path),
            "service_name": service_name,
            "runtime_dir": resolved_runtime_dir,
            "request_method": "request",
            "timeout_ms": timeout_ms,
            "envelope": envelope,
        }
        result = subprocess.run(
            [node_bin, str(bridge_path)],
            input=json.dumps(bridge_payload, ensure_ascii=False),
            text=True,
            capture_output=True,
            check=False,
        )
        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()
        helper_payload = self._safe_json_value(stdout)
        if not isinstance(helper_payload, dict):
            detail = stderr or stdout or "empty stdout"
            message = (
                "Trae AHA RPC bridge returned invalid JSON "
                f"(exit {result.returncode}): {detail}"
            )
            if bridge_error:
                message += f" Bridge attempt failed first: {bridge_error}"
            raise RuntimeError(message)
        if result.returncode != 0 or not helper_payload.get("ok"):
            error = helper_payload.get("error") or {}
            message = error.get("message")
            if not isinstance(message, str) or not message.strip():
                try:
                    message = json.dumps(error, ensure_ascii=False)
                except TypeError:
                    message = str(error)
            message = message or stderr or "AHA RPC bridge failed."
            if bridge_error:
                message += f" Bridge attempt failed first: {bridge_error}"
            raise RuntimeError(message)
        return {
            "service_name": service_name,
            "runtime_dir": resolved_runtime_dir,
            "request_method": "request",
            "envelope": envelope,
            "response": helper_payload.get("result"),
            "bridge": {
                "exit_code": result.returncode,
                "stderr": stderr,
                "meta": helper_payload.get("meta"),
            },
        }

    @staticmethod
    def _extract_rpc_response_payload(
        response: Any,
    ) -> Optional[dict[str, Any]]:
        if not isinstance(response, dict):
            return None
        params = response.get("params")
        if isinstance(params, dict):
            return params
        return response

    @classmethod
    def _extract_successful_rpc_response_data(
        cls,
        rpc_payload: dict[str, Any],
        *,
        service: str,
        method: str,
    ) -> dict[str, Any]:
        response = cls._extract_rpc_response_payload(rpc_payload.get("response"))
        if not isinstance(response, dict):
            raise RuntimeError(
                f"Trae {service}.{method} RPC returned an unexpected response payload."
            )
        if response.get("code") != 0:
            raise RuntimeError(
                str(response.get("message") or f"{service}.{method} failed.")
            )
        data = response.get("data")
        if not isinstance(data, dict):
            raise RuntimeError(
                f"Trae {service}.{method} RPC returned an unexpected response data payload."
            )
        return data

    def create_rpc_project(
        self,
        workspace_path: Optional[Path | str] = None,
        *,
        connect_session_id: str = "",
        runtime_dir: Optional[str] = None,
        timeout_ms: int = 10000,
        workspace: Optional[Path | str] = None,
        user_info: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        normalized_workspace = self.normalize_workspace_path(
            workspace_path or workspace or os.getcwd()
        )
        if not normalized_workspace:
            raise RuntimeError("A workspace path is required to create a Trae project.")

        rpc_payload = self.invoke_aha_rpc(
            service="project",
            method="create_project",
            data={"biz_project_id": normalized_workspace},
            connect_session_id=connect_session_id,
            runtime_dir=runtime_dir,
            timeout_ms=timeout_ms,
            workspace=workspace,
            user_info=user_info,
        )
        response_data = self._extract_successful_rpc_response_data(
            rpc_payload,
            service="project",
            method="create_project",
        )
        project_id = str(
            response_data.get("project_id") or response_data.get("real_project_id") or ""
        ).strip()
        if not project_id:
            raise RuntimeError("Trae project.create_project did not return a project_id.")
        return {
            "workspace": normalized_workspace,
            "project_id": project_id,
            "real_project_id": str(
                response_data.get("real_project_id") or project_id
            ).strip(),
            "response_data": response_data,
            "request": rpc_payload,
            "response": self._extract_rpc_response_payload(rpc_payload.get("response")),
            "response_packet": rpc_payload.get("response"),
        }

    def create_rpc_session(
        self,
        project_id: str,
        *,
        session_type: str = "inline_chat",
        connect_session_id: str = "",
        runtime_dir: Optional[str] = None,
        timeout_ms: int = 10000,
        workspace: Optional[Path | str] = None,
        user_info: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        normalized_project_id = str(project_id).strip()
        if not normalized_project_id:
            raise RuntimeError("A project_id is required to create a Trae chat session.")
        if session_type not in RPC_CHAT_SESSION_TYPES:
            valid = ", ".join(sorted(RPC_CHAT_SESSION_TYPES))
            raise RuntimeError(f"Unsupported session_type `{session_type}`. Expected one of: {valid}.")

        rpc_payload = self.invoke_aha_rpc(
            service="chat",
            method="create_session",
            data={
                "project_id": normalized_project_id,
                "session_type": session_type,
            },
            connect_session_id=connect_session_id,
            runtime_dir=runtime_dir,
            timeout_ms=timeout_ms,
            workspace=workspace,
            user_info=user_info,
        )
        response_data = self._extract_successful_rpc_response_data(
            rpc_payload,
            service="chat",
            method="create_session",
        )
        session_record = (
            response_data.get("session", {})
            if isinstance(response_data.get("session"), dict)
            else {}
        )
        session_id = str(
            response_data.get("session_id")
            or response_data.get("id")
            or response_data.get("sessionId")
            or session_record.get("session_id")
            or session_record.get("id")
            or session_record.get("sessionId")
            or ""
        ).strip()
        if not session_id:
            raise RuntimeError("Trae chat.create_session did not return a session_id.")
        return {
            "project_id": normalized_project_id,
            "session_type": session_type,
            "session_id": session_id,
            "session": session_record or None,
            "response_data": response_data,
            "request": rpc_payload,
            "response": self._extract_rpc_response_payload(rpc_payload.get("response")),
            "response_packet": rpc_payload.get("response"),
        }

    @staticmethod
    def _selected_model_config_name(selected_model: Optional[dict[str, Any]]) -> str:
        if not isinstance(selected_model, dict):
            return ""
        raw_name = str(selected_model.get("name") or "").strip()
        if raw_name:
            return raw_name.split("//")[-1].strip()
        return str(selected_model.get("display_name") or "").strip()

    def build_rpc_chat_model_info(
        self,
        *,
        model_name: Optional[str] = None,
        custom_model: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        model_state = self.read_model_state(sanitize=False)
        current_models = (
            model_state.get("current_models")
            if isinstance(model_state.get("current_models"), dict)
            else {}
        )
        selected_model_entry = (
            current_models.get("dev_builder")
            if isinstance(current_models.get("dev_builder"), dict)
            else {}
        )
        selected_model = (
            selected_model_entry.get("model")
            if isinstance(selected_model_entry.get("model"), dict)
            else {}
        )
        if not selected_model and isinstance(model_state.get("selected_model"), dict):
            selected_model = model_state.get("selected_model") or {}
        resolved_model_name = self._string_or_empty(
            model_name or self._selected_model_config_name(selected_model)
        )
        if not resolved_model_name:
            raise RuntimeError(
                "No RPC chat model could be resolved. Pass `model_name` explicitly."
            )
        resolved_custom_model = {
            "provider": self._string_or_empty(selected_model.get("provider")),
            "config_name": resolved_model_name,
            "display_model_name": self._string_or_empty(
                selected_model.get("display_name") or resolved_model_name
            ),
            "multimodal": bool(selected_model.get("multimodal") or False),
            "ak": self._string_or_empty(selected_model.get("ak")),
            "use_remote_service": not bool(selected_model.get("client_connect")),
            "is_preset": (
                bool(selected_model.get("is_preset"))
                if selected_model.get("is_preset") is not None
                else True
            ),
            "config_source": self._parse_optional_int(selected_model.get("config_source"))
            or 1,
            "base_url": self._string_or_empty(selected_model.get("base_url")),
            "region": None,
            "sk": self._string_or_empty(selected_model.get("sk")),
            "auth_type": self._parse_optional_int(selected_model.get("auth_type")) or 0,
        }
        if custom_model:
            resolved_custom_model.update(custom_model)
        if not resolved_custom_model.get("config_name"):
            resolved_custom_model["config_name"] = resolved_model_name
        if not resolved_custom_model.get("display_model_name"):
            resolved_custom_model["display_model_name"] = resolved_model_name
        return {
            "model_name": resolved_model_name,
            "custom_model": resolved_custom_model,
            "selected_model": selected_model or None,
        }

    def _rpc_chat_scene_location(
        self,
        *,
        session_type: str,
        scene_location: Optional[int] = None,
    ) -> int:
        if scene_location is not None:
            return int(scene_location)
        return RPC_CHAT_SCENE_LOCATION_BY_SESSION_TYPE.get(session_type, 1)

    def build_rpc_chat_request_data(
        self,
        prompt: str,
        *,
        session_id: str,
        workspace: Optional[Path | str] = None,
        session_type: str = "inline_chat",
        agent_type: Optional[str] = None,
        message_id: Optional[str] = None,
        model_name: Optional[str] = None,
        custom_model: Optional[dict[str, Any]] = None,
        scene_location: Optional[int] = None,
        extra_data: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        resolved_prompt = str(prompt or "").strip()
        resolved_session_id = str(session_id or "").strip()
        if not resolved_prompt:
            raise RuntimeError("A prompt is required to send a Trae RPC chat message.")
        if not resolved_session_id:
            raise RuntimeError("A session_id is required to send a Trae RPC chat message.")
        model_info = self.build_rpc_chat_model_info(
            model_name=model_name,
            custom_model=custom_model,
        )
        normalized_workspace = self.normalize_workspace_path(workspace)
        workspace_context = self.build_workspace_context(workspace=normalized_workspace)
        extra_agent_type = ""
        if isinstance(extra_data, dict):
            extra_agent_type = self._string_or_empty(extra_data.get("agent_type"))
        resolved_agent_type = (
            self._string_or_empty(agent_type)
            or extra_agent_type
            or RPC_CHAT_DEFAULT_AGENT_TYPE_BY_SESSION_TYPE.get(session_type, "inline_chat")
        )
        payload = {
            "agent_type": resolved_agent_type,
            "session_id": resolved_session_id,
            "message_id": str(message_id or uuid.uuid4().hex[:24]),
            "mention_context": {},
            "model_name": model_info["model_name"],
            "custom_model": model_info["custom_model"],
            "terminal_context": [],
            "message_content": [
                {
                    "type": "text",
                    "text_content": resolved_prompt,
                }
            ],
            "code_selections": [],
            "scene_location": self._rpc_chat_scene_location(
                session_type=session_type,
                scene_location=scene_location,
            ),
            "parsed_query": [],
            "multi_media": [],
            "workspace_folders": workspace_context.get("workspace_folders") or [],
        }
        for key in (
            "active_text_editor",
            "original_workspace_folders",
            "is_workspace_folder_changed",
        ):
            if key in workspace_context:
                payload[key] = workspace_context[key]
        if extra_data:
            payload.update({key: value for key, value in extra_data.items() if key != "agent_type"})
            if extra_agent_type and not self._string_or_empty(agent_type):
                payload["agent_type"] = extra_agent_type
        return payload

    @staticmethod
    def _extract_rpc_response_messages(response_data: dict[str, Any]) -> list[dict[str, Any]]:
        candidates = response_data.get("messages")
        if not isinstance(candidates, list):
            session = response_data.get("session")
            if isinstance(session, dict):
                candidates = session.get("messages")
        if not isinstance(candidates, list):
            nested = response_data.get("data")
            if isinstance(nested, dict):
                return TraeBackend._extract_rpc_response_messages(nested)
            return []
        return [item for item in candidates if isinstance(item, dict)]

    @staticmethod
    def _extract_rpc_message_role(message: Optional[dict[str, Any]]) -> str:
        if not isinstance(message, dict):
            return ""
        return str(
            message.get("role")
            or message.get("message_role")
            or message.get("sender_role")
            or message.get("author_role")
            or ""
        ).strip().lower()

    @staticmethod
    def _extract_rpc_message_status(message: Optional[dict[str, Any]]) -> Optional[str]:
        if not isinstance(message, dict):
            return None
        status = str(
            message.get("status")
            or message.get("message_status")
            or message.get("state")
            or ""
        ).strip().lower()
        return status or None

    def _extract_rpc_latest_assistant_message(
        self,
        messages: list[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        assistant_messages = [
            item
            for item in messages
            if self._extract_rpc_message_role(item) in {"assistant", "model"}
        ]
        if assistant_messages:
            return assistant_messages[-1]
        return messages[-1] if messages else None

    def _extract_rpc_chat_answer(
        self,
        messages: list[dict[str, Any]],
        *,
        prompt: Optional[str] = None,
    ) -> tuple[Optional[str], Optional[str]]:
        pseudo_result = {
            "unstableFields": {
                "session": {
                    "messages": messages,
                }
            }
        }
        return self._extract_headless_answer_payload(
            pseudo_result,
            prompt=prompt,
        )

    def get_rpc_messages(
        self,
        session_id: str,
        *,
        project_id: str,
        connect_session_id: str = "",
        client_info: Optional[dict[str, Any]] = None,
        runtime_dir: Optional[str] = None,
        timeout_ms: int = 10000,
        workspace: Optional[Path | str] = None,
        user_info: Optional[dict[str, Any]] = None,
        page_size: int = 20,
        next_page_token: Optional[str] = None,
        prompt: Optional[str] = None,
    ) -> dict[str, Any]:
        normalized_project_id = str(project_id or "").strip()
        normalized_session_id = str(session_id or "").strip()
        if not normalized_project_id:
            raise RuntimeError("A project_id is required to fetch Trae RPC chat messages.")
        if not normalized_session_id:
            raise RuntimeError("A session_id is required to fetch Trae RPC chat messages.")

        data = {
            "session_id": normalized_session_id,
            "project_id": normalized_project_id,
            "page_size": int(page_size),
        }
        if next_page_token:
            data["next_page_token"] = next_page_token

        rpc_payload = self.invoke_aha_rpc(
            service="chat",
            method="get_messages",
            data=data,
            connect_session_id=connect_session_id,
            client_info=client_info,
            runtime_dir=runtime_dir,
            timeout_ms=timeout_ms,
            workspace=workspace,
            user_info=user_info,
        )
        response_data = self._extract_successful_rpc_response_data(
            rpc_payload,
            service="chat",
            method="get_messages",
        )
        messages = self._extract_rpc_response_messages(response_data)
        latest_assistant_message = self._extract_rpc_latest_assistant_message(messages)
        assistant_status = self._extract_rpc_message_status(latest_assistant_message)
        answer_text, answer_source = self._extract_rpc_chat_answer(
            messages,
            prompt=prompt,
        )
        current_turn_assistant = self._extract_headless_latest_assistant_message(
            messages,
            prompt=prompt,
        )
        return {
            "project_id": normalized_project_id,
            "session_id": normalized_session_id,
            "messages": messages,
            "message_count": len(messages),
            "next_page_token": response_data.get("next_page_token"),
            "assistant_status": assistant_status,
            "latest_assistant_message": latest_assistant_message,
            "current_turn_assistant_message": current_turn_assistant,
            "assistant_pending": self._headless_message_pending(current_turn_assistant),
            "answer_text": answer_text,
            "answer_source": answer_source,
            "response_data": response_data,
            "request": rpc_payload,
            "response": self._extract_rpc_response_payload(rpc_payload.get("response")),
            "response_packet": rpc_payload.get("response"),
        }

    def wait_for_headless_messages(
        self,
        session_id: str,
        *,
        project_id: str,
        connect_session_id: str = "",
        client_info: Optional[dict[str, Any]] = None,
        runtime_dir: Optional[str] = None,
        timeout_ms: int = 10000,
        workspace: Optional[Path | str] = None,
        user_info: Optional[dict[str, Any]] = None,
        page_size: int = 20,
        next_page_token: Optional[str] = None,
        prompt: Optional[str] = None,
        wait_seconds: float = 0.0,
        poll_interval: float = 0.75,
    ) -> dict[str, Any]:
        payload = self.get_rpc_messages(
            session_id,
            project_id=project_id,
            connect_session_id=connect_session_id,
            client_info=client_info,
            runtime_dir=runtime_dir,
            timeout_ms=timeout_ms,
            workspace=workspace,
            user_info=user_info,
            page_size=page_size,
            next_page_token=next_page_token,
            prompt=prompt,
        )
        deadline = time.monotonic() + max(wait_seconds, 0.0)
        effective_poll_interval = max(poll_interval, 0.1)
        while time.monotonic() < deadline:
            if payload.get("answer_text") and not payload.get("assistant_pending"):
                break
            if payload.get("current_turn_assistant_message") and not payload.get("assistant_pending"):
                break
            time.sleep(min(effective_poll_interval, max(0.0, deadline - time.monotonic())))
            payload = self.get_rpc_messages(
                session_id,
                project_id=project_id,
                connect_session_id=connect_session_id,
                client_info=client_info,
                runtime_dir=runtime_dir,
                timeout_ms=timeout_ms,
                workspace=workspace,
                user_info=user_info,
                page_size=page_size,
                next_page_token=next_page_token,
                prompt=prompt,
            )
        return payload

    def wait_for_rpc_messages(
        self,
        session_id: str,
        *,
        project_id: str,
        connect_session_id: str = "",
        client_info: Optional[dict[str, Any]] = None,
        runtime_dir: Optional[str] = None,
        timeout_ms: int = 10000,
        workspace: Optional[Path | str] = None,
        user_info: Optional[dict[str, Any]] = None,
        page_size: int = 20,
        next_page_token: Optional[str] = None,
        prompt: Optional[str] = None,
        wait_seconds: float = 0.0,
        poll_interval: float = 1.0,
    ) -> dict[str, Any]:
        payload = self.get_rpc_messages(
            session_id,
            project_id=project_id,
            connect_session_id=connect_session_id,
            client_info=client_info,
            runtime_dir=runtime_dir,
            timeout_ms=timeout_ms,
            workspace=workspace,
            user_info=user_info,
            page_size=page_size,
            next_page_token=next_page_token,
            prompt=prompt,
        )
        deadline = time.monotonic() + max(wait_seconds, 0.0)
        effective_poll_interval = max(poll_interval, 0.1)
        while time.monotonic() < deadline:
            assistant_status = payload.get("assistant_status")
            answer_text = payload.get("answer_text")
            if assistant_status in RPC_CHAT_TERMINAL_STATUSES:
                break
            if answer_text and assistant_status not in RPC_CHAT_RUNNING_STATUSES:
                break
            time.sleep(min(effective_poll_interval, max(0.0, deadline - time.monotonic())))
            payload = self.get_rpc_messages(
                session_id,
                project_id=project_id,
                connect_session_id=connect_session_id,
                client_info=client_info,
                runtime_dir=runtime_dir,
                timeout_ms=timeout_ms,
                workspace=workspace,
                user_info=user_info,
                page_size=page_size,
                next_page_token=next_page_token,
                prompt=prompt,
            )
        return payload

    def send_rpc_chat(
        self,
        prompt: str,
        *,
        project_id: Optional[str] = None,
        session_id: Optional[str] = None,
        session_type: str = "inline_chat",
        agent_type: Optional[str] = None,
        connect_session_id: str = "",
        client_info: Optional[dict[str, Any]] = None,
        message_id: Optional[str] = None,
        runtime_dir: Optional[str] = None,
        timeout_ms: int = 10000,
        workspace: Optional[Path | str] = None,
        user_info: Optional[dict[str, Any]] = None,
        wait_seconds: float = 20.0,
        poll_interval: float = 1.0,
        page_size: int = 20,
        model_name: Optional[str] = None,
        custom_model: Optional[dict[str, Any]] = None,
        chat_data: Optional[dict[str, Any]] = None,
        scene_location: Optional[int] = None,
        create_project: bool = True,
        create_session: bool = True,
    ) -> dict[str, Any]:
        if session_type not in RPC_CHAT_SESSION_TYPES:
            valid = ", ".join(sorted(RPC_CHAT_SESSION_TYPES))
            raise RuntimeError(
                f"Unsupported session_type `{session_type}`. Expected one of: {valid}."
            )

        normalized_workspace = self.normalize_workspace_path(workspace or os.getcwd())
        bridge_state = self.read_bridge_state(workspace=normalized_workspace)
        resolved_connect_session_id = self._string_or_empty(connect_session_id)
        guessed_connect = None
        if resolved_connect_session_id:
            connect_source = "explicit"
        else:
            resolved_connect_session_id = self._string_or_empty(
                (client_info or {}).get("connect_session_id")
            )
            connect_source = "client_info" if resolved_connect_session_id else "none"
        if not resolved_connect_session_id and isinstance(bridge_state, dict):
            resolved_connect_session_id = self._string_or_empty(
                bridge_state.get("connect_session_id")
                or (
                    bridge_state.get("manager_exchange", {}) or {}
                ).get("connect_session_id")
                or bridge_state.get("ai_session_id")
            )
            if resolved_connect_session_id:
                connect_source = "bridge"
        if not resolved_connect_session_id:
            guessed_connect = self.guess_recent_connect_session(service="chat")
            if guessed_connect is not None:
                resolved_connect_session_id = self._string_or_empty(
                    guessed_connect.get("connect_session_id")
                )
                if resolved_connect_session_id:
                    connect_source = "logs"
        resolved_project_id = str(project_id or "").strip()
        project_payload = None
        if not resolved_project_id:
            if not create_project:
                raise RuntimeError("A project_id is required when create_project is disabled.")
            project_payload = self.create_rpc_project(
                normalized_workspace,
                connect_session_id=resolved_connect_session_id,
                runtime_dir=runtime_dir,
                timeout_ms=timeout_ms,
                workspace=normalized_workspace,
                user_info=user_info,
            )
            resolved_project_id = project_payload["project_id"]

        resolved_session_id = str(session_id or "").strip()
        session_payload = None
        if not resolved_session_id:
            if not create_session:
                raise RuntimeError("A session_id is required when create_session is disabled.")
            session_payload = self.create_rpc_session(
                resolved_project_id,
                session_type=session_type,
                connect_session_id=resolved_connect_session_id,
                runtime_dir=runtime_dir,
                timeout_ms=timeout_ms,
                workspace=normalized_workspace,
                user_info=user_info,
            )
            resolved_session_id = session_payload["session_id"]

        chat_request_data = self.build_rpc_chat_request_data(
            prompt,
            session_id=resolved_session_id,
            workspace=normalized_workspace,
            session_type=session_type,
            agent_type=agent_type,
            message_id=message_id,
            model_name=model_name,
            custom_model=custom_model,
            scene_location=scene_location,
            extra_data={
                key: value
                for key, value in {
                    **{
                        context_key: context_value
                        for context_key, context_value in self.build_workspace_context(
                            workspace=normalized_workspace
                        ).items()
                        if context_key
                        in {
                            "active_text_editor",
                            "workspace_folders",
                            "original_workspace_folders",
                            "is_workspace_folder_changed",
                        }
                    },
                    **(chat_data or {}),
                }.items()
            },
        )
        resolved_client_info = {
            **self.build_workspace_context(workspace=normalized_workspace),
            **(client_info or {}),
            "project_id": resolved_project_id,
        }
        chat_rpc_payload = self.invoke_aha_rpc(
            service="chat",
            method="chat",
            data=chat_request_data,
            connect_session_id=resolved_connect_session_id,
            client_info=resolved_client_info,
            runtime_dir=runtime_dir,
            timeout_ms=timeout_ms,
            workspace=normalized_workspace,
            user_info=user_info,
        )
        chat_response_data = self._extract_successful_rpc_response_data(
            chat_rpc_payload,
            service="chat",
            method="chat",
        )
        messages_payload = self.wait_for_rpc_messages(
            resolved_session_id,
            project_id=resolved_project_id,
            connect_session_id=resolved_connect_session_id,
            client_info=resolved_client_info,
            runtime_dir=runtime_dir,
            timeout_ms=timeout_ms,
            workspace=normalized_workspace,
            user_info=user_info,
            page_size=page_size,
            prompt=prompt,
            wait_seconds=wait_seconds,
            poll_interval=poll_interval,
        )
        return {
            "workspace": normalized_workspace,
            "project_id": resolved_project_id,
            "session_id": resolved_session_id,
            "session_type": session_type,
            "agent_type": chat_request_data.get("agent_type"),
            "connect_session_id": resolved_connect_session_id,
            "connect_session_source": connect_source,
            "guessed_connect_session": guessed_connect,
            "client_info": self.build_rpc_client_info(
                connect_session_id=resolved_connect_session_id,
                client_info=resolved_client_info,
            ),
            "message_id": chat_request_data.get("message_id"),
            "chat_data": chat_request_data,
            "project": project_payload,
            "session": session_payload,
            "chat_request": chat_rpc_payload,
            "chat_response": self._extract_rpc_response_payload(
                chat_rpc_payload.get("response")
            ),
            "chat_response_packet": chat_rpc_payload.get("response"),
            "chat_response_data": chat_response_data,
            "messages_request": messages_payload.get("request"),
            "messages_response": messages_payload.get("response"),
            "messages_response_packet": messages_payload.get("response_packet"),
            "messages_response_data": messages_payload.get("response_data"),
            "messages": messages_payload.get("messages") or [],
            "message_count": messages_payload.get("message_count"),
            "assistant_status": messages_payload.get("assistant_status"),
            "latest_assistant_message": messages_payload.get("latest_assistant_message"),
            "answer_text": messages_payload.get("answer_text"),
            "answer_source": messages_payload.get("answer_source"),
            "next_page_token": messages_payload.get("next_page_token"),
        }

    def _detect_remote_debugging_port(self) -> Optional[dict[str, Any]]:
        if os.name == "nt":
            return self._detect_remote_debugging_port_windows()
        if not self._command_available("ps"):
            return None
        ps_result = self._run_probe_command(["ps", "-axo", "pid=,ppid=,command="])
        if self._probe_error(ps_result):
            return None
        for process in reversed(self._parse_trae_processes(ps_result.stdout)):
            match = REMOTE_DEBUGGING_PORT_RE.search(process.get("command") or "")
            if not match:
                continue
            return {
                "host": "127.0.0.1",
                "port": int(match.group("port")),
                "source": "process",
                "pid": process.get("pid"),
                "role": process.get("role"),
                "command": process.get("command"),
            }
        return None

    def _detect_remote_debugging_port_windows(self) -> Optional[dict[str, Any]]:
        try:
            result = subprocess.run(
                ["wmic", "process", "where",
                 f"name='{self.paths.app_path.stem}.exe'",
                 "get", "ProcessId,CommandLine",
                 "/format:list"],
                text=True,
                capture_output=True,
                check=False,
                timeout=5,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        for line in result.stdout.splitlines():
            match = REMOTE_DEBUGGING_PORT_RE.search(line)
            if not match:
                continue
            pid_match = re.search(r"ProcessId=(\d+)", result.stdout)
            return {
                "host": "127.0.0.1",
                "port": int(match.group("port")),
                "source": "process",
                "pid": int(pid_match.group(1)) if pid_match else None,
                "role": "main-electron",
                "command": line.strip(),
            }
        return None

    def cdp_target_url_markers(self) -> list[str]:
        app_path = str(self.paths.app_path.resolve())
        app_name = self.paths.app_path.name
        workbench_root = str(
            (self.paths._app_resources_app_dir / "out").resolve()
        )
        if os.name == "nt":
            resource_rel = f"{app_name}/resources/app/out/"
        else:
            resource_rel = f"{app_name}/Contents/Resources/app/out/"
        candidates = {
            app_path,
            quote(app_path, safe="/"),
            app_name,
            quote(app_name, safe=""),
            workbench_root,
            quote(workbench_root, safe="/"),
            resource_rel,
            quote(resource_rel, safe="/"),
        }
        markers = [str(item).strip().lower() for item in candidates if str(item).strip()]
        return sorted(set(markers), key=len, reverse=True)

    @staticmethod
    def _fetch_cdp_json(
        *,
        host: str,
        port: int,
        path: str,
        timeout_seconds: float = 1.0,
    ) -> Any:
        normalized_path = path if path.startswith("/") else f"/{path}"
        connection = http.client.HTTPConnection(
            host,
            int(port),
            timeout=max(timeout_seconds, 0.1),
        )
        try:
            connection.request(
                "GET",
                normalized_path,
                headers={
                    "accept": "application/json",
                    "connection": "close",
                    "user-agent": "traecli/1.0",
                },
            )
            response = connection.getresponse()
            payload = response.read().decode("utf-8", "replace")
            if not 200 <= int(getattr(response, "status", 0) or 0) < 300:
                raise ValueError(
                    f"CDP endpoint returned HTTP {getattr(response, 'status', 'unknown')}"
                )
            return json.loads(payload)
        finally:
            connection.close()

    def cdp_endpoint_matches_app(
        self,
        *,
        host: str,
        port: int,
        timeout_seconds: float = 1.0,
    ) -> bool:
        try:
            payload = self._fetch_cdp_json(
                host=host,
                port=port,
                path="/json/list",
                timeout_seconds=timeout_seconds,
            )
        except (
            http.client.HTTPException,
            TimeoutError,
            socket.timeout,
            OSError,
            ValueError,
            json.JSONDecodeError,
        ):
            return False

        if not isinstance(payload, list):
            return False

        markers = self.cdp_target_url_markers()
        if not markers:
            return False

        for item in payload:
            if not isinstance(item, dict):
                continue
            if str(item.get("type") or "").strip().lower() != "page":
                continue
            url = str(item.get("url") or "").strip().lower()
            if not url:
                continue
            normalized_urls = {url, unquote(url)}
            if any(marker in candidate for candidate in normalized_urls for marker in markers):
                return True
        return False

    def _cdp_endpoint_mismatch_message(self, *, host: str, port: int) -> str:
        return (
            f"Debugger endpoint {host}:{port} does not appear to belong to "
            f"`{self.paths.app_path.name}`. Close the other Trae instance, relaunch "
            f"`{self.paths.app_path.name}` with `--remote-debugging-port`, or pass "
            "matching `--app-path/--support-dir/--cdp-port` values."
        )

    def resolve_cdp_endpoint(
        self,
        *,
        host: Optional[str] = None,
        port: Optional[int] = None,
    ) -> dict[str, Any]:
        resolved_host = (host or os.environ.get("TRAE_CDP_HOST") or "127.0.0.1").strip()
        if port is not None:
            return {
                "host": resolved_host,
                "port": int(port),
                "source": "explicit",
            }

        env_port = self._parse_optional_int(os.environ.get("TRAE_REMOTE_DEBUGGING_PORT"))
        if env_port is not None:
            return {
                "host": resolved_host,
                "port": env_port,
                "source": "env",
            }

        detected = self._detect_remote_debugging_port()
        if detected is not None:
            detected["host"] = resolved_host
            return detected

        return {
            "host": resolved_host,
            "port": 9222,
            "source": "default",
        }

    def cdp_version_url(self, *, host: str, port: int) -> str:
        return f"http://{host}:{port}/json/version"

    def cdp_endpoint_available(
        self,
        *,
        host: str,
        port: int,
        timeout_seconds: float = 1.0,
    ) -> bool:
        url = self.cdp_version_url(host=host, port=port)
        if self._command_available("curl"):
            timeout_arg = f"{max(timeout_seconds, 0.1):.3f}"
            result = subprocess.run(
                ["curl", "-fsS", "--max-time", timeout_arg, url],
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode == 0:
                return True

        try:
            payload = self._fetch_cdp_json(
                host=host,
                port=port,
                path="/json/version",
                timeout_seconds=timeout_seconds,
            )
            return isinstance(payload, dict)
        except (
            http.client.HTTPException,
            TimeoutError,
            socket.timeout,
            OSError,
            ValueError,
            json.JSONDecodeError,
        ):
            return False

    def build_cdp_launch_command(self, *, port: int) -> list[str]:
        if os.name == "nt":
            exe_name = self.paths.app_path.stem + ".exe"
            exe_path = self.paths.app_path / exe_name
            return [str(exe_path), f"--remote-debugging-port={int(port)}"]
        return [
            "open",
            "-na",
            str(self.paths.app_path),
            "--args",
            f"--remote-debugging-port={int(port)}",
        ]

    def launch_app_for_cdp(
        self,
        *,
        port: int,
        capture_output: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.build_cdp_launch_command(port=port),
            text=True,
            capture_output=capture_output,
            check=False,
        )

    def ensure_cdp_endpoint(
        self,
        *,
        host: Optional[str] = None,
        port: Optional[int] = None,
        launch_if_needed: bool = False,
        launch_timeout_ms: int = 12000,
    ) -> dict[str, Any]:
        endpoint = self.resolve_cdp_endpoint(host=host, port=port)
        resolved_host = endpoint["host"]
        resolved_port = int(endpoint["port"])
        if self.cdp_endpoint_available(host=resolved_host, port=resolved_port):
            if self.cdp_endpoint_matches_app(
                host=resolved_host,
                port=resolved_port,
                timeout_seconds=1.0,
            ):
                payload = dict(endpoint)
                payload["availability_source"] = "existing"
                payload["app_match"] = True
                return payload

            raise RuntimeError(
                self._cdp_endpoint_mismatch_message(
                    host=resolved_host,
                    port=resolved_port,
                )
            )

        if self.cdp_endpoint_available(host=resolved_host, port=resolved_port):
            payload = dict(endpoint)
            payload["availability_source"] = "existing"
            return payload

        if not launch_if_needed:
            if os.name == "nt":
                launch_hint = f"`{self.paths.app_path / (self.paths.app_path.stem + '.exe')} --remote-debugging-port={resolved_port}`, or pass `--cdp-port`."
            else:
                launch_hint = f"`open -na {self.paths.app_path} --args --remote-debugging-port={resolved_port}`, or pass `--cdp-port`."
            raise RuntimeError(
                "Failed to query the debugger endpoint. Start Trae with remote "
                "debugging enabled, for example "
                + launch_hint
            )

        launch_result = self.launch_app_for_cdp(port=resolved_port)
        deadline = time.monotonic() + max(launch_timeout_ms, 0) / 1000.0
        while time.monotonic() < deadline:
            if self.cdp_endpoint_available(
                host=resolved_host,
                port=resolved_port,
                timeout_seconds=1.0,
            ):
                payload = dict(endpoint)
                payload.update(
                    {
                        "availability_source": "launched",
                        "launch_command": self.build_cdp_launch_command(port=resolved_port),
                        "launch_exit_code": launch_result.returncode,
                    }
                )
                return payload
            time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))

        raise RuntimeError(
            "Tried to launch Trae with a remote debugging port but the debugger "
            f"endpoint at {resolved_host}:{resolved_port} did not come up. If Trae "
            "is already running without `--remote-debugging-port`, close it first and "
            "relaunch it with that flag, or point `--cdp-port` at a running debug port."
        )

    def invoke_cdp_chat(
        self,
        *,
        prompt: str,
        host: Optional[str] = None,
        port: Optional[int] = None,
        target_id: Optional[str] = None,
        prefer_focused: bool = False,
        title_contains: Optional[list[str]] = None,
        url_contains: Optional[list[str]] = None,
        ready_timeout_ms: int = 15000,
        timeout_ms: int = 30000,
        response_poll_interval_ms: int = 350,
        response_idle_ms: int = 1200,
        command_timeout_ms: int = 5000,
        launch_if_needed: bool = False,
        launch_timeout_ms: int = 12000,
    ) -> dict[str, Any]:
        endpoint = self.ensure_cdp_endpoint(
            host=host,
            port=port,
            launch_if_needed=launch_if_needed,
            launch_timeout_ms=launch_timeout_ms,
        )
        helper_payload, result, stderr = self._invoke_cdp_bridge(
            {
                "host": endpoint["host"],
                "port": endpoint["port"],
                "target_id": str(target_id or "").strip(),
                "prefer_focused": bool(prefer_focused),
                "title_contains": title_contains or [],
                "url_contains": url_contains or [],
                "required_url_contains": self.cdp_target_url_markers(),
                "app_path": str(self.paths.app_path),
                "app_name": self.paths.app_path.name,
                "prompt": prompt,
                "ready_timeout_ms": ready_timeout_ms,
                "response_timeout_ms": timeout_ms,
                "response_poll_interval_ms": response_poll_interval_ms,
                "response_idle_ms": response_idle_ms,
                "command_timeout_ms": command_timeout_ms,
            },
            endpoint=endpoint,
        )

        return {
            "endpoint": endpoint,
            "version": helper_payload.get("version"),
            "target": helper_payload.get("target"),
            "foreground": helper_payload.get("foreground"),
            "readiness": helper_payload.get("readiness"),
            "submit": helper_payload.get("submit"),
            "response": helper_payload.get("response"),
            "bridge": {
                "exit_code": result.returncode,
                "stderr": stderr,
            },
        }

    def invoke_cdp_websocket_probe(
        self,
        *,
        prompt: str,
        host: Optional[str] = None,
        port: Optional[int] = None,
        target_id: Optional[str] = None,
        prefer_focused: bool = True,
        title_contains: Optional[list[str]] = None,
        url_contains: Optional[list[str]] = None,
        ready_timeout_ms: int = 15000,
        timeout_ms: int = 30000,
        response_poll_interval_ms: int = 350,
        response_idle_ms: int = 1200,
        command_timeout_ms: int = 5000,
        reload_before_run: bool = False,
        launch_if_needed: bool = False,
        launch_timeout_ms: int = 12000,
    ) -> dict[str, Any]:
        endpoint = self.ensure_cdp_endpoint(
            host=host,
            port=port,
            launch_if_needed=launch_if_needed,
            launch_timeout_ms=launch_timeout_ms,
        )
        helper_payload, result, stderr = self._invoke_cdp_bridge(
            {
                "mode": "websocket_probe",
                "host": endpoint["host"],
                "port": endpoint["port"],
                "target_id": str(target_id or "").strip(),
                "prefer_focused": bool(prefer_focused),
                "title_contains": title_contains or [],
                "url_contains": url_contains or [],
                "required_url_contains": self.cdp_target_url_markers(),
                "app_path": str(self.paths.app_path),
                "app_name": self.paths.app_path.name,
                "prompt": prompt,
                "ready_timeout_ms": ready_timeout_ms,
                "response_timeout_ms": timeout_ms,
                "response_poll_interval_ms": response_poll_interval_ms,
                "response_idle_ms": response_idle_ms,
                "command_timeout_ms": command_timeout_ms,
                "reload_before_run": bool(reload_before_run),
            },
            endpoint=endpoint,
        )

        return {
            "endpoint": endpoint,
            "mode": helper_payload.get("mode") or "websocket_probe",
            "version": helper_payload.get("version"),
            "target": helper_payload.get("target"),
            "foreground": helper_payload.get("foreground"),
            "readiness": helper_payload.get("readiness"),
            "probe_install": helper_payload.get("probe_install"),
            "submit": helper_payload.get("submit"),
            "websocket_probe": helper_payload.get("websocket_probe"),
            "timings": helper_payload.get("timings"),
            "bridge": {
                "exit_code": result.returncode,
                "stderr": stderr,
            },
        }

    def _invoke_cdp_bridge(
        self,
        bridge_payload: dict[str, Any],
        *,
        endpoint: dict[str, Any],
    ) -> tuple[dict[str, Any], subprocess.CompletedProcess[str], str]:
        node_bin = shutil.which("node")
        if node_bin is None:
            raise RuntimeError("`node` is required for Trae CDP bridge calls.")

        bridge_path = self._cdp_bridge_script_path()
        if not bridge_path.exists():
            raise RuntimeError(f"Trae CDP bridge script not found: {bridge_path}")

        result = subprocess.run(
            [node_bin, str(bridge_path)],
            input=json.dumps(bridge_payload, ensure_ascii=False),
            text=True,
            capture_output=True,
            check=False,
        )
        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()
        helper_payload = self._safe_json_value(stdout)
        if not isinstance(helper_payload, dict):
            detail = stderr or stdout or "empty stdout"
            raise RuntimeError(
                "Trae CDP bridge returned invalid JSON "
                f"(exit {result.returncode}): {detail}"
            )
        if result.returncode != 0 or not helper_payload.get("ok"):
            error = helper_payload.get("error") or {}
            code = str(error.get("code") or "").strip() or None
            message = error.get("message")
            if not isinstance(message, str) or not message.strip():
                try:
                    message = json.dumps(error, ensure_ascii=False)
                except TypeError:
                    message = str(error)
            if code in {
                "CDP_DISCOVERY_FAILED",
                "CDP_DISCOVERY_TIMEOUT",
                "CDP_DISCOVERY_HTTP_ERROR",
                "CDP_TARGET_NOT_FOUND",
            }:
                message = (
                    f"{message} Start Trae with remote debugging enabled, for example "
                    + (
                        f"`{self.paths.app_path / (self.paths.app_path.stem + '.exe')} --remote-debugging-port={endpoint['port']}`, or pass `--cdp-port`."
                        if os.name == "nt"
                        else f"`open -na {self.paths.app_path} --args --remote-debugging-port={endpoint['port']}`, or pass `--cdp-port`."
                    )
                )
            raise CDPBridgeError(
                message or stderr or "Trae CDP bridge failed.",
                code=code,
            )
        return helper_payload, result, stderr

    def build_export_header_extra(self, workspace_path: Optional[str] = None) -> str:
        workspace = workspace_path
        if workspace is None:
            workspace = os.getcwd()
        return f"> **Workspace:** {workspace}" if workspace else ""

    def export_chat_session(
        self,
        session_id: str,
        *,
        connect_session_id: Optional[str] = None,
        export_path: Optional[Path | str] = None,
        header_extra: Optional[str] = None,
        runtime_dir: Optional[str] = None,
        timeout_ms: int = 10000,
        workspace: Optional[Path | str] = None,
    ) -> dict[str, Any]:
        connect_source = "explicit"
        resolved_connect_session_id = connect_session_id
        guessed_connect = None
        if not resolved_connect_session_id:
            guessed_connect = self.guess_recent_connect_session(service="chat")
            if guessed_connect is None:
                raise RuntimeError(
                    "No `connect_session_id` supplied and no recent chat connect session "
                    "was found in local logs."
                )
            resolved_connect_session_id = guessed_connect["connect_session_id"]
            connect_source = "logs"

        target_path = (
            Path(export_path).expanduser().resolve()
            if export_path
            else (Path(tempfile.gettempdir()) / f"traecli-past-chat-{session_id}.md").resolve()
        )
        payload = {
            "session_id": session_id,
            "export_path": str(target_path),
            "header_extra": header_extra
            if header_extra is not None
            else self.build_export_header_extra(),
        }
        rpc_payload = self.invoke_aha_rpc(
            service="chat",
            method="export_past_chat",
            data=payload,
            connect_session_id=resolved_connect_session_id,
            runtime_dir=runtime_dir,
            timeout_ms=timeout_ms,
            workspace=workspace,
        )
        response = self._extract_rpc_response_payload(rpc_payload.get("response"))
        if not isinstance(response, dict):
            raise RuntimeError("Trae export RPC returned an unexpected response payload.")
        if response.get("code") != 0:
            raise RuntimeError(response.get("message") or "Export chat failed.")

        response_data = response.get("data") if isinstance(response.get("data"), dict) else {}
        resolved_path = Path(response_data.get("file_path") or target_path).expanduser()
        content = resolved_path.read_text(errors="replace") if resolved_path.exists() else None
        return {
            "session_id": session_id,
            "connect_session_id": resolved_connect_session_id,
            "connect_session_source": connect_source,
            "guessed_connect_session": guessed_connect,
            "export_path": str(resolved_path),
            "content": content,
            "request": rpc_payload,
            "response": response,
            "response_packet": rpc_payload.get("response"),
        }

    def _load_json_file(self, path: Path) -> Optional[dict[str, Any]]:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _ordered_unique(values: list[Any]) -> list[Any]:
        seen: set[Any] = set()
        ordered: list[Any] = []
        for value in values:
            if value in seen:
                continue
            seen.add(value)
            ordered.append(value)
        return ordered

    @staticmethod
    def _safe_json_loads(raw: str) -> Optional[dict[str, Any]]:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if isinstance(payload, dict):
            return payload
        return None

    @staticmethod
    def _read_text_with_fallback(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return path.read_text(encoding="utf-8", errors="replace")

    @staticmethod
    def _safe_json_value(raw: str) -> Optional[Any]:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _decode_logged_string(raw: Optional[str]) -> Optional[str]:
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if isinstance(payload, str):
            return payload
        return None

    @staticmethod
    def _decode_logged_optional_string(raw: Optional[str]) -> Optional[str]:
        if not raw or raw == "None":
            return None
        if raw.startswith("Some(") and raw.endswith(")"):
            return TraeBackend._decode_logged_string(raw[5:-1])
        return TraeBackend._decode_logged_string(raw)

    @staticmethod
    def _extract_timestamp(line: str) -> Optional[str]:
        match = TIMESTAMP_PREFIX_RE.match(line)
        if not match:
            return None
        return match.group("timestamp")

    @staticmethod
    def _parse_iso_timestamp(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    @staticmethod
    def _parse_boolish(value: Any) -> Optional[bool]:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"true", "1", "yes"}:
                return True
            if lowered in {"false", "0", "no"}:
                return False
        return None

    @staticmethod
    def _parse_optional_int(value: Any) -> Optional[int]:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped or stripped.lower() == "undefined":
                return None
            try:
                return int(stripped)
            except ValueError:
                return None
        return None

    @staticmethod
    def _flatten_tool_logs(values: Any) -> list[str]:
        if not isinstance(values, list):
            return []
        lines: list[str] = []
        for value in values:
            if not isinstance(value, str):
                continue
            for line in value.splitlines() or [value]:
                stripped = TraeBackend._sanitize_terminal_log_line(line)
                if stripped:
                    lines.append(stripped)
        return lines

    @staticmethod
    def _sanitize_terminal_log_line(line: str) -> str:
        cleaned = ANSI_ESCAPE_RE.sub("", line)
        cleaned = CARET_OSC_ESCAPE_RE.sub("", cleaned)
        cleaned = CARET_CSI_ESCAPE_RE.sub("", cleaned)
        cleaned = CONTROL_CHAR_RE.sub("", cleaned)
        return cleaned.strip()

    @staticmethod
    def _renderer_log_sort_key(path: Path) -> tuple[str, int, str]:
        match = RENDERER_LOG_NAME_RE.match(path.name)
        rotation = int(match.group("rotation")) if match and match.group("rotation") else 0
        order = -rotation if rotation else 1_000_000
        return (str(path.parent), order, path.name)

    @staticmethod
    def _build_tool_log_excerpt(
        lines: list[str], *, max_lines: int = 8, max_chars: int = 1200
    ) -> list[str]:
        if not lines:
            return []
        excerpt: list[str] = []
        char_count = 0
        for index, line in enumerate(lines):
            if index >= max_lines or char_count + len(line) > max_chars:
                remaining = len(lines) - index
                if remaining > 0:
                    excerpt.append(f"... ({remaining} more lines)")
                break
            excerpt.append(line)
            char_count += len(line)
        return excerpt

    @staticmethod
    def _merge_tool_run_details(
        target: dict[str, Any], update: dict[str, Any]
    ) -> dict[str, Any]:
        for key, value in update.items():
            if key == "source_logs":
                target.setdefault("source_logs", set()).update(value or set())
                continue
            if key == "result_log_line_count":
                target[key] = max(
                    int(target.get(key) or 0),
                    int(value or 0),
                )
                continue
            if key == "metrics":
                if isinstance(value, dict) and not target.get(key):
                    target[key] = value
                continue
            if key == "result_log_excerpt":
                if value and not target.get(key):
                    target[key] = value
                continue
            if key == "exit_code":
                if value is not None:
                    target[key] = value
                continue
            if value is None:
                continue
            if target.get(key) in (None, "", []):
                target[key] = value
        return target

    @staticmethod
    def _chat_turn_sort_key(turn: dict[str, Any]) -> tuple[int, str]:
        timestamp = turn.get("last_seen_at") or turn.get("started_at") or ""
        dt = TraeBackend._parse_iso_timestamp(timestamp)
        ordinal = dt.toordinal() if dt else 0
        micros = (
            ((dt - dt.replace(hour=0, minute=0, second=0, microsecond=0)).seconds * 1_000_000)
            + dt.microsecond
            if dt
            else 0
        )
        return (ordinal * 86_400_000_000 + micros, timestamp)

    @staticmethod
    def _sanitize_ipc_name(name: str) -> str:
        return re.sub(r"[^a-zA-Z0-9._-]", "_", name)

    def load_storage_json(self) -> dict[str, Any]:
        if not self.paths.storage_json_path.exists():
            return {}
        return json.loads(self._read_text_with_fallback(self.paths.storage_json_path))

    def load_auth_record(self) -> Optional[dict[str, Any]]:
        storage = self.load_storage_json()
        raw = storage.get(AUTH_STORAGE_KEY)
        if not raw:
            return None
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def load_auth_info(self) -> Optional[dict[str, Any]]:
        parsed = self.load_auth_record()
        if not parsed:
            return None
        account = parsed.get("account", {}) if isinstance(parsed.get("account"), dict) else {}
        region = (
            parsed.get("userRegion", {})
            if isinstance(parsed.get("userRegion"), dict)
            else {}
        )
        return {
            "user_id": parsed.get("userId"),
            "host": parsed.get("host"),
            "region": region.get("region"),
            "username": account.get("username"),
            "email": account.get("email"),
            "login_scope": account.get("loginScope"),
            "scope": account.get("scope"),
            "token_expires_at": parsed.get("expiredAt"),
            "refresh_expires_at": parsed.get("refreshExpiredAt"),
        }

    @staticmethod
    def _string_or_empty(value: Any) -> str:
        if value in (None, ""):
            return ""
        return str(value)

    def build_rpc_user_info(
        self,
        user_info: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        auth_record = self.load_auth_record() or {}
        account = (
            auth_record.get("account", {})
            if isinstance(auth_record.get("account"), dict)
            else {}
        )
        region = (
            auth_record.get("userRegion", {})
            if isinstance(auth_record.get("userRegion"), dict)
            else {}
        )
        auth_user_info = {
            **DEFAULT_RPC_USER_INFO,
            "name": self._string_or_empty(
                account.get("username") or account.get("email")
            ),
            "token": self._string_or_empty(auth_record.get("token")),
            "region": self._string_or_empty(region.get("region")),
            "is_internal": bool(
                auth_record.get("isInternal")
                if auth_record.get("isInternal") is not None
                else auth_record.get("is_internal") or False
            ),
            "user_id": self._string_or_empty(
                auth_record.get("userId") or auth_record.get("user_id")
            ),
            "scope": self._string_or_empty(
                account.get("scope") or account.get("loginScope")
            ),
        }
        return {
            **auth_user_info,
            **(user_info or {}),
        }

    def _read_state_values(self, keys: list[str]) -> dict[str, str]:
        if not self.paths.state_db_path.exists():
            return {}
        connection = sqlite3.connect(str(self.paths.state_db_path))
        try:
            cursor = connection.execute(
                "select key, value from ItemTable where key in ({})".format(
                    ",".join("?" for _ in keys)
                ),
                keys,
            )
            return {key: value for key, value in cursor.fetchall()}
        finally:
            connection.close()

    @staticmethod
    def _maybe_json(value: Optional[str]) -> Any:
        if value is None:
            return None
        if isinstance(value, (dict, list)):
            return value
        stripped = value.strip()
        if not stripped:
            return stripped
        if stripped[0] in "[{":
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                return value
        return value

    @staticmethod
    def _sanitize_selected_model(value: Any) -> Any:
        return TraeBackend._sanitize_model_payload(value)

    @staticmethod
    def _sanitize_model_payload(value: Any) -> Any:
        if isinstance(value, dict):
            sanitized: dict[str, Any] = {}
            for key, item in value.items():
                if key in {"ak", "sk", "custom_config"}:
                    continue
                sanitized[key] = TraeBackend._sanitize_model_payload(item)
            return sanitized
        if isinstance(value, list):
            return [TraeBackend._sanitize_model_payload(item) for item in value]
        return value

    @staticmethod
    def build_model_key(model: Optional[dict[str, Any]]) -> str:
        if not isinstance(model, dict):
            return ""
        config_source = model.get("config_source")
        provider = model.get("provider")
        name = model.get("name")
        return f"{config_source if config_source is not None else ''}_{provider or '-'}_{name or ''}"

    @staticmethod
    def normalize_model_agent_type(agent_type: Optional[str]) -> str:
        normalized = str(agent_type or "").strip().lower().replace(" ", "_")
        if not normalized:
            return "dev_builder"
        resolved = MODEL_FRONT_AGENT_ALIASES.get(normalized)
        if resolved:
            return resolved
        valid = ", ".join(sorted(MODEL_FRONT_AGENT_TO_FUNCTION))
        raise ValueError(
            f"Unsupported model agent type `{agent_type}`. Expected one of: {valid}."
        )

    @staticmethod
    def _coerce_model_list(value: Any, *, sanitize: bool) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        models: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            models.append(
                TraeBackend._sanitize_model_payload(item) if sanitize else dict(item)
            )
        return models

    @staticmethod
    def _default_model_from_list(
        models: list[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        if not models:
            return None
        for item in models:
            if item.get("is_default") and item.get("selectable", True) is not False:
                return item
        for item in models:
            if item.get("selectable", True) is not False:
                return item
        return models[0]

    @classmethod
    def _model_aliases(cls, model: dict[str, Any]) -> set[str]:
        aliases: set[str] = set()
        for value in (
            model.get("name"),
            model.get("display_name"),
            cls.build_model_key(model),
        ):
            if value in (None, ""):
                continue
            aliases.add(str(value).strip().lower())
        return aliases

    @classmethod
    def _resolve_model_match(
        cls,
        models: list[dict[str, Any]],
        identifier: Optional[str],
        *,
        allow_prefix: bool = False,
    ) -> tuple[Optional[dict[str, Any]], list[dict[str, Any]]]:
        query = str(identifier or "").strip().lower()
        if not query:
            return None, []
        exact_matches = [item for item in models if query in cls._model_aliases(item)]
        if exact_matches:
            return (
                exact_matches[0] if len(exact_matches) == 1 else None,
                exact_matches,
            )
        if not allow_prefix:
            return None, []
        prefix_matches = [
            item
            for item in models
            if any(alias.startswith(query) for alias in cls._model_aliases(item))
        ]
        return (
            prefix_matches[0] if len(prefix_matches) == 1 else None,
            prefix_matches,
        )

    @classmethod
    def _find_model_by_key(
        cls,
        models: list[dict[str, Any]],
        model_key: Optional[str],
    ) -> Optional[dict[str, Any]]:
        resolved, _ = cls._resolve_model_match(models, model_key)
        return resolved

    @classmethod
    def _resolved_current_model_entry(
        cls,
        *,
        agent_type: str,
        models: list[dict[str, Any]],
        global_model_map: dict[str, Any],
        legacy_selected_model: Any,
        source_function: str,
    ) -> dict[str, Any]:
        front_agent = cls.normalize_model_agent_type(agent_type)
        configured_key = str(global_model_map.get(front_agent) or "").strip()
        if configured_key:
            configured_model = cls._find_model_by_key(models, configured_key)
            return {
                "agent_type": front_agent,
                "source_function": source_function,
                "model_key": configured_key,
                "source": "global_model_map",
                "model": configured_model,
            }
        if front_agent == "dev_builder" and isinstance(legacy_selected_model, dict):
            legacy_key = cls.build_model_key(legacy_selected_model)
            legacy_model = cls._find_model_by_key(
                models,
                legacy_key
                or legacy_selected_model.get("name")
                or legacy_selected_model.get("display_name"),
            )
            if legacy_model is not None:
                return {
                    "agent_type": front_agent,
                    "source_function": source_function,
                    "model_key": cls.build_model_key(legacy_model),
                    "source": "legacy_selected_model",
                    "model": legacy_model,
                }
            return {
                "agent_type": front_agent,
                "source_function": source_function,
                "model_key": legacy_key,
                "source": "legacy_selected_model",
                "model": legacy_selected_model,
            }
        default_model = cls._default_model_from_list(models)
        return {
            "agent_type": front_agent,
            "source_function": source_function,
            "model_key": cls.build_model_key(default_model),
            "source": "default_model" if default_model else "unavailable",
            "model": default_model,
        }

    def read_model_state(self, *, sanitize: bool = True) -> dict[str, Any]:
        auth = self.load_auth_info()
        user_id = auth.get("user_id") if auth else None
        state_keys = {
            "selected_model": (
                f"{user_id}_AI.agent.model.selected_model" if user_id else None
            ),
            "agent_mode": f"{user_id}_AI.agent.mode" if user_id else None,
            "model_list": f"{user_id}_AI.agent.model.model_list" if user_id else None,
            "model_list_map": (
                f"{user_id}_AI.agent.model.model_list_map" if user_id else None
            ),
            "global_model_map": (
                f"{user_id}_ai-chat:sessionRelation:globalModelMap" if user_id else None
            ),
            "global_mode_map": (
                f"{user_id}_ai-chat:sessionRelation:globalModeMap" if user_id else None
            ),
        }
        keys = [value for value in state_keys.values() if value]
        values = self._read_state_values(keys) if keys else {}
        legacy_selected_model = self._maybe_json(
            values.get(state_keys["selected_model"]) if state_keys["selected_model"] else None
        )
        if sanitize:
            legacy_selected_model = self._sanitize_model_payload(legacy_selected_model)
        global_model_map = self._maybe_json(
            values.get(state_keys["global_model_map"])
            if state_keys["global_model_map"]
            else None
        )
        if not isinstance(global_model_map, dict):
            global_model_map = {}
        global_mode_map = self._maybe_json(
            values.get(state_keys["global_mode_map"])
            if state_keys["global_mode_map"]
            else None
        )
        if not isinstance(global_mode_map, dict):
            global_mode_map = {}
        model_list = self._coerce_model_list(
            self._maybe_json(
                values.get(state_keys["model_list"]) if state_keys["model_list"] else None
            ),
            sanitize=sanitize,
        )
        raw_model_list_map = self._maybe_json(
            values.get(state_keys["model_list_map"])
            if state_keys["model_list_map"]
            else None
        )
        model_list_map: dict[str, list[dict[str, Any]]] = {}
        if isinstance(raw_model_list_map, dict):
            for key, item in raw_model_list_map.items():
                model_list_map[str(key)] = self._coerce_model_list(
                    item,
                    sanitize=sanitize,
                )
        available_models_by_agent: dict[str, list[dict[str, Any]]] = {}
        current_models: dict[str, dict[str, Any]] = {}
        available_model_counts: dict[str, int] = {}
        for front_agent, source_function in MODEL_FRONT_AGENT_TO_FUNCTION.items():
            models = model_list_map.get(source_function) or list(model_list)
            available_models_by_agent[front_agent] = models
            available_model_counts[front_agent] = len(models)
            current_models[front_agent] = self._resolved_current_model_entry(
                agent_type=front_agent,
                models=models,
                global_model_map=global_model_map,
                legacy_selected_model=legacy_selected_model,
                source_function=source_function,
            )
        return {
            "user_id": user_id,
            "state_keys": state_keys,
            "agent_mode": self._maybe_json(
                values.get(state_keys["agent_mode"]) if state_keys["agent_mode"] else None
            ),
            "selected_model": legacy_selected_model,
            "model_list": model_list,
            "model_list_map": model_list_map,
            "available_models_by_agent": available_models_by_agent,
            "available_model_counts": available_model_counts,
            "global_model_map": global_model_map,
            "global_mode_map": global_mode_map,
            "current_models": current_models,
        }

    def list_models(self, *, agent_type: Optional[str] = None) -> dict[str, Any]:
        state = self.read_model_state()
        requested_agent_type = (
            self.normalize_model_agent_type(agent_type) if agent_type else None
        )
        agents: dict[str, Any] = {}
        for front_agent, source_function in MODEL_FRONT_AGENT_TO_FUNCTION.items():
            if requested_agent_type and front_agent != requested_agent_type:
                continue
            current = state["current_models"].get(front_agent) or {}
            current_key = str(current.get("model_key") or "").strip()
            models = []
            for item in state["available_models_by_agent"].get(front_agent) or []:
                model_key = self.build_model_key(item)
                models.append(
                    {
                        "name": item.get("name"),
                        "display_name": item.get("display_name"),
                        "model_key": model_key,
                        "provider": item.get("provider"),
                        "config_source": item.get("config_source"),
                        "model_type": item.get("model_type"),
                        "multimodal": item.get("multimodal"),
                        "is_default": bool(item.get("is_default")),
                        "selectable": item.get("selectable"),
                        "tags": item.get("tags"),
                        "current": bool(model_key and model_key == current_key),
                    }
                )
            agents[front_agent] = {
                "agent_type": front_agent,
                "source_function": source_function,
                "current_model": current,
                "count": len(models),
                "models": models,
            }
        return {
            "requested_agent_type": requested_agent_type,
            "agents": agents,
        }

    def current_models(self, *, agent_type: Optional[str] = None) -> dict[str, Any]:
        state = self.read_model_state()
        requested_agent_type = (
            self.normalize_model_agent_type(agent_type) if agent_type else None
        )
        current_models = {
            front_agent: entry
            for front_agent, entry in (state.get("current_models") or {}).items()
            if not requested_agent_type or front_agent == requested_agent_type
        }
        return {
            "requested_agent_type": requested_agent_type,
            "legacy_selected_model": state.get("selected_model"),
            "global_model_map": state.get("global_model_map"),
            "current_models": current_models,
            "available_model_counts": state.get("available_model_counts"),
        }

    def switch_model(
        self,
        model_identifier: str,
        *,
        agent_type: str = "dev_builder",
        cwd: Optional[Path | str] = None,
        reload_if_needed: bool = True,
    ) -> dict[str, Any]:
        front_agent = self.normalize_model_agent_type(agent_type)
        workspace = self.normalize_workspace_path(cwd) if cwd is not None else None
        state = self.read_model_state(sanitize=False)
        user_id = str(state.get("user_id") or "").strip()
        if not user_id:
            raise RuntimeError("No authenticated Trae user was found in local storage.")
        available_models = list(
            state.get("available_models_by_agent", {}).get(front_agent) or []
        )
        if not available_models:
            raise RuntimeError(
                f"No available model list was found for agent `{front_agent}`."
            )
        selected_model, matches = self._resolve_model_match(
            available_models,
            model_identifier,
            allow_prefix=True,
        )
        if selected_model is None:
            if matches:
                candidates = ", ".join(
                    sorted(
                        {
                            item.get("display_name")
                            or item.get("name")
                            or self.build_model_key(item)
                            for item in matches
                        }
                    )
                )
                raise RuntimeError(
                    f"Model `{model_identifier}` is ambiguous for `{front_agent}`. "
                    f"Candidates: {candidates}."
                )
            suggestions = ", ".join(
                (
                    item.get("display_name")
                    or item.get("name")
                    or self.build_model_key(item)
                )
                for item in available_models[:8]
            )
            raise RuntimeError(
                f"Model `{model_identifier}` was not found for `{front_agent}`. "
                f"Try one of: {suggestions}."
            )

        target_model_key = self.build_model_key(selected_model)
        if not target_model_key:
            raise RuntimeError(
                f"Resolved model `{model_identifier}` does not expose a writable model key."
            )

        current_entry = (state.get("current_models") or {}).get(front_agent) or {}
        previous_model = current_entry.get("model")
        previous_model_key = self._string_or_empty(current_entry.get("model_key"))
        previous_global_map = dict(state.get("global_model_map") or {})
        next_global_map = dict(previous_global_map)
        next_global_map[front_agent] = target_model_key
        needs_global_write = previous_global_map.get(front_agent) != target_model_key

        legacy_selected_model = state.get("selected_model")
        legacy_selected_key = self.build_model_key(legacy_selected_model)
        needs_legacy_write = (
            front_agent == "dev_builder" and legacy_selected_key != target_model_key
        )

        storage: dict[str, Any] = {}
        applied_via: list[str] = []
        notes: list[str] = []
        if needs_global_write:
            storage["global_model_map"] = self._write_state_item_value(
                str(state["state_keys"]["global_model_map"]),
                json.dumps(next_global_map, ensure_ascii=False),
            )
            applied_via.append("state.vscdb")
        if needs_legacy_write:
            storage["selected_model"] = self._write_state_item_value(
                str(state["state_keys"]["selected_model"]),
                json.dumps(selected_model, ensure_ascii=False),
            )
            if "state.vscdb" not in applied_via:
                applied_via.append("state.vscdb")

        reload_dispatch = None
        reload_required = bool(storage)
        if reload_required and reload_if_needed:
            reload_dispatch = self.invoke_command_uri(
                RELOAD_WINDOW_COMMAND_ID,
                cwd=workspace,
            )
            if reload_dispatch.get("ok"):
                applied_via.append("reload-window")
                reload_required = False
            else:
                notes.append(
                    "Window reload dispatch failed; reopen or reload Trae manually to apply the model change immediately."
                )

        after = self.current_models(agent_type=front_agent)
        after_entry = (after.get("current_models") or {}).get(front_agent) or {}
        status = "switched"
        if not storage:
            status = "already-selected"

        return {
            "status": status,
            "workspace": workspace,
            "agent_type": front_agent,
            "requested_model": str(model_identifier),
            "previous_model_key": previous_model_key or None,
            "previous_model": self._sanitize_model_payload(previous_model),
            "target_model_key": target_model_key,
            "target_model": self._sanitize_model_payload(selected_model),
            "current_model": after_entry.get("model"),
            "current_model_key": after_entry.get("model_key"),
            "storage": storage or None,
            "reload": reload_dispatch,
            "reload_requested": bool(reload_if_needed),
            "reload_required": reload_required,
            "applied_via": self._ordered_unique(applied_via),
            "notes": self._ordered_unique(notes),
        }

    def read_state_summary(self) -> dict[str, Any]:
        model_state = self.read_model_state()
        keys = [
            "chat.ChatSessionStore.index",
            "chat.workspaceTransfer",
        ]
        values = self._read_state_values(keys)
        sessions = self.list_sessions(limit=20)
        return {
            "agent_mode": model_state.get("agent_mode"),
            "selected_model": model_state.get("selected_model"),
            "global_model_map": model_state.get("global_model_map"),
            "global_mode_map": model_state.get("global_mode_map"),
            "current_models": model_state.get("current_models"),
            "available_model_counts": model_state.get("available_model_counts"),
            "chat_session_index": self._maybe_json(values.get("chat.ChatSessionStore.index")),
            "workspace_transfer": self._maybe_json(values.get("chat.workspaceTransfer")),
            "solo_mode": self.read_solo_mode_state(),
            "session_badges": sessions,
            "session_badge_count": len(sessions),
        }

    def list_sessions(self, *, limit: int = 20) -> list[dict[str, Any]]:
        if not self.paths.state_db_path.exists():
            return []
        connection = sqlite3.connect(str(self.paths.state_db_path))
        try:
            cursor = connection.execute(
                "select key, length(value) from ItemTable "
                "where key like 'all_session_badges_%' "
                "order by key desc limit ?",
                (limit,),
            )
            records = []
            for key, value_length in cursor.fetchall():
                session_id = key.removeprefix("all_session_badges_")
                records.append(
                    {
                        "session_id": session_id,
                        "value_length": value_length,
                        "has_badge_payload": value_length > 2,
                    }
                )
            return records
        finally:
            connection.close()

    def list_mcp_gallery(self) -> list[dict[str, Any]]:
        if not self.paths.mcp_gallery_dir.exists():
            return []
        entries: list[dict[str, Any]] = []
        for path in sorted(self.paths.mcp_gallery_dir.glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            run_commands = payload.get("commands", {}).get("universal", {}).get("run", [])
            entries.append(
                {
                    "id": payload.get("id"),
                    "display_name": payload.get("displayName"),
                    "repository": payload.get("repository"),
                    "version": payload.get("version"),
                    "mcp_server_type": payload.get("mcpServerType"),
                    "run": run_commands,
                }
            )
        return entries

    def list_sandboxes(self, *, limit: int = 20) -> list[dict[str, Any]]:
        sandbox_dir = self.paths.modular_data_dir / "ai-agent" / "sandbox"
        if not sandbox_dir.exists():
            return []
        entries: list[dict[str, Any]] = []
        for path in sorted(sandbox_dir.glob("*.json"), reverse=True)[:limit]:
            payload = json.loads(path.read_text(encoding="utf-8"))
            permissions = payload.get("permission", [])
            inherited_paths = []
            for item in permissions:
                if "file_inherit_user" in item:
                    inherited_paths.append(item["file_inherit_user"])
            entries.append(
                {
                    "name": payload.get("name", path.stem),
                    "path": str(path),
                    "permission_count": len(permissions),
                    "sample_inherited_paths": inherited_paths[:5],
                }
            )
        return entries

    def read_sandbox(self, name: str) -> dict[str, Any]:
        sandbox_path = self.paths.modular_data_dir / "ai-agent" / "sandbox" / f"{name}.json"
        if not sandbox_path.exists():
            raise FileNotFoundError(f"Sandbox snapshot not found: {name}")
        return json.loads(sandbox_path.read_text(encoding="utf-8"))

    def list_log_files(self) -> list[str]:
        latest = self.latest_log_session_dir()
        if latest is None:
            return []
        files = []
        for path in sorted(latest.rglob("*.log")):
            files.append(str(path.relative_to(latest)))
        return files

    def tail_log(self, *, match: str = "main.log", lines: int = 40) -> dict[str, Any]:
        latest = self.latest_log_session_dir()
        if latest is None:
            raise FileNotFoundError("No Trae log sessions found")
        candidates = [path for path in latest.rglob("*.log") if match in str(path.relative_to(latest))]
        if not candidates:
            raise FileNotFoundError(f"No log file matched '{match}' in {latest}")
        target = sorted(candidates)[0]
        content = target.read_text(errors="replace").splitlines()
        tail_lines = content[-lines:]
        return {
            "session_dir": str(latest),
            "path": str(target),
            "relative_path": str(target.relative_to(latest)),
            "lines": tail_lines,
        }

    def read_ckg_local_env(self) -> Optional[dict[str, Any]]:
        path = self.paths.modular_data_dir / "ckg_server" / "local_env.json"
        return self._load_json_file(path)

    def parse_model_cache_counts(self) -> dict[str, int]:
        latest = self.latest_log_session_dir()
        if latest is None:
            return {}
        modular_dir = latest / "Modular"
        candidates = sorted(modular_dir.glob("ai-agent*_stdout.log"))
        if not candidates:
            return {}
        counts: dict[str, int] = {}
        for line in candidates[-1].read_text(errors="replace").splitlines():
            match = MODEL_CACHE_RE.search(line)
            if match:
                counts[match.group("function")] = int(match.group("count"))
        return counts

    def _load_ai_chat_bundle(self) -> str:
        if not self.paths.ai_chat_bundle_path.exists():
            return ""
        return self.paths.ai_chat_bundle_path.read_text(errors="replace")

    def _load_electron_main_bundle(self) -> str:
        if not self.paths.electron_main_bundle_path.exists():
            return ""
        return self.paths.electron_main_bundle_path.read_text(errors="replace")

    def _extract_local_aha_ipc_implementation(self) -> dict[str, Any]:
        bundle = self._load_electron_main_bundle()
        import_match = ELECTRON_AHA_IPC_IMPORT_RE.search(bundle)
        alias = import_match.group("alias") if import_match else None
        has_server_api = bool(alias and f"{alias}.serve(" in bundle)
        has_client_api = bool(ELECTRON_AHA_IPC_CONNECT_RE.search(bundle))
        class_match = ELECTRON_AHA_IPC_SERVER_CLASS_RE.search(bundle)
        return {
            "client_api": "electron.ahaIpc.connect" if has_client_api else None,
            "server_api": "electron.ahaIpc.serve" if has_server_api else None,
            "server_class": class_match.group("name") if class_match else None,
            "source_path": str(self.paths.electron_main_bundle_path),
            "bundle_verified": has_client_api or has_server_api,
        }

    def _extract_node_aha_ipc_socket_rule(self, service: str) -> dict[str, Any]:
        sanitized_service = self._sanitize_ipc_name(service)
        runtime_dir = Path("/tmp")
        aha_dir = runtime_dir / "aha"
        socket_path = aha_dir / f"{sanitized_service}.sock"
        return {
            "package_present": self.paths.aha_ipc_utils_path.exists(),
            "package_path": str(self.paths.aha_ipc_utils_path),
            "runtime_dir": str(runtime_dir),
            "aha_dir": str(aha_dir),
            "socket_path": str(socket_path),
            "marker_path": f"{socket_path}.ready",
            "service": sanitized_service,
        }

    @staticmethod
    def _command_available(name: str) -> bool:
        return shutil.which(name) is not None

    @staticmethod
    def _run_probe_command(args: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                args,
                text=True,
                capture_output=True,
                check=False,
                timeout=5,
            )
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(args, 124, "", "timed out")
        except OSError as exc:
            return subprocess.CompletedProcess(args, 1, "", str(exc))

    @staticmethod
    def _probe_error(result: subprocess.CompletedProcess[str]) -> Optional[str]:
        stderr = (result.stderr or "").strip()
        if result.returncode in {0, 1} and not stderr:
            return None
        if stderr:
            return stderr
        if result.returncode != 0:
            return f"exit code {result.returncode}"
        return None

    @staticmethod
    def _classify_probe_error(error: str) -> str:
        normalized = error.lower()
        if "operation not permitted" in normalized or "permission denied" in normalized:
            return "blocked"
        return "failed"

    @classmethod
    def _classify_aggregate_probe_errors(cls, errors: list[str]) -> str:
        stripped = [item.split(":", 1)[-1].strip() for item in errors if item]
        if stripped and all(cls._classify_probe_error(item) == "blocked" for item in stripped):
            return "blocked"
        return "failed"

    def _classify_trae_process(
        self, command: str, process_type: Optional[str], main_exec_dir: str
    ) -> Optional[str]:
        if main_exec_dir in command:
            return "main-electron"
        if process_type == "extensionHost":
            return "extension-host"
        if process_type == "ai":
            return "ai-helper"
        if process_type == "ckg":
            return "ckg-helper"
        if process_type == "ai-server":
            return "ai-server-helper"
        return None

    def _parse_trae_processes(self, raw: str) -> list[dict[str, Any]]:
        bundle_path = str(self.paths.app_path)
        bundle_marker = f"/{self.paths.app_path.name}/"
        main_exec_dir = f"/{self.paths.app_path.name}/Contents/MacOS/"
        processes: list[dict[str, Any]] = []
        for line in raw.splitlines():
            match = PS_PROCESS_ROW_RE.match(line)
            if not match:
                continue
            command = match.group("command").strip()
            if (
                bundle_path not in command
                and bundle_marker not in command
                and self.paths.app_path.name not in command
            ):
                continue
            process_type_match = REPORTER_PROCESS_TYPE_RE.search(command)
            process_type = process_type_match.group("kind") if process_type_match else None
            role = self._classify_trae_process(command, process_type, main_exec_dir)
            if role is None:
                continue
            processes.append(
                {
                    "pid": int(match.group("pid")),
                    "ppid": int(match.group("ppid")),
                    "role": role,
                    "process_type": process_type,
                    "command": command,
                }
            )
        return sorted(processes, key=lambda item: item["pid"])

    @staticmethod
    def _parse_lsof_field_output(raw: str) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        current: Optional[dict[str, Any]] = None
        for field in raw.split("\0"):
            if not field:
                continue
            prefix, value = field[0], field[1:]
            if prefix == "f":
                if current is not None:
                    entries.append(current)
                current = {"fd": value, "tcp_info": []}
                continue
            if current is None:
                continue
            if prefix == "t":
                current["type"] = value
            elif prefix == "n":
                current["name"] = value
            elif prefix == "T":
                current.setdefault("tcp_info", []).append(value)
        if current is not None:
            entries.append(current)
        return entries

    def _collect_process_socket_snapshot(self, pid: int) -> dict[str, Any]:
        snapshot: dict[str, Any] = {
            "named_unix_sockets": [],
            "anonymous_unix_socket_count": 0,
            "tcp_listeners": [],
            "errors": [],
        }

        unix_result = self._run_probe_command(
            ["lsof", "-nP", "-a", "-p", str(pid), "-U", "-F0fnt"]
        )
        unix_error = self._probe_error(unix_result)
        if unix_error:
            snapshot["errors"].append(f"unix:{unix_error}")
        for entry in self._parse_lsof_field_output(unix_result.stdout):
            if entry.get("type") != "unix":
                continue
            name = entry.get("name") or ""
            if not name:
                continue
            if name.startswith("->"):
                snapshot["anonymous_unix_socket_count"] += 1
                continue
            snapshot["named_unix_sockets"].append(name)

        tcp_result = self._run_probe_command(
            ["lsof", "-nP", "-a", "-p", str(pid), "-iTCP", "-sTCP:LISTEN", "-F0fnt"]
        )
        tcp_error = self._probe_error(tcp_result)
        if tcp_error:
            snapshot["errors"].append(f"tcp:{tcp_error}")
        for entry in self._parse_lsof_field_output(tcp_result.stdout):
            if entry.get("type") not in {"IPv4", "IPv6"}:
                continue
            name = entry.get("name")
            if name:
                snapshot["tcp_listeners"].append(name)

        snapshot["named_unix_sockets"] = self._ordered_unique(
            snapshot["named_unix_sockets"]
        )
        snapshot["tcp_listeners"] = self._ordered_unique(snapshot["tcp_listeners"])
        snapshot["errors"] = self._ordered_unique(snapshot["errors"])
        return snapshot

    def _collect_live_transport_probe(self, topology: dict[str, Any]) -> dict[str, Any]:
        live: dict[str, Any] = {
            "enabled": True,
            "ps_available": self._command_available("ps"),
            "ps_probe_status": "not-run",
            "ps_error": None,
            "lsof_available": self._command_available("lsof"),
            "lsof_probe_status": "not-run",
            "lsof_error_count": 0,
            "lsof_errors": [],
            "processes": [],
            "notes": [],
        }
        if not live["ps_available"]:
            live["ps_probe_status"] = "unavailable"
            live["lsof_probe_status"] = (
                "unavailable" if not live["lsof_available"] else "not-run"
            )
            live["notes"].append("`ps` is not available; live process inspection was skipped.")
            return live

        ps_result = self._run_probe_command(["ps", "-axo", "pid=,ppid=,command="])
        ps_error = self._probe_error(ps_result)
        if ps_error:
            live["ps_probe_status"] = self._classify_probe_error(ps_error)
            live["ps_error"] = ps_error
            live["lsof_probe_status"] = (
                "unavailable" if not live["lsof_available"] else "not-run"
            )
            if live["ps_probe_status"] == "blocked":
                live["notes"].append(
                    f"`ps` is installed but the live probe was blocked: {ps_error}"
                )
            else:
                live["notes"].append(f"`ps` probe failed: {ps_error}")
            return live
        live["ps_probe_status"] = "ok"

        processes = self._parse_trae_processes(ps_result.stdout)
        if not processes:
            live["lsof_probe_status"] = (
                "unavailable" if not live["lsof_available"] else "not-run"
            )
            live["notes"].append(
                f"No running processes matched {self.paths.app_path.name}."
            )
            return live

        if not live["lsof_available"]:
            live["lsof_probe_status"] = "unavailable"
            live["notes"].append(
                "`lsof` is not available; live socket inspection was skipped."
            )
        lsof_success_count = 0
        lsof_errors: list[str] = []
        for process in processes:
            sockets = (
                self._collect_process_socket_snapshot(process["pid"])
                if live["lsof_available"]
                else {
                    "named_unix_sockets": [],
                    "anonymous_unix_socket_count": 0,
                    "tcp_listeners": [],
                    "errors": ["lsof:not available"],
                }
            )
            if live["lsof_available"]:
                if sockets["errors"]:
                    lsof_errors.extend(sockets["errors"])
                else:
                    lsof_success_count += 1
            live["processes"].append({**process, "sockets": sockets})

        if live["lsof_available"]:
            live["lsof_errors"] = self._ordered_unique(lsof_errors)
            live["lsof_error_count"] = len(live["lsof_errors"])
            if not live["lsof_errors"]:
                live["lsof_probe_status"] = "ok"
            elif lsof_success_count:
                live["lsof_probe_status"] = "partial"
                live["notes"].append(
                    "Some `lsof` socket probes failed; live socket data is partial."
                )
            else:
                live["lsof_probe_status"] = self._classify_aggregate_probe_errors(
                    live["lsof_errors"]
                )
                if live["lsof_probe_status"] == "blocked":
                    live["notes"].append(
                        "`lsof` is installed but the live socket probe was blocked."
                    )
                else:
                    live["notes"].append("`lsof` socket probes failed.")

        if any(item["role"] == "main-electron" for item in live["processes"]) and any(
            item["role"] == "ai-helper" for item in live["processes"]
        ):
            live["notes"].append(
                "Live processes show a split between the desktop Electron main process and the ai helper."
            )

        ai_helper = next(
            (item for item in live["processes"] if item["role"] == "ai-helper"),
            None,
        )
        if ai_helper and not ai_helper["sockets"]["named_unix_sockets"]:
            live["notes"].append(
                "The ai helper does not expose a named local unix socket through `lsof`, which matches the Electron `ahaIpc` path inferred from the bundle."
            )

        expected_ckg_port = topology.get("ckg", {}).get("port")
        ckg_listener_found = False
        for process in live["processes"]:
            if process["role"] != "ckg-helper":
                continue
            match = next(
                (
                    listener
                    for listener in process["sockets"]["tcp_listeners"]
                    if expected_ckg_port is not None
                    and f":{expected_ckg_port}" in listener
                ),
                None,
            )
            if match:
                ckg_listener_found = True
                live["notes"].append(
                    f"ckg-helper pid {process['pid']} is listening on {match}."
                )
                break
        if expected_ckg_port is not None and not ckg_listener_found:
            live["notes"].append(
                f"No live TCP listener for port {expected_ckg_port} was observed via `lsof`; the CKG classification still comes from logs."
            )

        live["notes"] = self._ordered_unique(live["notes"])
        return live

    def extract_rpc_inventory(self) -> dict[str, dict[str, Any]]:
        bundle = self._load_ai_chat_bundle()
        inventory: dict[str, dict[str, Any]] = {}
        for match in RPC_SERVICE_RE.finditer(bundle):
            service = match.group("service")
            apply_service = match.group("apply")
            methods = [
                {"symbol": symbol, "method": method}
                for symbol, method in RPC_METHOD_ENTRY_RE.findall(match.group("methods"))
            ]
            if not methods:
                continue
            record = inventory.setdefault(
                service,
                {
                    "service": service,
                    "apply_service": apply_service,
                    "methods": [],
                    "source_path": str(self.paths.ai_chat_bundle_path),
                },
            )
            if apply_service and not record.get("apply_service"):
                record["apply_service"] = apply_service
            seen = {
                (item["symbol"], item["method"])
                for item in record["methods"]
                if isinstance(item, dict)
            }
            for item in methods:
                key = (item["symbol"], item["method"])
                if key in seen:
                    continue
                seen.add(key)
                record["methods"].append(item)
        return inventory

    def list_rpc_services(self) -> list[dict[str, Any]]:
        inventory = self.extract_rpc_inventory()
        entries: list[dict[str, Any]] = []
        for service in sorted(inventory):
            record = inventory[service]
            methods = self._ordered_unique(
                [item["method"] for item in record.get("methods", [])]
            )
            entries.append(
                {
                    "service": service,
                    "apply_service": record.get("apply_service"),
                    "method_count": len(methods),
                    "methods": methods,
                    "source_path": record.get("source_path"),
                }
            )
        return entries

    def get_rpc_methods(self, service: str) -> dict[str, Any]:
        inventory = self.extract_rpc_inventory()
        if service not in inventory:
            raise KeyError(f"Unknown RPC service: {service}")
        record = inventory[service]
        return {
            "service": service,
            "apply_service": record.get("apply_service"),
            "methods": record.get("methods", []),
            "source_path": record.get("source_path"),
        }

    def list_rpc_activity(self, *, limit: int = 50) -> list[dict[str, Any]]:
        latest = self.latest_log_session_dir()
        if latest is None:
            return []
        stats: dict[tuple[str, str], dict[str, Any]] = {}
        for path in sorted(
            latest.glob("window*/renderer*.log"),
            key=self._renderer_log_sort_key,
        ):
            relative_path = str(path.relative_to(latest))
            for line in path.read_text(errors="replace").splitlines():
                match = TRANSPORT_REQUEST_RE.search(line)
                if not match:
                    continue
                key = (match.group("service"), match.group("method"))
                entry = stats.setdefault(
                    key,
                    {
                        "service": match.group("service"),
                        "method": match.group("method"),
                        "request_count": 0,
                        "success_count": 0,
                        "avg_success_cost_ms": None,
                        "last_cost_ms": None,
                        "source_logs": set(),
                        "_success_cost_total": 0,
                    },
                )
                cost = int(match.group("cost"))
                if match.group("success"):
                    entry["success_count"] += 1
                    entry["_success_cost_total"] += cost
                else:
                    entry["request_count"] += 1
                entry["last_cost_ms"] = cost
                entry["source_logs"].add(relative_path)

        entries: list[dict[str, Any]] = []
        for entry in stats.values():
            success_count = entry["success_count"]
            success_cost_total = entry.pop("_success_cost_total")
            entry["avg_success_cost_ms"] = (
                round(success_cost_total / success_count, 2) if success_count else None
            )
            entry["source_logs"] = sorted(entry["source_logs"])
            entries.append(entry)

        entries.sort(key=lambda item: (-item["request_count"], item["service"], item["method"]))
        return entries[:limit]

    def list_rpc_traces(
        self,
        *,
        limit: int = 20,
        service: Optional[str] = None,
        method: Optional[str] = None,
        log_session_dir: Optional[Path | str] = None,
    ) -> dict[str, Any]:
        latest = (
            Path(log_session_dir).expanduser().resolve()
            if log_session_dir
            else self.latest_log_session_dir()
        )
        if latest is None or not latest.exists():
            return {
                "latest_log_session": None,
                "log_session": None,
                "filters": {"service": service, "method": method},
                "traces": [],
            }

        traces: dict[str, dict[str, Any]] = {}
        pending_history_builds: list[str] = []
        current_history_query_trace_id: Optional[str] = None

        def later_timestamp(
            current: Optional[str], candidate: Optional[str]
        ) -> Optional[str]:
            if not candidate:
                return current
            if not current:
                return candidate
            current_dt = self._parse_iso_timestamp(current)
            candidate_dt = self._parse_iso_timestamp(candidate)
            if current_dt and candidate_dt:
                return candidate if candidate_dt >= current_dt else current
            return max(current, candidate)

        def internal_trace_key(
            *, trace_id: Optional[str], channel_id: Optional[str]
        ) -> Optional[str]:
            if trace_id:
                return trace_id
            if channel_id:
                return f"channel:{channel_id}"
            return None

        def ensure_trace(
            *,
            trace_id: Optional[str],
            channel_id: Optional[str],
            service_name: Optional[str],
            method_name: Optional[str],
            timestamp: Optional[str],
            source_log: str,
        ) -> Optional[dict[str, Any]]:
            key = internal_trace_key(trace_id=trace_id, channel_id=channel_id)
            if key is None:
                return None
            entry = traces.setdefault(
                key,
                {
                    "trace_id": trace_id,
                    "channel_id": channel_id,
                    "service": service_name,
                    "method": method_name,
                    "timestamp": timestamp,
                    "last_seen_at": timestamp,
                    "connect_session_id": None,
                    "session_id": None,
                    "response_size_bytes": None,
                    "message_model_count": None,
                    "message_model_actual": None,
                    "message_count": None,
                    "turn_count": None,
                    "prepared_server_history_ids_count": None,
                    "prepared_server_history_ids_sample": [],
                    "query_history_state_request_count": None,
                    "query_history_state_status": None,
                    "query_history_state_url": None,
                    "query_history_state_response_content_length": None,
                    "query_history_state_response_request_id": None,
                    "query_history_state_error": None,
                    "source_logs": {source_log},
                },
            )
            if trace_id and not entry.get("trace_id"):
                entry["trace_id"] = trace_id
            if channel_id and not entry.get("channel_id"):
                entry["channel_id"] = channel_id
            if service_name and not entry.get("service"):
                entry["service"] = service_name
            if method_name and not entry.get("method"):
                entry["method"] = method_name
            if timestamp and not entry.get("timestamp"):
                entry["timestamp"] = timestamp
            entry["last_seen_at"] = later_timestamp(entry.get("last_seen_at"), timestamp)
            entry.setdefault("source_logs", set()).add(source_log)
            return entry

        def parse_string_list(raw: str) -> list[str]:
            payload = self._safe_json_value(raw)
            if not isinstance(payload, list):
                return []
            return [item for item in payload if isinstance(item, str)]

        modular_dir = latest / "Modular"
        for path in sorted(modular_dir.glob("ai-agent*_stdout.log")):
            relative_path = str(path.relative_to(latest))
            for line in path.read_text(errors="replace").splitlines():
                timestamp = self._extract_timestamp(line)

                request_match = PROCESS_IPC_REQUEST_RE.search(line)
                if request_match:
                    ensure_trace(
                        trace_id=request_match.group("trace_id"),
                        channel_id=request_match.group("channel_id"),
                        service_name=request_match.group("service"),
                        method_name=request_match.group("method"),
                        timestamp=timestamp,
                        source_log=relative_path,
                    )

                route_match = PROCESS_IPC_ROUTE_RE.search(line)
                if route_match:
                    entry = ensure_trace(
                        trace_id=route_match.group("trace_id"),
                        channel_id=None,
                        service_name=route_match.group("service"),
                        method_name=route_match.group("method"),
                        timestamp=timestamp,
                        source_log=relative_path,
                    )
                    if entry is not None:
                        entry["connect_session_id"] = route_match.group(
                            "connect_session_id"
                        )

                convert_match = FAST_CONVERT_CHAT_MESSAGE_MODELS_RE.search(line)
                if convert_match:
                    entry = ensure_trace(
                        trace_id=convert_match.group("trace_id"),
                        channel_id=None,
                        service_name=None,
                        method_name=None,
                        timestamp=timestamp,
                        source_log=relative_path,
                    )
                    if entry is not None:
                        entry["message_model_count"] = int(convert_match.group("count"))
                        entry["message_model_actual"] = int(convert_match.group("actual"))

                message_match = CHAT_SERVICE_MESSAGES_WITH_TRACE_RE.search(line)
                if message_match:
                    entry = ensure_trace(
                        trace_id=message_match.group("trace_id"),
                        channel_id=None,
                        service_name=None,
                        method_name=None,
                        timestamp=timestamp,
                        source_log=relative_path,
                    )
                    if entry is not None:
                        entry["message_count"] = int(message_match.group("count"))

                turns_match = CHAT_SERVICE_TURNS_WITH_TRACE_RE.search(line)
                if turns_match:
                    entry = ensure_trace(
                        trace_id=turns_match.group("trace_id"),
                        channel_id=None,
                        service_name=None,
                        method_name=None,
                        timestamp=timestamp,
                        source_log=relative_path,
                    )
                    if entry is not None:
                        entry["turn_count"] = int(turns_match.group("count"))

                history_start_match = SERVER_HISTORY_BUILD_START_RE.search(line)
                if history_start_match:
                    trace_key = internal_trace_key(
                        trace_id=history_start_match.group("trace_id"),
                        channel_id=None,
                    )
                    entry = ensure_trace(
                        trace_id=history_start_match.group("trace_id"),
                        channel_id=None,
                        service_name=None,
                        method_name=None,
                        timestamp=timestamp,
                        source_log=relative_path,
                    )
                    if entry is not None:
                        entry["session_id"] = history_start_match.group("session_id")
                    if trace_key and trace_key not in pending_history_builds:
                        pending_history_builds.append(trace_key)

                response_size_match = re.search(
                    r'route end: response_size_bytes: Some\((?P<size>\d+)\) trace_id="(?P<trace_id>[A-Za-z0-9-]+)"',
                    line,
                )
                if response_size_match:
                    entry = ensure_trace(
                        trace_id=response_size_match.group("trace_id"),
                        channel_id=None,
                        service_name=None,
                        method_name=None,
                        timestamp=timestamp,
                        source_log=relative_path,
                    )
                    if entry is not None:
                        entry["response_size_bytes"] = int(
                            response_size_match.group("size")
                        )

                prepared_history_match = PREPARED_SERVER_HISTORY_IDS_RE.search(line)
                if prepared_history_match:
                    trace_key = (
                        pending_history_builds.pop(0)
                        if pending_history_builds
                        else current_history_query_trace_id
                    )
                    if trace_key and trace_key in traces:
                        entry = traces[trace_key]
                        entry["last_seen_at"] = later_timestamp(
                            entry.get("last_seen_at"), timestamp
                        )
                        entry.setdefault("source_logs", set()).add(relative_path)
                        history_ids = parse_string_list(
                            prepared_history_match.group("payload")
                        )
                        entry["prepared_server_history_ids_count"] = len(history_ids)
                        entry["prepared_server_history_ids_sample"] = history_ids[:5]
                        if history_ids:
                            entry["query_history_state_status"] = "pending"
                            current_history_query_trace_id = trace_key
                        else:
                            entry["query_history_state_status"] = "not-needed"
                            current_history_query_trace_id = None

                history_body_match = GET_HISTORY_STATE_BODY_RE.search(line)
                if history_body_match and current_history_query_trace_id:
                    entry = traces.get(current_history_query_trace_id)
                    if entry is not None:
                        entry["last_seen_at"] = later_timestamp(
                            entry.get("last_seen_at"), timestamp
                        )
                        entry.setdefault("source_logs", set()).add(relative_path)
                        history_ids = parse_string_list(
                            history_body_match.group("payload")
                        )
                        entry["query_history_state_request_count"] = len(history_ids)
                        entry["query_history_state_status"] = "requested"

                query_request_match = QUERY_HISTORY_STATE_REQUEST_RE.search(line)
                if query_request_match and current_history_query_trace_id:
                    entry = traces.get(current_history_query_trace_id)
                    if entry is not None:
                        entry["last_seen_at"] = later_timestamp(
                            entry.get("last_seen_at"), timestamp
                        )
                        entry.setdefault("source_logs", set()).add(relative_path)
                        entry["query_history_state_url"] = query_request_match.group("url")
                        if entry.get("query_history_state_status") in {None, "pending"}:
                            entry["query_history_state_status"] = "requested"

                query_response_match = QUERY_HISTORY_STATE_RESPONSE_HEADERS_RE.search(line)
                if query_response_match and current_history_query_trace_id:
                    entry = traces.get(current_history_query_trace_id)
                    if entry is not None:
                        entry["last_seen_at"] = later_timestamp(
                            entry.get("last_seen_at"), timestamp
                        )
                        entry.setdefault("source_logs", set()).add(relative_path)
                        entry["query_history_state_url"] = query_response_match.group(
                            "url"
                        )
                        headers = self._safe_json_loads(
                            query_response_match.group("payload")
                        )
                        if headers:
                            entry["query_history_state_response_content_length"] = (
                                self._parse_optional_int(headers.get("content-length"))
                            )
                            request_id = headers.get("x-request-id")
                            if isinstance(request_id, str) and request_id.strip():
                                entry["query_history_state_response_request_id"] = (
                                    request_id
                                )
                        entry["query_history_state_status"] = "ok"

                query_error_match = GET_HISTORY_STATE_ERROR_RE.search(line)
                if query_error_match and current_history_query_trace_id:
                    entry = traces.get(current_history_query_trace_id)
                    if entry is not None:
                        entry["last_seen_at"] = later_timestamp(
                            entry.get("last_seen_at"), timestamp
                        )
                        entry.setdefault("source_logs", set()).add(relative_path)
                        entry["query_history_state_status"] = "error"
                        entry["query_history_state_error"] = query_error_match.group(
                            "error"
                        )
                    current_history_query_trace_id = None

        filtered_traces: list[dict[str, Any]] = []
        for entry in traces.values():
            if service and entry.get("service") != service:
                continue
            if method and entry.get("method") != method:
                continue
            finalized = {
                key: value
                for key, value in entry.items()
                if key != "source_logs"
            }
            finalized["source_logs"] = sorted(entry.get("source_logs") or [])
            filtered_traces.append(finalized)

        filtered_traces.sort(
            key=lambda item: (
                self._chat_turn_sort_key(
                    {
                        "last_seen_at": item.get("last_seen_at"),
                        "started_at": item.get("timestamp"),
                    }
                )
            ),
            reverse=True,
        )

        return {
            "latest_log_session": str(latest),
            "log_session": str(latest),
            "filters": {"service": service, "method": method},
            "traces": filtered_traces[:limit],
        }

    def extract_recent_rpc_context(self, *, limit: int = 20) -> dict[str, Any]:
        latest = self.latest_log_session_dir()
        if latest is None:
            return {"latest_log_session": None, "projects": [], "sessions": [], "messages": []}

        projects_seen: list[dict[str, Any]] = []
        sessions_seen: list[dict[str, Any]] = []
        messages_seen: list[dict[str, Any]] = []

        for path in sorted(
            latest.glob("window*/renderer*.log"),
            key=self._renderer_log_sort_key,
        ):
            relative_path = str(path.relative_to(latest))
            for line in path.read_text(errors="replace").splitlines():
                timestamp = self._extract_timestamp(line)

                project_match = PROJECT_RESULT_RE.search(line)
                if project_match:
                    payload = self._safe_json_loads(project_match.group("payload"))
                    if payload and payload.get("project_id"):
                        projects_seen.append(
                            {
                                "project_id": payload.get("project_id"),
                                "real_project_id": payload.get("real_project_id"),
                                "timestamp": timestamp,
                                "source_log": relative_path,
                            }
                        )

                session_match = SESSION_SWITCH_RE.search(line)
                if session_match:
                    payload = self._safe_json_loads(session_match.group("payload"))
                    session_id = payload.get("sessionId") if payload else None
                    if session_id:
                        sessions_seen.append(
                            {
                                "session_id": session_id,
                                "source": "switchToSession",
                                "timestamp": timestamp,
                                "source_log": relative_path,
                            }
                        )

                stream_match = SESSION_STREAM_RE.search(line)
                if stream_match:
                    sessions_seen.append(
                        {
                            "session_id": stream_match.group("session_id"),
                            "source": "chatStream",
                            "timestamp": timestamp,
                            "source_log": relative_path,
                        }
                    )

                event_match = PARAMS_EVENT_RE.search(line)
                if event_match:
                    payload = self._safe_json_loads(event_match.group("payload"))
                    if not payload:
                        continue
                    session_id = payload.get("session_id")
                    message_id = payload.get("message_id")
                    event_name = event_match.group("event")
                    if session_id:
                        sessions_seen.append(
                            {
                                "session_id": session_id,
                                "source": f"event:{event_name}",
                                "timestamp": timestamp,
                                "source_log": relative_path,
                            }
                        )
                    if session_id and message_id:
                        messages_seen.append(
                            {
                                "session_id": session_id,
                                "message_id": message_id,
                                "event": event_name,
                                "chat_model": payload.get("chat_model"),
                                "agent_type": payload.get("agent_type"),
                                "timestamp": timestamp,
                                "source_log": relative_path,
                            }
                        )

        def latest_unique(entries: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
            result: list[dict[str, Any]] = []
            seen: set[Any] = set()
            for item in reversed(entries):
                value = item.get(key)
                if not value or value in seen:
                    continue
                seen.add(value)
                result.append(item)
                if len(result) >= limit:
                    break
            return result

        return {
            "latest_log_session": str(latest),
            "projects": latest_unique(projects_seen, "project_id"),
            "sessions": latest_unique(sessions_seen, "session_id"),
            "messages": latest_unique(messages_seen, "message_id"),
        }

    def _merge_chat_trace_into_turn(
        self, turn: dict[str, Any], trace: dict[str, Any], *, matched_by: str
    ) -> None:
        def later_timestamp(current: Optional[str], candidate: Optional[str]) -> Optional[str]:
            if not candidate:
                return current
            if not current:
                return candidate
            current_dt = self._parse_iso_timestamp(current)
            candidate_dt = self._parse_iso_timestamp(candidate)
            if current_dt and candidate_dt:
                return candidate if candidate_dt >= current_dt else current
            return max(current, candidate)

        turn["trace_match"] = matched_by
        for key in (
            "trace_id",
            "task_id",
            "backend_message_id",
            "session_id",
            "first_token_preview",
            "first_token_preview_source",
            "first_token_thought",
            "first_token_reasoning",
        ):
            if trace.get(key) and not turn.get(key):
                turn[key] = trace[key]

        turn["last_seen_at"] = later_timestamp(
            turn.get("last_seen_at"), trace.get("last_seen_at")
        )
        if not turn.get("started_at"):
            turn["started_at"] = trace.get("started_at")
        if trace.get("error_message") and not turn.get("error_message"):
            turn["error_message"] = trace["error_message"]
        if trace.get("source_logs"):
            turn.setdefault("source_logs", set()).update(trace["source_logs"])
        if trace.get("progress_notice_count"):
            turn["progress_notice_count"] = max(
                int(turn.get("progress_notice_count") or 0),
                int(trace.get("progress_notice_count") or 0),
            )
        if trace.get("plan_final_token_cost_ms") is not None:
            turn["plan_final_token_cost_ms"] = trace["plan_final_token_cost_ms"]
        if trace.get("first_token_flushed"):
            turn["first_token_flushed"] = True
        if trace.get("chat_finished"):
            turn["ai_agent_chat_finished"] = True
        if trace.get("task_failed"):
            turn["ai_agent_task_failed"] = True

    def _finalize_chat_turn(
        self, turn: dict[str, Any], *, include_events: bool
    ) -> dict[str, Any]:
        terminal_status = {
            "code_comp_complete_shown": "completed",
            "code_comp_fail": "failed",
            "code_comp_canceled": "canceled",
        }
        status = terminal_status.get(turn.get("terminal_event"))
        if status is None:
            if turn.get("ai_agent_task_failed") or turn.get("error_message"):
                status = "failed"
            elif turn.get("ai_agent_chat_finished"):
                status = "completed"
            else:
                status = "running"

        finalized = {
            key: value
            for key, value in turn.items()
            if not key.startswith("_") and key not in {"source_logs"}
        }
        finalized["status"] = status
        finalized["source_logs"] = sorted(turn.get("source_logs") or [])
        finalized["tool_call_count"] = len(turn.get("_tool_ids") or set())
        finalized["run_script_count"] = len(turn.get("_run_script_ids") or set())
        finalized["run_script_success_count"] = len(
            turn.get("_run_script_success_ids") or set()
        )
        tool_runs = []
        for tool_run in (turn.get("_tool_runs") or {}).values():
            finalized_tool_run = {
                key: value
                for key, value in tool_run.items()
                if key not in {"source_logs"}
            }
            finalized_tool_run["source_logs"] = sorted(tool_run.get("source_logs") or [])
            tool_runs.append(finalized_tool_run)
        finalized["tool_runs"] = tool_runs
        finalized["tool_run_count"] = len(tool_runs)
        finalized["tool_run_success_count"] = sum(
            1 for tool_run in tool_runs if tool_run.get("exit_code") == 0
        )
        if include_events:
            finalized["events"] = list(turn.get("_events") or [])
        return finalized

    def extract_chat_turns(
        self,
        *,
        limit: Optional[int] = 20,
        started_after: Optional[datetime] = None,
        include_events: bool = False,
        log_session_dir: Optional[Path | str] = None,
    ) -> dict[str, Any]:
        latest = (
            Path(log_session_dir).expanduser().resolve()
            if log_session_dir
            else self.latest_log_session_dir()
        )
        if latest is None or not latest.exists():
            return {"latest_log_session": None, "log_session": None, "turns": []}

        turns: dict[str, dict[str, Any]] = {}
        session_turns: dict[str, list[str]] = {}
        tool_runs_by_id: dict[str, dict[str, Any]] = {}
        pending_tool_runs: dict[str, dict[str, Any]] = {}

        def latest_turn_for_session(session_id: Optional[str]) -> Optional[dict[str, Any]]:
            if not session_id:
                return None
            frontend_ids = session_turns.get(session_id) or []
            if not frontend_ids:
                return None
            return turns.get(frontend_ids[-1])

        def ensure_tool_run(
            turn: dict[str, Any],
            tool_id: str,
            *,
            timestamp: Optional[str],
            source_log: str,
        ) -> dict[str, Any]:
            tool_run = (turn.setdefault("_tool_runs", {})).setdefault(
                tool_id,
                {
                    "tool_id": tool_id,
                    "timestamp": timestamp,
                    "command": None,
                    "terminal_type": None,
                    "terminal_instance_id": None,
                    "blocking": None,
                    "is_reused": None,
                    "detection_strategy": None,
                    "last_stage": None,
                    "exit_code": None,
                    "terminal_id": None,
                    "create_time": None,
                    "result_log_line_count": 0,
                    "result_log_excerpt": [],
                    "metrics": None,
                    "source_logs": {source_log},
                },
            )
            tool_run.setdefault("source_logs", set()).add(source_log)
            if timestamp and not tool_run.get("timestamp"):
                tool_run["timestamp"] = timestamp
            tool_runs_by_id[tool_id] = tool_run
            pending_update = pending_tool_runs.pop(tool_id, None)
            if pending_update:
                self._merge_tool_run_details(tool_run, pending_update)
            return tool_run

        for path in sorted(
            latest.glob("window*/renderer*.log"),
            key=self._renderer_log_sort_key,
        ):
            relative_path = str(path.relative_to(latest))
            for line in path.read_text(errors="replace").splitlines():
                timestamp = self._extract_timestamp(line)

                tooling_trace_match = TOOLING_TERMINAL_TRACE_RE.search(line)
                if tooling_trace_match:
                    payload = self._safe_json_value(tooling_trace_match.group("payload"))
                    if isinstance(payload, dict):
                        categories = payload.get("categories") or {}
                        tool_id = categories.get("tool_call_key")
                        session_id = categories.get("chat_session_id")
                        if isinstance(tool_id, str):
                            turn = latest_turn_for_session(session_id)
                            update = {
                                "timestamp": timestamp,
                                "command": categories.get("command"),
                                "terminal_type": categories.get("terminal_type"),
                                "terminal_instance_id": categories.get(
                                    "terminal_instance_id"
                                ),
                                "blocking": self._parse_boolish(categories.get("blocking")),
                                "is_reused": self._parse_boolish(
                                    categories.get("is_reused")
                                ),
                                "detection_strategy": categories.get(
                                    "detectionStrategy"
                                ),
                                "last_stage": categories.get("last_stage"),
                                "exit_code": self._parse_optional_int(
                                    categories.get("exitCode")
                                ),
                                "metrics": payload.get("metrics")
                                if isinstance(payload.get("metrics"), dict)
                                else None,
                                "source_logs": {relative_path},
                            }
                            if turn is None:
                                pending_tool_runs[tool_id] = self._merge_tool_run_details(
                                    pending_tool_runs.get(tool_id, {}),
                                    update,
                                )
                            else:
                                tool_run = ensure_tool_run(
                                    turn,
                                    tool_id,
                                    timestamp=timestamp,
                                    source_log=relative_path,
                                )
                                self._merge_tool_run_details(tool_run, update)

                tooling_result_match = TOOLING_COMMAND_RESULT_RE.search(line)
                if tooling_result_match:
                    payload = self._safe_json_value(tooling_result_match.group("payload"))
                    if isinstance(payload, list):
                        for item in payload:
                            if not isinstance(item, dict):
                                continue
                            tool_id = item.get("serverCallId")
                            if not isinstance(tool_id, str):
                                continue
                            logs = self._flatten_tool_logs(item.get("logs"))
                            update = {
                                "command": item.get("command"),
                                "exit_code": self._parse_optional_int(item.get("exitCode")),
                                "terminal_id": self._parse_optional_int(
                                    item.get("terminalId")
                                ),
                                "create_time": self._parse_optional_int(
                                    item.get("createTime")
                                ),
                                "detection_strategy": item.get("detectionStrategy"),
                                "result_log_line_count": len(logs),
                                "result_log_excerpt": self._build_tool_log_excerpt(logs),
                                "source_logs": {relative_path},
                            }
                            tool_run = tool_runs_by_id.get(tool_id)
                            if tool_run is None:
                                pending_tool_runs[tool_id] = self._merge_tool_run_details(
                                    pending_tool_runs.get(tool_id, {}),
                                    update,
                                )
                            else:
                                self._merge_tool_run_details(tool_run, update)

                event_match = PARAMS_EVENT_RE.search(line)
                if not event_match:
                    continue
                payload = self._safe_json_loads(event_match.group("payload"))
                if not payload:
                    continue
                frontend_message_id = payload.get("message_id")
                session_id = payload.get("session_id")
                if not frontend_message_id or not session_id:
                    continue

                event_name = event_match.group("event").strip()
                timestamp = self._extract_timestamp(line)
                turn = turns.setdefault(
                    frontend_message_id,
                    {
                        "session_id": session_id,
                        "frontend_message_id": frontend_message_id,
                        "backend_message_id": None,
                        "task_id": None,
                        "trace_id": None,
                        "chat_model": payload.get("chat_model"),
                        "agent_type": payload.get("agent_type"),
                        "started_at": None,
                        "last_seen_at": timestamp,
                        "terminal_event": None,
                        "error_code": None,
                        "error_message": None,
                        "cancel_reason": None,
                        "request_round_count": None,
                        "renderer_reported_tool_count": None,
                        "progress_notice_count": 0,
                        "plan_final_token_cost_ms": None,
                        "first_token_flushed": False,
                        "first_token_preview": None,
                        "first_token_preview_source": None,
                        "first_token_thought": None,
                        "first_token_reasoning": None,
                        "ai_agent_chat_finished": False,
                        "ai_agent_task_failed": False,
                        "trace_match": None,
                        "source_logs": {relative_path},
                        "_tool_ids": set(),
                        "_run_script_ids": set(),
                        "_run_script_success_ids": set(),
                        "_tool_runs": {},
                        "_events": [],
                    },
                )

                if session_id and session_id != turn.get("session_id"):
                    turn["session_id"] = session_id
                if payload.get("chat_model") and not turn.get("chat_model"):
                    turn["chat_model"] = payload.get("chat_model")
                if payload.get("agent_type") and not turn.get("agent_type"):
                    turn["agent_type"] = payload.get("agent_type")

                if event_name == "code_comp_trigger" and not turn.get("started_at"):
                    turn["started_at"] = timestamp
                turn["last_seen_at"] = timestamp or turn.get("last_seen_at")
                turn["source_logs"].add(relative_path)

                tool_id = payload.get("tool_id")
                if tool_id and event_name in {"tool_call_show", "file_tool_show"}:
                    turn["_tool_ids"].add(tool_id)
                if tool_id and event_name == "run_script_show":
                    turn["_tool_ids"].add(tool_id)
                    turn["_run_script_ids"].add(tool_id)
                if tool_id and event_name == "run_script_success":
                    turn["_tool_ids"].add(tool_id)
                    turn["_run_script_success_ids"].add(tool_id)

                if event_name == "code_comp_fail":
                    turn["terminal_event"] = event_name
                    turn["error_code"] = payload.get("error_code")
                    turn["error_message"] = payload.get("error_message")
                elif event_name == "code_comp_canceled":
                    turn["terminal_event"] = event_name
                    turn["cancel_reason"] = payload.get("cancel_reason")
                elif event_name == "code_comp_complete_shown":
                    turn["terminal_event"] = event_name
                    turn["request_round_count"] = payload.get("request_round_count")
                    turn["renderer_reported_tool_count"] = payload.get("tool_count")
                elif event_name == "code_comp_shown":
                    turn["reasoning_shown"] = payload.get("is_reasoning_shown")
                    turn["reasoning_duration_ms"] = payload.get("duration")

                event_summary = {"timestamp": timestamp, "event": event_name}
                if tool_id:
                    event_summary["tool_id"] = tool_id
                if payload.get("tool_type"):
                    event_summary["tool_type"] = payload.get("tool_type")
                if payload.get("block_type"):
                    event_summary["block_type"] = payload.get("block_type")
                if payload.get("duration") is not None:
                    event_summary["duration_ms"] = payload.get("duration")
                if payload.get("runtime_duration") is not None:
                    event_summary["runtime_duration_ms"] = payload.get("runtime_duration")
                if event_name == "code_comp_fail":
                    event_summary["error_code"] = payload.get("error_code")
                turn["_events"].append(event_summary)

                session_turns.setdefault(session_id, []).append(frontend_message_id)

        traces: dict[str, dict[str, Any]] = {}
        modular_dir = latest / "Modular"
        for path in sorted(modular_dir.glob("ai-agent*_stdout.log")):
            relative_path = str(path.relative_to(latest))
            for line in path.read_text(errors="replace").splitlines():
                timestamp = self._extract_timestamp(line)

                finish_match = CHAT_TURN_FINISH_RE.search(line)
                if finish_match:
                    trace = traces.setdefault(
                        finish_match.group("trace_id"),
                        {
                            "trace_id": finish_match.group("trace_id"),
                            "session_id": finish_match.group("session_id"),
                            "frontend_message_id": None,
                            "backend_message_id": None,
                            "task_id": None,
                            "started_at": timestamp,
                            "last_seen_at": timestamp,
                            "error_message": None,
                            "progress_notice_count": 0,
                            "plan_final_token_cost_ms": None,
                            "first_token_flushed": False,
                            "first_token_preview": None,
                            "first_token_preview_source": None,
                            "first_token_thought": None,
                            "first_token_reasoning": None,
                            "task_failed": False,
                            "chat_finished": False,
                            "source_logs": {relative_path},
                        },
                    )
                    trace["frontend_message_id"] = finish_match.group("message_id")
                    trace["session_id"] = finish_match.group("session_id")
                    trace["last_seen_at"] = timestamp
                    trace["chat_finished"] = True
                    trace["source_logs"].add(relative_path)

                trace_context_match = AI_AGENT_TRACE_CONTEXT_RE.search(line)
                if trace_context_match:
                    trace = traces.setdefault(
                        trace_context_match.group("trace_id"),
                        {
                            "trace_id": trace_context_match.group("trace_id"),
                            "session_id": trace_context_match.group("session_id"),
                            "frontend_message_id": None,
                            "backend_message_id": trace_context_match.group("message_id"),
                            "task_id": trace_context_match.group("task_id"),
                            "started_at": timestamp,
                            "last_seen_at": timestamp,
                            "error_message": None,
                            "progress_notice_count": 0,
                            "plan_final_token_cost_ms": None,
                            "first_token_flushed": False,
                            "first_token_preview": None,
                            "first_token_preview_source": None,
                            "first_token_thought": None,
                            "first_token_reasoning": None,
                            "task_failed": False,
                            "chat_finished": False,
                            "source_logs": {relative_path},
                        },
                    )
                    trace["session_id"] = trace_context_match.group("session_id")
                    if trace_context_match.group("task_id"):
                        trace["task_id"] = trace_context_match.group("task_id")
                    if trace_context_match.group("message_id"):
                        trace["backend_message_id"] = trace_context_match.group("message_id")
                    trace["last_seen_at"] = timestamp
                    trace["source_logs"].add(relative_path)

                    if "progress_notice" in line:
                        trace["progress_notice_count"] += 1
                    if "first token flushed" in line:
                        trace["first_token_flushed"] = True
                    first_token_match = FIRST_TOKEN_FLUSHED_RE.search(line)
                    if first_token_match:
                        thought = self._decode_logged_string(
                            first_token_match.group("thought")
                        )
                        reasoning = self._decode_logged_optional_string(
                            first_token_match.group("reasoning")
                        )
                        if thought is not None and trace.get("first_token_thought") is None:
                            trace["first_token_thought"] = thought
                        if (
                            reasoning is not None
                            and trace.get("first_token_reasoning") is None
                        ):
                            trace["first_token_reasoning"] = reasoning
                        if trace.get("first_token_preview") is None:
                            preview = thought or reasoning
                            if preview:
                                trace["first_token_preview"] = preview
                                trace["first_token_preview_source"] = (
                                    "thought" if thought else "reasoning"
                                )
                    cost_match = PLAN_FINAL_TOKEN_COST_RE.search(line)
                    if cost_match:
                        trace["plan_final_token_cost_ms"] = int(cost_match.group("cost"))
                    if "[ChatContextEntity] chat finished" in line:
                        trace["chat_finished"] = True
                    if "task failed:" in line:
                        trace["task_failed"] = True
                        error_match = TASK_FAILED_ERROR_RE.search(line)
                        if error_match:
                            trace["error_message"] = error_match.group("error")
                    error_match = CHAT_FINISHED_WITH_ERROR_RE.search(line)
                    if error_match:
                        trace["task_failed"] = True
                        trace["error_message"] = error_match.group("error")

        matched_turns: set[str] = set()
        for trace in traces.values():
            frontend_message_id = trace.get("frontend_message_id")
            if frontend_message_id and frontend_message_id in turns:
                self._merge_chat_trace_into_turn(
                    turns[frontend_message_id], trace, matched_by="frontend_message_id"
                )
                matched_turns.add(frontend_message_id)

        for trace in traces.values():
            if trace.get("frontend_message_id") in matched_turns:
                continue
            session_id = trace.get("session_id")
            if not session_id or session_id not in session_turns:
                continue

            trace_started_at = self._parse_iso_timestamp(trace.get("started_at"))
            candidate_turn: Optional[dict[str, Any]] = None
            candidate_key: Optional[str] = None
            for frontend_message_id in session_turns[session_id]:
                turn = turns.get(frontend_message_id)
                if not turn or turn.get("trace_id"):
                    continue
                turn_started_at = self._parse_iso_timestamp(turn.get("started_at"))
                if trace_started_at and turn_started_at and turn_started_at > trace_started_at:
                    continue
                if candidate_turn is None:
                    candidate_turn = turn
                    candidate_key = frontend_message_id
                    continue
                candidate_started_at = self._parse_iso_timestamp(candidate_turn.get("started_at"))
                if (
                    turn_started_at
                    and candidate_started_at
                    and turn_started_at > candidate_started_at
                ):
                    candidate_turn = turn
                    candidate_key = frontend_message_id

            if candidate_turn is not None and candidate_key is not None:
                self._merge_chat_trace_into_turn(
                    candidate_turn, trace, matched_by="session_started_at"
                )
                matched_turns.add(candidate_key)

        finalized_turns = [
            self._finalize_chat_turn(turn, include_events=include_events)
            for turn in turns.values()
        ]
        if started_after is not None:
            filtered_turns: list[dict[str, Any]] = []
            for turn in finalized_turns:
                turn_started_at = self._parse_iso_timestamp(
                    turn.get("started_at") or turn.get("last_seen_at")
                )
                if turn_started_at and turn_started_at >= started_after:
                    filtered_turns.append(turn)
            finalized_turns = filtered_turns

        finalized_turns.sort(key=self._chat_turn_sort_key, reverse=True)
        return {
            "latest_log_session": str(latest),
            "log_session": str(latest),
            "turns": finalized_turns if limit is None else finalized_turns[:limit],
        }

    def describe_chat_turn(self, target: str) -> dict[str, Any]:
        latest_session = self.latest_log_session_dir()
        for session_dir in reversed(self.list_log_session_dirs()):
            payload = self.extract_chat_turns(
                limit=None,
                include_events=True,
                log_session_dir=session_dir,
            )
            for turn in payload["turns"]:
                if target in {
                    turn.get("frontend_message_id"),
                    turn.get("backend_message_id"),
                    turn.get("session_id"),
                    turn.get("trace_id"),
                    turn.get("task_id"),
                }:
                    return {
                        "latest_log_session": str(latest_session) if latest_session else None,
                        "log_session": payload.get("log_session"),
                        "turn": turn,
                    }
        raise KeyError(f"Unknown chat turn target: {target}")

    def _select_matching_chat_turn(
        self,
        turns: list[dict[str, Any]],
        *,
        started_after: Optional[datetime] = None,
        exclude_frontend_ids: Optional[set[str]] = None,
    ) -> Optional[dict[str, Any]]:
        excluded = exclude_frontend_ids or set()
        unseen_turns = [
            turn
            for turn in turns
            if turn.get("frontend_message_id")
            and turn.get("frontend_message_id") not in excluded
        ]

        if started_after is not None:
            unseen_after_dispatch = []
            for turn in unseen_turns:
                turn_started_at = self._parse_iso_timestamp(
                    turn.get("started_at") or turn.get("last_seen_at")
                )
                if turn_started_at and turn_started_at >= started_after:
                    unseen_after_dispatch.append(turn)
            if unseen_after_dispatch:
                unseen_after_dispatch.sort(key=self._chat_turn_sort_key, reverse=True)
                return unseen_after_dispatch[0]

            # If we captured a baseline before dispatch, an unseen frontend message id
            # is still the strongest signal even when log timestamps arrive slightly
            # out of order.
            if excluded and unseen_turns:
                unseen_turns.sort(key=self._chat_turn_sort_key, reverse=True)
                return unseen_turns[0]

            timed_turns = []
            for turn in turns:
                turn_started_at = self._parse_iso_timestamp(
                    turn.get("started_at") or turn.get("last_seen_at")
                )
                if turn_started_at and turn_started_at >= started_after:
                    timed_turns.append(turn)
            if timed_turns:
                timed_turns.sort(key=self._chat_turn_sort_key, reverse=True)
                return timed_turns[0]
            return None

        if unseen_turns:
            unseen_turns.sort(key=self._chat_turn_sort_key, reverse=True)
            return unseen_turns[0]
        return turns[0] if turns else None

    def wait_for_chat_turn(
        self,
        *,
        started_after: Optional[datetime] = None,
        wait_seconds: float = 0.0,
        poll_interval: float = 0.5,
        exclude_frontend_ids: Optional[set[str]] = None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max(wait_seconds, 0.0)
        latest_payload = self.extract_chat_turns(
            limit=20,
            include_events=True,
        )
        latest_turn = latest_payload["turns"][0] if latest_payload["turns"] else None
        best_turn = self._select_matching_chat_turn(
            latest_payload["turns"],
            started_after=started_after,
            exclude_frontend_ids=exclude_frontend_ids,
        )
        terminal_statuses = {"completed", "failed", "canceled"}

        while time.monotonic() < deadline:
            if best_turn and best_turn.get("status") in terminal_statuses:
                break
            time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))
            latest_payload = self.extract_chat_turns(
                limit=20,
                include_events=True,
            )
            latest_turn = latest_payload["turns"][0] if latest_payload["turns"] else latest_turn
            matched_turn = self._select_matching_chat_turn(
                latest_payload["turns"],
                started_after=started_after,
                exclude_frontend_ids=exclude_frontend_ids,
            )
            if matched_turn is not None:
                best_turn = matched_turn

        note = None
        if best_turn is None:
            note = (
                "No new local chat turn matched the dispatch window. The bundled "
                "Trae CLI may have opened or focused the app without submitting a "
                "prompt, or the turn has not been logged yet."
            )

        return {
            "latest_log_session": latest_payload.get("latest_log_session"),
            "matched_after_dispatch": best_turn is not None,
            "turn": best_turn,
            "latest_turn": latest_turn,
            "note": note,
        }

    def extract_transport_topology(self, *, include_live: bool = False) -> dict[str, Any]:
        latest = self.latest_log_session_dir()
        topology: dict[str, Any] = {
            "latest_log_session": str(latest) if latest else None,
            "main_socket": str(self.main_socket()) if self.main_socket() else None,
            "app_rpc": {
                "kind": "aha-ipc",
                "client_connected_service": None,
                "connected_services": [],
                "server_enabled": None,
                "service_name": None,
                "ffi_connection_accepted": False,
                "jsonrpsee_server_started": False,
                "source_logs": [],
                "local_impl": self._extract_local_aha_ipc_implementation(),
                "node_socket_rule": None,
                "observed_ipc_addresses": [],
            },
            "ckg": {
                "kind": "grpc",
                "host": None,
                "port": None,
                "saw_grpc_content_type": False,
                "source_logs": [],
            },
            "oauth_callback": {
                "kind": "local-oauth-callback",
                "port": None,
                "source_logs": [],
            },
            "request_count": 0,
            "ai_agent_request_count": 0,
            "sample_request": None,
            "notes": [],
        }
        if latest is None:
            service_name = (
                topology["app_rpc"]["service_name"]
                or topology["app_rpc"]["client_connected_service"]
                or "ai-agent"
            )
            topology["app_rpc"]["node_socket_rule"] = self._extract_node_aha_ipc_socket_rule(
                service_name
            )
            return topology

        requests: list[dict[str, Any]] = []
        responses: list[dict[str, Any]] = []
        ai_agent_requests: dict[str, dict[str, Any]] = {}

        main_log = latest / "main.log"
        if main_log.exists():
            relative_path = str(main_log.relative_to(latest))
            for line in main_log.read_text(errors="replace").splitlines():
                timestamp = self._extract_timestamp(line)

                connect_match = AHA_IPC_CONNECT_RE.search(line)
                if connect_match:
                    connected_service = connect_match.group("service")
                    topology["app_rpc"]["connected_services"].append(connected_service)
                    if (
                        topology["app_rpc"]["client_connected_service"] is None
                        or connected_service == "ai-agent"
                    ):
                        topology["app_rpc"]["client_connected_service"] = connected_service
                    topology["app_rpc"]["source_logs"].append(relative_path)

                options_match = AHA_IPC_OPTIONS_RE.search(line)
                if options_match:
                    options = self._safe_json_loads(options_match.group("payload"))
                    if options:
                        topology["app_rpc"]["server_enabled"] = self._parse_boolish(
                            options.get("ENABLE_IPC_SERVER")
                        )
                        topology["app_rpc"]["service_name"] = options.get(
                            "AHA_IPC_SERVICE_NAME"
                        )
                        topology["app_rpc"]["source_logs"].append(relative_path)
                if topology["app_rpc"]["server_enabled"] is None:
                    enabled_match = AI_AGENT_IPC_SERVER_ENABLED_RE.search(line)
                    if enabled_match:
                        topology["app_rpc"]["server_enabled"] = self._parse_boolish(
                            enabled_match.group("value")
                        )
                        topology["app_rpc"]["source_logs"].append(relative_path)
                if topology["app_rpc"]["service_name"] is None:
                    service_match = AI_AGENT_IPC_SERVICE_RE.search(line)
                    if service_match:
                        topology["app_rpc"]["service_name"] = service_match.group("service")
                        topology["app_rpc"]["source_logs"].append(relative_path)

                request_match = DO_REQUEST_DATA_RE.search(line)
                if request_match:
                    payload = self._safe_json_loads(request_match.group("payload"))
                    if payload:
                        params = payload.get("params") or {}
                        client_info = params.get("client_info") or {}
                        requests.append(
                            {
                                "timestamp": timestamp,
                                "source_log": relative_path,
                                "packet_type": payload.get("packet_type"),
                                "session_id": payload.get("session_id"),
                                "channel_id": payload.get("channel_id"),
                                "service": params.get("service"),
                                "method": params.get("method"),
                                "connect_session_id": client_info.get(
                                    "connect_session_id"
                                ),
                                "payload": payload,
                            }
                        )
                        topology["app_rpc"]["source_logs"].append(relative_path)

                response_match = DO_REQUEST_RESPONSE_RE.search(line)
                if response_match:
                    payload = self._safe_json_loads(response_match.group("payload"))
                    if payload:
                        responses.append(
                            {
                                "timestamp": timestamp,
                                "source_log": relative_path,
                                "message": payload.get("message"),
                                "code": payload.get("code"),
                                "payload": payload,
                            }
                        )
                        topology["app_rpc"]["source_logs"].append(relative_path)

                oauth_match = OAUTH_PORT_RE.search(line)
                if oauth_match:
                    topology["oauth_callback"]["port"] = int(oauth_match.group("port"))
                    topology["oauth_callback"]["source_logs"].append(relative_path)

                if "ElectronAhaIpcServer" in line:
                    topology["app_rpc"]["source_logs"].append(relative_path)

        modular_dir = latest / "Modular"
        for path in sorted(modular_dir.glob("ai-agent*_stdout.log")):
            relative_path = str(path.relative_to(latest))
            for line in path.read_text(errors="replace").splitlines():
                timestamp = self._extract_timestamp(line)

                if "[aha_ipc] new FFI connection accepted" in line:
                    topology["app_rpc"]["ffi_connection_accepted"] = True
                    topology["app_rpc"]["source_logs"].append(relative_path)

                if "[aha_ipc] jsonrpsee server started" in line:
                    topology["app_rpc"]["jsonrpsee_server_started"] = True
                    topology["app_rpc"]["source_logs"].append(relative_path)

                ipc_match = PROCESS_IPC_REQUEST_RE.search(line)
                if ipc_match:
                    channel_id = ipc_match.group("channel_id")
                    ai_agent_requests[channel_id] = {
                        "timestamp": timestamp,
                        "source_log": relative_path,
                        "channel_id": channel_id,
                        "service": ipc_match.group("service"),
                        "method": ipc_match.group("method"),
                    }
                    topology["app_rpc"]["source_logs"].append(relative_path)

                lookup_match = CKG_LOOKUP_ADDR_RE.search(line)
                if lookup_match:
                    topology["ckg"]["host"] = lookup_match.group("host")
                    topology["ckg"]["port"] = int(lookup_match.group("port"))
                    topology["ckg"]["source_logs"].append(relative_path)

                if GRPC_CONTENT_TYPE_RE.search(line):
                    topology["ckg"]["saw_grpc_content_type"] = True
                    topology["ckg"]["source_logs"].append(relative_path)

        for path in sorted(modular_dir.glob("ckg*_stdout.log")):
            relative_path = str(path.relative_to(latest))
            for line in path.read_text(errors="replace").splitlines():
                start_match = CKG_SERVER_START_RE.search(line)
                if start_match:
                    topology["ckg"]["port"] = int(start_match.group("port"))
                    topology["ckg"]["source_logs"].append(relative_path)

        for path in sorted(latest.rglob("*.log")):
            relative_path = str(path.relative_to(latest))
            for line in path.read_text(errors="replace").splitlines():
                address_match = AHA_IPC_ADDRESS_RE.search(line)
                if not address_match:
                    continue
                if address_match.group("service") not in {"ai-agent", "ai_agent"}:
                    continue
                topology["app_rpc"]["observed_ipc_addresses"].append(
                    {
                        "source_log": relative_path,
                        "service": address_match.group("service"),
                        "address": address_match.group("address"),
                    }
                )

        for index, request in enumerate(requests):
            if index < len(responses):
                request["response"] = responses[index]
            matched_request = ai_agent_requests.get(request.get("channel_id"))
            if matched_request:
                request["matched_ai_agent_request"] = matched_request

        topology["request_count"] = len(requests)
        topology["ai_agent_request_count"] = len(ai_agent_requests)

        if requests:
            matched_requests = [
                request
                for request in requests
                if request.get("matched_ai_agent_request") is not None
            ]
            topology["sample_request"] = (matched_requests or requests)[-1]

        topology["app_rpc"]["source_logs"] = self._ordered_unique(
            topology["app_rpc"]["source_logs"]
        )
        topology["app_rpc"]["connected_services"] = self._ordered_unique(
            topology["app_rpc"]["connected_services"]
        )
        if (
            "ai-agent" in topology["app_rpc"]["connected_services"]
            and topology["app_rpc"]["client_connected_service"] != "ai-agent"
        ):
            topology["app_rpc"]["client_connected_service"] = "ai-agent"
        deduped_ipc_addresses: list[dict[str, Any]] = []
        seen_ipc_addresses: set[tuple[Any, Any]] = set()
        for item in topology["app_rpc"]["observed_ipc_addresses"]:
            key = (item.get("address"), item.get("source_log"))
            if key in seen_ipc_addresses:
                continue
            seen_ipc_addresses.add(key)
            deduped_ipc_addresses.append(item)
        topology["app_rpc"]["observed_ipc_addresses"] = deduped_ipc_addresses
        topology["ckg"]["source_logs"] = self._ordered_unique(
            topology["ckg"]["source_logs"]
        )
        topology["oauth_callback"]["source_logs"] = self._ordered_unique(
            topology["oauth_callback"]["source_logs"]
        )
        service_name = (
            topology["app_rpc"]["service_name"]
            or topology["app_rpc"]["client_connected_service"]
            or "ai-agent"
        )
        topology["app_rpc"]["node_socket_rule"] = self._extract_node_aha_ipc_socket_rule(
            service_name
        )
        if include_live:
            topology["live"] = self._collect_live_transport_probe(topology)

        notes: list[str] = []
        sample_request = topology.get("sample_request") or {}
        matched_request = sample_request.get("matched_ai_agent_request") or {}
        if sample_request and matched_request:
            notes.append(
                f"Main-process doRequest {sample_request['service']}.{sample_request['method']} "
                f"is forwarded to ai-agent over AHA IPC and matches ai-agent channel_id "
                f"{sample_request['channel_id']}."
            )
        elif topology["app_rpc"]["client_connected_service"]:
            notes.append(
                f"Main-process RPC connects to local AHA IPC service "
                f"{topology['app_rpc']['client_connected_service']}."
            )
        local_impl = topology["app_rpc"].get("local_impl") or {}
        if local_impl.get("bundle_verified"):
            notes.append(
                "The installed desktop bundle uses Electron's built-in ahaIpc API "
                "for local AHA RPC (`electron.ahaIpc.connect/serve`)."
            )
        observed_addresses = topology["app_rpc"].get("observed_ipc_addresses") or []
        if observed_addresses:
            notes.append(
                "Named `ipc://.../aha/*.sock` addresses were observed in node-side "
                "logs, which matches the bundled `@aha-kit/ipc` implementation."
            )
        if topology["ckg"]["port"] is not None:
            host = topology["ckg"]["host"] or "127.0.0.1"
            notes.append(
                f"{host}:{topology['ckg']['port']} is the CKG sidecar gRPC endpoint, not "
                "the app RPC envelope transport."
            )
        if topology["oauth_callback"]["port"] is not None:
            notes.append(
                f"Port {topology['oauth_callback']['port']} belongs to the local Supabase "
                "OAuth callback server."
            )
        topology["notes"] = notes

        return topology

    def _extract_chat_request_payload_hint(self, bundle: str) -> Optional[dict[str, Any]]:
        match = CHAT_REQUEST_OBJECT_RE.search(bundle)
        if not match:
            return None
        body = match.group("body")
        base_match = CHAT_REQUEST_BASE_RE.search(body)
        if not base_match:
            return None
        base_fields = self._ordered_unique(OBJECT_FIELD_RE.findall(base_match.group("fields")))
        derived_fields = self._ordered_unique(OBJECT_ASSIGNMENT_RE.findall(body))
        return {
            "source": "bundle.createChatRequestObject",
            "base_fields": base_fields,
            "derived_fields": derived_fields,
            "has_model_auto_selection_spread": "...S" in base_match.group("fields"),
            "notes": [
                "message_content is synthesized from text input and image attachments",
                "workspace_folders may be rewritten to an active worktree path",
                "plan/spec flags are injected after the base object is created",
            ],
        }

    def _extract_callsite_fields(self, bundle: str, call_name: str) -> list[str]:
        pattern = re.compile(rf"{call_name}\(\{{(?P<body>.*?)\}}\)", re.S)
        fields: list[str] = []
        for match in pattern.finditer(bundle):
            fields.extend(OBJECT_FIELD_RE.findall(match.group("body")))
        return self._ordered_unique(fields)

    def extract_rpc_payload_hints(self) -> dict[tuple[str, str], dict[str, Any]]:
        bundle = self._load_ai_chat_bundle()
        hints: dict[tuple[str, str], dict[str, Any]] = {}

        chat_hint = self._extract_chat_request_payload_hint(bundle)
        if chat_hint:
            hints[("chat", "chat")] = chat_hint

        callsite_specs = [
            (
                ("chat", "create_session"),
                "createSession",
                "bundle.createNewSession",
                ["project_id is sourced from the current project store"],
            ),
            (
                ("chat", "get_sessions"),
                "getSessions",
                "bundle.loadSessionList",
                ["session_type is optional and used for proactive chat lists"],
            ),
            (
                ("chat", "count_sessions"),
                "countSessions",
                "bundle.loadSessionList",
                ["project_id is the only observed field in current bundle callsites"],
            ),
            (
                ("chat", "get_messages"),
                "getSessionMessages",
                "bundle.loadSessionMessages",
                ["page_size and next_page_token appear in different bundle callsites"],
            ),
        ]

        for key, call_name, source, notes in callsite_specs:
            fields = self._extract_callsite_fields(bundle, call_name)
            if not fields:
                continue
            hints[key] = {
                "source": source,
                "fields": fields,
                "notes": notes,
            }

        return hints

    def describe_rpc_method(self, service: str, method: str) -> dict[str, Any]:
        inventory = self.extract_rpc_inventory()
        record = inventory.get(service)
        known_methods = (
            {item["method"]: item["symbol"] for item in record.get("methods", [])}
            if record
            else {}
        )
        payload_hints = self.extract_rpc_payload_hints()
        recent_context = self.extract_recent_rpc_context(limit=5)

        recent_samples: dict[str, Any] = {}
        if method in {"create_session", "get_sessions", "count_sessions"} and recent_context["projects"]:
            recent_samples["project_id"] = recent_context["projects"][0]["project_id"]
        if method in {"chat", "get_messages"} and recent_context["sessions"]:
            recent_samples["session_id"] = recent_context["sessions"][0]["session_id"]
        if method == "chat" and recent_context["messages"]:
            recent_samples["message_id"] = recent_context["messages"][0]["message_id"]

        return {
            "service": service,
            "method": method,
            "bundle_verified": method in known_methods,
            "symbol": known_methods.get(method),
            "apply_service": record.get("apply_service") if record else None,
            "source_path": record.get("source_path") if record else str(self.paths.ai_chat_bundle_path),
            "payload_hint": payload_hints.get((service, method)),
            "recent_samples": recent_samples or None,
        }

    def _binary_store_kind(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {"path": str(path), "exists": False, "kind": "missing"}
        header = path.read_bytes()[:16]
        kind = "sqlite" if header.startswith(b"SQLite format 3\x00") else "opaque-binary"
        return {"path": str(path), "exists": True, "kind": kind}

    def probe(self) -> dict[str, Any]:
        info = self.load_info_plist()
        product = self.load_product_json()
        state = self.read_state_summary()
        latest_log_session = self.latest_log_session_dir()
        cli_config_path = self.paths.cli_config_path
        cli_config = self.read_cli_config()
        configured_app_path = self._config_path_value(cli_config, "app_path")
        configured_support_dir = self._config_path_value(cli_config, "support_dir")
        configured_user_data_dir = self._config_path_value(cli_config, "user_data_dir")
        return {
            "app": {
                "path": str(self.paths.app_path),
                "display_name": info.get("CFBundleDisplayName"),
                "bundle_identifier": info.get("CFBundleIdentifier"),
                "bundle_version": info.get("CFBundleShortVersionString"),
                "product_app_version": product.get("appVersion"),
                "url_protocol": product.get("urlProtocol"),
                "cli_script": str(self.paths.cli_script_path),
                "cli_exists": self.paths.cli_script_path.exists(),
            },
            "paths": {
                "support_dir": str(self.paths.support_dir),
                "user_data_dir": str(self.paths.user_data_dir),
                "main_socket": str(self.main_socket()) if self.main_socket() else None,
                "latest_log_session": str(latest_log_session)
                if latest_log_session
                else None,
                "modular_data_dir": str(self.paths.modular_data_dir),
                "ai_chat_bundle": str(self.paths.ai_chat_bundle_path),
            },
            "config": {
                "path": str(cli_config_path),
                "exists": cli_config_path.exists(),
                "app_path": str(configured_app_path) if configured_app_path else None,
                "support_dir": str(configured_support_dir)
                if configured_support_dir
                else None,
                "user_data_dir": str(configured_user_data_dir)
                if configured_user_data_dir
                else None,
            },
            "auth": self.load_auth_info(),
            "state": state,
            "mcp_gallery_count": len(self.list_mcp_gallery()),
            "sandbox_count": len(self.list_sandboxes(limit=200)),
            "ckg_local_env": self.read_ckg_local_env(),
            "model_cache_counts": self.parse_model_cache_counts(),
            "data_stores": {
                "ai_agent_db": self._binary_store_kind(
                    self.paths.modular_data_dir / "ai-agent" / "database.db"
                ),
                "ckg_env_db": self._binary_store_kind(
                    self.paths.modular_data_dir / "ckg_server" / "env_codekg.db"
                ),
            },
        }
