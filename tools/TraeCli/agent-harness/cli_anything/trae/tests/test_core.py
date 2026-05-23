from __future__ import annotations

import json
import shutil
import sqlite3
import socket
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from cli_anything.trae.tests.fixtures import create_fixture
from cli_anything.trae.utils.trae_backend import (
    BRIDGE_EXTENSION_DEFAULT_TIMEOUT_SECONDS,
    HEADLESS_PATCH_MARKER,
    TraeBackend,
)


class TraeBackendCoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.fixture = create_fixture(self.root)
        self.backend = TraeBackend(
            app_path=self.fixture["app_path"],
            support_dir=self.fixture["support_dir"],
            user_data_dir=self.fixture["user_data_dir"],
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _evaluate_cdp_extract_response(
        self,
        *,
        snapshot: list[dict[str, object]],
        baseline: list[dict[str, object]],
    ) -> dict[str, object]:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not available")

        bridge_path = (
            Path(__file__).resolve().parent.parent / "utils" / "trae_cdp_bridge.js"
        )
        script = """
const fs = require("node:fs");
const vm = require("node:vm");

const bridgePath = process.argv[1];
const payload = JSON.parse(fs.readFileSync(0, "utf8"));
const source = fs.readFileSync(bridgePath, "utf8");
const start = source.indexOf("function extractAutomationResponse(snapshot = [], baseline = []) {");
const end = source.indexOf("function normalizeComparableText", start);
if (start < 0 || end < 0) {
  throw new Error("extractAutomationResponse source not found");
}
const snippet = source.slice(start, end);
const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(
  `${snippet}\\n__result = extractAutomationResponse(__snapshot, __baseline);`,
  Object.assign(sandbox, {
    __snapshot: payload.snapshot,
    __baseline: payload.baseline,
  }),
);
process.stdout.write(JSON.stringify(sandbox.__result));
"""

        completed = subprocess.run(
            [node, "-e", script, str(bridge_path)],
            input=json.dumps(
                {
                    "snapshot": snapshot,
                    "baseline": baseline,
                },
                ensure_ascii=False,
            ),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=completed.stderr or completed.stdout or "node evaluation failed",
        )
        return json.loads(completed.stdout)

    def _evaluate_cdp_build_activity_state(
        self,
        *,
        text: str,
        prompt: str,
    ) -> dict[str, object]:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not available")

        bridge_path = (
            Path(__file__).resolve().parent.parent / "utils" / "trae_cdp_bridge.js"
        )
        script = """
const fs = require("node:fs");
const vm = require("node:vm");

const bridgePath = process.argv[1];
const payload = JSON.parse(fs.readFileSync(0, "utf8"));
const source = fs.readFileSync(bridgePath, "utf8");
const start = source.indexOf("function normalizeComparableText(value) {");
const end = source.indexOf("async function waitForReady", start);
if (start < 0 || end < 0) {
  throw new Error("buildActivityState source not found");
}
const snippet = source.slice(start, end);
const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(
  `${snippet}\\n__result = buildActivityState(__text, __prompt);`,
  Object.assign(sandbox, {
    __text: payload.text,
    __prompt: payload.prompt,
  }),
);
process.stdout.write(JSON.stringify(sandbox.__result));
"""

        completed = subprocess.run(
            [node, "-e", script, str(bridge_path)],
            input=json.dumps(
                {
                    "text": text,
                    "prompt": prompt,
                },
                ensure_ascii=False,
            ),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=completed.stderr or completed.stdout or "node evaluation failed",
        )
        return json.loads(completed.stdout)

    def _evaluate_cdp_select_target(
        self,
        *,
        targets: list[dict[str, object]],
        config: dict[str, object],
        inspect_states: Optional[dict[str, dict[str, object]]] = None,
    ) -> dict[str, object]:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not available")

        bridge_path = (
            Path(__file__).resolve().parent.parent / "utils" / "trae_cdp_bridge.js"
        )
        script = """
const fs = require("node:fs");
const vm = require("node:vm");

const bridgePath = process.argv[1];
const payload = JSON.parse(fs.readFileSync(0, "utf8"));
const source = fs.readFileSync(bridgePath, "utf8");
const start = source.indexOf("function isInspectablePageTarget(target) {");
const end = source.indexOf("async function discoverTarget", start);
if (start < 0 || end < 0) {
  throw new Error("selectTarget source not found");
}
const snippet = source.slice(start, end);
const sandbox = {};
vm.createContext(sandbox);
Object.assign(sandbox, {
  __targets: payload.targets,
  __config: payload.config,
  __inspectStates: payload.inspect_states,
});
Promise.resolve(
  vm.runInContext(
    `(async () => {
      ${snippet}
      inspectTargetWindowState = async (target) => (__inspectStates || {})[String(target.id || "")] || null;
      return await selectTarget(__targets, __config);
    })()`,
    sandbox,
  ),
)
  .then((result) => {
    process.stdout.write(JSON.stringify(result));
  })
  .catch((error) => {
    console.error(error && error.stack ? error.stack : String(error));
    process.exitCode = 1;
  });
"""

        completed = subprocess.run(
            [node, "-e", script, str(bridge_path)],
            input=json.dumps(
                {
                    "targets": targets,
                    "config": config,
                    "inspect_states": inspect_states or {},
                },
                ensure_ascii=False,
            ),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=completed.stderr or completed.stdout or "node evaluation failed",
        )
        return json.loads(completed.stdout)

    def test_probe_collects_sanitized_runtime_state(self) -> None:
        probe = self.backend.probe()

        self.assertEqual(probe["app"]["display_name"], "Trae")
        self.assertEqual(probe["auth"]["username"], "Fixture User")
        self.assertNotIn("token", probe["auth"])
        self.assertEqual(probe["state"]["selected_model"]["display_name"], "GLM-4.7")
        self.assertEqual(probe["state"]["solo_mode"]["mode"], "ide")
        self.assertEqual(probe["model_cache_counts"]["chat_v3"], 28)
        self.assertEqual(probe["model_cache_counts"]["builder_v3"], 29)
        self.assertEqual(probe["mcp_gallery_count"], 1)
        self.assertEqual(probe["sandbox_count"], 1)
        self.assertEqual(probe["data_stores"]["ai_agent_db"]["kind"], "opaque-binary")

    def test_read_model_state_resolves_current_models_and_sanitizes_secrets(self) -> None:
        payload = self.backend.read_model_state()

        self.assertEqual(
            payload["current_models"]["dev_builder"]["model"]["display_name"],
            "GPT-5.3 Codex",
        )
        self.assertEqual(
            payload["current_models"]["dev_builder"]["model_key"],
            "1_-_gpt-5.3-codex",
        )
        self.assertEqual(
            payload["current_models"]["solo_coder"]["model"]["display_name"],
            "GLM-4.7 SOLO",
        )
        self.assertEqual(
            payload["available_model_counts"],
            {
                "dev_builder": 2,
                "solo_coder": 2,
                "solo_builder": 1,
            },
        )
        self.assertNotIn("ak", payload["selected_model"])
        self.assertNotIn("sk", payload["selected_model"])
        self.assertNotIn("custom_config", payload["selected_model"])

    def test_cdp_extract_response_ignores_layout_only_changes(self) -> None:
        baseline = [
            {
                "text": "收到！请告诉我您需要什么帮助。",
                "tagName": "div",
                "className": "assistant-chat-turn-content",
                "top": 120,
                "left": 960,
                "messageId": "",
                "dataId": "",
                "testId": "",
                "parentMessageId": "",
                "parentTurnIndex": "",
            }
        ]
        snapshot = [
            {
                **baseline[0],
                "top": 88,
                "left": 944,
            }
        ]

        payload = self._evaluate_cdp_extract_response(
            snapshot=snapshot,
            baseline=baseline,
        )

        self.assertEqual(payload["source"], "unchanged")
        self.assertEqual(payload["text"], "")

    def test_cdp_extract_response_keeps_same_text_when_parent_message_changes(self) -> None:
        baseline = [
            {
                "text": "OK",
                "tagName": "div",
                "className": "assistant-chat-turn-content",
                "top": 120,
                "left": 960,
                "messageId": "",
                "dataId": "",
                "testId": "",
                "parentMessageId": "msg-1",
                "parentTurnIndex": "1",
            }
        ]
        snapshot = [
            baseline[0],
            {
                **baseline[0],
                "top": 168,
                "parentMessageId": "msg-2",
                "parentTurnIndex": "2",
            },
        ]

        payload = self._evaluate_cdp_extract_response(
            snapshot=snapshot,
            baseline=baseline,
        )

        self.assertEqual(payload["source"], "new_message_ids")
        self.assertEqual(payload["text"], "OK")

    def test_cdp_extract_response_prefers_growth_delta_over_replaced_whole_text(self) -> None:
        baseline = [
            {
                "text": "用户95104202310收到回复我 已收到您的消息，请告诉我您需要什么帮助。",
                "tagName": "div",
                "className": "chat-content-container",
                "top": 120,
                "left": 960,
                "messageId": "",
                "dataId": "",
                "testId": "",
                "parentMessageId": "",
                "parentTurnIndex": "",
            }
        ]
        snapshot = [
            {
                **baseline[0],
                "text": (
                    "用户95104202310收到回复我 已收到您的消息，请告诉我您需要什么帮助。 "
                    "今天是2026年4月12日，星期日。"
                ),
                "top": 88,
            }
        ]

        payload = self._evaluate_cdp_extract_response(
            snapshot=snapshot,
            baseline=baseline,
        )

        self.assertEqual(payload["source"], "last_node_growth")
        self.assertEqual(payload["text"], " 今天是2026年4月12日，星期日。")

    def test_cdp_extract_response_extracts_inserted_middle_segment(self) -> None:
        baseline = [
            {
                "text": "用户95104202310收到回复我 收到！请问有什么需要我帮助的吗？任务完成",
                "tagName": "div",
                "className": "chat-content-container",
                "top": 120,
                "left": 960,
                "messageId": "",
                "dataId": "",
                "testId": "",
                "parentMessageId": "",
                "parentTurnIndex": "",
            }
        ]
        snapshot = [
            {
                **baseline[0],
                "text": (
                    "用户95104202310收到回复我 收到！请问有什么需要我帮助的吗？"
                    "让我帮你查一下今天是星期几。今天是星期日。任务完成"
                ),
                "top": 88,
            }
        ]

        payload = self._evaluate_cdp_extract_response(
            snapshot=snapshot,
            baseline=baseline,
        )

        self.assertEqual(payload["source"], "last_node_inserted")
        self.assertEqual(payload["text"], "让我帮你查一下今天是星期几。今天是星期日。")

    def test_cdp_extract_response_prefers_precise_leaf_for_new_message(self) -> None:
        baseline = [
            {
                "text": "收到！请告诉我您需要什么帮助。",
                "tagName": "div",
                "className": "assistant-chat-turn-content",
                "top": 120,
                "left": 960,
                "messageId": "",
                "dataId": "",
                "testId": "",
                "parentMessageId": "msg-1",
                "parentTurnIndex": "1",
            }
        ]
        snapshot = [
            baseline[0],
            {
                "text": (
                    "让我帮您计算一下今天是星期几。"
                    "TraeDemo自动运行在终端查看$date +\"%A, %Y-%m-%d\"SundayThought"
                    "今天是星期日。"
                ),
                "tagName": "div",
                "className": "assistant-chat-turn-content",
                "top": 182,
                "left": 960,
                "messageId": "",
                "dataId": "",
                "testId": "",
                "parentMessageId": "msg-2",
                "parentTurnIndex": "2",
            },
            {
                "text": "今天是星期日。",
                "tagName": "p",
                "className": "chat-markdown-p",
                "top": 188,
                "left": 978,
                "messageId": "",
                "dataId": "",
                "testId": "",
                "parentMessageId": "msg-2",
                "parentTurnIndex": "2",
            },
        ]

        payload = self._evaluate_cdp_extract_response(
            snapshot=snapshot,
            baseline=baseline,
        )

        self.assertEqual(payload["source"], "new_message_ids")
        self.assertEqual(payload["text"], "今天是星期日。")

    def test_cdp_extract_response_deduplicates_equal_leaf_and_container_text(self) -> None:
        snapshot = [
            {
                "text": "今天是星期日。",
                "tagName": "div",
                "className": "assistant-chat-turn-content",
                "top": 182,
                "left": 960,
                "messageId": "",
                "dataId": "",
                "testId": "",
                "parentMessageId": "msg-2",
                "parentTurnIndex": "2",
            },
            {
                "text": "今天是星期日。",
                "tagName": "p",
                "className": "chat-markdown-p",
                "top": 188,
                "left": 978,
                "messageId": "",
                "dataId": "",
                "testId": "",
                "parentMessageId": "msg-2",
                "parentTurnIndex": "2",
            },
        ]

        payload = self._evaluate_cdp_extract_response(
            snapshot=snapshot,
            baseline=[],
        )

        self.assertEqual(payload["source"], "new_message_ids")
        self.assertEqual(payload["text"], "今天是星期日。")

    def test_repo_install_script_is_shell_parseable(self) -> None:
        shell = shutil.which("sh")
        if shell is None:
            self.skipTest("sh is not available")

        script_path = Path(__file__).resolve().parents[4] / "scripts" / "install-traecli.sh"
        completed = subprocess.run(
            [shell, "-n", str(script_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=completed.stderr or completed.stdout or "shell parse failed",
        )

    def test_repo_release_check_script_is_shell_parseable(self) -> None:
        shell = shutil.which("sh")
        if shell is None:
            self.skipTest("sh is not available")

        script_path = Path(__file__).resolve().parents[4] / "scripts" / "release-check.sh"
        completed = subprocess.run(
            [shell, "-n", str(script_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=completed.stderr or completed.stdout or "shell parse failed",
        )

    def test_repo_bootstrap_install_script_is_shell_parseable(self) -> None:
        shell = shutil.which("sh")
        if shell is None:
            self.skipTest("sh is not available")

        script_path = (
            Path(__file__).resolve().parents[4]
            / "scripts"
            / "bootstrap-install-traecli.sh"
        )
        completed = subprocess.run(
            [shell, "-n", str(script_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=completed.stderr or completed.stdout or "shell parse failed",
        )

    def test_repo_uninstall_script_is_shell_parseable(self) -> None:
        shell = shutil.which("sh")
        if shell is None:
            self.skipTest("sh is not available")

        script_path = (
            Path(__file__).resolve().parents[4]
            / "scripts"
            / "uninstall-traecli.sh"
        )
        completed = subprocess.run(
            [shell, "-n", str(script_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=completed.stderr or completed.stdout or "shell parse failed",
        )

    def test_build_rpc_chat_model_info_prefers_current_dev_builder_model(self) -> None:
        payload = self.backend.build_rpc_chat_model_info()

        self.assertEqual(payload["model_name"], "gpt-5.3-codex")
        self.assertEqual(payload["custom_model"]["config_name"], "gpt-5.3-codex")
        self.assertEqual(
            payload["selected_model"]["display_name"],
            "GPT-5.3 Codex",
        )

    def test_switch_model_updates_global_map_and_legacy_selected_model(self) -> None:
        with mock.patch.object(
            self.backend,
            "invoke_command_uri",
            return_value={"ok": True, "command_id": "workbench.action.reloadWindow"},
        ) as mocked_reload:
            payload = self.backend.switch_model(
                "GLM-4.7",
                agent_type="dev_builder",
                cwd=self.root,
                reload_if_needed=True,
            )

        self.assertEqual(payload["status"], "switched")
        self.assertEqual(payload["agent_type"], "dev_builder")
        self.assertEqual(payload["target_model_key"], "3_bigmodel-plan_bigmodel-plan//glm-4.7")
        self.assertIn("state.vscdb", payload["applied_via"])
        self.assertIn("reload-window", payload["applied_via"])
        self.assertFalse(payload["reload_required"])
        mocked_reload.assert_called_once()

        state = self.backend.read_model_state()
        self.assertEqual(
            state["global_model_map"]["dev_builder"],
            "3_bigmodel-plan_bigmodel-plan//glm-4.7",
        )
        self.assertEqual(state["selected_model"]["display_name"], "GLM-4.7")

    def test_builds_rpc_user_info_from_local_auth_storage(self) -> None:
        auth_info = self.backend.load_auth_info()
        rpc_user_info = self.backend.build_rpc_user_info()

        self.assertEqual(auth_info["username"], "Fixture User")
        self.assertNotIn("token", auth_info)
        self.assertEqual(rpc_user_info["name"], "Fixture User")
        self.assertEqual(rpc_user_info["token"], "redacted-token")
        self.assertEqual(rpc_user_info["region"], "SG")
        self.assertEqual(rpc_user_info["user_id"], "7594301358220624917")
        self.assertEqual(rpc_user_info["scope"], "marscode")
        self.assertFalse(rpc_user_info["is_internal"])

    def test_extract_timestamp_uses_iso_prefix_only(self) -> None:
        timestamp = TraeBackend._extract_timestamp(
            '2026-04-05T11:53:26.813546+08:00  INFO process_ipc_request:route:chat_stopped_handle:chat_turn_finish: ai_agent::domain::snapshot::snapshot_service: [snapshot_v2][fixture] chat_turn_finish completed'
        )

        self.assertEqual(timestamp, "2026-04-05T11:53:26.813546+08:00")

    def test_lists_session_badges_and_sandboxes(self) -> None:
        sessions = self.backend.list_sessions(limit=10)
        sandboxes = self.backend.list_sandboxes(limit=10)

        self.assertEqual(len(sessions), 2)
        self.assertTrue(any(item["has_badge_payload"] for item in sessions))
        self.assertEqual(sandboxes[0]["name"], "fixture-session")

    def test_lists_mcp_gallery_entries(self) -> None:
        entries = self.backend.list_mcp_gallery()
        self.assertEqual(entries[0]["display_name"], "Playwright")
        self.assertEqual(entries[0]["run"][0]["command"], "npx")

    def test_extracts_rpc_inventory_and_payload_hints(self) -> None:
        services = self.backend.list_rpc_services()
        self.assertTrue(any(item["service"] == "chat" for item in services))

        description = self.backend.describe_rpc_method("chat", "chat")
        self.assertTrue(description["bundle_verified"])
        self.assertIn("session_id", description["payload_hint"]["base_fields"])
        self.assertIn("workspace_folders", description["payload_hint"]["derived_fields"])

    def test_extracts_recent_rpc_context_and_activity(self) -> None:
        context = self.backend.extract_recent_rpc_context(limit=5)
        activity = self.backend.list_rpc_activity(limit=10)

        self.assertEqual(context["projects"][0]["project_id"], "fixture-project")
        self.assertEqual(context["sessions"][0]["session_id"], "fixture-session-id")
        self.assertEqual(context["messages"][0]["message_id"], "fixture-message-id")
        self.assertTrue(
            any(
                item["service"] == "chat" and item["method"] == "chat"
                for item in activity
            )
        )

    def test_latest_log_session_dir_prefers_active_process_logs(self) -> None:
        latest_dir = self.fixture["support_dir"] / "logs" / "20260404T120000"
        newer_dir = self.fixture["support_dir"] / "logs" / "20260405T120000"
        newer_dir.mkdir(parents=True, exist_ok=True)
        bundle_path = self.fixture["app_path"]
        ps_output = f"18156 1 {bundle_path}/Contents/MacOS/Trae"
        open_path_output = f"f67\0n{latest_dir / 'main.log'}\0"

        def fake_probe(args: list[str]) -> subprocess.CompletedProcess[str]:
            if args == ["ps", "-axo", "pid=,ppid=,command="]:
                return subprocess.CompletedProcess(args, 0, ps_output, "")
            if args == ["lsof", "-nP", "-a", "-p", "18156", "-F0fn"]:
                return subprocess.CompletedProcess(args, 0, open_path_output, "")
            raise AssertionError(f"unexpected probe command: {args}")

        with mock.patch.object(self.backend, "_command_available", return_value=True):
            with mock.patch.object(
                self.backend, "_run_probe_command", side_effect=fake_probe
            ):
                latest = self.backend.latest_log_session_dir()

        self.assertEqual(latest.resolve(), latest_dir.resolve())

    def test_extracts_ai_agent_rpc_traces(self) -> None:
        payload = self.backend.list_rpc_traces(
            limit=3,
            service="chat",
            method="get_messages",
        )

        self.assertTrue(str(payload["latest_log_session"]).endswith("20260404T120000"))
        self.assertEqual(payload["filters"]["service"], "chat")
        self.assertEqual(payload["filters"]["method"], "get_messages")
        self.assertEqual(len(payload["traces"]), 3)

        latest = payload["traces"][0]
        self.assertEqual(latest["trace_id"], "fixture-ok-trace")
        self.assertEqual(latest["connect_session_id"], "fixture-ok-connect")
        self.assertEqual(latest["session_id"], "fixture-ok-session")
        self.assertEqual(latest["message_model_count"], 6)
        self.assertEqual(latest["message_model_actual"], 6)
        self.assertEqual(latest["message_count"], 6)
        self.assertEqual(latest["turn_count"], 3)
        self.assertEqual(latest["response_size_bytes"], 4096)
        self.assertEqual(latest["prepared_server_history_ids_count"], 2)
        self.assertEqual(
            latest["prepared_server_history_ids_sample"],
            ["hist-ok-1", "hist-ok-2"],
        )
        self.assertEqual(latest["query_history_state_request_count"], 2)
        self.assertEqual(latest["query_history_state_status"], "ok")
        self.assertEqual(latest["query_history_state_response_content_length"], 94)
        self.assertEqual(
            latest["query_history_state_response_request_id"], "req-fixture-ok"
        )
        self.assertIsNone(latest["query_history_state_error"])

        middle = payload["traces"][1]
        self.assertEqual(middle["trace_id"], "fixture-error-trace")
        self.assertEqual(middle["message_count"], 4)
        self.assertEqual(middle["turn_count"], 2)
        self.assertEqual(middle["prepared_server_history_ids_count"], 3)
        self.assertEqual(middle["query_history_state_request_count"], 3)
        self.assertEqual(middle["query_history_state_status"], "error")
        self.assertEqual(middle["query_history_state_response_content_length"], 43)
        self.assertEqual(
            middle["query_history_state_response_request_id"],
            "req-fixture-error",
        )
        self.assertIn("invalid type: null", middle["query_history_state_error"])

        earliest = payload["traces"][2]
        self.assertEqual(earliest["trace_id"], "fixture-empty-trace")
        self.assertEqual(earliest["message_count"], 0)
        self.assertEqual(earliest["turn_count"], 0)
        self.assertEqual(earliest["prepared_server_history_ids_count"], 0)
        self.assertEqual(earliest["query_history_state_status"], "not-needed")
        self.assertIsNone(earliest["query_history_state_request_count"])
        self.assertIsNone(earliest["query_history_state_response_content_length"])

    def test_guess_recent_connect_session_searches_older_log_sessions(self) -> None:
        (self.fixture["support_dir"] / "logs" / "20260405T120000").mkdir()

        payload = self.backend.guess_recent_connect_session(service="chat")

        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["connect_session_id"], "fixture-ok-connect")
        self.assertEqual(payload["trace_id"], "fixture-ok-trace")

    def test_guess_recent_connect_session_falls_back_to_any_service(self) -> None:
        newest = self.fixture["support_dir"] / "logs" / "20260405T120000"
        newest.mkdir()

        def fake_list_rpc_traces(
            *,
            limit: int = 20,
            service: Optional[str] = None,
            method: Optional[str] = None,
            log_session_dir: Optional[Path | str] = None,
        ) -> dict[str, Any]:
            _ = (limit, method)
            if str(log_session_dir).endswith("20260405T120000") and service == "chat":
                return {"traces": []}
            if str(log_session_dir).endswith("20260405T120000") and service is None:
                return {
                    "traces": [
                        {
                            "connect_session_id": "fixture-ckg-connect",
                            "trace_id": "fixture-ckg-trace",
                            "service": "ckg",
                            "method": "setup",
                            "source_logs": ["Modular/ai-agent_fixture_stdout.log"],
                            "last_seen_at": "2026-04-05T12:00:03+08:00",
                        }
                    ]
                }
            return {"traces": []}

        with mock.patch.object(
            self.backend,
            "latest_log_session_dir",
            return_value=newest,
        ):
            with mock.patch.object(
                self.backend,
                "list_log_session_dirs",
                return_value=[newest],
            ):
                with mock.patch.object(
                    self.backend,
                    "list_rpc_traces",
                    side_effect=fake_list_rpc_traces,
                ):
                    with mock.patch.object(
                        self.backend,
                        "extract_transport_topology",
                        return_value={},
                    ):
                        payload = self.backend.guess_recent_connect_session(service="chat")

        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["connect_session_id"], "fixture-ckg-connect")
        self.assertEqual(payload["service"], "ckg")
        self.assertEqual(payload["method"], "setup")

    def test_extracts_chat_turns(self) -> None:
        payload = self.backend.extract_chat_turns(limit=10, include_events=True)
        turn = payload["turns"][0]

        self.assertTrue(str(payload["latest_log_session"]).endswith("20260404T120000"))
        self.assertEqual(turn["status"], "completed")
        self.assertEqual(turn["session_id"], "fixture-session-id")
        self.assertEqual(turn["frontend_message_id"], "fixture-message-id")
        self.assertEqual(turn["backend_message_id"], "fixture-backend-message-id")
        self.assertEqual(turn["trace_id"], "fixture-trace-id")
        self.assertEqual(turn["task_id"], "fixture-task-id")
        self.assertEqual(turn["tool_call_count"], 2)
        self.assertEqual(turn["run_script_count"], 1)
        self.assertEqual(turn["run_script_success_count"], 1)
        self.assertEqual(turn["tool_run_count"], 1)
        self.assertEqual(turn["tool_run_success_count"], 1)
        self.assertEqual(turn["progress_notice_count"], 1)
        self.assertEqual(turn["plan_final_token_cost_ms"], 237)
        self.assertEqual(turn["first_token_preview"], "fixture-first-token")
        self.assertEqual(turn["first_token_preview_source"], "thought")
        self.assertEqual(turn["request_round_count"], 2)
        self.assertEqual(turn["tool_runs"][0]["tool_id"], "fixture-tool-2")
        self.assertEqual(turn["tool_runs"][0]["command"], "echo fixture")
        self.assertEqual(turn["tool_runs"][0]["exit_code"], 0)
        self.assertEqual(
            turn["tool_runs"][0]["result_log_excerpt"],
            ["$ echo fixture", "fixture"],
        )
        self.assertEqual(turn["events"][-1]["event"], "code_comp_complete_shown")

        described = self.backend.describe_chat_turn("fixture-trace-id")
        self.assertEqual(described["turn"]["frontend_message_id"], "fixture-message-id")
        self.assertEqual(described["turn"]["first_token_preview"], "fixture-first-token")
        self.assertEqual(described["turn"]["tool_runs"][0]["terminal_type"], "zsh")

    def test_describe_chat_turn_searches_older_log_sessions(self) -> None:
        newer_log_dir = (
            self.fixture["support_dir"] / "logs" / "20260405T120000" / "window1"
        )
        newer_log_dir.mkdir(parents=True, exist_ok=True)
        (newer_log_dir / "renderer.log").write_text(
            "\n".join(
                [
                    '2026-04-05T12:00:00.000+08:00 [info] [ai-chat][ai-chat][☕] event:  code_comp_trigger ; params:  '
                    '{"chat_model":"gpt-5.4","session_id":"newer-session-id","message_id":"newer-message-id",'
                    '"agent_type":"solo_coder"}',
                    '2026-04-05T12:00:01.000+08:00 [info] [ai-chat][ai-chat][☕] event:  code_comp_complete_shown ; params:  '
                    '{"chat_model":"gpt-5.4","session_id":"newer-session-id","message_id":"newer-message-id",'
                    '"agent_type":"solo_coder","request_round_count":1,"tool_count":0,"duration":1000,"is_interrupted":0}',
                ]
            )
            + "\n"
        )

        described = self.backend.describe_chat_turn("fixture-message-id")

        self.assertTrue(described["latest_log_session"].endswith("20260405T120000"))
        self.assertTrue(described["log_session"].endswith("20260404T120000"))
        self.assertEqual(described["turn"]["frontend_message_id"], "fixture-message-id")

    def test_describe_chat_turn_reads_rotated_renderer_logs(self) -> None:
        window_dir = self.fixture["support_dir"] / "logs" / "20260404T120000" / "window1"
        renderer_log = window_dir / "renderer.log"
        rotated_log = window_dir / "renderer.1.log"
        renderer_log.rename(rotated_log)
        renderer_log.write_text(
            "\n".join(
                [
                    '2026-04-04T12:10:00.000+08:00 [info] [ai-chat][ai-chat][☕] event:  code_comp_trigger ; params:  '
                    '{"chat_model":"gpt-5.4","session_id":"new-session-id","message_id":"new-message-id",'
                    '"agent_type":"solo_coder"}',
                    '2026-04-04T12:10:02.000+08:00 [info] [ai-chat][ai-chat][☕] event:  code_comp_complete_shown ; params:  '
                    '{"chat_model":"gpt-5.4","session_id":"new-session-id","message_id":"new-message-id",'
                    '"agent_type":"solo_coder","request_round_count":1,"tool_count":0,"duration":2000,"is_interrupted":0}',
                ]
            )
            + "\n"
        )

        described = self.backend.describe_chat_turn("fixture-message-id")

        self.assertEqual(described["turn"]["frontend_message_id"], "fixture-message-id")
        self.assertEqual(described["turn"]["tool_runs"][0]["command"], "echo fixture")

    def test_builds_side_chat_deep_link(self) -> None:
        deep_link = self.backend.build_side_chat_deep_link(
            "hello fixture",
            new_chat=True,
        )

        self.assertEqual(
            deep_link,
            "trae://trae.ai-ide/side-chat?query=hello+fixture&newChat=true",
        )

    def test_builds_side_chat_command_uri(self) -> None:
        command_uri = self.backend.build_side_chat_command_uri(
            "hello fixture",
            new_chat=True,
        )

        self.assertEqual(
            command_uri,
            "command:workbench.action.chat.icube.open?"
            "%5B%7B%22query%22%3A%22hello%20fixture%22%2C%22keepOpen%22%3Atrue%2C"
            "%22newChat%22%3Atrue%7D%5D",
        )

    def test_builds_side_chat_command_uri_without_prompt(self) -> None:
        command_uri = self.backend.build_side_chat_command_uri(new_chat=True)

        self.assertEqual(
            command_uri,
            "command:workbench.action.chat.icube.open?"
            "%5B%7B%22keepOpen%22%3Atrue%2C%22newChat%22%3Atrue%7D%5D",
        )

    def test_builds_url_aware_open_command(self) -> None:
        command = self.backend.open_command("trae://trae.ai-ide/side-chat?query=hello")

        self.assertEqual(
            command,
            ["open", "-b", "com.trae.app", "-u", "trae://trae.ai-ide/side-chat?query=hello"],
        )

    def test_builds_cli_open_url_command(self) -> None:
        command = self.backend.cli_open_url_command(
            "command:workbench.action.chat.icube.open"
        )

        self.assertEqual(
            command,
            [
                str(self.backend.paths.cli_script_path),
                "--open-url",
                "--",
                "command:workbench.action.chat.icube.open",
            ],
        )

    def test_open_path_via_cli_uses_directory_as_cwd(self) -> None:
        workspace = self.root / "path-open-workspace"
        workspace.mkdir()

        result = self.backend.open_path_via_cli(workspace)

        self.assertEqual(result.returncode, 0)
        stdout_payload = json.loads(result.stdout)
        self.assertEqual(stdout_payload["argv"], [str(workspace.resolve())])
        self.assertEqual(Path(stdout_payload["cwd"]).resolve(), workspace.resolve())

    def test_open_path_via_cli_uses_parent_dir_as_cwd_for_files(self) -> None:
        workspace = self.root / "path-open-file-workspace"
        workspace.mkdir()
        file_path = workspace / "fixture.txt"
        file_path.write_text("fixture")

        result = self.backend.open_path_via_cli(file_path)

        self.assertEqual(result.returncode, 0)
        stdout_payload = json.loads(result.stdout)
        self.assertEqual(stdout_payload["argv"], [str(file_path.resolve())])
        self.assertEqual(Path(stdout_payload["cwd"]).resolve(), workspace.resolve())

    def test_lists_recently_opened_entries_from_shared_gui_db(self) -> None:
        payload = self.backend.list_recently_opened()

        self.assertEqual(payload["count"], 4)
        self.assertEqual(payload["entries"][0]["kind"], "folder")
        self.assertTrue(payload["entries"][0]["local"])
        self.assertTrue(payload["entries"][0]["exists"])
        self.assertEqual(payload["entries"][1]["kind"], "workspace")
        self.assertTrue(str(payload["entries"][1]["path"]).endswith("recent.code-workspace"))
        self.assertEqual(payload["entries"][2]["kind"], "file")
        self.assertEqual(payload["entries"][2]["label"], "recent-note.txt")
        self.assertEqual(payload["entries"][3]["kind"], "folder")
        self.assertFalse(payload["entries"][3]["local"])
        self.assertEqual(
            payload["entries"][3]["remote_authority"],
            "ssh-remote+fixture",
        )

    def test_open_recently_opened_local_workspace_entry_uses_cli_path(self) -> None:
        payload = self.backend.open_recently_opened(2)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["dispatch"], "cli-path")
        self.assertEqual(payload["recent_index"], 2)
        self.assertEqual(payload["kind"], "workspace")
        self.assertTrue(str(payload["path"]).endswith("recent.code-workspace"))
        stdout_payload = json.loads(payload["stdout"])
        self.assertEqual(stdout_payload["argv"], [str(Path(payload["path"]).resolve())])
        self.assertEqual(
            Path(stdout_payload["cwd"]).resolve(),
            Path(payload["path"]).resolve().parent,
        )

    def test_open_recently_opened_remote_folder_entry_uses_cli_folder_uri(self) -> None:
        payload = self.backend.open_recently_opened(4)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["dispatch"], "cli-uri")
        self.assertEqual(payload["recent_index"], 4)
        self.assertEqual(payload["kind"], "folder")
        self.assertEqual(payload["remote_authority"], "ssh-remote+fixture")
        stdout_payload = json.loads(payload["stdout"])
        self.assertEqual(
            stdout_payload["argv"],
            [
                "--folder-uri",
                "vscode-remote://ssh-remote%2Bfixture/home/test/repo",
            ],
        )

    def test_discovers_gui_commands_from_product_and_main_bundle(self) -> None:
        payload = self.backend.discover_gui_commands()
        command_ids = {item["command_id"]: item for item in payload["commands"]}

        self.assertIn("update.checkForUpdate", command_ids)
        self.assertIn("trae.solo.guide.tryShowSoloGuide", command_ids)
        self.assertIn("soloMode", command_ids)
        self.assertIn("soloBuilder", command_ids)
        self.assertIn("workbench.action.icube.openSettings", command_ids)
        self.assertIn("workbench.action.newWindow", command_ids)
        self.assertIn("main.js", command_ids["workbench.action.newWindow"]["sources"])
        self.assertIn("product.json", command_ids["soloMode"]["sources"])

    def test_reads_solo_mode_state_from_shared_gui_db(self) -> None:
        state = self.backend.read_solo_mode_state()

        self.assertEqual(state["mode"], "ide")
        self.assertFalse(state["enabled"])
        self.assertEqual(state["current_solo_tab_id"], "")
        self.assertTrue(state["is_visible_extension_view"])

    def test_switch_mode_to_solo_uses_command_uri_and_persists_state(self) -> None:
        workspace = self.root / "mode-solo-workspace"
        workspace.mkdir()

        payload = self.backend.switch_mode("solo", cwd=workspace, reload_if_needed=True)

        self.assertEqual(payload["status"], "switched")
        self.assertEqual(payload["previous_mode"], "ide")
        self.assertEqual(payload["mode"], "solo")
        self.assertIn("command-uri", payload["applied_via"])
        self.assertIn("state.vscdb", payload["applied_via"])
        self.assertIn("reload-window", payload["applied_via"])
        self.assertIsNotNone(payload["command_dispatch"])
        self.assertEqual(payload["command_dispatch"]["command_id"], "soloMode")
        self.assertIsNotNone(payload["storage"])
        self.assertIsNotNone(payload["reload"])
        self.assertFalse(payload["reload_required"])
        stdout_payload = json.loads(payload["command_dispatch"]["stdout"])
        self.assertEqual(
            stdout_payload["argv"],
            ["--open-url", "--", "command:soloMode"],
        )
        self.assertEqual(Path(stdout_payload["cwd"]).resolve(), workspace.resolve())
        self.assertEqual(self.backend.read_solo_mode_state()["mode"], "solo")

    def test_switch_mode_to_ide_updates_shared_state_and_reloads(self) -> None:
        workspace = self.root / "mode-ide-workspace"
        workspace.mkdir()
        self.backend.write_solo_mode_state(enabled=True, source="test")

        payload = self.backend.switch_mode("ide", cwd=workspace, reload_if_needed=True)

        self.assertEqual(payload["status"], "switched")
        self.assertEqual(payload["previous_mode"], "solo")
        self.assertEqual(payload["mode"], "ide")
        self.assertIsNone(payload["command_dispatch"])
        self.assertIn("state.vscdb", payload["applied_via"])
        self.assertIn("reload-window", payload["applied_via"])
        self.assertIsNotNone(payload["storage"])
        self.assertIsNotNone(payload["reload"])
        self.assertFalse(payload["reload_required"])
        self.assertEqual(self.backend.read_solo_mode_state()["mode"], "ide")

    def test_resolves_cn_cli_script_path(self) -> None:
        fixture = create_fixture(
            self.root / "cn-fixture",
            app_name="Trae CN",
            cli_name="trae-cn",
            bundle_identifier="cn.trae.app",
            url_protocol="trae-cn",
            support_dir_name="Trae CN",
        )
        backend = TraeBackend(
            app_path=fixture["app_path"],
            support_dir=fixture["support_dir"],
            user_data_dir=fixture["user_data_dir"],
        )

        self.assertEqual(backend.paths.cli_script_path.name, "trae-cn")
        probe = backend.probe()
        self.assertEqual(probe["app"]["display_name"], "Trae CN")
        self.assertEqual(probe["app"]["url_protocol"], "trae-cn")
        self.assertTrue(probe["app"]["cli_exists"])

    def test_default_app_candidates_prefer_cn_first(self) -> None:
        self.assertEqual(
            TraeBackend._default_app_candidates(),
            [
                Path("~/.trae-cn/app-copies/Trae CN-headless.app").expanduser(),
                Path("/Applications/Trae CN.app"),
                Path("~/.trae/app-copies/Trae-headless.app").expanduser(),
                Path("/Applications/Trae.app"),
            ],
        )

    def test_default_support_dir_uses_cn_for_headless_copy(self) -> None:
        support_dir = self.backend._default_support_dir(Path("/tmp/Trae CN-headless.app"))

        self.assertTrue(str(support_dir).endswith("Application Support/Trae CN"))

    def test_default_user_data_dir_uses_cn_for_cn_bundle(self) -> None:
        user_data_dir = TraeBackend._default_user_data_dir(Path("/Applications/Trae CN.app"))

        self.assertTrue(str(user_data_dir).endswith(".trae-cn"))

    def test_backend_prefers_cli_config_defaults(self) -> None:
        fixture = create_fixture(
            self.root / "configured",
            app_name="Trae CN",
            cli_name="trae-cn",
            bundle_identifier="cn.trae.app",
            url_protocol="trae-cn",
            support_dir_name="Trae CN",
        )
        config_path = fixture["user_data_dir"] / "traecli.json"
        config_path.write_text(
            json.dumps(
                {
                    "app_path": str(fixture["app_path"]),
                    "support_dir": str(fixture["support_dir"]),
                }
            )
        )

        backend = TraeBackend(user_data_dir=fixture["user_data_dir"])

        self.assertEqual(backend.paths.app_path.resolve(), fixture["app_path"].resolve())
        self.assertEqual(
            backend.paths.support_dir.resolve(),
            fixture["support_dir"].resolve(),
        )
        self.assertEqual(backend.paths.cli_script_path.name, "trae-cn")

    def test_write_and_read_cli_config_round_trip(self) -> None:
        payload = self.backend.write_cli_config()
        loaded = self.backend.read_cli_config()

        self.assertTrue(Path(payload["path"]).exists())
        self.assertEqual(loaded["app_path"], str(self.backend.paths.app_path))
        self.assertEqual(loaded["support_dir"], str(self.backend.paths.support_dir))
        self.assertEqual(loaded["user_data_dir"], str(self.backend.paths.user_data_dir))

    def test_save_and_resume_cli_session_records(self) -> None:
        workspace = self.root / "workspace-a"
        workspace.mkdir()
        older = {
            "id": "older-session",
            "created_at": "2026-04-08T10:00:00+08:00",
            "updated_at": "2026-04-08T10:01:00+08:00",
            "workspace": str(workspace.resolve()),
            "app_path": str(self.backend.paths.app_path),
            "support_dir": str(self.backend.paths.support_dir),
            "user_data_dir": str(self.backend.paths.user_data_dir),
            "last_headless_session_id": "older-hidden-session",
        }
        newer = {
            "id": "newer-session",
            "created_at": "2026-04-08T11:00:00+08:00",
            "updated_at": "2026-04-08T11:01:00+08:00",
            "workspace": str(workspace.resolve()),
            "app_path": str(self.backend.paths.app_path),
            "support_dir": str(self.backend.paths.support_dir),
            "user_data_dir": str(self.backend.paths.user_data_dir),
            "last_headless_session_id": "newer-hidden-session",
        }

        self.backend.save_cli_session(older)
        self.backend.save_cli_session(newer)

        sessions = self.backend.read_cli_sessions()
        self.assertEqual(sessions[0]["id"], "newer-session")
        self.assertEqual(
            self.backend.load_cli_session("older-session")["last_headless_session_id"],
            "older-hidden-session",
        )
        self.assertEqual(
            self.backend.latest_cli_session(
                workspace=workspace,
                resumable_only=True,
            )["id"],
            "newer-session",
        )

    def test_probe_reports_cli_config_summary(self) -> None:
        self.backend.write_cli_config()

        probe = self.backend.probe()

        self.assertTrue(probe["config"]["exists"])
        self.assertEqual(probe["config"]["path"], str(self.backend.paths.cli_config_path))
        self.assertEqual(probe["config"]["app_path"], str(self.backend.paths.app_path))
        self.assertEqual(
            probe["config"]["support_dir"],
            str(self.backend.paths.support_dir),
        )
        self.assertEqual(
            probe["config"]["user_data_dir"],
            str(self.backend.paths.user_data_dir),
        )

    def test_resolves_cdp_endpoint_from_running_process_args(self) -> None:
        ps_output = "\n".join(
            [
                f"18156 1 {self.fixture['app_path']}/Contents/MacOS/Trae --remote-debugging-port=9333",
                f"18164 18156 {self.fixture['app_path']}/Contents/Frameworks/Trae Helper (Plugin).app/Contents/MacOS/Trae Helper (Plugin) --vscode-crash-reporter-process-type=ai",
            ]
        )

        with mock.patch(
            "cli_anything.trae.utils.trae_backend.TraeBackend._command_available",
            return_value=True,
        ):
            with mock.patch(
                "cli_anything.trae.utils.trae_backend.TraeBackend._run_probe_command",
                return_value=subprocess.CompletedProcess(
                    ["ps", "-axo", "pid=,ppid=,command="],
                    0,
                    ps_output,
                    "",
                ),
            ):
                endpoint = self.backend.resolve_cdp_endpoint()

        self.assertEqual(endpoint["host"], "127.0.0.1")
        self.assertEqual(endpoint["port"], 9333)
        self.assertEqual(endpoint["source"], "process")
        self.assertEqual(endpoint["pid"], 18156)

    def test_workspace_cdp_binding_round_trip(self) -> None:
        workspace = self.root / "fixture-workspace"
        workspace.mkdir()

        binding = self.backend.write_workspace_cdp_binding(
            workspace=workspace,
            target={
                "id": "fixture-target-id",
                "title": "fixture-workspace",
                "url": "file:///fixture-workspace",
            },
        )
        loaded = self.backend.read_workspace_cdp_binding(workspace=workspace)

        self.assertIsNotNone(binding)
        self.assertEqual(loaded["target_id"], "fixture-target-id")
        self.assertEqual(loaded["workspace"], str(workspace.resolve()))
        self.assertTrue(self.backend.clear_workspace_cdp_binding(workspace=workspace))
        self.assertIsNone(self.backend.read_workspace_cdp_binding(workspace=workspace))

    def test_trusted_workspace_round_trip(self) -> None:
        workspace = self.root / "trusted-workspace"
        workspace.mkdir()

        record = self.backend.trust_workspace(workspace=workspace, source="prompt")
        loaded = self.backend.read_trusted_workspace(workspace=workspace)
        connection = sqlite3.connect(str(self.backend.paths.state_db_path))
        try:
            row = connection.execute(
                "select value from ItemTable where key = ?",
                ("content.trust.model.key",),
            ).fetchone()
        finally:
            connection.close()

        self.assertTrue(self.backend.workspace_is_trusted(workspace=workspace))
        self.assertEqual(record["workspace"], str(workspace.resolve()))
        self.assertEqual(record["source"], "prompt")
        self.assertTrue(record["trusted"])
        self.assertEqual(loaded["workspace"], str(workspace.resolve()))
        self.assertEqual(loaded["matched_workspace"], str(workspace.resolve()))
        self.assertFalse(loaded["inherited"])
        self.assertIsNotNone(row)
        self.assertIn(str(workspace.resolve()), row[0])
        self.assertTrue(self.backend.clear_trusted_workspace(workspace=workspace))
        self.assertFalse(self.backend.workspace_is_trusted(workspace=workspace))

    def test_write_state_item_value_retries_locked_database(self) -> None:
        connections: list[object] = []

        class FakeConnection:
            def __init__(self, fail_locked: bool) -> None:
                self.fail_locked = fail_locked
                self.closed = False
                self.committed = False

            def execute(self, sql: str, params: tuple[object, ...] = ()) -> object:
                _ = (sql, params)
                if self.fail_locked:
                    self.fail_locked = False
                    raise sqlite3.OperationalError("database is locked")
                return object()

            def commit(self) -> None:
                self.committed = True

            def close(self) -> None:
                self.closed = True

        first = FakeConnection(True)
        second = FakeConnection(False)
        connections.extend([first, second])

        with mock.patch(
            "cli_anything.trae.utils.trae_backend.sqlite3.connect",
            side_effect=connections,
        ) as connect_mock:
            with mock.patch("cli_anything.trae.utils.trae_backend.time.sleep") as sleep_mock:
                payload = self.backend._write_state_item_value("fixture.key", "fixture-value")

        self.assertTrue(payload["written"])
        self.assertEqual(connect_mock.call_count, 2)
        sleep_mock.assert_called_once()
        self.assertTrue(first.closed)
        self.assertTrue(second.closed)
        self.assertTrue(second.committed)

    def test_trusted_parent_workspace_applies_to_child_workspace(self) -> None:
        parent = self.root / "trusted-parent"
        child = parent / "child"
        child.mkdir(parents=True)

        self.backend.trust_workspace(workspace=parent, source="prompt")
        loaded = self.backend.read_trusted_workspace(workspace=child)

        self.assertTrue(self.backend.workspace_is_trusted(workspace=child))
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["workspace"], str(child.resolve()))
        self.assertEqual(loaded["matched_workspace"], str(parent.resolve()))
        self.assertTrue(loaded["inherited"])

    def test_cdp_target_url_markers_include_bundle_specific_paths(self) -> None:
        markers = self.backend.cdp_target_url_markers()

        self.assertTrue(any("trae.app" in marker for marker in markers))
        self.assertTrue(any("contents/resources/app/out" in marker for marker in markers))

    def test_wait_for_bridge_state_polls_until_workspace_matches(self) -> None:
        expected = {"workspace_folders": [{"fsPath": str((self.root / "workspace-a").resolve())}]}

        with mock.patch.object(
            self.backend,
            "read_bridge_state",
            side_effect=[None, None, expected],
        ) as mocked_read:
            with mock.patch(
                "cli_anything.trae.utils.trae_backend.time.sleep",
            ) as mocked_sleep:
                loaded = self.backend.wait_for_bridge_state(
                    workspace=self.root / "workspace-a",
                    wait_seconds=1.0,
                    poll_interval=0.1,
                )

        self.assertEqual(loaded, expected)
        self.assertEqual(mocked_read.call_count, 3)
        self.assertEqual(mocked_sleep.call_count, 2)

    def test_cdp_endpoint_matches_app_uses_http_client_probe(self) -> None:
        target_url = (
            "vscode-file://vscode-app"
            f"{self.backend.cdp_target_url_markers()[0]}"
            "/vs/code/electron-browser/workbench/workbench.html"
        )

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                if self.path != "/json/list":
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps(
                        [
                            {
                                "type": "page",
                                "url": target_url,
                            }
                        ]
                    ).encode("utf-8")
                )

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with mock.patch(
                "cli_anything.trae.utils.trae_backend.urllib.request.urlopen",
                side_effect=AssertionError("cdp_endpoint_matches_app should not use urllib"),
            ):
                matched = self.backend.cdp_endpoint_matches_app(
                    host="127.0.0.1",
                    port=server.server_address[1],
                    timeout_seconds=1.0,
                )
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

        self.assertTrue(matched)

    def test_ensure_cdp_endpoint_rejects_existing_endpoint_for_other_app(self) -> None:
        with mock.patch.object(
            self.backend,
            "resolve_cdp_endpoint",
            return_value={"host": "127.0.0.1", "port": 9222, "source": "explicit"},
        ):
            with mock.patch.object(
                self.backend,
                "cdp_endpoint_available",
                return_value=True,
            ):
                with mock.patch.object(
                    self.backend,
                    "cdp_endpoint_matches_app",
                    return_value=False,
                ):
                    with self.assertRaisesRegex(RuntimeError, "does not appear to belong"):
                        self.backend.ensure_cdp_endpoint(port=9222)

    def test_ensure_cdp_endpoint_rejects_default_mismatch_without_fallback(self) -> None:
        with mock.patch.object(
            self.backend,
            "resolve_cdp_endpoint",
            return_value={"host": "127.0.0.1", "port": 9222, "source": "default"},
        ):
            with mock.patch.object(
                self.backend,
                "cdp_endpoint_available",
                side_effect=[True, False, True],
            ):
                with mock.patch.object(
                    self.backend,
                    "cdp_endpoint_matches_app",
                    return_value=False,
                ):
                    with self.assertRaisesRegex(RuntimeError, "does not appear to belong"):
                        self.backend.ensure_cdp_endpoint(
                            launch_if_needed=True,
                            launch_timeout_ms=1000,
                        )

    def test_cdp_endpoint_available_returns_false_on_socket_timeout(self) -> None:
        with mock.patch.object(self.backend, "_command_available", return_value=False):
            with mock.patch.object(
                self.backend,
                "_fetch_cdp_json",
                side_effect=socket.timeout("timed out"),
            ):
                available = self.backend.cdp_endpoint_available(
                    host="127.0.0.1",
                    port=9222,
                )

        self.assertFalse(available)

    def test_cdp_endpoint_available_prefers_curl_when_available(self) -> None:
        with mock.patch.object(self.backend, "_command_available", return_value=True):
            with mock.patch(
                "cli_anything.trae.utils.trae_backend.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    ["curl"],
                    0,
                    '{"Browser":"Chrome"}',
                    "",
                ),
            ) as mocked_run:
                available = self.backend.cdp_endpoint_available(
                    host="127.0.0.1",
                    port=9222,
                )

        self.assertTrue(available)
        self.assertEqual(mocked_run.call_args.args[0][:3], ["curl", "-fsS", "--max-time"])

    def test_invoke_cdp_chat_passes_required_url_markers_to_bridge(self) -> None:
        helper_stdout = json.dumps(
            {
                "ok": True,
                "version": {"Browser": "Chrome"},
                "target": {
                    "title": "Trae",
                    "url": "file:///workbench.html",
                    "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/fixture",
                },
                "foreground": {
                    "attempted": True,
                    "activated": True,
                    "restored": True,
                },
                "response": {"text": "fixture cdp answer"},
            }
        )

        with mock.patch(
            "cli_anything.trae.utils.trae_backend.shutil.which",
            return_value="/usr/bin/node",
        ):
            with mock.patch.object(
                self.backend,
                "ensure_cdp_endpoint",
                return_value={"host": "127.0.0.1", "port": 9222, "source": "explicit"},
            ):
                with mock.patch(
                    "cli_anything.trae.utils.trae_backend.subprocess.run",
                    return_value=subprocess.CompletedProcess(
                        ["/usr/bin/node"],
                        0,
                        helper_stdout,
                        "",
                    ),
                ) as mocked_run:
                    payload = self.backend.invoke_cdp_chat(
                        prompt="hello fixture",
                        target_id="fixture-target-id",
                        prefer_focused=True,
                    )

        self.assertEqual(payload["response"]["text"], "fixture cdp answer")
        self.assertTrue(payload["foreground"]["activated"])
        bridge_payload = json.loads(mocked_run.call_args.kwargs["input"])
        self.assertIn("required_url_contains", bridge_payload)
        self.assertEqual(bridge_payload["target_id"], "fixture-target-id")
        self.assertTrue(bridge_payload["prefer_focused"])
        self.assertTrue(
            any("trae.app" in item for item in bridge_payload["required_url_contains"])
        )

    def test_invoke_cdp_websocket_probe_passes_probe_mode_to_bridge(self) -> None:
        helper_stdout = json.dumps(
            {
                "ok": True,
                "mode": "websocket_probe",
                "version": {"Browser": "Chrome"},
                "target": {
                    "title": "Trae CN",
                    "url": "file:///workbench.html",
                    "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/fixture",
                },
                "foreground": {
                    "attempted": True,
                    "activated": True,
                    "restored": True,
                },
                "websocket_probe": {
                    "page": {
                        "records": [
                            {
                                "url": "wss://trae-ws-cn.mchost.guru/custom_model",
                                "protocols": ["fixture"],
                            }
                        ]
                    }
                },
            }
        )

        with mock.patch(
            "cli_anything.trae.utils.trae_backend.shutil.which",
            return_value="/usr/bin/node",
        ):
            with mock.patch.object(
                self.backend,
                "ensure_cdp_endpoint",
                return_value={"host": "127.0.0.1", "port": 9222, "source": "explicit"},
            ):
                with mock.patch(
                    "cli_anything.trae.utils.trae_backend.subprocess.run",
                    return_value=subprocess.CompletedProcess(
                        ["/usr/bin/node"],
                        0,
                        helper_stdout,
                        "",
                    ),
                ) as mocked_run:
                    payload = self.backend.invoke_cdp_websocket_probe(
                        prompt="hello fixture",
                        target_id="fixture-target-id",
                        reload_before_run=True,
                    )

        self.assertEqual(payload["mode"], "websocket_probe")
        self.assertTrue(payload["foreground"]["restored"])
        self.assertEqual(
            payload["websocket_probe"]["page"]["records"][0]["url"],
            "wss://trae-ws-cn.mchost.guru/custom_model",
        )
        bridge_payload = json.loads(mocked_run.call_args.kwargs["input"])
        self.assertEqual(bridge_payload["mode"], "websocket_probe")
        self.assertEqual(bridge_payload["target_id"], "fixture-target-id")
        self.assertTrue(bridge_payload["reload_before_run"])
        self.assertIn("required_url_contains", bridge_payload)

    def test_builds_rpc_request_envelope(self) -> None:
        envelope = self.backend.build_rpc_request_envelope(
            service="healthcheck",
            method="ping",
            data="",
            connect_session_id="fixture-connect",
            channel_id="fixture-channel",
            user_info={"user_id": "fixture-user"},
        )

        self.assertEqual(envelope["packet_type"], "request")
        self.assertEqual(envelope["session_id"], "fixture-connect")
        self.assertEqual(envelope["channel_id"], "fixture-channel")
        self.assertEqual(envelope["params"]["service"], "healthcheck")
        self.assertEqual(envelope["params"]["method"], "ping")
        self.assertEqual(
            envelope["params"]["client_info"]["connect_session_id"],
            "fixture-connect",
        )
        self.assertEqual(envelope["params"]["user_info"]["name"], "")
        self.assertFalse(envelope["params"]["user_info"]["is_internal"])
        self.assertEqual(envelope["params"]["user_info"]["user_id"], "fixture-user")

    def test_builds_rpc_request_envelope_merges_client_info(self) -> None:
        envelope = self.backend.build_rpc_request_envelope(
            service="chat",
            method="chat",
            data={"message": "fixture"},
            connect_session_id="fixture-connect",
            client_info={"project_id": "fixture-project"},
        )

        self.assertEqual(
            envelope["params"]["client_info"],
            {
                "project_id": "fixture-project",
                "connect_session_id": "fixture-connect",
            },
        )

    def test_invokes_aha_rpc_bridge(self) -> None:
        helper_stdout = json.dumps(
            {
                "ok": True,
                "result": {
                    "message": "success",
                    "code": 0,
                    "data": {"message": "pong"},
                },
                "meta": {
                    "service_name": "ai-agent",
                    "runtime_dir": "/tmp",
                    "request_method": "request",
                },
            }
        )

        with mock.patch(
            "cli_anything.trae.utils.trae_backend.shutil.which",
            return_value="/usr/bin/node",
        ):
            with mock.patch(
                "cli_anything.trae.utils.trae_backend.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    ["/usr/bin/node"],
                    0,
                    helper_stdout,
                    "",
                ),
            ) as mocked_run:
                payload = self.backend.invoke_aha_rpc(
                    service="healthcheck",
                    method="ping",
                    connect_session_id="fixture-connect",
                )

        self.assertEqual(payload["service_name"], "ai-agent")
        self.assertEqual(payload["runtime_dir"], "/tmp")
        self.assertEqual(payload["response"]["code"], 0)
        self.assertEqual(payload["response"]["data"]["message"], "pong")

        helper_input = json.loads(mocked_run.call_args.kwargs["input"])
        self.assertEqual(helper_input["service_name"], "ai-agent")
        self.assertEqual(helper_input["runtime_dir"], "/tmp")
        self.assertEqual(
            helper_input["envelope"]["params"]["client_info"]["connect_session_id"],
            "fixture-connect",
        )
        self.assertEqual(
            helper_input["envelope"]["params"]["service"],
            "healthcheck",
        )
        self.assertEqual(helper_input["envelope"]["params"]["method"], "ping")
        self.assertEqual(
            helper_input["envelope"]["params"]["user_info"]["name"], "Fixture User"
        )
        self.assertEqual(
            helper_input["envelope"]["params"]["user_info"]["token"],
            "redacted-token",
        )
        self.assertEqual(
            helper_input["envelope"]["params"]["user_info"]["scope"],
            "marscode",
        )
        self.assertFalse(helper_input["envelope"]["params"]["user_info"]["is_internal"])

    def test_invokes_aha_rpc_via_bridge_for_electron_transport(self) -> None:
        with mock.patch.object(
            self.backend,
            "extract_transport_topology",
            return_value={
                "app_rpc": {
                    "local_impl": {"client_api": "electron.ahaIpc.connect"},
                    "observed_ipc_addresses": [],
                    "node_socket_rule": {"runtime_dir": "/tmp"},
                }
            },
        ):
            with mock.patch.object(
                self.backend,
                "invoke_bridge_aha_rpc",
                return_value={
                    "ok": True,
                    "result": {
                        "message": "success",
                        "code": 0,
                        "data": {"message": "pong"},
                    },
                    "meta": {
                        "transport": "bridge.electron.ahaIpc",
                        "service_name": "ai-agent",
                        "request_method": "request",
                    },
                },
            ) as mocked_bridge:
                payload = self.backend.invoke_aha_rpc(
                    service="healthcheck",
                    method="ping",
                    connect_session_id="fixture-connect",
                    workspace=self.fixture["support_dir"],
                )

        self.assertEqual(payload["response"]["code"], 0)
        self.assertEqual(
            payload["bridge"]["meta"]["transport"],
            "bridge.electron.ahaIpc",
        )
        mocked_bridge.assert_called_once()
        self.assertEqual(
            mocked_bridge.call_args.kwargs["envelope"]["params"]["user_info"]["token"],
            "redacted-token",
        )
        self.assertEqual(
            mocked_bridge.call_args.kwargs["workspace"],
            self.fixture["support_dir"],
        )

    def test_create_rpc_project_uses_workspace_payload(self) -> None:
        workspace = self.root / "workspace-a"
        workspace.mkdir()
        normalized_workspace = str(workspace.resolve())

        with mock.patch.object(
            self.backend,
            "invoke_aha_rpc",
            return_value={
                "response": {
                    "message": "success",
                    "code": 0,
                    "data": {
                        "project_id": "fixture-project-id",
                        "real_project_id": "fixture-project-id",
                        "biz_project_id": normalized_workspace,
                    },
                }
            },
        ) as mocked_rpc:
            payload = self.backend.create_rpc_project(workspace)

        self.assertEqual(payload["workspace"], normalized_workspace)
        self.assertEqual(payload["project_id"], "fixture-project-id")
        self.assertEqual(
            mocked_rpc.call_args.kwargs["data"],
            {"biz_project_id": normalized_workspace},
        )

    def test_create_rpc_session_uses_project_id_payload(self) -> None:
        with mock.patch.object(
            self.backend,
            "invoke_aha_rpc",
            return_value={
                "response": {
                    "message": "success",
                    "code": 0,
                    "data": {
                        "session_id": "fixture-session-id",
                    },
                }
            },
        ) as mocked_rpc:
            payload = self.backend.create_rpc_session(
                "fixture-project-id",
                session_type="inline_chat",
            )

        self.assertEqual(payload["project_id"], "fixture-project-id")
        self.assertEqual(payload["session_type"], "inline_chat")
        self.assertEqual(payload["session_id"], "fixture-session-id")
        self.assertEqual(
            mocked_rpc.call_args.kwargs["data"],
            {
                "project_id": "fixture-project-id",
                "session_type": "inline_chat",
            },
        )

    def test_create_rpc_session_accepts_nested_session_payload(self) -> None:
        with mock.patch.object(
            self.backend,
            "invoke_aha_rpc",
            return_value={
                "response": {
                    "message": "success",
                    "code": 0,
                    "data": {
                        "session": {
                            "session_id": "fixture-nested-session-id",
                        }
                    },
                }
            },
        ):
            payload = self.backend.create_rpc_session("fixture-project-id")

        self.assertEqual(payload["session_id"], "fixture-nested-session-id")
        self.assertEqual(
            payload["session"]["session_id"], "fixture-nested-session-id"
        )

    def test_get_rpc_messages_extracts_assistant_answer(self) -> None:
        with mock.patch.object(
            self.backend,
            "invoke_aha_rpc",
            return_value={
                "response": {
                    "message": "success",
                    "code": 0,
                    "data": {
                        "messages": [
                            {"role": "user", "content": "收到回复我"},
                            {
                                "role": "assistant",
                                "content": "fixture rpc answer",
                                "status": "completed",
                            },
                        ],
                        "next_page_token": "fixture-next",
                    },
                }
            },
        ):
            payload = self.backend.get_rpc_messages(
                "fixture-session-id",
                project_id="fixture-project-id",
                prompt="收到回复我",
            )

        self.assertEqual(payload["message_count"], 2)
        self.assertEqual(payload["assistant_status"], "completed")
        self.assertEqual(payload["answer_text"], "fixture rpc answer")
        self.assertEqual(payload["next_page_token"], "fixture-next")
        self.assertEqual(payload["latest_assistant_message"]["role"], "assistant")

    def test_build_rpc_chat_request_data_defaults_agent_type_for_side_chat(self) -> None:
        payload = self.backend.build_rpc_chat_request_data(
            "收到回复我",
            session_id="fixture-session-id",
            session_type="side_chat",
        )

        self.assertEqual(payload["agent_type"], "chat_v3")

    def test_build_rpc_chat_request_data_prefers_explicit_agent_type(self) -> None:
        payload = self.backend.build_rpc_chat_request_data(
            "收到回复我",
            session_id="fixture-session-id",
            session_type="side_chat",
            agent_type="builder_v3",
            extra_data={"agent_type": "chat_v3"},
        )

        self.assertEqual(payload["agent_type"], "builder_v3")

    def test_extract_successful_rpc_response_data_accepts_response_envelope(self) -> None:
        payload = {
            "response": {
                "packet_type": "response",
                "channel_id": "fixture-channel",
                "params": {
                    "message": "success",
                    "code": 0,
                    "data": {"project_id": "fixture-project-id"},
                },
            }
        }

        response_data = self.backend._extract_successful_rpc_response_data(
            payload,
            service="project",
            method="create_project",
        )

        self.assertEqual(response_data["project_id"], "fixture-project-id")

    def test_send_rpc_chat_creates_context_and_waits_for_messages(self) -> None:
        workspace = self.root / "workspace-a"
        workspace.mkdir()

        with mock.patch.object(
            self.backend,
            "guess_recent_connect_session",
            return_value=None,
        ):
            with mock.patch.object(
                self.backend,
                "create_rpc_project",
                return_value={"project_id": "fixture-project-id"},
            ) as mocked_project:
                with mock.patch.object(
                    self.backend,
                    "create_rpc_session",
                    return_value={"session_id": "fixture-session-id"},
                ) as mocked_session:
                    with mock.patch.object(
                        self.backend,
                        "invoke_aha_rpc",
                        return_value={
                            "response": {
                                "message": "success",
                                "code": 0,
                                "data": {"event": "metadata"},
                            }
                        },
                    ) as mocked_rpc:
                        with mock.patch.object(
                            self.backend,
                            "wait_for_rpc_messages",
                            return_value={
                                "request": {"response": {"message": "success", "code": 0}},
                                "response": {"message": "success", "code": 0},
                                "response_data": {
                                    "messages": [
                                        {
                                            "role": "assistant",
                                            "content": "fixture rpc answer",
                                            "status": "completed",
                                        }
                                    ]
                                },
                                "messages": [
                                    {
                                        "role": "assistant",
                                        "content": "fixture rpc answer",
                                        "status": "completed",
                                    }
                                ],
                                "message_count": 1,
                                "assistant_status": "completed",
                                "latest_assistant_message": {
                                    "role": "assistant",
                                    "content": "fixture rpc answer",
                                    "status": "completed",
                                },
                                "answer_text": "fixture rpc answer",
                                "answer_source": "message.content",
                                "next_page_token": None,
                            },
                        ) as mocked_wait:
                            payload = self.backend.send_rpc_chat(
                                "收到回复我",
                                workspace=workspace,
                                session_type="inline_chat",
                            )

        self.assertEqual(payload["project_id"], "fixture-project-id")
        self.assertEqual(payload["session_id"], "fixture-session-id")
        self.assertEqual(payload["assistant_status"], "completed")
        self.assertEqual(payload["answer_text"], "fixture rpc answer")
        self.assertEqual(payload["connect_session_id"], "")
        self.assertEqual(payload["connect_session_source"], "none")
        self.assertIsNone(payload["guessed_connect_session"])
        self.assertEqual(payload["agent_type"], "inline_chat")
        self.assertEqual(payload["chat_data"]["agent_type"], "inline_chat")
        self.assertEqual(payload["chat_data"]["model_name"], "gpt-5.3-codex")
        self.assertEqual(
            payload["chat_data"]["workspace_folders"],
            [str(workspace.resolve())],
        )
        mocked_project.assert_called_once()
        mocked_session.assert_called_once()
        mocked_wait.assert_called_once()
        self.assertEqual(
            mocked_rpc.call_args.kwargs["client_info"]["project_id"],
            "fixture-project-id",
        )
        self.assertEqual(
            mocked_wait.call_args.kwargs["project_id"],
            "fixture-project-id",
        )
        self.assertEqual(
            mocked_wait.call_args.kwargs["prompt"],
            "收到回复我",
        )

    def test_send_rpc_chat_guesses_connect_session_from_logs(self) -> None:
        workspace = self.root / "workspace-a"
        workspace.mkdir()
        guessed_connect = {
            "connect_session_id": "fixture-recent-connect",
            "trace_id": "fixture-chat-trace",
            "service": "chat",
            "method": "chat",
            "source_logs": ["Modular/ai-agent_fixture_stdout.log"],
            "timestamp": "2026-04-09T10:18:06.123+08:00",
        }

        with mock.patch.object(
            self.backend,
            "guess_recent_connect_session",
            return_value=guessed_connect,
        ) as mocked_guess:
            with mock.patch.object(
                self.backend,
                "create_rpc_project",
                return_value={"project_id": "fixture-project-id"},
            ) as mocked_project:
                with mock.patch.object(
                    self.backend,
                    "create_rpc_session",
                    return_value={"session_id": "fixture-session-id"},
                ) as mocked_session:
                    with mock.patch.object(
                        self.backend,
                        "invoke_aha_rpc",
                        return_value={
                            "response": {
                                "message": "success",
                                "code": 0,
                                "data": {"event": "metadata"},
                            }
                        },
                    ) as mocked_rpc:
                        with mock.patch.object(
                            self.backend,
                            "wait_for_rpc_messages",
                            return_value={
                                "request": {"response": {"message": "success", "code": 0}},
                                "response": {"message": "success", "code": 0},
                                "response_data": {
                                    "messages": [
                                        {
                                            "role": "assistant",
                                            "content": "fixture rpc answer",
                                            "status": "completed",
                                        }
                                    ]
                                },
                                "messages": [
                                    {
                                        "role": "assistant",
                                        "content": "fixture rpc answer",
                                        "status": "completed",
                                    }
                                ],
                                "message_count": 1,
                                "assistant_status": "completed",
                                "latest_assistant_message": {
                                    "role": "assistant",
                                    "content": "fixture rpc answer",
                                    "status": "completed",
                                },
                                "answer_text": "fixture rpc answer",
                                "answer_source": "message.content",
                                "next_page_token": None,
                            },
                        ) as mocked_wait:
                            payload = self.backend.send_rpc_chat(
                                "收到回复我",
                                workspace=workspace,
                                session_type="inline_chat",
                            )

        mocked_guess.assert_called_once_with(service="chat")
        self.assertEqual(payload["connect_session_id"], "fixture-recent-connect")
        self.assertEqual(payload["connect_session_source"], "logs")
        self.assertEqual(payload["guessed_connect_session"], guessed_connect)
        self.assertEqual(
            mocked_project.call_args.kwargs["connect_session_id"],
            "fixture-recent-connect",
        )
        self.assertEqual(
            mocked_session.call_args.kwargs["connect_session_id"],
            "fixture-recent-connect",
        )
        self.assertEqual(
            mocked_rpc.call_args.kwargs["connect_session_id"],
            "fixture-recent-connect",
        )
        self.assertEqual(
            mocked_wait.call_args.kwargs["connect_session_id"],
            "fixture-recent-connect",
        )
        self.assertEqual(
            payload["client_info"]["connect_session_id"],
            "fixture-recent-connect",
        )

    def test_send_rpc_chat_prefers_bridge_state_connect_session(self) -> None:
        workspace = self.root / "workspace-a"
        workspace.mkdir()
        bridge_state = {
            "workspace_folders": [{"fsPath": str(workspace.resolve())}],
            "connect_session_id": "fixture-bridge-connect",
            "manager_exchange": {
                "status": "ready",
                "connect_session_id": "fixture-bridge-connect",
            },
        }

        with mock.patch.object(
            self.backend,
            "read_bridge_state",
            return_value=bridge_state,
        ):
            with mock.patch.object(
                self.backend,
                "create_rpc_project",
                return_value={"project_id": "fixture-project-id"},
            ) as mocked_project:
                with mock.patch.object(
                    self.backend,
                    "create_rpc_session",
                    return_value={"session_id": "fixture-session-id"},
                ) as mocked_session:
                    with mock.patch.object(
                        self.backend,
                        "invoke_aha_rpc",
                        return_value={
                            "response": {
                                "message": "success",
                                "code": 0,
                                "data": {"event": "metadata"},
                            }
                        },
                    ) as mocked_rpc:
                        with mock.patch.object(
                            self.backend,
                            "wait_for_rpc_messages",
                            return_value={
                                "request": {"response": {"message": "success", "code": 0}},
                                "response": {"message": "success", "code": 0},
                                "response_data": {
                                    "messages": [
                                        {
                                            "role": "assistant",
                                            "content": "fixture rpc answer",
                                            "status": "completed",
                                        }
                                    ]
                                },
                                "messages": [
                                    {
                                        "role": "assistant",
                                        "content": "fixture rpc answer",
                                        "status": "completed",
                                    }
                                ],
                                "message_count": 1,
                                "assistant_status": "completed",
                                "latest_assistant_message": {
                                    "role": "assistant",
                                    "content": "fixture rpc answer",
                                    "status": "completed",
                                },
                                "answer_text": "fixture rpc answer",
                                "answer_source": "message.content",
                                "next_page_token": None,
                            },
                        ) as mocked_wait:
                            payload = self.backend.send_rpc_chat(
                                "收到回复我",
                                workspace=workspace,
                                session_type="inline_chat",
                            )

        self.assertEqual(payload["connect_session_id"], "fixture-bridge-connect")
        self.assertEqual(payload["connect_session_source"], "bridge")
        self.assertIsNone(payload["guessed_connect_session"])
        self.assertEqual(
            mocked_project.call_args.kwargs["connect_session_id"],
            "fixture-bridge-connect",
        )
        self.assertEqual(
            mocked_session.call_args.kwargs["connect_session_id"],
            "fixture-bridge-connect",
        )
        self.assertEqual(
            mocked_rpc.call_args.kwargs["connect_session_id"],
            "fixture-bridge-connect",
        )
        self.assertEqual(
            mocked_wait.call_args.kwargs["connect_session_id"],
            "fixture-bridge-connect",
        )

    def test_resolve_aha_runtime_dir_prefers_support_dir_when_aha_dir_exists(self) -> None:
        (self.fixture["support_dir"] / "aha").mkdir(exist_ok=True)

        with mock.patch.object(
            self.backend,
            "extract_transport_topology",
            return_value={
                "app_rpc": {
                    "observed_ipc_addresses": [],
                    "node_socket_rule": {"runtime_dir": "/tmp/fixture-missing-runtime"},
                }
            },
        ):
            runtime_dir = self.backend.resolve_aha_runtime_dir()

        self.assertEqual(
            Path(runtime_dir).resolve(), self.fixture["support_dir"].resolve()
        )

    def test_exports_chat_session_via_rpc_bridge(self) -> None:
        export_path = self.root / "exported-chat.md"
        export_path.write_text("# fixture export\n\nhello world\n")

        with mock.patch.object(
            self.backend,
            "guess_recent_connect_session",
            return_value={
                "connect_session_id": "fixture-recent-connect",
                "trace_id": "fixture-ok-trace",
                "service": "chat",
                "method": "get_messages",
                "source_logs": ["Modular/ai-agent_0_fixture_stdout.log"],
                "timestamp": "2026-04-04T12:00:00.401+08:00",
            },
        ):
            with mock.patch.object(
                self.backend,
                "invoke_aha_rpc",
                return_value={
                    "response": {
                        "message": "success",
                        "code": 0,
                        "data": {"file_path": str(export_path)},
                    }
                },
            ) as mocked_rpc:
                payload = self.backend.export_chat_session("fixture-session-id")

        self.assertEqual(payload["session_id"], "fixture-session-id")
        self.assertEqual(payload["connect_session_id"], "fixture-recent-connect")
        self.assertEqual(payload["connect_session_source"], "logs")
        self.assertEqual(payload["export_path"], str(export_path))
        self.assertIn("hello world", payload["content"])
        mocked_rpc.assert_called_once()
        self.assertEqual(
            mocked_rpc.call_args.kwargs["connect_session_id"],
            "fixture-recent-connect",
        )

    def test_wait_for_chat_turn_prefers_unseen_frontend_message_id(self) -> None:
        started_after = TraeBackend._parse_iso_timestamp("2026-04-04T12:00:10+08:00")
        payload = {
            "latest_log_session": "/tmp/fixture-logs",
            "turns": [
                {
                    "frontend_message_id": "new-message-id",
                    "started_at": "2026-04-04T12:00:09+08:00",
                    "last_seen_at": "2026-04-04T12:00:11+08:00",
                    "status": "completed",
                },
                {
                    "frontend_message_id": "fixture-message-id",
                    "started_at": "2026-04-04T12:00:05+08:00",
                    "last_seen_at": "2026-04-04T12:00:08+08:00",
                    "status": "completed",
                },
            ],
        }

        with mock.patch.object(self.backend, "extract_chat_turns", return_value=payload):
            inspection = self.backend.wait_for_chat_turn(
                started_after=started_after,
                exclude_frontend_ids={"fixture-message-id"},
            )

        self.assertTrue(inspection["matched_after_dispatch"])
        self.assertEqual(inspection["turn"]["frontend_message_id"], "new-message-id")
        self.assertIsNone(inspection["note"])

    def test_wait_for_chat_turn_reports_latest_turn_when_no_match(self) -> None:
        started_after = TraeBackend._parse_iso_timestamp("2026-04-04T12:00:10+08:00")
        payload = {
            "latest_log_session": "/tmp/fixture-logs",
            "turns": [
                {
                    "frontend_message_id": "fixture-message-id",
                    "started_at": "2026-04-04T12:00:05+08:00",
                    "last_seen_at": "2026-04-04T12:00:08+08:00",
                    "status": "completed",
                }
            ],
        }

        with mock.patch.object(self.backend, "extract_chat_turns", return_value=payload):
            inspection = self.backend.wait_for_chat_turn(
                started_after=started_after,
                exclude_frontend_ids={"fixture-message-id"},
            )

        self.assertFalse(inspection["matched_after_dispatch"])
        self.assertIsNone(inspection["turn"])
        self.assertEqual(
            inspection["latest_turn"]["frontend_message_id"],
            "fixture-message-id",
        )
        self.assertIn("No new local chat turn matched", inspection["note"])

    def test_extracts_transport_topology(self) -> None:
        topology = self.backend.extract_transport_topology()

        self.assertEqual(
            Path(topology["main_socket"]).resolve(),
            (self.fixture["support_dir"] / "1.10-main.sock").resolve(),
        )
        self.assertEqual(topology["app_rpc"]["client_connected_service"], "ai-agent")
        self.assertEqual(topology["app_rpc"]["service_name"], "ai-agent")
        self.assertTrue(topology["app_rpc"]["server_enabled"])
        self.assertTrue(topology["app_rpc"]["ffi_connection_accepted"])
        self.assertTrue(topology["app_rpc"]["jsonrpsee_server_started"])
        self.assertEqual(
            topology["app_rpc"]["local_impl"]["client_api"],
            "electron.ahaIpc.connect",
        )
        self.assertEqual(
            topology["app_rpc"]["local_impl"]["server_api"],
            "electron.ahaIpc.serve",
        )
        self.assertEqual(
            topology["app_rpc"]["node_socket_rule"]["socket_path"],
            "/tmp/aha/ai-agent.sock",
        )
        self.assertEqual(
            topology["app_rpc"]["observed_ipc_addresses"][0]["address"],
            "ipc:///home/test/.trae-server/socks/2061289/e7f53dc/aha/ai-agent.sock",
        )
        self.assertEqual(topology["ckg"]["host"], "127.0.0.1")
        self.assertEqual(topology["ckg"]["port"], 51002)
        self.assertTrue(topology["ckg"]["saw_grpc_content_type"])
        self.assertEqual(topology["oauth_callback"]["port"], 17790)
        self.assertEqual(topology["request_count"], 1)
        self.assertEqual(topology["ai_agent_request_count"], 4)

        sample_request = topology["sample_request"]
        self.assertEqual(sample_request["service"], "healthcheck")
        self.assertEqual(sample_request["method"], "ping")
        self.assertEqual(
            sample_request["matched_ai_agent_request"]["channel_id"],
            sample_request["channel_id"],
        )
        self.assertEqual(sample_request["response"]["code"], 0)
        self.assertEqual(sample_request["response"]["payload"]["data"]["message"], "pong")
        self.assertTrue(
            any("CKG sidecar gRPC endpoint" in note for note in topology["notes"])
        )
        self.assertTrue(
            any("electron.ahaIpc.connect/serve" in note for note in topology["notes"])
        )

    def test_extracts_transport_topology_live_probe(self) -> None:
        bundle_path = self.fixture["app_path"]
        ps_output = "\n".join(
            [
                f"18156 1 {bundle_path}/Contents/MacOS/Trae",
                f"18164 18156 {bundle_path}/Contents/Frameworks/Trae Helper (Plugin).app/Contents/MacOS/Trae Helper (Plugin) --vscode-crash-reporter-process-type=ai",
                f"18165 18156 {bundle_path}/Contents/Frameworks/Trae Helper (Plugin).app/Contents/MacOS/Trae Helper (Plugin) --vscode-crash-reporter-process-type=ckg",
                f"18166 18156 {bundle_path}/Contents/Frameworks/Trae Helper (Plugin).app/Contents/MacOS/Trae Helper (Plugin) --vscode-crash-reporter-process-type=ai-server",
            ]
        )
        unix_outputs = {
            18164: "f18\0tunix\0n->0xa1\0f19\0tunix\0n->0xa2\0",
        }
        tcp_outputs = {
            18165: "f22\0tIPv4\0n127.0.0.1:51002\0",
        }

        def fake_probe(args: list[str]) -> subprocess.CompletedProcess[str]:
            if args == ["ps", "-axo", "pid=,ppid=,command="]:
                return subprocess.CompletedProcess(args, 0, ps_output, "")
            pid = int(args[4])
            if args[-1] == "-F0fn":
                return subprocess.CompletedProcess(args, 0, "", "")
            if "-U" in args:
                return subprocess.CompletedProcess(args, 0, unix_outputs.get(pid, ""), "")
            if "-iTCP" in args:
                return subprocess.CompletedProcess(args, 0, tcp_outputs.get(pid, ""), "")
            raise AssertionError(f"unexpected probe command: {args}")

        with mock.patch.object(self.backend, "_command_available", return_value=True):
            with mock.patch.object(
                self.backend, "_run_probe_command", side_effect=fake_probe
            ):
                topology = self.backend.extract_transport_topology(include_live=True)

        live = topology["live"]
        self.assertTrue(live["ps_available"])
        self.assertTrue(live["lsof_available"])
        self.assertEqual(len(live["processes"]), 4)

        ai_helper = next(
            item for item in live["processes"] if item["role"] == "ai-helper"
        )
        self.assertEqual(ai_helper["process_type"], "ai")
        self.assertEqual(ai_helper["sockets"]["named_unix_sockets"], [])
        self.assertEqual(ai_helper["sockets"]["anonymous_unix_socket_count"], 2)

        ckg_helper = next(
            item for item in live["processes"] if item["role"] == "ckg-helper"
        )
        self.assertEqual(ckg_helper["sockets"]["tcp_listeners"], ["127.0.0.1:51002"])
        self.assertTrue(
            any("desktop Electron main process and the ai helper" in note for note in live["notes"])
        )
        self.assertTrue(
            any("does not expose a named local unix socket" in note for note in live["notes"])
        )
        self.assertTrue(
            any("ckg-helper pid 18165 is listening on 127.0.0.1:51002" in note for note in live["notes"])
        )
        self.assertEqual(live["ps_probe_status"], "ok")
        self.assertEqual(live["lsof_probe_status"], "ok")
        self.assertEqual(live["lsof_error_count"], 0)
        self.assertEqual(live["lsof_errors"], [])

    def test_extracts_transport_topology_live_probe_blocked_ps(self) -> None:
        def fake_probe(args: list[str]) -> subprocess.CompletedProcess[str]:
            self.assertEqual(args, ["ps", "-axo", "pid=,ppid=,command="])
            return subprocess.CompletedProcess(
                args,
                1,
                "",
                "[Errno 1] Operation not permitted: 'ps'",
            )

        with mock.patch.object(self.backend, "_command_available", return_value=True):
            with mock.patch.object(
                self.backend,
                "_run_probe_command",
                side_effect=fake_probe,
            ):
                topology = self.backend.extract_transport_topology(include_live=True)

        live = topology["live"]
        self.assertTrue(live["ps_available"])
        self.assertEqual(live["ps_probe_status"], "blocked")
        self.assertEqual(live["ps_error"], "[Errno 1] Operation not permitted: 'ps'")
        self.assertTrue(live["lsof_available"])
        self.assertEqual(live["lsof_probe_status"], "not-run")
        self.assertEqual(live["processes"], [])
        self.assertTrue(
            any("installed but the live probe was blocked" in note for note in live["notes"])
        )

    def test_install_bridge_extension_materializes_extension_files(self) -> None:
        payload = self.backend.install_bridge_extension()

        install_dir = Path(payload["install_dir"])
        registry_path = Path(payload["registry_path"])
        registry = json.loads(registry_path.read_text())

        self.assertEqual(payload["extension_id"], "traecli.headless-bridge")
        self.assertTrue(install_dir.exists())
        self.assertTrue((install_dir / "package.json").exists())
        self.assertTrue((install_dir / "extension.js").exists())
        self.assertTrue((install_dir / ".vsixmanifest").exists())
        matching_entry = next(
            (
                entry
                for entry in registry
                if isinstance(entry, dict)
                and entry.get("identifier", {}).get("id") == "traecli.headless-bridge"
            ),
            None,
        )
        self.assertIsNotNone(matching_entry)
        assert matching_entry is not None
        self.assertTrue(matching_entry["identifier"].get("uuid"))
        self.assertTrue(matching_entry["metadata"].get("id"))
        self.assertEqual(
            matching_entry["metadata"].get("publisherDisplayName"),
            "traecli",
        )
        self.assertIn("*", json.loads((install_dir / "package.json").read_text())["activationEvents"])

    def test_install_and_uninstall_headless_patch_restores_bundle(self) -> None:
        bundle_path = self.backend.paths.ai_chat_bundle_path
        original = bundle_path.read_text()

        installed = self.backend.install_headless_patch()
        patched = bundle_path.read_text()

        self.assertEqual(installed["status"], "installed")
        self.assertTrue(installed["patch_installed"])
        self.assertTrue(installed["patch_up_to_date"])
        self.assertIn("traecli.headless.send", patched)
        self.assertIn(HEADLESS_PATCH_MARKER, patched)
        self.assertNotEqual(patched, original)
        self.assertTrue(Path(installed["backup_path"]).exists())

        restored = self.backend.uninstall_headless_patch()

        self.assertEqual(restored["status"], "restored")
        self.assertFalse(restored["patch_installed"])
        self.assertEqual(bundle_path.read_text(), original)
        self.assertFalse(Path(restored["backup_path"]).exists())

    def test_install_headless_patch_upgrades_legacy_patch_in_place(self) -> None:
        bundle_path = self.backend.paths.ai_chat_bundle_path
        legacy_bundle = bundle_path.read_text().replace(
            'i.registerCommand("workbench.action.chat.icube.send.codeReview",async(e,t,r)=>{',
            'i.registerCommand("traecli.headless.send",async(e,t,r)=>{'
            'if(!t||0===t.length)throw Error("no inputs provided");'
            'let i=ur.getInstance(),n=i.resolve(Sn.IICubeAuthService);'
            'if("not-login"===n.getCurrentLoginStatusSync())throw Error("the headless command only support login user");'
            'let o={enableUnstableFields:!0};'
            'return r&&(r.sessionId&&(o.sessionId=r.sessionId),'
            'r.modelName&&(o.modelName=r.modelName),'
            'r.agentName&&(o.agentName=r.agentName),'
            'r.agentId&&(o.agentId=r.agentId),'
            'r.workspaceFolder&&(o.workspaceFolder=r.workspaceFolder),'
            'r.integrations&&(o.integrations=r.integrations),'
            'r.cancelEventKey&&(o.cancelEventKey=r.cancelEventKey)),'
            'try{let e=i.resolve(Sn.IViewsService);await e.openViewContainer(zN,!0)}catch(e){}'
            'try{document.dispatchEvent(new CustomEvent("icube.ai-agent.focusInput"))}catch(e){}'
            'await z7.sendToAgent(t,o)}),'
            'i.registerCommand("workbench.action.chat.icube.send.codeReview",async(e,t,r)=>{',
        )
        bundle_path.write_text(legacy_bundle)

        payload = self.backend.install_headless_patch()
        patched = bundle_path.read_text()

        self.assertEqual(payload["status"], "upgraded")
        self.assertIn(HEADLESS_PATCH_MARKER, patched)
        self.assertIn("answerText", patched)
        self.assertEqual(patched.count('i.registerCommand("traecli.headless.send"'), 1)
        self.assertTrue(payload["patch_up_to_date"])

    def test_install_headless_patch_upgrades_marked_broken_block_in_place(self) -> None:
        bundle_path = self.backend.paths.ai_chat_bundle_path
        broken_bundle = bundle_path.read_text().replace(
            'i.registerCommand("workbench.action.chat.icube.send.codeReview",async(e,t,r)=>{',
            '/* traecli-headless-patch:v5 */function __traecliHeadlessTrimText(e){return e}'
            'i.registerCommand("traecli.headless.send",async(e,t,r)=>{'
            'if(!t||0===t.length)throw Error("no inputs provided");'
            'try{let e=i.resolve(Sn.IViewsService);await e.openViewContainer(zN,!0)}catch(e){}'
            'try{document.dispatchEvent(new CustomEvent("icube.ai-agent.focusInput"))}catch(e){}'
            'return await z7.sendToAgent(t,{enableUnstableFields:!0})}),'
            'i.registerCommand("workbench.action.chat.icube.send.codeReview",async(e,t,r)=>{',
        )
        bundle_path.write_text(broken_bundle)

        payload = self.backend.install_headless_patch()
        patched = bundle_path.read_text()

        self.assertEqual(payload["status"], "upgraded")
        self.assertIn(HEADLESS_PATCH_MARKER, patched)
        self.assertNotIn("/* traecli-headless-patch:v5 */function __traecliHeadless", patched)
        self.assertIn("(()=>{function __traecliHeadlessTrimText", patched)
        self.assertEqual(patched.count('i.registerCommand("traecli.headless.send"'), 1)
        self.assertTrue(payload["patch_up_to_date"])

    def test_headless_patch_snippet_is_wrapped_in_iife_expression(self) -> None:
        snippet = self.backend._build_headless_patch_snippet()

        self.assertTrue(snippet.startswith(f"/* {HEADLESS_PATCH_MARKER} */(()=>{{"))
        self.assertIn('return i.registerCommand("traecli.headless.send"', snippet)
        self.assertTrue(snippet.endswith("})(),"))
        self.assertNotIn(f"/* {HEADLESS_PATCH_MARKER} */function __traecliHeadless", snippet)

    def test_headless_patch_snippet_is_node_parseable_when_node_available(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not available")

        snippet_path = self.root / "headless-patch-snippet.js"
        snippet_path.write_text(f"0,{self.backend._build_headless_patch_snippet()}0;\n")

        completed = subprocess.run(
            [node, "--check", str(snippet_path)],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(
            completed.returncode,
            0,
            msg=completed.stderr or completed.stdout or "node --check failed",
        )

    def test_app_bundle_writable_reports_true_for_fixture(self) -> None:
        payload = self.backend.app_bundle_writable()

        self.assertTrue(payload["writable"])
        self.assertEqual(
            Path(payload["probe_dir"]).resolve(),
            self.backend.paths.ai_chat_bundle_path.parent.resolve(),
        )

    def test_clone_app_bundle_creates_copy(self) -> None:
        target_app_path = self.root / "prepared" / "Trae Headless.app"

        payload = self.backend.clone_app_bundle(target_app_path=target_app_path)

        self.assertEqual(payload["status"], "created")
        self.assertEqual(Path(payload["target_app_path"]).resolve(), target_app_path.resolve())
        self.assertTrue(target_app_path.exists())
        copied_bundle_path = (
            target_app_path
            / "Contents"
            / "Resources"
            / "app"
            / "node_modules"
            / "@byted-icube"
            / "ai-modules-chat"
            / "dist"
            / "index.js"
        )
        self.assertTrue(copied_bundle_path.exists())

    def test_prepare_headless_app_copy_creates_patched_copy(self) -> None:
        target_app_path = self.root / "prepared" / "Trae Headless.app"

        with mock.patch.object(
            TraeBackend,
            "resign_app_bundle",
            return_value={
                "status": "signed",
                "app_path": str(target_app_path),
                "verified": True,
            },
        ) as mocked_resign:
            payload = self.backend.prepare_headless_app_copy(target_app_path=target_app_path)

        self.assertEqual(payload["status"], "prepared")
        self.assertEqual(Path(payload["app_path"]).resolve(), target_app_path.resolve())
        self.assertEqual(
            Path(payload["source_app_path"]).resolve(),
            self.backend.paths.app_path.resolve(),
        )
        self.assertTrue(payload["patch"]["patch_installed"])
        self.assertEqual(payload["signature"]["status"], "signed")
        self.assertTrue(payload["signature"]["verified"])
        self.assertTrue(payload["writable"]["writable"])
        self.assertIn(
            "traecli.headless.send",
            Path(payload["patch"]["bundle_path"]).read_text(),
        )
        self.assertNotIn(
            "traecli.headless.send",
            self.backend.paths.ai_chat_bundle_path.read_text(),
        )
        mocked_resign.assert_called_once()

    def test_resign_app_bundle_runs_codesign_and_verify(self) -> None:
        sign_result = subprocess.CompletedProcess(
            ["codesign"],
            0,
            "",
            "/tmp/fake.app: replacing existing signature\n",
        )
        verify_result = subprocess.CompletedProcess(
            ["codesign"],
            0,
            "",
            "",
        )

        with mock.patch.object(self.backend, "_command_available", return_value=True):
            with mock.patch(
                "cli_anything.trae.utils.trae_backend.subprocess.run",
                side_effect=[sign_result, verify_result],
            ) as mocked_run:
                payload = self.backend.resign_app_bundle()

        self.assertEqual(payload["status"], "signed")
        self.assertTrue(payload["verified"])
        self.assertEqual(mocked_run.call_count, 2)
        self.assertEqual(mocked_run.call_args_list[0].args[0][:5], ["codesign", "--force", "--deep", "--sign", "-"])
        self.assertEqual(mocked_run.call_args_list[1].args[0][:4], ["codesign", "--verify", "--deep", "--strict"])

    def test_install_headless_patch_guides_to_prepare_when_bundle_is_not_writable(self) -> None:
        with mock.patch.object(
            self.backend,
            "app_bundle_writable",
            return_value={
                "writable": False,
                "error": "Operation not permitted",
                "probe_dir": str(self.backend.paths.ai_chat_bundle_path.parent),
            },
        ):
            with self.assertRaises(RuntimeError) as ctx:
                self.backend.install_headless_patch()

        message = str(ctx.exception)
        self.assertIn("headless prepare", message)
        self.assertIn(str(self.backend.default_headless_app_copy_path()), message)

    def test_invoke_headless_chat_extracts_session_and_answer(self) -> None:
        mocked_payload = {
            "ok": True,
            "command": "traecli.headless.send",
            "arg_count": 2,
            "result": {
                "sessionId": "fixture-headless-session",
                "requestMessageId": "fixture-request-id",
                "unstableFields": {
                    "session": {
                        "sessionId": "fixture-headless-session",
                        "messages": [
                            {"role": "user", "content": "hello fixture"},
                            {"role": "assistant", "content": "fixture headless answer"},
                        ],
                    }
                },
            },
        }

        with mock.patch.object(
            self.backend,
            "create_rpc_project",
            return_value={
                "project_id": "fixture-project-id",
                "real_project_id": "fixture-real-project-id",
            },
        ) as mocked_project:
            with mock.patch.object(
                self.backend,
                "execute_bridge_command",
                return_value=mocked_payload,
            ) as mocked_execute:
                payload = self.backend.invoke_headless_chat(
                    "hello fixture",
                    session_id="fixture-headless-session",
                    timeout_seconds=12.0,
                    workspace=self.root / "workspace-a",
                )

        self.assertEqual(payload["session_id"], "fixture-headless-session")
        self.assertEqual(payload["request_message_id"], "fixture-request-id")
        self.assertEqual(payload["answer_text"], "fixture headless answer")
        self.assertEqual(payload["session"]["sessionId"], "fixture-headless-session")
        mocked_project.assert_called_once()
        self.assertEqual(
            mocked_execute.call_args.kwargs["args"],
            [
                ["hello fixture"],
                {
                    "projectId": "fixture-project-id",
                    "realProjectId": "fixture-real-project-id",
                    "sessionId": "fixture-headless-session",
                    "workspaceFolder": str((self.root / "workspace-a").resolve()),
                },
            ],
        )

    def test_invoke_headless_chat_prefers_result_answer_text(self) -> None:
        mocked_payload = {
            "ok": True,
            "command": "traecli.headless.send",
            "arg_count": 2,
            "result": {
                "sessionId": "fixture-headless-session",
                "requestMessageId": "fixture-request-id",
                "answerText": "HEADLESS-FINAL-OK",
                "answerSource": "plan_item.finish.params.summary",
                "unstableFields": {
                    "session": {
                        "sessionId": "fixture-headless-session",
                        "messages": [
                            {"role": "assistant", "parsedQuery": ["收到回复我"]},
                        ],
                    }
                },
            },
        }

        with mock.patch.object(
            self.backend,
            "execute_bridge_command",
            return_value=mocked_payload,
        ):
            payload = self.backend.invoke_headless_chat("收到回复我")

        self.assertEqual(payload["answer_text"], "HEADLESS-FINAL-OK")
        self.assertEqual(payload["answer_source"], "plan_item.finish.params.summary")

    def test_extract_headless_answer_ignores_prompt_echo_and_uses_finish_plan_item(self) -> None:
        result = {
            "sessionId": "fixture-headless-session",
            "requestMessageId": "fixture-request-id",
            "unstableFields": {
                "session": {
                    "sessionId": "fixture-headless-session",
                    "messages": [
                        {"role": "user", "content": "请只回复 HEADLESS-FINAL-OK"},
                        {
                            "role": "assistant",
                            "content": "",
                            "parsedQuery": ["请只回复 HEADLESS-FINAL-OK"],
                            "agentTaskContent": {
                                "proposal": "",
                                "proposalReasoningContent": "我会按要求只返回指定字符串。",
                                "guideline": {
                                    "planItems": [
                                        {
                                            "toolName": "finish",
                                            "thought": "HEADLESS-FINAL-OK",
                                            "params": {
                                                "summary": "HEADLESS-FINAL-OK",
                                                "content": "HEADLESS-FINAL-OK",
                                            },
                                        }
                                    ]
                                },
                            },
                        },
                    ],
                }
            },
        }

        text, source = TraeBackend._extract_headless_answer_payload(
            result,
            prompt="请只回复 HEADLESS-FINAL-OK",
        )

        self.assertEqual(text, "HEADLESS-FINAL-OK")
        self.assertEqual(source, "plan_item.finish.params.summary")

    def test_extract_headless_answer_ignores_previous_turn_without_current_assistant(self) -> None:
        result = {
            "sessionId": "fixture-headless-session",
            "requestMessageId": "fixture-request-id",
            "unstableFields": {
                "session": {
                    "sessionId": "fixture-headless-session",
                    "messages": [
                        {"role": "user", "content": "旧问题"},
                        {"role": "assistant", "content": "旧回答"},
                        {"role": "user", "content": "新问题"},
                    ],
                }
            },
        }

        text, source = TraeBackend._extract_headless_answer_payload(
            result,
            prompt="新问题",
        )

        self.assertIsNone(text)
        self.assertIsNone(source)

    def test_extract_headless_answer_prefers_rpc_finish_summary_over_task_id(self) -> None:
        result = {
            "sessionId": "fixture-headless-session",
            "requestMessageId": "fixture-request-id",
            "unstableFields": {
                "session": {
                    "sessionId": "fixture-headless-session",
                    "messages": [
                        {"role": "user", "content": "这个项目是什么？"},
                        {
                            "role": "assistant",
                            "content": {
                                "task_id": "69db4daec1c358fedf4ccb4d",
                                "messages": [
                                    {
                                        "type": "plan_item",
                                        "plan_item": {
                                            "thought": "我来帮您了解这个项目。",
                                            "tool_call_info": {
                                                "name": "LS",
                                                "result": {"status": "success"},
                                            },
                                        },
                                    },
                                    {
                                        "type": "plan_item",
                                        "plan_item": {
                                            "tool_call_info": {
                                                "name": "finish",
                                                "params": {
                                                    "summary": "这是一个 Trae CLI Harness 项目。",
                                                },
                                                "result": {"status": "success"},
                                            },
                                        },
                                    },
                                ],
                            },
                        },
                    ],
                }
            },
        }

        text, source = TraeBackend._extract_headless_answer_payload(
            result,
            prompt="这个项目是什么？",
        )

        self.assertEqual(text, "这是一个 Trae CLI Harness 项目。")
        self.assertEqual(source, "plan_item.finish.tool_call_info.params.summary")

    def test_invoke_headless_chat_polls_rpc_messages_when_current_turn_is_pending(self) -> None:
        mocked_payload = {
            "ok": True,
            "command": "traecli.headless.send",
            "arg_count": 2,
            "result": {
                "sessionId": "fixture-headless-session",
                "requestMessageId": "",
                "unstableFields": {
                    "session": {
                        "sessionId": "fixture-headless-session",
                        "messages": [
                            {"role": "user", "content": "这个项目是什么？"},
                        ],
                    }
                },
            },
        }
        polled_messages_payload = {
            "messages": [
                {"role": "user", "content": "这个项目是什么？"},
                {"role": "assistant", "content": "这是一个 Trae CLI Harness 项目。"},
            ],
            "message_count": 2,
            "answer_text": "这是一个 Trae CLI Harness 项目。",
            "answer_source": "message.content",
            "assistant_pending": False,
            "current_turn_assistant_message": {
                "role": "assistant",
                "content": "这是一个 Trae CLI Harness 项目。",
                "status": "completed",
            },
        }

        with mock.patch.object(
            self.backend,
            "create_rpc_project",
            return_value={
                "project_id": "fixture-project-id",
                "real_project_id": "fixture-real-project-id",
            },
        ):
            with mock.patch.object(
                self.backend,
                "execute_bridge_command",
                return_value=mocked_payload,
            ):
                with mock.patch.object(
                    self.backend,
                    "read_bridge_state",
                    return_value=None,
                ):
                    with mock.patch.object(
                        self.backend,
                        "guess_recent_connect_session",
                        return_value=None,
                    ):
                        with mock.patch.object(
                            self.backend,
                            "wait_for_headless_messages",
                            return_value=polled_messages_payload,
                        ) as mocked_wait:
                            payload = self.backend.invoke_headless_chat(
                                "这个项目是什么？",
                                timeout_seconds=12.0,
                                workspace=self.root / "workspace-a",
                            )

        self.assertEqual(payload["answer_text"], "这是一个 Trae CLI Harness 项目。")
        self.assertEqual(payload["answer_source"], "message.content")
        self.assertEqual(payload["session"]["messages"], polled_messages_payload["messages"])
        self.assertEqual(payload["messages_poll"], polled_messages_payload)
        self.assertEqual(mocked_wait.call_args.kwargs["project_id"], "fixture-project-id")
        self.assertEqual(
            mocked_wait.call_args.kwargs["workspace"],
            str((self.root / "workspace-a").resolve()),
        )

    def test_cdp_build_activity_state_marks_action_preface_as_pending(self) -> None:
        payload = self._evaluate_cdp_build_activity_state(
            text="我来帮你了解这个项目。让我先查看一下项目的目录结构。",
            prompt="这个项目是什么？",
        )

        self.assertTrue(payload["pending"])
        self.assertEqual(
            payload["text"],
            "我来帮你了解这个项目。让我先查看一下项目的目录结构。",
        )

    def test_cdp_build_activity_state_keeps_final_summary_non_pending(self) -> None:
        payload = self._evaluate_cdp_build_activity_state(
            text="这个项目是一个 Trae CLI Harness，用于把 Trae 桌面能力包装成命令行工具。",
            prompt="这个项目是什么？",
        )

        self.assertFalse(payload["pending"])
        self.assertTrue(payload["meaningful"])

    def test_cdp_select_target_prefers_exact_title_match_over_focused_prefix_match(self) -> None:
        target = self._evaluate_cdp_select_target(
            targets=[
                {
                    "id": "exact-target",
                    "type": "page",
                    "title": "Traeclaw",
                    "url": "vscode-file://vscode-app/workbench.html",
                    "webSocketDebuggerUrl": "ws://127.0.0.1/devtools/page/exact-target",
                },
                {
                    "id": "focused-prefix-target",
                    "type": "page",
                    "title": "Traeclawcli",
                    "url": "vscode-file://vscode-app/workbench.html",
                    "webSocketDebuggerUrl": "ws://127.0.0.1/devtools/page/focused-prefix-target",
                },
            ],
            config={
                "titleContains": ["Traeclaw"],
                "urlContains": [],
                "requiredUrlContains": ["workbench"],
                "preferFocused": True,
                "targetId": "",
                "host": "127.0.0.1",
                "port": 9222,
                "commandTimeoutMs": 1000,
            },
            inspect_states={
                "exact-target": {
                    "hasFocus": False,
                    "visibilityState": "visible",
                    "hidden": False,
                },
                "focused-prefix-target": {
                    "hasFocus": True,
                    "visibilityState": "visible",
                    "hidden": False,
                },
            },
        )

        self.assertEqual(target["id"], "exact-target")

    def test_cdp_select_target_ignores_bound_prefix_match_when_exact_workspace_exists(self) -> None:
        target = self._evaluate_cdp_select_target(
            targets=[
                {
                    "id": "exact-target",
                    "type": "page",
                    "title": "Traeclaw",
                    "url": "vscode-file://vscode-app/workbench.html",
                    "webSocketDebuggerUrl": "ws://127.0.0.1/devtools/page/exact-target",
                },
                {
                    "id": "bound-prefix-target",
                    "type": "page",
                    "title": "Traeclawcli",
                    "url": "vscode-file://vscode-app/workbench.html",
                    "webSocketDebuggerUrl": "ws://127.0.0.1/devtools/page/bound-prefix-target",
                },
            ],
            config={
                "titleContains": ["Traeclaw"],
                "urlContains": [],
                "requiredUrlContains": ["workbench"],
                "preferFocused": False,
                "targetId": "bound-prefix-target",
                "host": "127.0.0.1",
                "port": 9222,
                "commandTimeoutMs": 1000,
            },
        )

        self.assertEqual(target["id"], "exact-target")

    def test_headless_status_reports_visible_command_when_bridge_is_active(self) -> None:
        self.backend.install_headless_patch()
        self.backend.paths.bridge_state_path.parent.mkdir(parents=True, exist_ok=True)
        self.backend.paths.bridge_state_path.write_text(
            json.dumps(
                {
                    "host": "127.0.0.1",
                    "port": 53098,
                    "token": "fixture-bridge-token",
                }
            )
        )

        with mock.patch.object(self.backend, "headless_command_available", return_value=True):
            payload = self.backend.headless_status(ping=True, timeout_seconds=1.0)

        self.assertTrue(payload["patch_installed"])
        self.assertTrue(payload["bridge_state_present"])
        self.assertTrue(payload["command_available"])

    def test_bridge_status_reports_runtime_diagnostics_when_state_is_missing(self) -> None:
        latest = self.fixture["support_dir"] / "logs" / "20260405T120000"
        (latest / "window1").mkdir(parents=True, exist_ok=True)
        (latest / "window1" / "renderer.log").write_text(
            "\n".join(
                [
                    "2026-04-05T12:00:00.000+08:00 [info] Created extension scanner input for file:///Applications/Trae.app/Contents/Resources/app/extensions with language zh-cn {}",
                    "2026-04-05T12:00:00.001+08:00 [info] Created extension scanner input for vscode-userdata:/tmp/extensions.json with language zh-cn {}",
                ]
            )
            + "\n"
        )

        with mock.patch.object(self.backend, "latest_log_session_dir", return_value=latest):
            with mock.patch.object(
                self.backend,
                "extract_transport_topology",
                return_value={
                    "latest_log_session": str(latest),
                    "live": {
                        "ps_probe_status": "ok",
                        "lsof_probe_status": "ok",
                        "processes": [],
                        "notes": [],
                    },
                },
            ):
                payload = self.backend.bridge_status(workspace=self.root / "workspace-root")

        self.assertIsNone(payload["state"])
        self.assertIsNotNone(payload["runtime_diagnostics"])
        diagnostics = payload["runtime_diagnostics"]
        self.assertEqual(diagnostics["extension_scanner_input_count"], 2)
        self.assertEqual(diagnostics["started_local_extension_host_count"], 0)
        self.assertEqual(diagnostics["extension_host_process_count"], 0)
        self.assertIn(
            "Renderer logs show extension scanning",
            " ".join(diagnostics["notes"]),
        )

    def test_bridge_status_runtime_diagnostics_falls_back_from_empty_active_session(self) -> None:
        older = self.fixture["support_dir"] / "logs" / "20260405T120000"
        newer = self.fixture["support_dir"] / "logs" / "20260406T120000"
        (older / "window1").mkdir(parents=True, exist_ok=True)
        older_renderer = older / "window1" / "renderer.log"
        older_renderer.write_text(
            "2026-04-05T12:00:00.000+08:00 [info] Started local extension host with pid 54321.\n"
        )
        newer.mkdir(parents=True, exist_ok=True)

        with mock.patch.object(self.backend, "latest_log_session_dir", return_value=newer):
            with mock.patch.object(
                self.backend,
                "extract_transport_topology",
                return_value={
                    "latest_log_session": str(newer),
                    "live": {
                        "ps_probe_status": "ok",
                        "lsof_probe_status": "ok",
                        "processes": [],
                        "notes": [],
                    },
                },
            ):
                payload = self.backend.bridge_status(workspace=self.root / "workspace-root")

        diagnostics = payload["runtime_diagnostics"]
        self.assertEqual(diagnostics["active_log_session"], str(newer))
        self.assertEqual(
            Path(diagnostics["latest_log_session"]).resolve(),
            older.resolve(),
        )
        self.assertEqual(diagnostics["started_local_extension_host_count"], 1)
        self.assertIn("fell back to", " ".join(diagnostics["notes"]))

    def test_read_bridge_state_prefers_matching_workspace_instance_and_dedupes_legacy_copy(self) -> None:
        workspace_a = self.root / "workspace-a"
        workspace_b = self.root / "workspace-b"
        workspace_a.mkdir()
        workspace_b.mkdir()
        instances_dir = self.backend.paths.bridge_state_instances_dir
        instances_dir.mkdir(parents=True, exist_ok=True)

        state_a = {
            "instance_id": "fixture-instance-a",
            "pid": 41001,
            "host": "127.0.0.1",
            "port": 53081,
            "token": "fixture-token-a",
            "last_updated_at": "2026-04-09T10:00:00+08:00",
            "workspace_folders": [{"fsPath": str(workspace_a.resolve())}],
        }
        state_b = {
            "instance_id": "fixture-instance-b",
            "pid": 41002,
            "host": "127.0.0.1",
            "port": 53082,
            "token": "fixture-token-b",
            "last_updated_at": "2026-04-09T11:00:00+08:00",
            "workspace_folders": [{"fsPath": str(workspace_b.resolve())}],
        }

        (instances_dir / "41001.json").write_text(json.dumps(state_a))
        (instances_dir / "41002.json").write_text(json.dumps(state_b))
        self.backend.paths.bridge_state_path.parent.mkdir(parents=True, exist_ok=True)
        self.backend.paths.bridge_state_path.write_text(json.dumps(state_a))

        states = self.backend.read_bridge_states()
        selected = self.backend.read_bridge_state(workspace=workspace_a)

        self.assertEqual(len(states), 2)
        self.assertEqual(states[0]["instance_id"], "fixture-instance-b")
        self.assertEqual(selected["instance_id"], "fixture-instance-a")
        self.assertEqual(
            Path(selected["state_path"]).resolve(),
            (instances_dir / "41001.json").resolve(),
        )

    def test_read_bridge_state_does_not_guess_nested_workspace_matches(self) -> None:
        workspace_root = self.root / "workspace-root"
        workspace_child = workspace_root / "child"
        workspace_child.mkdir(parents=True)
        instances_dir = self.backend.paths.bridge_state_instances_dir
        instances_dir.mkdir(parents=True, exist_ok=True)
        (instances_dir / "41001.json").write_text(
            json.dumps(
                {
                    "instance_id": "fixture-instance-child",
                    "pid": 41001,
                    "host": "127.0.0.1",
                    "port": 53081,
                    "token": "fixture-token-child",
                    "last_updated_at": "2026-04-09T10:00:00+08:00",
                    "workspace_folders": [{"fsPath": str(workspace_child.resolve())}],
                }
            )
        )

        selected = self.backend.read_bridge_state(workspace=workspace_root)
        candidates = self.backend.bridge_state_candidates(workspace=workspace_root)

        self.assertIsNone(selected)
        self.assertEqual(candidates, [])

    def test_bridge_request_prefers_workspace_matched_instance_state(self) -> None:
        observed: list[dict[str, str]] = []
        workspace_a = self.root / "workspace-a"
        workspace_b = self.root / "workspace-b"
        workspace_a.mkdir()
        workspace_b.mkdir()

        def start_server(label: str, command_id: str) -> tuple[ThreadingHTTPServer, threading.Thread]:
            class Handler(BaseHTTPRequestHandler):
                def log_message(self, format: str, *args: object) -> None:
                    return

                def do_GET(self) -> None:  # noqa: N802
                    observed.append(
                        {
                            "label": label,
                            "path": self.path,
                            "token": str(self.headers.get("x-traecli-token") or ""),
                        }
                    )
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(
                        json.dumps(
                            {
                                "ok": True,
                                "count": 1,
                                "commands": [command_id],
                            }
                        ).encode("utf-8")
                    )

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            return server, thread

        server_a, thread_a = start_server("workspace-a", "fixture.command.a")
        server_b, thread_b = start_server("workspace-b", "fixture.command.b")
        try:
            instances_dir = self.backend.paths.bridge_state_instances_dir
            instances_dir.mkdir(parents=True, exist_ok=True)
            (instances_dir / "41001.json").write_text(
                json.dumps(
                    {
                        "instance_id": "fixture-instance-a",
                        "pid": 41001,
                        "host": "127.0.0.1",
                        "port": server_a.server_address[1],
                        "token": "fixture-token-a",
                        "last_updated_at": "2026-04-09T10:00:00+08:00",
                        "workspace_folders": [{"fsPath": str(workspace_a.resolve())}],
                    }
                )
            )
            (instances_dir / "41002.json").write_text(
                json.dumps(
                    {
                        "instance_id": "fixture-instance-b",
                        "pid": 41002,
                        "host": "127.0.0.1",
                        "port": server_b.server_address[1],
                        "token": "fixture-token-b",
                        "last_updated_at": "2026-04-09T11:00:00+08:00",
                        "workspace_folders": [{"fsPath": str(workspace_b.resolve())}],
                    }
                )
            )

            payload = self.backend.list_bridge_commands(
                match="chat",
                workspace=workspace_a,
            )
        finally:
            server_a.shutdown()
            thread_a.join(timeout=5)
            server_a.server_close()
            server_b.shutdown()
            thread_b.join(timeout=5)
            server_b.server_close()

        self.assertEqual(payload["commands"], ["fixture.command.a"])
        self.assertEqual(observed, [{"label": "workspace-a", "path": "/commands?internal=true&match=chat", "token": "fixture-token-a"}])

    def test_bridge_request_reports_no_exact_workspace_binding(self) -> None:
        workspace_root = self.root / "workspace-root"
        workspace_child = workspace_root / "child"
        workspace_child.mkdir(parents=True)
        instances_dir = self.backend.paths.bridge_state_instances_dir
        instances_dir.mkdir(parents=True, exist_ok=True)
        (instances_dir / "41001.json").write_text(
            json.dumps(
                {
                    "instance_id": "fixture-instance-child",
                    "pid": 41001,
                    "host": "127.0.0.1",
                    "port": 53081,
                    "token": "fixture-token-child",
                    "last_updated_at": "2026-04-09T10:00:00+08:00",
                    "workspace_folders": [{"fsPath": str(workspace_child.resolve())}],
                }
            )
        )

        with self.assertRaises(RuntimeError) as ctx:
            self.backend.list_bridge_commands(match="chat", workspace=workspace_root)

        self.assertIn(
            f"No running Trae bridge instance is bound to workspace `{workspace_root.resolve()}`.",
            str(ctx.exception),
        )

    def test_send_rpc_chat_merges_workspace_context_into_chat_and_client_info(self) -> None:
        workspace = self.root / "workspace-a"
        workspace.mkdir()
        bridge_state = {
            "workspace_folders": [{"fsPath": str(workspace.resolve())}],
            "active_editor": {
                "uri": "file:///tmp/fixture.py",
                "fsPath": "/tmp/fixture.py",
                "languageId": "python",
            },
        }

        with mock.patch.object(
            self.backend,
            "create_rpc_project",
            return_value={"project_id": "fixture-project-id"},
        ):
            with mock.patch.object(
                self.backend,
                "create_rpc_session",
                return_value={"session_id": "fixture-session-id"},
            ):
                with mock.patch.object(
                    self.backend,
                    "read_bridge_state",
                    return_value=bridge_state,
                ):
                    with mock.patch.object(
                        self.backend,
                        "invoke_aha_rpc",
                        return_value={
                            "response": {
                                "message": "success",
                                "code": 0,
                                "data": {"event": "metadata"},
                            }
                        },
                    ) as mocked_rpc:
                        with mock.patch.object(
                            self.backend,
                            "wait_for_rpc_messages",
                            return_value={
                                "request": {"response": {"message": "success", "code": 0}},
                                "response": {"message": "success", "code": 0},
                                "response_data": {
                                    "messages": [
                                        {
                                            "role": "assistant",
                                            "content": "fixture rpc answer",
                                            "status": "completed",
                                        }
                                    ]
                                },
                                "messages": [
                                    {
                                        "role": "assistant",
                                        "content": "fixture rpc answer",
                                        "status": "completed",
                                    }
                                ],
                                "message_count": 1,
                                "assistant_status": "completed",
                                "latest_assistant_message": {
                                    "role": "assistant",
                                    "content": "fixture rpc answer",
                                    "status": "completed",
                                },
                                "answer_text": "fixture rpc answer",
                                "answer_source": "message.content",
                                "next_page_token": None,
                            },
                        ):
                            payload = self.backend.send_rpc_chat(
                                "收到回复我",
                                workspace=workspace,
                                session_type="side_chat",
                            )

        active_text_editor = payload["chat_data"]["active_text_editor"]
        self.assertEqual(
            active_text_editor["document"]["uri"],
            "file:///tmp/fixture.py",
        )
        self.assertEqual(active_text_editor["document"]["language_id"], "python")
        self.assertEqual(active_text_editor["document"]["file_name"], "fixture.py")
        self.assertEqual(active_text_editor["document"]["scheme"], "file")
        self.assertEqual(active_text_editor["language"]["comment_style"], "#")
        self.assertIn("selection", active_text_editor)
        self.assertIn("whole_range", active_text_editor)
        self.assertIn("visible_ranges", active_text_editor)
        self.assertGreaterEqual(active_text_editor["file_size"], 0)
        self.assertEqual(payload["agent_type"], "chat_v3")
        self.assertEqual(payload["chat_data"]["agent_type"], "chat_v3")
        self.assertEqual(
            mocked_rpc.call_args.kwargs["client_info"]["workspace_folder"],
            str(workspace.resolve()),
        )
        self.assertEqual(
            mocked_rpc.call_args.kwargs["client_info"]["workspace_folders"],
            [str(workspace.resolve())],
        )

    def test_send_rpc_chat_accepts_explicit_agent_type(self) -> None:
        workspace = self.root / "workspace-agent"
        workspace.mkdir()

        with mock.patch.object(
            self.backend,
            "create_rpc_project",
            return_value={"project_id": "fixture-project-id"},
        ):
            with mock.patch.object(
                self.backend,
                "create_rpc_session",
                return_value={"session_id": "fixture-session-id"},
            ):
                with mock.patch.object(
                    self.backend,
                    "invoke_aha_rpc",
                    return_value={
                        "response": {
                            "message": "success",
                            "code": 0,
                            "data": {"event": "metadata"},
                        }
                    },
                ):
                    with mock.patch.object(
                        self.backend,
                        "wait_for_rpc_messages",
                        return_value={
                            "request": {"response": {"message": "success", "code": 0}},
                            "response": {"message": "success", "code": 0},
                            "response_data": {"messages": []},
                            "messages": [],
                            "message_count": 0,
                            "assistant_status": "completed",
                            "latest_assistant_message": None,
                            "answer_text": None,
                            "answer_source": None,
                            "next_page_token": None,
                        },
                    ):
                        payload = self.backend.send_rpc_chat(
                            "收到回复我",
                            workspace=workspace,
                            session_type="side_chat",
                            agent_type="builder_v3",
                        )

        self.assertEqual(payload["agent_type"], "builder_v3")
        self.assertEqual(payload["chat_data"]["agent_type"], "builder_v3")

    def test_bridge_request_uses_state_file_token_and_returns_json(self) -> None:
        observed = {}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                observed["path"] = self.path
                observed["token"] = self.headers.get("x-traecli-token")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps(
                        {
                            "ok": True,
                            "count": 1,
                            "commands": ["workbench.action.chat.icube.open"],
                        }
                    ).encode("utf-8")
                )

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.backend.paths.bridge_state_path.parent.mkdir(parents=True, exist_ok=True)
            self.backend.paths.bridge_state_path.write_text(
                json.dumps(
                    {
                        "host": "127.0.0.1",
                        "port": server.server_address[1],
                        "token": "fixture-bridge-token",
                    }
                )
            )

            payload = self.backend.list_bridge_commands(match="chat")
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

        self.assertEqual(observed["token"], "fixture-bridge-token")
        self.assertEqual(observed["path"], "/commands?internal=true&match=chat")
        self.assertEqual(payload["commands"], ["workbench.action.chat.icube.open"])

    def test_bridge_vscode_surface_builds_path_query(self) -> None:
        with mock.patch.object(
            self.backend,
            "bridge_request",
            return_value={"ok": True, "path": "icubeBootConfig"},
        ) as mocked_request:
            payload = self.backend.bridge_vscode_surface(path="icubeBootConfig")

        self.assertEqual(payload["path"], "icubeBootConfig")
        mocked_request.assert_called_once_with(
            "/vscode-surface?path=icubeBootConfig",
            timeout_seconds=BRIDGE_EXTENSION_DEFAULT_TIMEOUT_SECONDS,
            workspace=None,
        )

    def test_invoke_bridge_vscode_api_posts_path_and_args(self) -> None:
        with mock.patch.object(
            self.backend,
            "bridge_request",
            return_value={"ok": True, "path": "icubeBootConfig.getBootConfig"},
        ) as mocked_request:
            payload = self.backend.invoke_bridge_vscode_api(
                "icubeBootConfig.getBootConfig",
                args=["fixture"],
            )

        self.assertEqual(payload["path"], "icubeBootConfig.getBootConfig")
        mocked_request.assert_called_once_with(
            "/vscode-api",
            payload={
                "path": "icubeBootConfig.getBootConfig",
                "args": ["fixture"],
            },
            timeout_seconds=BRIDGE_EXTENSION_DEFAULT_TIMEOUT_SECONDS,
            workspace=None,
        )

    def test_invoke_bridge_icube_rpc_posts_client_payload(self) -> None:
        with mock.patch.object(
            self.backend,
            "bridge_request",
            return_value={"ok": True, "method": "serverInfo"},
        ) as mocked_request:
            payload = self.backend.invoke_bridge_icube_rpc(
                client_url="wss://fixture.example/ws",
                method="serverInfo",
                args=[{"module_port": "tooling/0"}],
                client_options={"name": "fixture"},
                close_after=False,
            )

        self.assertEqual(payload["method"], "serverInfo")
        mocked_request.assert_called_once_with(
            "/icube-rpc",
            payload={
                "client_url": "wss://fixture.example/ws",
                "client_options": {"name": "fixture"},
                "method": "serverInfo",
                "args": [{"module_port": "tooling/0"}],
                "close_after": False,
            },
            timeout_seconds=BRIDGE_EXTENSION_DEFAULT_TIMEOUT_SECONDS,
            workspace=None,
        )

    def test_inspect_bridge_icube_rpc_client_posts_probe_payload(self) -> None:
        with mock.patch.object(
            self.backend,
            "bridge_request",
            return_value={"ok": True, "capture": {"connections": []}},
        ) as mocked_request:
            payload = self.backend.inspect_bridge_icube_rpc_client(
                client_url="wss://fixture.example/ws",
                client_options={"name": "fixture"},
                method="serverInfo",
                args=[{"showLog": True}],
                wait_ms=250,
            )

        self.assertEqual(payload["capture"]["connections"], [])
        mocked_request.assert_called_once_with(
            "/icube-rpc-inspect",
            payload={
                "client_url": "wss://fixture.example/ws",
                "client_options": {"name": "fixture"},
                "method": "serverInfo",
                "args": [{"showLog": True}],
                "wait_ms": 250,
            },
            timeout_seconds=BRIDGE_EXTENSION_DEFAULT_TIMEOUT_SECONDS,
            workspace=None,
        )


if __name__ == "__main__":
    unittest.main()
