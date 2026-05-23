from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Optional
from unittest import mock

from click.testing import CliRunner

from cli_anything.trae import trae_cli
from cli_anything.trae.tests.fixtures import create_fixture
from cli_anything.trae.core.state import SessionState
from cli_anything.trae.trae_cli import (
    AppContext,
    InteractiveViewState,
    build_interactive_screen,
    cli,
    handle_interactive_view_input,
    parse_terminal_keypress,
    process_alt_screen_key,
    process_repl_line,
    render_interactive_input,
    rewrite_argv_for_prompt,
    run_interactive_prompt,
    startup_landing_lines,
)
from cli_anything.trae.utils.trae_backend import CDPBridgeError, TraeBackend


class TraeCliEndToEndTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.fixture = create_fixture(self.root)
        self.runner = CliRunner()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def bound_bridge_state_payload(
        self,
        workspace: Optional[Path | str] = None,
    ) -> dict[str, object]:
        resolved = Path(workspace or Path.cwd()).expanduser().resolve()
        return {
            "workspace_folders": [{"fsPath": str(resolved)}],
            "active_editor": {"fsPath": str(resolved)},
        }

    def patch_bound_bridge_state(
        self,
        workspace: Optional[Path | str] = None,
    ):
        return mock.patch.object(
            TraeBackend,
            "read_bridge_state",
            return_value=self.bound_bridge_state_payload(workspace),
        )

    def invoke(self, *args: str, **kwargs):
        trust_workspace = kwargs.pop("trust_workspace", True)
        return self.runner.invoke(
            cli,
            [
                *(["--trust-workspace"] if trust_workspace else []),
                "--app-path",
                str(self.fixture["app_path"]),
                "--support-dir",
                str(self.fixture["support_dir"]),
                "--user-data-dir",
                str(self.fixture["user_data_dir"]),
                *args,
            ],
            **kwargs,
        )

    def invoke_in_workspace(self, workspace: Path, *args: str, **kwargs):
        trust_workspace = kwargs.pop("trust_workspace", True)
        return self.runner.invoke(
            cli,
            [
                "--workspace",
                str(workspace),
                *(["--trust-workspace"] if trust_workspace else []),
                "--app-path",
                str(self.fixture["app_path"]),
                "--support-dir",
                str(self.fixture["support_dir"]),
                "--user-data-dir",
                str(self.fixture["user_data_dir"]),
                *args,
            ],
            **kwargs,
        )

    def test_help_output_uses_chinese_command_descriptions(self) -> None:
        result = self.runner.invoke(cli, ["--help"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("启动交互式 Trae CLI 会话", result.output)
        self.assertIn("chat      发送聊天请求，可续接当前 workspace 的已保存上下文。", result.output)
        self.assertIn("doctor    检查 Trae 安装、配置、认证和本地运行状态。", result.output)
        self.assertIn("exec      执行一次性请求，不恢复也不保存会话。", result.output)
        self.assertIn("rpc       检查内部 RPC 服务、流量和桥接调用。", result.output)

    def test_rpc_help_output_uses_chinese_subcommand_descriptions(self) -> None:
        result = self.runner.invoke(cli, ["rpc", "--help"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("检查 Trae 内部 RPC 服务、流量和桥接调用。", result.output)
        self.assertIn("request      通过 AHA bridge 直接发起 RPC 请求。", result.output)
        self.assertIn("export-chat  通过内部 RPC 导出聊天记录。", result.output)

    def test_doctor_json(self) -> None:
        result = self.invoke("--json", "doctor")
        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["app"]["display_name"], "Trae")
        self.assertEqual(payload["auth"]["region"], "SG")
        self.assertTrue(payload["runtime"]["workspace"])
        self.assertFalse(payload["config"]["exists"])

    def test_doctor_json_for_cn_bundle(self) -> None:
        fixture = create_fixture(
            self.root / "cn-fixture",
            app_name="Trae CN",
            cli_name="trae-cn",
            bundle_identifier="cn.trae.app",
            url_protocol="trae-cn",
            support_dir_name="Trae CN",
        )

        result = self.runner.invoke(
            cli,
            [
                "--app-path",
                str(fixture["app_path"]),
                "--support-dir",
                str(fixture["support_dir"]),
                "--user-data-dir",
                str(fixture["user_data_dir"]),
                "--json",
                "doctor",
            ],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["app"]["display_name"], "Trae CN")
        self.assertEqual(payload["app"]["url_protocol"], "trae-cn")
        self.assertTrue(payload["app"]["cli_exists"])
        self.assertTrue(payload["app"]["cli_script"].endswith("/bin/trae-cn"))

    def test_doctor_uses_cli_config_defaults_without_explicit_paths(self) -> None:
        fixture = create_fixture(
            self.root / "configured-doctor",
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

        result = self.runner.invoke(
            cli,
            [
                "--json",
                "--user-data-dir",
                str(fixture["user_data_dir"]),
                "doctor",
            ],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["app"]["path"], str(fixture["app_path"].resolve()))
        self.assertTrue(payload["config"]["exists"])
        self.assertEqual(payload["config"]["app_path"], str(fixture["app_path"].resolve()))
        self.assertEqual(
            payload["config"]["support_dir"],
            str(fixture["support_dir"].resolve()),
        )
        self.assertEqual(Path(payload["config"]["path"]).resolve(), config_path.resolve())

    def test_init_json_in_place_writes_config(self) -> None:
        result = self.invoke("--json", "init", "--no-ping")

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["status"], "initialized")
        self.assertEqual(payload["mode"], "in-place")
        self.assertEqual(payload["selected_app_path"], str(self.fixture["app_path"].resolve()))
        self.assertTrue(payload["config"]["written"])
        config_path = Path(payload["config"]["path"])
        self.assertTrue(config_path.exists())
        config = json.loads(config_path.read_text())
        self.assertEqual(config["app_path"], str(self.fixture["app_path"].resolve()))
        self.assertEqual(config["support_dir"], str(self.fixture["support_dir"].resolve()))
        self.assertIn("headless status --ping", payload["next_steps"][0])
        self.assertIn("traecli '收到回复我'", payload["next_steps"][-1])

    def test_init_json_prepares_copy_when_bundle_is_not_writable(self) -> None:
        probe_dir = str(self.fixture["app_path"] / "Contents" / "Resources" / "app" / "node_modules" / "@byted-icube" / "ai-modules-chat" / "dist")
        writable_payloads = [
            {"writable": False, "error": "Operation not permitted", "probe_dir": probe_dir},
            {"writable": True, "error": None, "probe_dir": probe_dir},
            {"writable": True, "error": None, "probe_dir": probe_dir},
        ]

        with mock.patch(
            "cli_anything.trae.utils.trae_backend.TraeBackend.app_bundle_writable",
            side_effect=writable_payloads,
        ):
            result = self.invoke("--json", "init", "--no-ping")

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["mode"], "prepared-copy")
        self.assertTrue(payload["config"]["written"])
        self.assertTrue(payload["selected_app_path"].endswith("Trae-headless.app"))
        self.assertIn("open -na", payload["next_steps"][0])
        self.assertIn("traecli '收到回复我'", payload["next_steps"][-1])
        config = json.loads(Path(payload["config"]["path"]).read_text())
        self.assertEqual(config["app_path"], payload["selected_app_path"])

    def test_state_models_json(self) -> None:
        result = self.invoke("--json", "state", "models")
        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["selected_model"]["display_name"], "GLM-4.7")
        self.assertEqual(
            payload["current_models"]["dev_builder"]["model"]["display_name"],
            "GPT-5.3 Codex",
        )
        self.assertEqual(payload["global_model_map"]["dev_builder"], "1_-_gpt-5.3-codex")
        self.assertEqual(payload["available_model_counts"]["solo_builder"], 1)

    def test_models_current_json(self) -> None:
        result = self.invoke("--json", "models", "current")

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(
            payload["current_models"]["dev_builder"]["model"]["display_name"],
            "GPT-5.3 Codex",
        )
        self.assertEqual(
            payload["current_models"]["solo_coder"]["model"]["display_name"],
            "GLM-4.7 SOLO",
        )

    def test_models_list_json(self) -> None:
        result = self.invoke("--json", "models", "list", "--agent-type", "dev_builder")

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        agent = payload["agents"]["dev_builder"]
        self.assertEqual(agent["count"], 2)
        self.assertTrue(agent["models"][0]["current"])
        self.assertEqual(agent["models"][0]["display_name"], "GPT-5.3 Codex")

    def test_models_set_json(self) -> None:
        result = self.invoke(
            "--json",
            "models",
            "set",
            "GLM-4.7",
            "--agent-type",
            "dev_builder",
        )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["status"], "switched")
        self.assertEqual(
            payload["current_model"]["display_name"],
            "GLM-4.7",
        )
        self.assertEqual(
            payload["current_model_key"],
            "3_bigmodel-plan_bigmodel-plan//glm-4.7",
        )

    def test_mode_status_json(self) -> None:
        result = self.invoke("--json", "mode")

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["mode"], "ide")
        self.assertFalse(payload["enabled"])
        self.assertEqual(payload["current_solo_tab_id"], "")

    def test_mode_switch_solo_and_back_to_ide_json(self) -> None:
        to_solo = self.invoke("--json", "mode", "solo")
        to_ide = self.invoke("--json", "mode", "ide")

        self.assertEqual(to_solo.exit_code, 0, to_solo.output)
        solo_payload = json.loads(to_solo.output)
        self.assertEqual(solo_payload["target_mode"], "solo")
        self.assertEqual(solo_payload["mode"], "solo")
        self.assertEqual(solo_payload["command_dispatch"]["command_id"], "soloMode")
        self.assertIsNotNone(solo_payload["storage"])
        self.assertIsNotNone(solo_payload["reload"])
        self.assertFalse(solo_payload["reload_required"])

        self.assertEqual(to_ide.exit_code, 0, to_ide.output)
        ide_payload = json.loads(to_ide.output)
        self.assertEqual(ide_payload["target_mode"], "ide")
        self.assertEqual(ide_payload["previous_mode"], "solo")
        self.assertEqual(ide_payload["mode"], "ide")
        self.assertIsNone(ide_payload["command_dispatch"])
        self.assertIsNotNone(ide_payload["storage"])
        self.assertIsNotNone(ide_payload["reload"])
        self.assertFalse(ide_payload["reload_required"])

    def test_gui_settings_json(self) -> None:
        result = self.invoke("--json", "gui", "settings")

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["command_id"], "workbench.action.icube.openSettings")
        stdout_payload = json.loads(payload["stdout"])
        self.assertEqual(
            stdout_payload["argv"],
            [
                "--open-url",
                "--",
                "command:workbench.action.icube.openSettings",
            ],
        )

    def test_gui_commands_json(self) -> None:
        result = self.invoke("--json", "gui", "commands", "--match", "window")

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        command_ids = [item["command_id"] for item in payload["commands"]]
        self.assertIn("workbench.action.newWindow", command_ids)
        self.assertIn("workbench.action.switchWindow", command_ids)

    def test_gui_recent_json(self) -> None:
        result = self.invoke("--json", "gui", "recent", "--limit", "2")

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["entries"][0]["kind"], "folder")
        self.assertEqual(payload["entries"][1]["kind"], "workspace")

    def test_gui_recent_json_filters_by_kind(self) -> None:
        result = self.invoke("--json", "gui", "recent", "--kind", "file")

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["entries"][0]["kind"], "file")
        self.assertEqual(payload["entries"][0]["label"], "recent-note.txt")

    def test_gui_open_recent_local_workspace_json(self) -> None:
        result = self.invoke("--json", "gui", "open-recent", "2")

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["command_id"], "gui.openRecent")
        self.assertEqual(payload["dispatch"], "cli-path")
        self.assertEqual(payload["recent_index"], 2)
        self.assertEqual(payload["kind"], "workspace")
        stdout_payload = json.loads(payload["stdout"])
        self.assertEqual(stdout_payload["argv"], [str(Path(payload["path"]).resolve())])

    def test_gui_open_recent_remote_folder_json(self) -> None:
        result = self.invoke("--json", "gui", "open-recent", "4")

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["command_id"], "gui.openRecent")
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

    def test_gui_check_update_json(self) -> None:
        result = self.invoke("--json", "gui", "check-update")

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["command_id"], "update.checkForUpdate")
        stdout_payload = json.loads(payload["stdout"])
        self.assertEqual(
            stdout_payload["argv"],
            [
                "--open-url",
                "--",
                "command:update.checkForUpdate",
            ],
        )

    def test_gui_new_window_json(self) -> None:
        result = self.invoke("--json", "gui", "new-window")

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["command_id"], "workbench.action.newWindow")
        stdout_payload = json.loads(payload["stdout"])
        self.assertEqual(
            stdout_payload["argv"],
            [
                "--open-url",
                "--",
                "command:workbench.action.newWindow",
            ],
        )

    def test_gui_open_folder_with_path_json(self) -> None:
        workspace = self.root / "gui-open-folder-workspace"
        workspace.mkdir()

        result = self.invoke(
            "--json",
            "gui",
            "open-folder",
            str(workspace),
        )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["dispatch"], "cli-path")
        self.assertEqual(payload["command_id"], "workbench.action.files.openFolder")
        self.assertEqual(payload["path"], str(workspace.resolve()))
        stdout_payload = json.loads(payload["stdout"])
        self.assertEqual(stdout_payload["argv"], [str(workspace.resolve())])
        self.assertEqual(Path(stdout_payload["cwd"]).resolve(), workspace.resolve())

    def test_gui_open_file_folder_with_file_path_json(self) -> None:
        workspace = self.root / "gui-open-file-folder-workspace"
        workspace.mkdir()
        file_path = workspace / "fixture.txt"
        file_path.write_text("fixture")

        result = self.invoke(
            "--json",
            "gui",
            "open-file-folder",
            str(file_path),
        )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["dispatch"], "cli-path")
        self.assertEqual(payload["command_id"], "workbench.action.files.openFileFolder")
        self.assertEqual(payload["path"], str(file_path.resolve()))
        stdout_payload = json.loads(payload["stdout"])
        self.assertEqual(stdout_payload["argv"], [str(file_path.resolve())])
        self.assertEqual(Path(stdout_payload["cwd"]).resolve(), workspace.resolve())

    def test_gui_open_workspace_with_workspace_file_json(self) -> None:
        workspace = self.root / "gui-open-workspace-file"
        workspace.mkdir()
        workspace_file = workspace / "fixture.code-workspace"
        workspace_file.write_text(json.dumps({"folders": [{"path": "."}]}))

        result = self.invoke(
            "--json",
            "gui",
            "open-workspace",
            str(workspace_file),
        )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["dispatch"], "cli-path")
        self.assertEqual(payload["command_id"], "workbench.action.openWorkspace")
        self.assertEqual(payload["path"], str(workspace_file.resolve()))
        stdout_payload = json.loads(payload["stdout"])
        self.assertEqual(stdout_payload["argv"], [str(workspace_file.resolve())])
        self.assertEqual(Path(stdout_payload["cwd"]).resolve(), workspace.resolve())

    def test_gui_command_json_with_args(self) -> None:
        result = self.invoke(
            "--json",
            "gui",
            "command",
            "workbench.action.files.openFolder",
            "--args",
            '[{"forceNewWindow":true}]',
        )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["command_id"], "workbench.action.files.openFolder")
        self.assertEqual(payload["command_uri"], "command:workbench.action.files.openFolder?%5B%7B%22forceNewWindow%22%3Atrue%7D%5D")
        stdout_payload = json.loads(payload["stdout"])
        self.assertEqual(
            stdout_payload["argv"],
            [
                "--open-url",
                "--",
                "command:workbench.action.files.openFolder?%5B%7B%22forceNewWindow%22%3Atrue%7D%5D",
            ],
        )

    def test_bridge_install_json(self) -> None:
        result = self.invoke("--json", "bridge", "install")

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["extension_id"], "traecli.headless-bridge")
        self.assertTrue(payload["installed"])
        self.assertTrue(Path(payload["install_dir"]).exists())

    def test_bridge_commands_json(self) -> None:
        mocked_payload = {
            "ok": True,
            "count": 2,
            "commands": [
                "workbench.action.chat.icube.open",
                "workbench.action.reloadWindow",
            ],
        }

        with mock.patch(
            "cli_anything.trae.utils.trae_backend.TraeBackend.list_bridge_commands",
            return_value=mocked_payload,
        ):
            result = self.invoke("--json", "bridge", "commands", "--match", "chat")

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["count"], 2)
        self.assertIn("workbench.action.chat.icube.open", payload["commands"])

    def test_bridge_invoke_json(self) -> None:
        mocked_payload = {
            "ok": True,
            "command": "workbench.action.reloadWindow",
            "arg_count": 0,
            "result": None,
        }

        with mock.patch(
            "cli_anything.trae.utils.trae_backend.TraeBackend.execute_bridge_command",
            return_value=mocked_payload,
        ):
            result = self.invoke(
                "--json",
                "bridge",
                "invoke",
                "workbench.action.reloadWindow",
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["command"], "workbench.action.reloadWindow")
        self.assertEqual(payload["arg_count"], 0)

    def test_headless_install_json(self) -> None:
        result = self.invoke("--json", "headless", "install")

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["command_id"], "traecli.headless.send")
        self.assertEqual(payload["app_path"], str(self.fixture["app_path"].resolve()))
        self.assertTrue(payload["patch_installed"])
        self.assertTrue(Path(payload["backup_path"]).exists())

    def test_headless_prepare_json(self) -> None:
        target_app_path = self.root / "prepared" / "Trae Headless.app"

        result = self.invoke(
            "--json",
            "headless",
            "prepare",
            "--target-app-path",
            str(target_app_path),
        )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["status"], "prepared")
        self.assertEqual(payload["app_path"], str(target_app_path.resolve()))
        self.assertEqual(payload["source_app_path"], str(self.fixture["app_path"].resolve()))
        self.assertTrue(payload["patch"]["patch_installed"])
        self.assertTrue(payload["writable"]["writable"])
        self.assertEqual(len(payload["next_steps"]), 3)

    def test_headless_status_json(self) -> None:
        result = self.invoke("--json", "headless", "status")

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["command_id"], "traecli.headless.send")
        self.assertEqual(payload["app_path"], str(self.fixture["app_path"].resolve()))
        self.assertFalse(payload["patch_installed"])
        self.assertTrue(payload["writable"]["writable"])

    def test_chat_auto_dispatches_via_cdp_json(self) -> None:
        mocked_cdp = {
            "endpoint": {"host": "127.0.0.1", "port": 9222, "source": "default"},
            "target": {"title": "Trae", "url": "file:///workbench.html"},
            "response": {"text": "fixture cdp answer"},
        }

        with self.patch_bound_bridge_state():
            with mock.patch(
                "cli_anything.trae.utils.trae_backend.TraeBackend.invoke_cdp_chat",
                return_value=mocked_cdp,
            ) as invoke_cdp_chat:
                result = self.invoke("--json", "chat", "hello fixture")

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["requested_dispatch_method"], "auto")
        self.assertEqual(payload["dispatch_method"], "cdp")
        self.assertEqual(payload["answer_text"], "fixture cdp answer")
        self.assertEqual(payload["stdout"], "fixture cdp answer")
        invoke_cdp_chat.assert_called_once()
        self.assertTrue(invoke_cdp_chat.call_args.kwargs["launch_if_needed"])

    def test_chat_auto_persists_cdp_session_without_hidden_session(self) -> None:
        workspace = self.root / "workspace-cdp-history"
        workspace.mkdir()

        def fake_invoke_cdp_chat(
            _backend: TraeBackend,
            *,
            prompt: str,
            host: object = None,
            port: object = None,
            target_id: object = None,
            prefer_focused: bool = False,
            title_contains: object = None,
            url_contains: object = None,
            ready_timeout_ms: int = 15000,
            timeout_ms: int = 30000,
            response_poll_interval_ms: int = 350,
            response_idle_ms: int = 1200,
            command_timeout_ms: int = 5000,
            launch_if_needed: bool = False,
            launch_timeout_ms: int = 12000,
        ) -> dict[str, object]:
            return {
                "endpoint": {"host": "127.0.0.1", "port": 9222, "source": "default"},
                "target": {
                    "id": "fixture-target-id",
                    "title": "Trae",
                    "url": "file:///workbench.html",
                },
                "response": {"text": f"answer for {prompt}"},
            }

        with mock.patch.object(
            TraeBackend,
            "invoke_cdp_chat",
            autospec=True,
            side_effect=fake_invoke_cdp_chat,
        ):
            with self.patch_bound_bridge_state(workspace):
                first = self.invoke_in_workspace(workspace, "--json", "chat", "hello one")
                second = self.invoke_in_workspace(workspace, "--json", "chat", "hello two")

        self.assertEqual(first.exit_code, 0, first.output)
        self.assertEqual(second.exit_code, 0, second.output)

        backend = TraeBackend(
            app_path=self.fixture["app_path"],
            support_dir=self.fixture["support_dir"],
            user_data_dir=self.fixture["user_data_dir"],
        )
        latest = backend.latest_cli_session(workspace=workspace, resumable_only=False)
        self.assertIsNotNone(latest)
        self.assertIsNone(latest["last_headless_session_id"])
        self.assertEqual(latest["last_dispatch_method"], "cdp")
        self.assertEqual(latest["message_count"], 2)
        self.assertEqual(len(backend.read_cli_sessions()), 1)

    def test_exec_dispatches_non_interactively_without_restoring_or_persisting_session(self) -> None:
        workspace = self.root / "workspace-exec-stateless"
        workspace.mkdir()
        sessions_path = self.fixture["user_data_dir"] / "traecli-sessions.json"
        sessions_path.write_text(
            json.dumps(
                [
                    {
                        "id": "fixture-cli-session",
                        "title": "Saved chat",
                        "created_at": "2026-04-08T10:00:00+08:00",
                        "updated_at": "2026-04-08T10:01:00+08:00",
                        "workspace": str(workspace.resolve()),
                        "app_path": str(self.fixture["app_path"].resolve()),
                        "support_dir": str(self.fixture["support_dir"].resolve()),
                        "user_data_dir": str(self.fixture["user_data_dir"].resolve()),
                        "last_headless_session_id": "fixture-hidden-session",
                        "resumable": True,
                        "message_count": 1,
                        "last_dispatch_method": "headless",
                        "messages": [
                            {"role": "assistant", "content": "saved answer"},
                        ],
                    }
                ]
            )
        )
        observed_launch_flags: list[bool] = []

        def fake_invoke_cdp_chat(
            _backend: TraeBackend,
            *,
            prompt: str,
            host: object = None,
            port: object = None,
            target_id: object = None,
            prefer_focused: bool = False,
            title_contains: object = None,
            url_contains: object = None,
            ready_timeout_ms: int = 15000,
            timeout_ms: int = 30000,
            response_poll_interval_ms: int = 350,
            response_idle_ms: int = 1200,
            command_timeout_ms: int = 5000,
            launch_if_needed: bool = False,
            launch_timeout_ms: int = 12000,
        ) -> dict[str, object]:
            observed_launch_flags.append(bool(launch_if_needed))
            return {
                "endpoint": {"host": "127.0.0.1", "port": 9222, "source": "default"},
                "target": {
                    "id": "fixture-target-id",
                    "title": "Trae",
                    "url": "file:///workbench.html",
                },
                "response": {"text": f"answer for {prompt}"},
            }

        with mock.patch.object(
            TraeBackend,
            "invoke_cdp_chat",
            autospec=True,
            side_effect=fake_invoke_cdp_chat,
        ):
            with self.patch_bound_bridge_state(workspace):
                result = self.invoke_in_workspace(workspace, "--json", "exec", "hello fixture")

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["dispatch_method"], "cdp")
        self.assertEqual(payload["answer_text"], "answer for hello fixture")
        self.assertTrue(payload["new_chat"])
        self.assertEqual(observed_launch_flags, [True])

        saved_sessions = json.loads(sessions_path.read_text())
        self.assertEqual(len(saved_sessions), 1)
        self.assertEqual(saved_sessions[0]["id"], "fixture-cli-session")
        self.assertEqual(saved_sessions[0]["message_count"], 1)
        self.assertEqual(saved_sessions[0]["last_headless_session_id"], "fixture-hidden-session")

    def test_repl_initial_prompt_prints_answer_and_persists_session(self) -> None:
        mocked_cdp = {
            "endpoint": {"host": "127.0.0.1", "port": 9222, "source": "default"},
            "target": {"id": "fixture-target-id", "title": "Trae", "url": "file:///workbench.html"},
            "response": {"text": "fixture cdp answer"},
        }

        with self.patch_bound_bridge_state():
            with mock.patch(
                "cli_anything.trae.utils.trae_backend.TraeBackend.invoke_cdp_chat",
                return_value=mocked_cdp,
            ):
                result = self.invoke("repl", "hello fixture", input="/exit\n")

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("  █████  ████    ███   █████   ████  █      █████", result.output)
        self.assertNotIn("直接输入消息开始对话", result.output)
        self.assertIn("fixture cdp answer", result.output)
        sessions = json.loads(self.fixture["user_data_dir"].joinpath("traecli-sessions.json").read_text())
        self.assertIsNone(sessions[0]["last_headless_session_id"])
        self.assertEqual(sessions[0]["message_count"], 1)
        self.assertEqual(sessions[0]["initial_prompt"], "hello fixture")

    def test_resume_last_reuses_hidden_session_id(self) -> None:
        first_payload = {
            "command_id": "traecli.headless.send",
            "result": {
                "sessionId": "fixture-headless-session",
                "requestMessageId": "fixture-request-id-1",
            },
            "session": {
                "sessionId": "fixture-headless-session",
                "messages": [
                    {"role": "assistant", "content": "first answer"},
                ],
            },
            "session_id": "fixture-headless-session",
            "request_message_id": "fixture-request-id-1",
            "answer_text": "first answer",
        }
        observed_session_ids: list[str | None] = []

        def second_invoke(
            prompt: str,
            *,
            session_id: str | None = None,
            timeout_seconds: float = 30.0,
            workspace: object = None,
        ):
            observed_session_ids.append(session_id)
            return {
                "command_id": "traecli.headless.send",
                "result": {
                    "sessionId": session_id or "fixture-headless-session",
                    "requestMessageId": "fixture-request-id-2",
                },
                "session": {
                    "sessionId": session_id or "fixture-headless-session",
                    "messages": [
                        {"role": "assistant", "content": "second answer"},
                    ],
                },
                "session_id": session_id or "fixture-headless-session",
                "request_message_id": "fixture-request-id-2",
                "answer_text": "second answer",
            }

        with mock.patch(
            "cli_anything.trae.utils.trae_backend.TraeBackend.headless_dispatch_available",
            return_value=True,
        ):
            with mock.patch(
                "cli_anything.trae.utils.trae_backend.TraeBackend.invoke_headless_chat",
                return_value=first_payload,
            ):
                first_result = self.invoke(
                    "--json",
                    "chat",
                    "--dispatch",
                    "headless",
                    "hello fixture",
                )

        self.assertEqual(first_result.exit_code, 0, first_result.output)

        with mock.patch(
            "cli_anything.trae.utils.trae_backend.TraeBackend.headless_dispatch_available",
            return_value=True,
        ):
            with mock.patch(
                "cli_anything.trae.utils.trae_backend.TraeBackend.invoke_headless_chat",
                side_effect=second_invoke,
            ):
                second_result = self.invoke("resume", "--last", "hello again", input="/exit\n")

        self.assertEqual(second_result.exit_code, 0, second_result.output)
        self.assertEqual(observed_session_ids, ["fixture-headless-session"])
        self.assertIn("hidden: fixture-headless-session", second_result.output)

    def test_sessions_command_lists_saved_cli_sessions_json(self) -> None:
        sessions_path = self.fixture["user_data_dir"] / "traecli-sessions.json"
        sessions_path.write_text(
            json.dumps(
                [
                    {
                        "id": "fixture-cli-session",
                        "title": "Initial prompt",
                        "created_at": "2026-04-08T10:00:00+08:00",
                        "updated_at": "2026-04-08T10:01:00+08:00",
                        "workspace": str(self.root.resolve()),
                        "app_path": str(self.fixture["app_path"].resolve()),
                        "support_dir": str(self.fixture["support_dir"].resolve()),
                        "user_data_dir": str(self.fixture["user_data_dir"].resolve()),
                        "last_headless_session_id": "fixture-hidden-session",
                        "resumable": True,
                        "messages": [
                            {"role": "user", "content": "hello fixture"},
                            {"role": "assistant", "content": "fixture headless answer"},
                        ],
                    }
                ]
            )
        )

        result = self.invoke_in_workspace(self.root, "--json", "sessions")

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["total_count"], 1)
        self.assertEqual(payload["sessions"][0]["id"], "fixture-cli-session")
        self.assertEqual(payload["sessions"][0]["preview"], "fixture headless answer")

    def test_sessions_show_returns_saved_cli_session_detail_json(self) -> None:
        sessions_path = self.fixture["user_data_dir"] / "traecli-sessions.json"
        sessions_path.write_text(
            json.dumps(
                [
                    {
                        "id": "fixture-cli-session",
                        "title": "Initial prompt",
                        "created_at": "2026-04-08T10:00:00+08:00",
                        "updated_at": "2026-04-08T10:01:00+08:00",
                        "workspace": str(self.root.resolve()),
                        "app_path": str(self.fixture["app_path"].resolve()),
                        "support_dir": str(self.fixture["support_dir"].resolve()),
                        "user_data_dir": str(self.fixture["user_data_dir"].resolve()),
                        "last_headless_session_id": "fixture-hidden-session",
                        "resumable": True,
                        "last_dispatch_method": "headless",
                        "messages": [
                            {"role": "user", "content": "hello fixture"},
                            {"role": "assistant", "content": "fixture headless answer"},
                        ],
                    }
                ]
            )
        )

        result = self.invoke("--json", "sessions", "--show", "fixture-cli-session")

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertTrue(payload["found"])
        self.assertEqual(payload["session"]["id"], "fixture-cli-session")
        self.assertEqual(payload["session"]["last_dispatch_method"], "headless")

    def test_resume_picker_selects_session_from_prompt(self) -> None:
        sessions_path = self.fixture["user_data_dir"] / "traecli-sessions.json"
        sessions_path.write_text(
            json.dumps(
                [
                    {
                        "id": "fixture-cli-session",
                        "title": "Initial prompt",
                        "created_at": "2026-04-08T10:00:00+08:00",
                        "updated_at": "2026-04-08T10:01:00+08:00",
                        "workspace": str(self.root.resolve()),
                        "app_path": str(self.fixture["app_path"].resolve()),
                        "support_dir": str(self.fixture["support_dir"].resolve()),
                        "user_data_dir": str(self.fixture["user_data_dir"].resolve()),
                        "last_headless_session_id": "fixture-hidden-session",
                        "resumable": True,
                        "messages": [
                            {"role": "user", "content": "hello fixture"},
                            {"role": "assistant", "content": "fixture headless answer"},
                        ],
                    }
                ]
            )
        )
        observed_session_ids: list[str | None] = []

        def resumed_invoke(
            prompt: str,
            *,
            session_id: str | None = None,
            timeout_seconds: float = 30.0,
            workspace: object = None,
        ):
            observed_session_ids.append(session_id)
            return {
                "command_id": "traecli.headless.send",
                "result": {
                    "sessionId": session_id or "fixture-hidden-session",
                    "requestMessageId": "fixture-request-id-2",
                },
                "session": {
                    "sessionId": session_id or "fixture-hidden-session",
                    "messages": [
                        {"role": "assistant", "content": "second answer"},
                    ],
                },
                "session_id": session_id or "fixture-hidden-session",
                "request_message_id": "fixture-request-id-2",
                "answer_text": "second answer",
            }

        with mock.patch(
            "cli_anything.trae.utils.trae_backend.TraeBackend.headless_dispatch_available",
            return_value=True,
        ):
            with mock.patch(
                "cli_anything.trae.utils.trae_backend.TraeBackend.invoke_headless_chat",
                side_effect=resumed_invoke,
            ):
                result = self.invoke_in_workspace(
                    self.root,
                    "resume",
                    input="1\nhello again\n/exit\n",
                )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Select session (j/k/s/q or number):", result.output)
        self.assertEqual(observed_session_ids, ["fixture-hidden-session"])

    def test_resume_picker_supports_j_and_select_navigation(self) -> None:
        sessions_path = self.fixture["user_data_dir"] / "traecli-sessions.json"
        sessions_path.write_text(
            json.dumps(
                [
                    {
                        "id": "fixture-cli-session-1",
                        "title": "First",
                        "created_at": "2026-04-08T10:00:00+08:00",
                        "updated_at": "2026-04-08T10:01:00+08:00",
                        "workspace": str(self.root.resolve()),
                        "app_path": str(self.fixture["app_path"].resolve()),
                        "support_dir": str(self.fixture["support_dir"].resolve()),
                        "user_data_dir": str(self.fixture["user_data_dir"].resolve()),
                        "last_headless_session_id": "fixture-hidden-session-1",
                        "resumable": True,
                        "messages": [{"role": "assistant", "content": "first answer"}],
                    },
                    {
                        "id": "fixture-cli-session-2",
                        "title": "Second",
                        "created_at": "2026-04-08T11:00:00+08:00",
                        "updated_at": "2026-04-08T11:01:00+08:00",
                        "workspace": str(self.root.resolve()),
                        "app_path": str(self.fixture["app_path"].resolve()),
                        "support_dir": str(self.fixture["support_dir"].resolve()),
                        "user_data_dir": str(self.fixture["user_data_dir"].resolve()),
                        "last_headless_session_id": "fixture-hidden-session-2",
                        "resumable": True,
                        "messages": [{"role": "assistant", "content": "second answer"}],
                    },
                ]
            )
        )
        observed_session_ids: list[str | None] = []

        def resumed_invoke(
            prompt: str,
            *,
            session_id: str | None = None,
            timeout_seconds: float = 30.0,
            workspace: object = None,
        ):
            observed_session_ids.append(session_id)
            return {
                "command_id": "traecli.headless.send",
                "result": {
                    "sessionId": session_id or "fixture-hidden-session-2",
                    "requestMessageId": "fixture-request-id-2",
                },
                "session": {
                    "sessionId": session_id or "fixture-hidden-session-2",
                    "messages": [
                        {"role": "assistant", "content": "second answer"},
                    ],
                },
                "session_id": session_id or "fixture-hidden-session-2",
                "request_message_id": "fixture-request-id-2",
                "answer_text": "second answer",
            }

        with mock.patch(
            "cli_anything.trae.utils.trae_backend.TraeBackend.headless_dispatch_available",
            return_value=True,
        ):
            with mock.patch(
                "cli_anything.trae.utils.trae_backend.TraeBackend.invoke_headless_chat",
                side_effect=resumed_invoke,
            ):
                result = self.invoke_in_workspace(
                    self.root,
                    "resume",
                    input="j\ns\nhello again\n/exit\n",
                )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("> 2. First", result.output)
        self.assertEqual(observed_session_ids, ["fixture-hidden-session-1"])

    def test_chat_uses_cli_config_defaults_without_explicit_app_path(self) -> None:
        fixture = create_fixture(
            self.root / "configured-chat",
            app_name="Trae CN",
            cli_name="trae-cn",
            bundle_identifier="cn.trae.app",
            url_protocol="trae-cn",
            support_dir_name="Trae CN",
        )
        (fixture["user_data_dir"] / "traecli.json").write_text(
            json.dumps(
                {
                    "app_path": str(fixture["app_path"]),
                    "support_dir": str(fixture["support_dir"]),
                }
            )
        )
        mocked_cdp = {
            "endpoint": {"host": "127.0.0.1", "port": 9222, "source": "default"},
            "target": {"title": "Trae", "url": "file:///workbench.html"},
            "response": {"text": "fixture cdp answer"},
        }

        with self.patch_bound_bridge_state():
            with mock.patch(
                "cli_anything.trae.utils.trae_backend.TraeBackend.invoke_cdp_chat",
                return_value=mocked_cdp,
            ) as invoke_cdp_chat:
                result = self.runner.invoke(
                    cli,
                    [
                        "--trust-workspace",
                        "--json",
                        "--user-data-dir",
                        str(fixture["user_data_dir"]),
                        "chat",
                        "hello fixture",
                    ],
                )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["dispatch_method"], "cdp")
        self.assertEqual(payload["answer_text"], "fixture cdp answer")
        self.assertTrue(invoke_cdp_chat.call_args.kwargs["launch_if_needed"])

    def test_chat_auto_uses_cdp_for_headless_app_copy(self) -> None:
        fixture = create_fixture(
            self.root / "headless-copy",
            app_name="Trae CN-headless",
            cli_name="trae-cn",
            bundle_identifier="cn.trae.app",
            url_protocol="trae-cn",
            support_dir_name="Trae CN",
        )
        mocked_cdp = {
            "endpoint": {"host": "127.0.0.1", "port": 9222, "source": "default"},
            "target": {"title": "Trae", "url": "file:///workbench.html"},
            "response": {"text": "fixture cdp answer"},
        }

        with self.patch_bound_bridge_state():
            with mock.patch(
                "cli_anything.trae.utils.trae_backend.TraeBackend.invoke_cdp_chat",
                return_value=mocked_cdp,
            ) as invoke_cdp_chat:
                result = self.runner.invoke(
                    cli,
                    [
                        "--trust-workspace",
                        "--json",
                        "--app-path",
                        str(fixture["app_path"]),
                        "--support-dir",
                        str(fixture["support_dir"]),
                        "--user-data-dir",
                        str(fixture["user_data_dir"]),
                        "chat",
                        "hello fixture",
                    ],
                )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["requested_dispatch_method"], "auto")
        self.assertEqual(payload["dispatch_method"], "cdp")
        self.assertEqual(payload["answer_text"], "fixture cdp answer")
        self.assertEqual(payload["stdout"], "fixture cdp answer")
        invoke_cdp_chat.assert_called_once()
        self.assertTrue(invoke_cdp_chat.call_args.kwargs["launch_if_needed"])

    def test_chat_dispatch_headless_text_prints_answer(self) -> None:
        mocked_headless = {
            "command_id": "traecli.headless.send",
            "result": {
                "sessionId": "fixture-headless-session",
                "requestMessageId": "fixture-request-id",
            },
            "session": {
                "sessionId": "fixture-headless-session",
                "messages": [
                    {"role": "assistant", "content": "fixture headless answer"},
                ],
            },
            "session_id": "fixture-headless-session",
            "request_message_id": "fixture-request-id",
            "answer_text": "fixture headless answer",
        }

        with mock.patch(
            "cli_anything.trae.utils.trae_backend.TraeBackend.headless_dispatch_available",
            return_value=True,
        ):
            with mock.patch(
                "cli_anything.trae.utils.trae_backend.TraeBackend.invoke_headless_chat",
                return_value=mocked_headless,
            ):
                result = self.invoke("chat", "--dispatch", "headless", "hello fixture")

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.output.strip(), "fixture headless answer")

    def test_chat_auto_uses_cdp_for_simple_prompt(self) -> None:
        mocked_cdp = {
            "endpoint": {"host": "127.0.0.1", "port": 9222, "source": "default"},
            "target": {"title": "Trae", "url": "file:///workbench.html"},
            "response": {"text": "fixture cdp answer"},
        }

        with self.patch_bound_bridge_state():
            with mock.patch(
                "cli_anything.trae.utils.trae_backend.TraeBackend.invoke_cdp_chat",
                return_value=mocked_cdp,
            ) as invoke_cdp_chat:
                result = self.invoke("--json", "chat", "hello fixture")

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["dispatch_method"], "cdp")
        self.assertEqual(payload["answer_text"], "fixture cdp answer")
        self.assertTrue(invoke_cdp_chat.call_args.kwargs["launch_if_needed"])

    def test_chat_auto_falls_back_to_cli_for_add_file_and_uses_workspace_as_cwd(self) -> None:
        workspace = self.root / "workspace-chat"
        workspace.mkdir()

        result = self.invoke_in_workspace(
            workspace,
            "--json",
            "chat",
            "--add-file",
            "README.md",
            "hello fixture",
        )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        stdout_payload = json.loads(payload["stdout"])
        self.assertEqual(payload["requested_dispatch_method"], "auto")
        self.assertEqual(payload["dispatch_method"], "cli")
        self.assertEqual(
            payload["command"],
            ["chat", "--mode", "agent", "--add-file", "README.md", "hello fixture"],
        )
        self.assertEqual(Path(stdout_payload["cwd"]).resolve(), workspace.resolve())

    def test_open_uses_explicit_workspace_as_cwd(self) -> None:
        workspace = self.root / "workspace-open"
        workspace.mkdir()

        result = self.invoke_in_workspace(workspace, "--json", "open", ".")

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        stdout_payload = json.loads(payload["stdout"])
        self.assertEqual(stdout_payload["argv"], ["."])
        self.assertEqual(Path(stdout_payload["cwd"]).resolve(), workspace.resolve())

    def test_open_prompts_to_trust_workspace_once(self) -> None:
        workspace = self.root / "workspace-trust-prompt"
        workspace.mkdir()
        backend = TraeBackend(
            app_path=self.fixture["app_path"],
            support_dir=self.fixture["support_dir"],
            user_data_dir=self.fixture["user_data_dir"],
        )

        first = self.invoke_in_workspace(
            workspace,
            "open",
            ".",
            input="y\n",
            trust_workspace=False,
        )
        second = self.invoke_in_workspace(
            workspace,
            "open",
            ".",
            trust_workspace=False,
        )

        self.assertEqual(first.exit_code, 0, first.output)
        self.assertIn("Trust this workspace?", first.output)
        self.assertTrue(backend.workspace_is_trusted(workspace=workspace))
        self.assertIn(
            str(workspace.resolve()),
            json.dumps(backend.read_content_trust_model(), ensure_ascii=False),
        )
        self.assertEqual(second.exit_code, 0, second.output)
        self.assertNotIn("Trust this workspace?", second.output)

    def test_open_json_requires_explicit_trust_for_untrusted_workspace(self) -> None:
        workspace = self.root / "workspace-untrusted-json"
        workspace.mkdir()

        result = self.invoke_in_workspace(
            workspace,
            "--json",
            "open",
            ".",
            trust_workspace=False,
        )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("not trusted", result.output)
        self.assertIn("--trust-workspace", result.output)

    def test_open_json_with_trust_workspace_flag_marks_workspace_trusted(self) -> None:
        workspace = self.root / "workspace-trust-flag"
        workspace.mkdir()
        backend = TraeBackend(
            app_path=self.fixture["app_path"],
            support_dir=self.fixture["support_dir"],
            user_data_dir=self.fixture["user_data_dir"],
        )

        result = self.invoke_in_workspace(
            workspace,
            "--trust-workspace",
            "--json",
            "open",
            ".",
            trust_workspace=False,
        )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        stdout_payload = json.loads(payload["stdout"])
        self.assertEqual(Path(stdout_payload["cwd"]).resolve(), workspace.resolve())
        self.assertTrue(backend.workspace_is_trusted(workspace=workspace))
        self.assertIn(
            str(workspace.resolve()),
            json.dumps(backend.read_content_trust_model(), ensure_ascii=False),
        )

    def test_open_json_with_trust_workspace_flag_surfaces_backend_write_failure(self) -> None:
        workspace = self.root / "workspace-trust-locked"
        workspace.mkdir()

        with mock.patch(
            "cli_anything.trae.trae_cli.TraeBackend.trust_workspace",
            side_effect=RuntimeError(
                "Timed out while waiting for Trae to release state.vscdb."
            ),
        ):
            result = self.invoke_in_workspace(
                workspace,
                "--trust-workspace",
                "--json",
                "open",
                ".",
                trust_workspace=False,
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Timed out while waiting for Trae to release", result.output)
        self.assertNotIn("Traceback", result.output)

    def test_open_skips_prompt_when_parent_workspace_is_already_trusted(self) -> None:
        parent = self.root / "workspace-trust-parent"
        workspace = parent / "child"
        workspace.mkdir(parents=True)
        backend = TraeBackend(
            app_path=self.fixture["app_path"],
            support_dir=self.fixture["support_dir"],
            user_data_dir=self.fixture["user_data_dir"],
        )
        backend.trust_workspace(workspace=parent, source="prompt")

        result = self.invoke_in_workspace(
            workspace,
            "open",
            ".",
            trust_workspace=False,
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("Trust this workspace?", result.output)
        trust_record = backend.read_trusted_workspace(workspace=workspace)
        self.assertIsNotNone(trust_record)
        self.assertEqual(trust_record["matched_workspace"], str(parent.resolve()))
        self.assertTrue(trust_record["inherited"])

    def test_chat_dispatches_via_uri_json(self) -> None:
        fake_result = subprocess.CompletedProcess(["open"], 0, "", "")

        with mock.patch(
            "cli_anything.trae.utils.trae_backend.TraeBackend.open_target",
            return_value=fake_result,
        ):
            result = self.invoke("--json", "chat", "--dispatch", "uri", "hello fixture")

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["dispatch_method"], "uri")
        self.assertEqual(payload["command"], ["chat", "--mode", "agent", "hello fixture"])
        self.assertEqual(
            payload["deep_link"],
            "trae://trae.ai-ide/side-chat?query=hello+fixture",
        )
        self.assertEqual(
            payload["dispatch_command"],
            ["open", "-b", "com.trae.app", "-u", payload["deep_link"]],
        )

    def test_chat_dispatches_via_command_uri_json(self) -> None:
        result = self.invoke("--json", "chat", "--dispatch", "command", "hello fixture")

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["dispatch_method"], "command")
        self.assertEqual(payload["command"], ["chat", "--mode", "agent", "hello fixture"])
        self.assertEqual(
            payload["command_uri"],
            "command:workbench.action.chat.icube.open?"
            "%5B%7B%22query%22%3A%22hello%20fixture%22%2C%22keepOpen%22%3Atrue%7D%5D",
        )
        self.assertEqual(
            Path(payload["dispatch_command"][0]).resolve(),
            (self.fixture["app_path"] / "Contents" / "Resources" / "app" / "bin" / "trae").resolve(),
        )
        self.assertEqual(payload["dispatch_command"][1:], ["--open-url", "--", payload["command_uri"]])
        stdout_payload = json.loads(payload["stdout"])
        self.assertEqual(
            stdout_payload["argv"],
            ["--open-url", "--", payload["command_uri"]],
        )

    def test_chat_dispatches_via_cdp_json(self) -> None:
        mocked_cdp = {
            "endpoint": {"host": "127.0.0.1", "port": 9222, "source": "default"},
            "target": {"title": "Trae", "url": "file:///workbench.html"},
            "response": {"text": "fixture cdp answer"},
        }

        with self.patch_bound_bridge_state():
            with mock.patch(
                "cli_anything.trae.utils.trae_backend.TraeBackend.invoke_cdp_chat",
                return_value=mocked_cdp,
            ):
                result = self.invoke("--json", "chat", "--dispatch", "cdp", "hello fixture")

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["dispatch_method"], "cdp")
        self.assertEqual(payload["answer_text"], "fixture cdp answer")
        self.assertEqual(payload["stdout"], "fixture cdp answer")
        self.assertEqual(payload["cdp_endpoint"]["port"], 9222)
        self.assertEqual(payload["dispatch_target"]["title"], "Trae")
        self.assertEqual(
            payload["command_uri"],
            "command:workbench.action.chat.icube.open?"
            "%5B%7B%22keepOpen%22%3Atrue%7D%5D",
        )

    def test_chat_dispatches_via_cdp_uses_workspace_for_open_url(self) -> None:
        workspace = self.root / "workspace-cdp"
        workspace.mkdir()
        mocked_cdp = {
            "endpoint": {"host": "127.0.0.1", "port": 9222, "source": "default"},
            "target": {"title": "Trae", "url": "file:///workbench.html"},
            "response": {"text": "fixture cdp answer"},
        }

        with mock.patch.object(
            TraeBackend,
            "read_bridge_state",
            return_value=None,
        ):
            with mock.patch.object(
                TraeBackend,
                "wait_for_bridge_state",
                return_value={"workspace": str(workspace.resolve())},
            ):
                with mock.patch(
                    "cli_anything.trae.utils.trae_backend.TraeBackend.invoke_cdp_chat",
                    return_value=mocked_cdp,
                ):
                    result = self.invoke_in_workspace(
                        workspace,
                        "--json",
                        "chat",
                        "--dispatch",
                        "cdp",
                        "hello fixture",
                    )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        open_stdout = json.loads(payload["open_stdout"])
        self.assertEqual(Path(open_stdout["cwd"]).resolve(), workspace.resolve())

    def test_chat_dispatch_cdp_reuses_saved_workspace_binding(self) -> None:
        workspace = self.root / "bound-workspace"
        workspace.mkdir()
        mocked_cdp = {
            "endpoint": {"host": "127.0.0.1", "port": 9222, "source": "default"},
            "target": {
                "id": "fixture-target-id",
                "title": "bound-workspace",
                "url": "file:///bound-workspace",
            },
            "response": {"text": "fixture cdp answer"},
        }

        with mock.patch.object(
            TraeBackend,
            "read_workspace_cdp_binding",
            return_value={"target_id": "bound-target-id"},
        ):
            with mock.patch.object(
                TraeBackend,
                "open_workspace_via_cli",
            ) as mocked_open_workspace:
                with mock.patch.object(
                    TraeBackend,
                    "invoke_cdp_chat",
                    return_value=mocked_cdp,
                ) as mocked_invoke:
                    result = self.invoke_in_workspace(
                        workspace,
                        "--json",
                        "chat",
                        "--dispatch",
                        "cdp",
                        "hello fixture",
                    )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertFalse(mocked_open_workspace.called)
        self.assertEqual(mocked_invoke.call_args.kwargs["target_id"], "bound-target-id")
        self.assertFalse(mocked_invoke.call_args.kwargs["prefer_focused"])

    def test_chat_dispatch_cdp_fails_if_opened_workspace_never_binds(self) -> None:
        workspace = self.root / "workspace-needs-bind"
        workspace.mkdir()

        with mock.patch.object(
            TraeBackend,
            "read_workspace_cdp_binding",
            return_value=None,
        ):
            with mock.patch.object(
                TraeBackend,
                "read_bridge_state",
                return_value=None,
            ):
                with mock.patch.object(
                    TraeBackend,
                    "open_workspace_via_cli",
                    return_value=subprocess.CompletedProcess(
                        ["trae"],
                        0,
                        json.dumps({"cwd": str(workspace.resolve())}),
                        "",
                    ),
                ) as mocked_open_workspace:
                    with mock.patch.object(
                        TraeBackend,
                        "wait_for_bridge_state",
                        return_value=None,
                    ) as mocked_wait:
                        with mock.patch.object(
                            TraeBackend,
                            "invoke_cdp_chat",
                        ) as mocked_invoke:
                            result = self.invoke_in_workspace(
                                workspace,
                                "--json",
                                "chat",
                                "--dispatch",
                                "cdp",
                                "hello fixture",
                            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertTrue(mocked_open_workspace.called)
        self.assertTrue(mocked_wait.called)
        self.assertFalse(mocked_invoke.called)
        self.assertIn("project is not bound yet", result.output)

    def test_chat_dispatch_cdp_new_chat_reuses_bound_target_and_prefers_bridge_open(self) -> None:
        workspace = self.root / "bound-workspace-new-chat"
        workspace.mkdir()
        mocked_cdp = {
            "endpoint": {"host": "127.0.0.1", "port": 9222, "source": "default"},
            "target": {
                "id": "fixture-target-id",
                "title": "bound-workspace-new-chat",
                "url": "file:///bound-workspace-new-chat",
            },
            "response": {"text": "fixture cdp answer"},
        }

        with mock.patch.object(
            TraeBackend,
            "read_workspace_cdp_binding",
            return_value={"target_id": "bound-target-id"},
        ):
            with mock.patch.object(
                TraeBackend,
                "read_bridge_state",
                return_value={"workspace": str(workspace.resolve())},
            ):
                with mock.patch.object(
                    TraeBackend,
                    "invoke_bridge_vscode_api",
                    return_value={"ok": True, "path": "commands.executeCommand"},
                ) as mocked_bridge_open:
                    with mock.patch.object(
                        TraeBackend,
                        "open_workspace_via_cli",
                    ) as mocked_open_workspace:
                        with mock.patch.object(
                            TraeBackend,
                            "open_url_via_cli",
                        ) as mocked_open_url:
                            with mock.patch.object(
                                TraeBackend,
                                "invoke_cdp_chat",
                                return_value=mocked_cdp,
                            ) as mocked_invoke:
                                result = self.invoke_in_workspace(
                                    workspace,
                                    "--json",
                                    "chat",
                                    "--dispatch",
                                    "cdp",
                                    "--new-chat",
                                    "hello fixture",
                                )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertFalse(mocked_open_workspace.called)
        self.assertFalse(mocked_open_url.called)
        self.assertEqual(payload["open_via"], "bridge")
        self.assertEqual(
            mocked_bridge_open.call_args.args[0],
            "commands.executeCommand",
        )
        self.assertEqual(mocked_invoke.call_args.kwargs["target_id"], "bound-target-id")
        self.assertFalse(mocked_invoke.call_args.kwargs["prefer_focused"])

    def test_chat_dispatch_cdp_rebinds_stale_saved_target_automatically(self) -> None:
        workspace = self.root / "rebind-workspace"
        workspace.mkdir()
        rebound_cdp = {
            "endpoint": {"host": "127.0.0.1", "port": 9222, "source": "default"},
            "target": {
                "id": "rebound-target-id",
                "title": "rebind-workspace",
                "url": "file:///rebind-workspace",
            },
            "response": {"text": "fixture cdp answer"},
        }

        with mock.patch.object(
            TraeBackend,
            "read_workspace_cdp_binding",
            return_value={"target_id": "stale-target-id"},
        ):
            with mock.patch.object(
                TraeBackend,
                "read_bridge_state",
                return_value={"workspace": str(workspace.resolve())},
            ):
                with mock.patch.object(
                    TraeBackend,
                    "clear_workspace_cdp_binding",
                    return_value=True,
                ) as mocked_clear:
                    with mock.patch.object(
                        TraeBackend,
                        "open_workspace_via_cli",
                        return_value=subprocess.CompletedProcess(
                            ["trae"],
                            0,
                            json.dumps({"cwd": str(workspace.resolve())}),
                            "",
                        ),
                    ) as mocked_open_workspace:
                        with mock.patch.object(
                            TraeBackend,
                            "invoke_cdp_chat",
                            side_effect=[
                                CDPBridgeError(
                                    "The bound Trae window is no longer available for this workspace",
                                    code="CDP_BOUND_TARGET_NOT_FOUND",
                                ),
                                rebound_cdp,
                            ],
                        ) as mocked_invoke:
                            result = self.invoke_in_workspace(
                                workspace,
                                "--json",
                                "chat",
                                "--dispatch",
                                "cdp",
                                "hello fixture",
                            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertTrue(mocked_clear.called)
        self.assertFalse(mocked_open_workspace.called)
        self.assertEqual(mocked_invoke.call_count, 2)
        first_call = mocked_invoke.call_args_list[0].kwargs
        second_call = mocked_invoke.call_args_list[1].kwargs
        self.assertEqual(first_call["target_id"], "stale-target-id")
        self.assertIsNone(second_call["target_id"])
        self.assertTrue(second_call["prefer_focused"])
        self.assertEqual(payload["answer_text"], "fixture cdp answer")
        self.assertEqual(payload["dispatch_target"]["id"], "rebound-target-id")

    def test_chat_dispatch_cdp_text_prints_answer(self) -> None:
        mocked_cdp = {
            "endpoint": {"host": "127.0.0.1", "port": 9222, "source": "default"},
            "target": {"title": "Trae", "url": "file:///workbench.html"},
            "response": {"text": "fixture cdp answer"},
        }

        with self.patch_bound_bridge_state():
            with mock.patch(
                "cli_anything.trae.utils.trae_backend.TraeBackend.invoke_cdp_chat",
                return_value=mocked_cdp,
            ):
                result = self.invoke("chat", "--dispatch", "cdp", "hello fixture")

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.output.strip(), "fixture cdp answer")

    def test_chat_dispatch_cdp_reports_click_error_cleanly(self) -> None:
        with self.patch_bound_bridge_state():
            with mock.patch(
                "cli_anything.trae.utils.trae_backend.TraeBackend.invoke_cdp_chat",
                side_effect=RuntimeError("Failed to query the debugger endpoint"),
            ):
                result = self.invoke("chat", "--dispatch", "cdp", "hello fixture")

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Error: Failed to query the debugger endpoint", result.output)

    def test_chat_dispatch_uri_rejects_cli_only_options(self) -> None:
        result = self.invoke(
            "chat",
            "--dispatch",
            "uri",
            "--add-file",
            "README.md",
            "hello fixture",
        )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("does not support --add-file", result.output)

    def test_chat_dispatch_command_rejects_cli_only_options(self) -> None:
        result = self.invoke(
            "chat",
            "--dispatch",
            "command",
            "--add-file",
            "README.md",
            "hello fixture",
        )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("does not support --add-file", result.output)

    def test_chat_dispatch_cdp_rejects_cli_only_options(self) -> None:
        result = self.invoke(
            "chat",
            "--dispatch",
            "cdp",
            "--add-file",
            "README.md",
            "hello fixture",
        )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("does not support --add-file", result.output)

    def test_logs_tail_uses_latest_session(self) -> None:
        result = self.invoke("logs", "tail", "--match", "main.log", "--lines", "1")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("main log line 2", result.output)

    def test_rpc_describe_json(self) -> None:
        result = self.invoke("--json", "rpc", "describe", "chat", "chat")
        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertTrue(payload["bundle_verified"])
        self.assertEqual(payload["recent_samples"]["session_id"], "fixture-session-id")

    def test_rpc_context_json(self) -> None:
        result = self.invoke("--json", "rpc", "context", "--limit", "5")
        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["projects"][0]["project_id"], "fixture-project")
        self.assertEqual(payload["messages"][0]["message_id"], "fixture-message-id")

    def test_rpc_traces_json(self) -> None:
        result = self.invoke(
            "--json",
            "rpc",
            "traces",
            "--service",
            "chat",
            "--method",
            "get_messages",
            "--limit",
            "3",
        )
        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["filters"]["service"], "chat")
        self.assertEqual(payload["filters"]["method"], "get_messages")
        self.assertEqual(payload["traces"][0]["trace_id"], "fixture-ok-trace")
        self.assertEqual(payload["traces"][0]["query_history_state_status"], "ok")
        self.assertEqual(payload["traces"][1]["query_history_state_status"], "error")
        self.assertEqual(payload["traces"][2]["query_history_state_status"], "not-needed")

    def test_trace_chats_json(self) -> None:
        result = self.invoke("--json", "trace", "chats", "--limit", "5")
        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertTrue(str(payload["latest_log_session"]).endswith("20260404T120000"))
        self.assertEqual(payload["turns"][0]["status"], "completed")
        self.assertEqual(payload["turns"][0]["trace_id"], "fixture-trace-id")

    def test_trace_show_json(self) -> None:
        result = self.invoke("--json", "trace", "show", "fixture-message-id")
        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["turn"]["session_id"], "fixture-session-id")
        self.assertEqual(payload["turn"]["backend_message_id"], "fixture-backend-message-id")
        self.assertEqual(payload["turn"]["first_token_preview"], "fixture-first-token")
        self.assertEqual(payload["turn"]["tool_run_count"], 1)
        self.assertEqual(payload["turn"]["tool_runs"][0]["command"], "echo fixture")
        self.assertEqual(payload["turn"]["tool_runs"][0]["exit_code"], 0)

    def test_trace_show_text_includes_tool_run_excerpt(self) -> None:
        result = self.invoke("trace", "show", "fixture-message-id")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Tool runs:", result.output)
        self.assertIn("command=echo fixture", result.output)
        self.assertIn("$ echo fixture", result.output)

    def test_trace_cdp_ws_json(self) -> None:
        mocked_probe = {
            "endpoint": {"host": "127.0.0.1", "port": 9222, "source": "default"},
            "target": {"title": "Trae CN", "url": "file:///workbench.html"},
            "probe_install": {"reload_before_run": True},
            "websocket_probe": {
                "page": {
                    "records": [
                        {
                            "url": "wss://trae-ws-cn.mchost.guru/custom_model",
                            "protocols": ["fixture-protocol"],
                        }
                    ]
                },
                "network": {
                    "handshakes": [
                        {
                            "requestId": "fixture-request-id",
                            "url": "wss://trae-ws-cn.mchost.guru/custom_model",
                            "secWebSocketProtocol": "fixture-protocol",
                        }
                    ]
                },
            },
        }

        with mock.patch(
            "cli_anything.trae.utils.trae_backend.TraeBackend.invoke_cdp_websocket_probe",
            return_value=mocked_probe,
        ):
            result = self.invoke(
                "--json",
                "trace",
                "cdp-ws",
                "--reload-before-run",
                "收到回复我",
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["endpoint"]["port"], 9222)
        self.assertTrue(payload["probe_install"]["reload_before_run"])
        self.assertEqual(
            payload["websocket_probe"]["page"]["records"][0]["url"],
            "wss://trae-ws-cn.mchost.guru/custom_model",
        )

    def test_trace_cdp_ws_text(self) -> None:
        mocked_probe = {
            "endpoint": {"host": "127.0.0.1", "port": 9222, "source": "default"},
            "target": {"title": "Trae CN", "url": "file:///workbench.html"},
            "probe_install": {"reload_before_run": False},
            "websocket_probe": {
                "page": {
                    "records": [
                        {
                            "url": "wss://trae-ws-cn.mchost.guru/custom_model",
                            "protocols": ["fixture-protocol"],
                            "selectedProtocol": "fixture-protocol",
                            "sendCount": 1,
                            "receiveCount": 0,
                            "readyState": "open",
                            "sentFrames": [{"preview": "{\"packet_type\":\"request\"}"}],
                        }
                    ]
                },
                "network": {
                    "handshakes": [
                        {
                            "requestId": "fixture-request-id",
                            "url": "wss://trae-ws-cn.mchost.guru/custom_model",
                            "secWebSocketProtocol": "fixture-protocol",
                        }
                    ],
                    "responses": [],
                },
            },
        }

        with mock.patch(
            "cli_anything.trae.utils.trae_backend.TraeBackend.invoke_cdp_websocket_probe",
            return_value=mocked_probe,
        ):
            result = self.invoke("trace", "cdp-ws", "收到回复我")

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("CDP WebSocket probe", result.output)
        self.assertIn("Page sockets:", result.output)
        self.assertIn("wss://trae-ws-cn.mchost.guru/custom_model", result.output)
        self.assertIn("Sec-WebSocket-Protocol=fixture-protocol", result.output)

    def test_trace_show_searches_older_log_sessions(self) -> None:
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

        result = self.invoke("trace", "show", "fixture-message-id")

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Log session:", result.output)
        self.assertIn("20260404T120000", result.output)
        self.assertIn("Latest logs:", result.output)
        self.assertIn("20260405T120000", result.output)

    def test_trace_show_reads_rotated_renderer_logs(self) -> None:
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

        result = self.invoke("trace", "show", "fixture-message-id")

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Frontend message: fixture-message-id", result.output)
        self.assertIn("Tool runs:", result.output)

    def test_chat_inspect_json(self) -> None:
        inspection = {
            "latest_log_session": str(self.fixture["support_dir"] / "logs" / "20260404T120000"),
            "matched_after_dispatch": True,
            "turn": {
                "status": "completed",
                "session_id": "fixture-session-id",
                "frontend_message_id": "fixture-message-id",
                "backend_message_id": "fixture-backend-message-id",
                "trace_id": "fixture-trace-id",
                "task_id": "fixture-task-id",
                "chat_model": "GLM-4.7",
                "tool_call_count": 2,
                "run_script_count": 1,
                "run_script_success_count": 1,
            },
        }

        with self.patch_bound_bridge_state():
            with mock.patch(
                "cli_anything.trae.utils.trae_backend.TraeBackend.wait_for_chat_turn",
                return_value=inspection,
            ):
                with mock.patch(
                    "cli_anything.trae.utils.trae_backend.TraeBackend.invoke_cdp_chat",
                    return_value={
                        "endpoint": {"host": "127.0.0.1", "port": 9222, "source": "default"},
                        "target": {
                            "id": "fixture-target-id",
                            "title": "Trae",
                            "url": "file:///workbench.html",
                        },
                        "response": {"text": "fixture cdp answer"},
                    },
                ):
                    result = self.invoke("--json", "chat", "--inspect", "hello fixture")

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["requested_dispatch_method"], "auto")
        self.assertEqual(payload["dispatch_method"], "cdp")
        self.assertTrue(payload["inspection"]["matched_after_dispatch"])
        self.assertEqual(payload["inspection"]["turn"]["trace_id"], "fixture-trace-id")
        self.assertEqual(payload["inspection"]["turn"]["status"], "completed")

    def test_chat_inspect_uri_json(self) -> None:
        inspection = {
            "latest_log_session": str(self.fixture["support_dir"] / "logs" / "20260404T120000"),
            "matched_after_dispatch": True,
            "turn": {
                "status": "completed",
                "session_id": "fixture-session-id",
                "frontend_message_id": "fixture-message-id",
                "backend_message_id": "fixture-backend-message-id",
                "trace_id": "fixture-trace-id",
                "task_id": "fixture-task-id",
                "chat_model": "GLM-4.7",
                "tool_call_count": 2,
                "run_script_count": 1,
                "run_script_success_count": 1,
            },
        }

        with mock.patch(
            "cli_anything.trae.utils.trae_backend.TraeBackend.open_target",
            return_value=subprocess.CompletedProcess(["open"], 0, "", ""),
        ):
            with mock.patch(
                "cli_anything.trae.utils.trae_backend.TraeBackend.wait_for_chat_turn",
                return_value=inspection,
            ):
                result = self.invoke(
                    "--json",
                    "chat",
                    "--dispatch",
                    "uri",
                    "--inspect",
                    "hello fixture",
                )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["dispatch_method"], "uri")
        self.assertTrue(payload["inspection"]["matched_after_dispatch"])
        self.assertEqual(payload["inspection"]["turn"]["trace_id"], "fixture-trace-id")

    def test_chat_inspect_command_json(self) -> None:
        inspection = {
            "latest_log_session": str(self.fixture["support_dir"] / "logs" / "20260404T120000"),
            "matched_after_dispatch": True,
            "turn": {
                "status": "completed",
                "session_id": "fixture-session-id",
                "frontend_message_id": "fixture-message-id",
                "backend_message_id": "fixture-backend-message-id",
                "trace_id": "fixture-trace-id",
                "task_id": "fixture-task-id",
                "chat_model": "GLM-4.7",
                "tool_call_count": 2,
                "run_script_count": 1,
                "run_script_success_count": 1,
            },
        }

        with mock.patch(
            "cli_anything.trae.utils.trae_backend.TraeBackend.wait_for_chat_turn",
            return_value=inspection,
        ):
            result = self.invoke(
                "--json",
                "chat",
                "--dispatch",
                "command",
                "--inspect",
                "hello fixture",
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["dispatch_method"], "command")
        self.assertTrue(payload["inspection"]["matched_after_dispatch"])
        self.assertEqual(payload["inspection"]["turn"]["trace_id"], "fixture-trace-id")

    def test_chat_inspect_text_reports_latest_turn_when_no_match(self) -> None:
        inspection = {
            "latest_log_session": str(self.fixture["support_dir"] / "logs" / "20260404T120000"),
            "matched_after_dispatch": False,
            "turn": None,
            "latest_turn": {
                "status": "completed",
                "started_at": "2026-04-04T12:00:00.200+08:00",
                "session_id": "fixture-session-id",
                "frontend_message_id": "fixture-message-id",
                "backend_message_id": "fixture-backend-message-id",
                "trace_id": "fixture-trace-id",
                "task_id": "fixture-task-id",
                "chat_model": "GLM-4.7",
                "tool_call_count": 2,
                "run_script_count": 1,
                "run_script_success_count": 1,
                "progress_notice_count": 1,
                "first_token_preview": "fixture-preview",
            },
            "note": "No new local chat turn matched the dispatch window.",
        }

        with self.patch_bound_bridge_state():
            with mock.patch(
                "cli_anything.trae.utils.trae_backend.TraeBackend.wait_for_chat_turn",
                return_value=inspection,
            ):
                with mock.patch(
                    "cli_anything.trae.utils.trae_backend.TraeBackend.invoke_cdp_chat",
                    return_value={
                        "endpoint": {"host": "127.0.0.1", "port": 9222, "source": "default"},
                        "target": {
                            "id": "fixture-target-id",
                            "title": "Trae",
                            "url": "file:///workbench.html",
                        },
                        "response": {"text": "fixture cdp answer"},
                    },
                ):
                    result = self.invoke("chat", "--inspect", "hello fixture")

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Requested dispatch: auto -> cdp", result.output)
        self.assertIn("No new local chat turn matched the dispatch window.", result.output)
        self.assertIn("Latest observed local turn:", result.output)
        self.assertIn("frontend_message=fixture-message-id", result.output)
        self.assertIn('preview="fixture-preview"', result.output)

    def test_rewrite_argv_for_prompt_rewrites_bare_prompt(self) -> None:
        self.assertEqual(
            rewrite_argv_for_prompt(["traecli", "hello fixture"]),
            ["traecli", "repl", "hello fixture"],
        )
        self.assertEqual(
            rewrite_argv_for_prompt(["traecli", "--json", "hello fixture"]),
            ["traecli", "--json", "exec", "hello fixture"],
        )
        self.assertEqual(
            rewrite_argv_for_prompt(["traecli", "-C", "/tmp/project", "hello fixture"]),
            ["traecli", "-C", "/tmp/project", "repl", "hello fixture"],
        )
        self.assertEqual(
            rewrite_argv_for_prompt(["traecli", "--no-alt-screen", "hello fixture"]),
            ["traecli", "--no-alt-screen", "repl", "hello fixture"],
        )

    def test_rewrite_argv_for_prompt_preserves_commands_and_help(self) -> None:
        self.assertEqual(
            rewrite_argv_for_prompt(["traecli", "doctor"]),
            ["traecli", "doctor"],
        )
        self.assertEqual(
            rewrite_argv_for_prompt(["traecli", "init"]),
            ["traecli", "init"],
        )
        self.assertEqual(
            rewrite_argv_for_prompt(["traecli", "--help"]),
            ["traecli", "--help"],
        )

    def test_build_interactive_screen_contains_conversation_and_panel(self) -> None:
        backend = TraeBackend(
            app_path=self.fixture["app_path"],
            support_dir=self.fixture["support_dir"],
            user_data_dir=self.fixture["user_data_dir"],
        )
        state = SessionState(
            workspace=str(self.root.resolve()),
            app_path=str(self.fixture["app_path"].resolve()),
            support_dir=str(self.fixture["support_dir"].resolve()),
            user_data_dir=str(self.fixture["user_data_dir"].resolve()),
            last_headless_session_id="fixture-hidden-session",
        )
        app = AppContext(backend=backend, state=state, json_output=False)
        record = {
            "id": "fixture-cli-session",
            "message_count": 2,
            "messages": [
                {"role": "user", "content": "hello fixture"},
                {"role": "assistant", "content": "fixture headless answer"},
            ],
        }
        view = InteractiveViewState(
            notice="sent via headless",
            panel_title="Dispatch: headless",
            panel_body="fixture headless answer",
        )

        rendered = build_interactive_screen(app, record, view)

        self.assertIn("TraeCLI", rendered)
        self.assertIn("Tip: 输入 / 打开命令；使用 /model 查看或切换当前模型。", rendered)
        self.assertIn("› hello fixture", rendered)
        self.assertIn("• fixture headless answer", rendered)
        self.assertNotIn("Context", rendered)
        self.assertNotIn("Chat", rendered)
        self.assertNotIn("prompt: Enter 发送 | Up/Down 滚动聊天 | / 打开命令 | /exit", rendered)
        self.assertNotIn("最新回复贴近底部显示；向上滚动查看更早内容。", rendered)
        self.assertIn("› 输入消息或 / 打开命令", rendered)
        self.assertIn("GPT-5.3 Codex ·", rendered)
        self.assertRegex(rendered, r"\x1b\[\d+;\d+H$")

    def test_build_interactive_screen_renders_execution_timeline_events(self) -> None:
        backend = TraeBackend(
            app_path=self.fixture["app_path"],
            support_dir=self.fixture["support_dir"],
            user_data_dir=self.fixture["user_data_dir"],
        )
        state = SessionState(
            workspace=str(self.root.resolve()),
            app_path=str(self.fixture["app_path"].resolve()),
            support_dir=str(self.fixture["support_dir"].resolve()),
            user_data_dir=str(self.fixture["user_data_dir"].resolve()),
        )
        app = AppContext(backend=backend, state=state, json_output=False)
        record = {
            "id": "fixture-cli-session",
            "message_count": 2,
            "messages": [
                {"role": "user", "content": "run tests"},
                {
                    "role": "event",
                    "status": "working",
                    "headline": "Working",
                    "details": ["Sending prompt via auto dispatch."],
                },
                {
                    "role": "event",
                    "status": "success",
                    "headline": "Ran `python3 -m unittest sample -v`",
                    "details": ["test_sample (suite) ... ok", "OK"],
                },
                {"role": "assistant", "content": "All tests passed."},
            ],
        }
        view = InteractiveViewState(
            notice="completed via headless",
            panel_title="Dispatch: headless",
            panel_body="All tests passed.",
        )

        rendered = build_interactive_screen(app, record, view)

        self.assertIn("● Working", rendered)
        self.assertIn("✓ Ran `python3 -m unittest sample -v`", rendered)
        self.assertIn("│ test_sample (suite) ... ok", rendered)
        self.assertIn("└ OK", rendered)
        self.assertIn("• All tests passed.", rendered)

    def test_build_interactive_screen_bottom_anchors_chat_and_shows_scroll_marker(self) -> None:
        backend = TraeBackend(
            app_path=self.fixture["app_path"],
            support_dir=self.fixture["support_dir"],
            user_data_dir=self.fixture["user_data_dir"],
        )
        state = SessionState(
            workspace=str(self.root.resolve()),
            app_path=str(self.fixture["app_path"].resolve()),
            support_dir=str(self.fixture["support_dir"].resolve()),
            user_data_dir=str(self.fixture["user_data_dir"].resolve()),
        )
        app = AppContext(backend=backend, state=state, json_output=False)
        record = {
            "id": "fixture-cli-session",
            "message_count": 10,
            "messages": [
                {"role": "user", "content": f"user line {index} " * 8}
                for index in range(1, 11)
            ],
        }
        view = InteractiveViewState(notice="interactive session ready")

        with mock.patch(
            "cli_anything.trae.trae_cli.terminal_screen_size",
            return_value=(100, 20),
        ):
            rendered = build_interactive_screen(app, record, view)

        self.assertIn("...上面还有", rendered)
        self.assertNotIn("user line 1 user line 1", rendered)
        self.assertIn("› user line 10", rendered)

    def test_build_interactive_screen_shows_slash_palette_for_commands_and_settings(self) -> None:
        backend = TraeBackend(
            app_path=self.fixture["app_path"],
            support_dir=self.fixture["support_dir"],
            user_data_dir=self.fixture["user_data_dir"],
        )
        state = SessionState(
            workspace=str(self.root.resolve()),
            app_path=str(self.fixture["app_path"].resolve()),
            support_dir=str(self.fixture["support_dir"].resolve()),
            user_data_dir=str(self.fixture["user_data_dir"].resolve()),
        )
        app = AppContext(backend=backend, state=state, json_output=False)
        record = {
            "id": "fixture-cli-session",
            "message_count": 0,
            "messages": [],
        }
        view = InteractiveViewState(
            notice="interactive session ready",
            input_buffer="/",
            input_cursor=1,
        )

        rendered = build_interactive_screen(app, record, view)
        lines = rendered.splitlines()
        prompt_index = next(
            index for index, line in enumerate(lines) if line.strip().startswith("› /")
        )
        command_index = next(
            index for index, line in enumerate(lines) if line.strip().startswith("> /help")
        )

        self.assertNotIn("命令面板", rendered)
        self.assertIn("/status", rendered)
        self.assertIn("/diff", rendered)
        self.assertIn("/model <model>", rendered)
        self.assertIn("/approvals", rendered)
        self.assertIn("> /help", rendered)
        self.assertLess(prompt_index, command_index)
        self.assertNotIn("slash: 输入以筛选命令和设置 | 上下键选择 | Enter 执行", rendered)

    def test_build_interactive_screen_filters_slash_palette_matches(self) -> None:
        backend = TraeBackend(
            app_path=self.fixture["app_path"],
            support_dir=self.fixture["support_dir"],
            user_data_dir=self.fixture["user_data_dir"],
        )
        state = SessionState(
            workspace=str(self.root.resolve()),
            app_path=str(self.fixture["app_path"].resolve()),
            support_dir=str(self.fixture["support_dir"].resolve()),
            user_data_dir=str(self.fixture["user_data_dir"].resolve()),
        )
        app = AppContext(backend=backend, state=state, json_output=False)
        record = {
            "id": "fixture-cli-session",
            "message_count": 0,
            "messages": [],
        }
        view = InteractiveViewState(
            notice="interactive session ready",
            input_buffer="/set",
            input_cursor=4,
        )

        rendered = build_interactive_screen(app, record, view)

        self.assertIn("> /set workspace <path>", rendered)
        self.assertIn("/set app <path>", rendered)
        self.assertIn("/set user-data <path>", rendered)
        self.assertNotIn("命令面板", rendered)

    def test_build_interactive_screen_scrolls_slash_palette_with_selection(self) -> None:
        backend = TraeBackend(
            app_path=self.fixture["app_path"],
            support_dir=self.fixture["support_dir"],
            user_data_dir=self.fixture["user_data_dir"],
        )
        state = SessionState(
            workspace=str(self.root.resolve()),
            app_path=str(self.fixture["app_path"].resolve()),
            support_dir=str(self.fixture["support_dir"].resolve()),
            user_data_dir=str(self.fixture["user_data_dir"].resolve()),
        )
        app = AppContext(backend=backend, state=state, json_output=False)
        record = {
            "id": "fixture-cli-session",
            "message_count": 0,
            "messages": [],
        }
        view = InteractiveViewState(
            notice="interactive session ready",
            input_buffer="/",
            input_cursor=1,
            slash_palette_index=19,
        )

        rendered = build_interactive_screen(app, record, view)

        self.assertIn("/trace chats", rendered)
        self.assertIn("> /set workspace <path>", rendered)
        self.assertIn("/set user-data <path>", rendered)
        self.assertNotIn("/help", rendered)

    def test_build_interactive_screen_shows_empty_chat_prompt_before_first_message(self) -> None:
        backend = TraeBackend(
            app_path=self.fixture["app_path"],
            support_dir=self.fixture["support_dir"],
            user_data_dir=self.fixture["user_data_dir"],
        )
        state = SessionState(
            workspace=str(self.root.resolve()),
            app_path=str(self.fixture["app_path"].resolve()),
            support_dir=str(self.fixture["support_dir"].resolve()),
            user_data_dir=str(self.fixture["user_data_dir"].resolve()),
        )
        app = AppContext(backend=backend, state=state, json_output=False)
        record = {
            "id": "fixture-cli-session",
            "message_count": 0,
            "messages": [],
        }
        view = InteractiveViewState(
            notice="interactive session ready",
            panel_title="Activity",
            panel_body="Send a prompt to begin.",
        )

        rendered = build_interactive_screen(app, record, view)

        self.assertIn("TraeCLI", rendered)
        self.assertIn("TraeCLI", rendered)
        self.assertIn("model: GPT-5.3 Codex   /model", rendered)
        self.assertIn("directory:", rendered)
        self.assertNotIn(".-----------.", rendered)
        self.assertIn("Tip: 输入 / 打开命令；使用 /model 查看或切换当前模型。", rendered)
        self.assertIn("GPT-5.3 Codex ·", rendered)
        self.assertIn("› 输入消息或 / 打开命令", rendered)
        self.assertNotIn("Chat", rendered)
        self.assertNotIn("直接输入消息开始对话", rendered)
        self.assertNotIn("/status 环境   /diff 改动   /sessions 历史", rendered)
        self.assertNotIn("prompt: Enter 发送 | Up/Down 滚动聊天 | / 打开命令 | /exit", rendered)
        self.assertNotIn("最新回复贴近底部显示；向上滚动查看更早内容。", rendered)
        self.assertRegex(rendered, r"\x1b\[\d+;\d+H$")

    def test_build_interactive_screen_moves_terminal_cursor_to_chat_input(self) -> None:
        backend = TraeBackend(
            app_path=self.fixture["app_path"],
            support_dir=self.fixture["support_dir"],
            user_data_dir=self.fixture["user_data_dir"],
        )
        state = SessionState(
            workspace=str(self.root.resolve()),
            app_path=str(self.fixture["app_path"].resolve()),
            support_dir=str(self.fixture["support_dir"].resolve()),
            user_data_dir=str(self.fixture["user_data_dir"].resolve()),
        )
        app = AppContext(backend=backend, state=state, json_output=False)
        record = {
            "id": "fixture-cli-session",
            "message_count": 1,
            "messages": [{"role": "user", "content": "hello fixture"}],
        }
        view = InteractiveViewState(notice="interactive session ready")

        rendered = build_interactive_screen(app, record, view)

        self.assertIn("› hello fixture", rendered)
        self.assertIn("› 输入消息或 / 打开命令", rendered)
        self.assertRegex(rendered, r"\x1b\[\d+;\d+H$")

    def test_startup_landing_lines_keep_wordmark_rows_centered(self) -> None:
        lines = startup_landing_lines(width=80, color=False)
        wordmark_lines = [line for line in lines if "█" in line]

        self.assertEqual(len(wordmark_lines), 5)
        first_block_columns = [line.index("█") for line in wordmark_lines]
        self.assertEqual(len(set(first_block_columns)), 1)
        self.assertEqual(len({len(line) for line in wordmark_lines}), 1)

    def test_build_interactive_screen_contains_saved_sessions_view(self) -> None:
        backend = TraeBackend(
            app_path=self.fixture["app_path"],
            support_dir=self.fixture["support_dir"],
            user_data_dir=self.fixture["user_data_dir"],
        )
        state = SessionState(
            workspace=str(self.root.resolve()),
            app_path=str(self.fixture["app_path"].resolve()),
            support_dir=str(self.fixture["support_dir"].resolve()),
            user_data_dir=str(self.fixture["user_data_dir"].resolve()),
        )
        app = AppContext(backend=backend, state=state, json_output=False)
        record = {
            "id": "fixture-cli-session",
            "message_count": 2,
            "messages": [
                {"role": "user", "content": "hello fixture"},
                {"role": "assistant", "content": "fixture headless answer"},
            ],
        }
        view = InteractiveViewState(
            notice="saved sessions",
            mode="sessions",
            sessions_payload={
                "all_workspaces": False,
                "sessions": [
                    {
                        "id": "fixture-cli-session-1",
                        "title": "First",
                        "workspace": str(self.root.resolve()),
                        "preview": "first answer",
                        "resumable": True,
                    },
                    {
                        "id": "fixture-cli-session-2",
                        "title": "Second",
                        "workspace": str(self.root.resolve()),
                        "preview": "second answer",
                        "resumable": True,
                    },
                ],
            },
            session_list_index=1,
        )

        rendered = build_interactive_screen(app, record, view)

        self.assertIn("History", rendered)
        self.assertIn("> 2. Second", rendered)
        self.assertIn("Inspector", rendered)
        self.assertIn("Session: fixture-cli-session-2", rendered)
        self.assertIn("history: Up/Down or j/k move | Enter/o open", rendered)

    def test_build_interactive_screen_scrolls_saved_sessions_view_with_selection(self) -> None:
        backend = TraeBackend(
            app_path=self.fixture["app_path"],
            support_dir=self.fixture["support_dir"],
            user_data_dir=self.fixture["user_data_dir"],
        )
        state = SessionState(
            workspace=str(self.root.resolve()),
            app_path=str(self.fixture["app_path"].resolve()),
            support_dir=str(self.fixture["support_dir"].resolve()),
            user_data_dir=str(self.fixture["user_data_dir"].resolve()),
        )
        app = AppContext(backend=backend, state=state, json_output=False)
        record = {
            "id": "fixture-cli-session",
            "message_count": 0,
            "messages": [],
        }
        sessions = [
            {
                "id": f"fixture-cli-session-{index}",
                "title": f"Session {index}",
                "workspace": str(self.root.resolve()),
                "preview": f"preview {index}",
                "resumable": True,
            }
            for index in range(1, 13)
        ]
        view = InteractiveViewState(
            notice="saved sessions",
            mode="sessions",
            sessions_payload={
                "all_workspaces": False,
                "sessions": sessions,
            },
            session_list_index=9,
        )

        rendered = build_interactive_screen(app, record, view)

        self.assertIn("...前面还有", rendered)
        self.assertIn("> 10. Session 10", rendered)
        self.assertIn("11. Session 11", rendered)
        self.assertIn("12. Session 12", rendered)

    def test_build_interactive_screen_contains_session_detail_view(self) -> None:
        backend = TraeBackend(
            app_path=self.fixture["app_path"],
            support_dir=self.fixture["support_dir"],
            user_data_dir=self.fixture["user_data_dir"],
        )
        state = SessionState(
            workspace=str(self.root.resolve()),
            app_path=str(self.fixture["app_path"].resolve()),
            support_dir=str(self.fixture["support_dir"].resolve()),
            user_data_dir=str(self.fixture["user_data_dir"].resolve()),
        )
        app = AppContext(backend=backend, state=state, json_output=False)
        record = {
            "id": "fixture-cli-session",
            "message_count": 2,
            "messages": [
                {"role": "user", "content": "hello fixture"},
                {"role": "assistant", "content": "fixture headless answer"},
            ],
        }
        view = InteractiveViewState(
            notice="session detail",
            mode="detail",
            panel_title="Status",
            panel_body="Use /back to return.",
            session_detail_title="Session Detail: fixture-cli-session",
            session_detail_payload={
                "found": True,
                "session": {
                    "id": "fixture-cli-session",
                    "title": "Initial prompt",
                    "workspace": str(self.root.resolve()),
                    "updated_at": "2026-04-08T10:01:00+08:00",
                    "resumable": True,
                    "last_headless_session_id": "fixture-hidden-session",
                    "last_dispatch_method": "headless",
                    "messages": [
                        {
                            "role": "assistant",
                            "content": "fixture headless answer",
                            "timestamp": "2026-04-08T10:01:00+08:00",
                        }
                    ],
                },
            },
        )

        rendered = build_interactive_screen(app, record, view)

        self.assertIn("Session Detail: fixture-cli-session", rendered)
        self.assertIn("CLI session: fixture-cli-session", rendered)
        self.assertIn("Hidden session: fixture-hidden-session", rendered)
        self.assertIn("inspector: Esc back | /chat conversation | /exit", rendered)

    def test_parse_terminal_keypress_splits_escape_sequences_and_text(self) -> None:
        parsed = parse_terminal_keypress("\x1b[Aab\r\x1b[D")

        self.assertEqual(parsed, ["UP", "a", "b", "ENTER", "LEFT"])

    def test_render_interactive_input_marks_cursor_and_truncates(self) -> None:
        view = InteractiveViewState(
            input_buffer="abcdefghijklmnopqrstuvwxyz0123456789",
            input_cursor=20,
        )

        rendered = render_interactive_input(view, width=18)
        extracted_lines, cursor_row, cursor_col = trae_cli.extract_cursor_from_lines([rendered])

        self.assertEqual(cursor_row, 1)
        self.assertIsNotNone(cursor_col)
        self.assertEqual(len(extracted_lines), 1)
        self.assertNotIn(trae_cli.INTERACTIVE_CURSOR_MARKER, extracted_lines[0])
        self.assertTrue(extracted_lines[0].startswith("..."))
        self.assertTrue(extracted_lines[0].endswith("..."))

    def test_handle_interactive_view_input_clamps_navigation_and_opens_detail(self) -> None:
        sessions_path = self.fixture["user_data_dir"] / "traecli-sessions.json"
        sessions_path.write_text(
            json.dumps(
                [
                    {
                        "id": "fixture-cli-session-1",
                        "title": "First",
                        "created_at": "2026-04-08T10:00:00+08:00",
                        "updated_at": "2026-04-08T10:01:00+08:00",
                        "workspace": str(self.root.resolve()),
                        "app_path": str(self.fixture["app_path"].resolve()),
                        "support_dir": str(self.fixture["support_dir"].resolve()),
                        "user_data_dir": str(self.fixture["user_data_dir"].resolve()),
                        "last_headless_session_id": "fixture-hidden-session-1",
                        "resumable": True,
                        "last_dispatch_method": "headless",
                        "messages": [
                            {"role": "assistant", "content": "first answer"},
                        ],
                    },
                    {
                        "id": "fixture-cli-session-2",
                        "title": "Second",
                        "created_at": "2026-04-08T11:00:00+08:00",
                        "updated_at": "2026-04-08T11:01:00+08:00",
                        "workspace": str(self.root.resolve()),
                        "app_path": str(self.fixture["app_path"].resolve()),
                        "support_dir": str(self.fixture["support_dir"].resolve()),
                        "user_data_dir": str(self.fixture["user_data_dir"].resolve()),
                        "last_headless_session_id": "fixture-hidden-session-2",
                        "resumable": True,
                        "last_dispatch_method": "headless",
                        "messages": [
                            {"role": "assistant", "content": "second answer"},
                        ],
                    },
                ]
            )
        )
        backend = TraeBackend(
            app_path=self.fixture["app_path"],
            support_dir=self.fixture["support_dir"],
            user_data_dir=self.fixture["user_data_dir"],
        )
        state = SessionState(
            workspace=str(self.root.resolve()),
            app_path=str(self.fixture["app_path"].resolve()),
            support_dir=str(self.fixture["support_dir"].resolve()),
            user_data_dir=str(self.fixture["user_data_dir"].resolve()),
        )
        app = AppContext(backend=backend, state=state, json_output=False)
        record = {
            "id": "fixture-active-session",
            "message_count": 0,
            "messages": [],
        }
        view = InteractiveViewState(
            mode="sessions",
            sessions_payload={
                "all_workspaces": False,
                "sessions": [
                    {"id": "fixture-cli-session-1", "title": "First", "preview": "first answer"},
                    {"id": "fixture-cli-session-2", "title": "Second", "preview": "second answer"},
                ],
            },
            session_list_index=0,
            history=["chat"],
        )

        self.assertTrue(handle_interactive_view_input(app, record, view, "k"))
        self.assertEqual(view.session_list_index, 0)
        self.assertTrue(handle_interactive_view_input(app, record, view, "j"))
        self.assertEqual(view.session_list_index, 1)
        self.assertTrue(handle_interactive_view_input(app, record, view, "j"))
        self.assertEqual(view.session_list_index, 1)
        self.assertTrue(handle_interactive_view_input(app, record, view, "o"))
        self.assertEqual(view.mode, "detail")
        self.assertEqual(view.session_detail_payload["session"]["id"], "fixture-cli-session-2")
        self.assertTrue(handle_interactive_view_input(app, record, view, "q"))
        self.assertEqual(view.mode, "sessions")

    def test_process_alt_screen_key_moves_sessions_without_enter(self) -> None:
        backend = TraeBackend(
            app_path=self.fixture["app_path"],
            support_dir=self.fixture["support_dir"],
            user_data_dir=self.fixture["user_data_dir"],
        )
        state = SessionState(
            workspace=str(self.root.resolve()),
            app_path=str(self.fixture["app_path"].resolve()),
            support_dir=str(self.fixture["support_dir"].resolve()),
            user_data_dir=str(self.fixture["user_data_dir"].resolve()),
        )
        app = AppContext(backend=backend, state=state, json_output=False)
        record = {"id": "fixture-active-session", "message_count": 0, "messages": []}
        view = InteractiveViewState(
            mode="sessions",
            sessions_payload={
                "sessions": [
                    {"id": "fixture-cli-session-1", "title": "First"},
                    {"id": "fixture-cli-session-2", "title": "Second"},
                ]
            },
            session_list_index=0,
            history=["chat"],
        )

        with mock.patch(
            "cli_anything.trae.trae_cli.render_interactive_session",
            return_value=None,
        ):
            exited = process_alt_screen_key(app, record, view, "j")

        self.assertFalse(exited)
        self.assertEqual(view.session_list_index, 1)

    def test_process_alt_screen_key_moves_slash_palette_selection_with_arrows(self) -> None:
        backend = TraeBackend(
            app_path=self.fixture["app_path"],
            support_dir=self.fixture["support_dir"],
            user_data_dir=self.fixture["user_data_dir"],
        )
        state = SessionState(
            workspace=str(self.root.resolve()),
            app_path=str(self.fixture["app_path"].resolve()),
            support_dir=str(self.fixture["support_dir"].resolve()),
            user_data_dir=str(self.fixture["user_data_dir"].resolve()),
        )
        app = AppContext(backend=backend, state=state, json_output=False)
        record = {"id": "fixture-active-session", "message_count": 0, "messages": []}
        view = InteractiveViewState(
            input_buffer="/",
            input_cursor=1,
            slash_palette_index=0,
        )

        with mock.patch(
            "cli_anything.trae.trae_cli.render_interactive_session",
            return_value=None,
        ):
            exited = process_alt_screen_key(app, record, view, "DOWN")

        self.assertFalse(exited)
        self.assertEqual(view.slash_palette_index, 1)

    def test_process_alt_screen_key_scrolls_chat_history_with_arrows(self) -> None:
        backend = TraeBackend(
            app_path=self.fixture["app_path"],
            support_dir=self.fixture["support_dir"],
            user_data_dir=self.fixture["user_data_dir"],
        )
        state = SessionState(
            workspace=str(self.root.resolve()),
            app_path=str(self.fixture["app_path"].resolve()),
            support_dir=str(self.fixture["support_dir"].resolve()),
            user_data_dir=str(self.fixture["user_data_dir"].resolve()),
        )
        app = AppContext(backend=backend, state=state, json_output=False)
        record = {
            "id": "fixture-active-session",
            "message_count": 10,
            "messages": [
                {"role": "user", "content": f"user line {index} " * 8}
                for index in range(1, 11)
            ],
        }
        view = InteractiveViewState(mode="chat", chat_scroll_offset=0)

        with mock.patch(
            "cli_anything.trae.trae_cli.terminal_screen_size",
            return_value=(100, 20),
        ):
            with mock.patch(
                "cli_anything.trae.trae_cli.render_interactive_session",
                return_value=None,
            ):
                exited = process_alt_screen_key(app, record, view, "UP")
                self.assertFalse(exited)
                self.assertEqual(view.chat_scroll_offset, 1)
                exited = process_alt_screen_key(app, record, view, "DOWN")

        self.assertFalse(exited)
        self.assertEqual(view.chat_scroll_offset, 0)

    def test_process_alt_screen_key_executes_selected_slash_command(self) -> None:
        backend = TraeBackend(
            app_path=self.fixture["app_path"],
            support_dir=self.fixture["support_dir"],
            user_data_dir=self.fixture["user_data_dir"],
        )
        state = SessionState(
            workspace=str(self.root.resolve()),
            app_path=str(self.fixture["app_path"].resolve()),
            support_dir=str(self.fixture["support_dir"].resolve()),
            user_data_dir=str(self.fixture["user_data_dir"].resolve()),
        )
        app = AppContext(backend=backend, state=state, json_output=False)
        record = {"id": "fixture-active-session", "message_count": 0, "messages": []}
        view = InteractiveViewState(
            input_buffer="/sess",
            input_cursor=5,
            slash_palette_index=0,
        )

        with mock.patch(
            "cli_anything.trae.trae_cli.process_repl_line",
            return_value=False,
        ) as mocked_process:
            exited = process_alt_screen_key(app, record, view, "ENTER")

        self.assertFalse(exited)
        mocked_process.assert_called_once_with(
            app,
            record,
            view,
            alt_screen=True,
            line="/sessions",
        )
        self.assertEqual(view.input_buffer, "")
        self.assertEqual(view.input_cursor, 0)

    def test_process_alt_screen_key_fills_selected_slash_setting_template(self) -> None:
        backend = TraeBackend(
            app_path=self.fixture["app_path"],
            support_dir=self.fixture["support_dir"],
            user_data_dir=self.fixture["user_data_dir"],
        )
        state = SessionState(
            workspace=str(self.root.resolve()),
            app_path=str(self.fixture["app_path"].resolve()),
            support_dir=str(self.fixture["support_dir"].resolve()),
            user_data_dir=str(self.fixture["user_data_dir"].resolve()),
        )
        app = AppContext(backend=backend, state=state, json_output=False)
        record = {"id": "fixture-active-session", "message_count": 0, "messages": []}
        view = InteractiveViewState(
            input_buffer="/set",
            input_cursor=4,
            slash_palette_index=0,
        )

        with mock.patch(
            "cli_anything.trae.trae_cli.render_interactive_session",
            return_value=None,
        ) as mocked_render:
            with mock.patch(
                "cli_anything.trae.trae_cli.process_repl_line",
                return_value=False,
            ) as mocked_process:
                exited = process_alt_screen_key(app, record, view, "ENTER")

        self.assertFalse(exited)
        mocked_render.assert_called_once()
        mocked_process.assert_not_called()
        self.assertEqual(view.input_buffer, "/set workspace ")
        self.assertEqual(view.input_cursor, len("/set workspace "))
        self.assertIn("请继续补全参数后回车", view.notice or "")

    def test_process_alt_screen_key_fills_model_switch_template(self) -> None:
        backend = TraeBackend(
            app_path=self.fixture["app_path"],
            support_dir=self.fixture["support_dir"],
            user_data_dir=self.fixture["user_data_dir"],
        )
        state = SessionState(
            workspace=str(self.root.resolve()),
            app_path=str(self.fixture["app_path"].resolve()),
            support_dir=str(self.fixture["support_dir"].resolve()),
            user_data_dir=str(self.fixture["user_data_dir"].resolve()),
        )
        app = AppContext(backend=backend, state=state, json_output=False)
        record = {"id": "fixture-active-session", "message_count": 0, "messages": []}
        view = InteractiveViewState(
            input_buffer="/model",
            input_cursor=len("/model"),
            slash_palette_index=1,
        )

        with mock.patch(
            "cli_anything.trae.trae_cli.render_interactive_session",
            return_value=None,
        ) as mocked_render:
            with mock.patch(
                "cli_anything.trae.trae_cli.process_repl_line",
                return_value=False,
            ) as mocked_process:
                exited = process_alt_screen_key(app, record, view, "ENTER")

        self.assertFalse(exited)
        mocked_render.assert_called_once()
        mocked_process.assert_not_called()
        self.assertEqual(view.input_buffer, "/model ")
        self.assertEqual(view.input_cursor, len("/model "))
        self.assertIn("请继续补全参数后回车", view.notice or "")

    def test_process_repl_line_init_creates_workspace_docs(self) -> None:
        backend = TraeBackend(
            app_path=self.fixture["app_path"],
            support_dir=self.fixture["support_dir"],
            user_data_dir=self.fixture["user_data_dir"],
        )
        state = SessionState(
            workspace=str(self.root.resolve()),
            app_path=str(self.fixture["app_path"].resolve()),
            support_dir=str(self.fixture["support_dir"].resolve()),
            user_data_dir=str(self.fixture["user_data_dir"].resolve()),
        )
        app = AppContext(backend=backend, state=state, json_output=False)
        record = {"id": "fixture-active-session", "message_count": 0, "messages": []}
        view = InteractiveViewState()

        with mock.patch(
            "cli_anything.trae.trae_cli.render_interactive_session",
            return_value=None,
        ):
            exited = process_repl_line(
                app,
                record,
                view,
                alt_screen=True,
                line="/init",
            )

        self.assertFalse(exited)
        self.assertEqual(view.output_title, "Init")
        self.assertIn("AGENTS.md: created", view.output_body)
        self.assertIn("memory.md: created", view.output_body)
        self.assertTrue((self.root / "AGENTS.md").exists())
        self.assertTrue((self.root / "memory.md").exists())
        self.assertIn("Record every project progress update", (self.root / "AGENTS.md").read_text())
        self.assertIn("Initialized workspace progress tracking", (self.root / "memory.md").read_text())

    def test_process_repl_line_status_shows_codex_like_summary(self) -> None:
        backend = TraeBackend(
            app_path=self.fixture["app_path"],
            support_dir=self.fixture["support_dir"],
            user_data_dir=self.fixture["user_data_dir"],
        )
        state = SessionState(
            workspace=str(self.root.resolve()),
            app_path=str(self.fixture["app_path"].resolve()),
            support_dir=str(self.fixture["support_dir"].resolve()),
            user_data_dir=str(self.fixture["user_data_dir"].resolve()),
            last_headless_session_id="fixture-hidden-session",
        )
        app = AppContext(backend=backend, state=state, json_output=False)
        record = {"id": "fixture-active-session", "message_count": 2, "messages": []}
        view = InteractiveViewState(mode="chat")

        with mock.patch(
            "cli_anything.trae.trae_cli.render_interactive_session",
            return_value=None,
        ):
            exited = process_repl_line(
                app,
                record,
                view,
                alt_screen=True,
                line="/status",
            )

        self.assertFalse(exited)
        self.assertEqual(view.output_title, "Status")
        self.assertIn(f"Workspace: {self.root.resolve()}", view.output_body)
        self.assertIn("Session: fixture-active-session", view.output_body)
        self.assertIn("Hidden session: fixture-hidden-session", view.output_body)
        self.assertIn("Model: GLM-4.7", view.output_body)

    def test_process_repl_line_model_shows_switchable_inventory(self) -> None:
        backend = TraeBackend(
            app_path=self.fixture["app_path"],
            support_dir=self.fixture["support_dir"],
            user_data_dir=self.fixture["user_data_dir"],
        )
        state = SessionState(
            workspace=str(self.root.resolve()),
            app_path=str(self.fixture["app_path"].resolve()),
            support_dir=str(self.fixture["support_dir"].resolve()),
            user_data_dir=str(self.fixture["user_data_dir"].resolve()),
        )
        app = AppContext(backend=backend, state=state, json_output=False)
        record = {"id": "fixture-active-session", "message_count": 0, "messages": []}
        view = InteractiveViewState(mode="chat")

        with mock.patch(
            "cli_anything.trae.trae_cli.render_interactive_session",
            return_value=None,
        ):
            exited = process_repl_line(
                app,
                record,
                view,
                alt_screen=True,
                line="/model",
            )

        self.assertFalse(exited)
        self.assertEqual(view.output_title, "Model")
        self.assertIn("Current models:", view.output_body)
        self.assertIn("Selectable models for dev_builder:", view.output_body)
        self.assertIn("GPT-5.3 Codex", view.output_body)
        self.assertIn("GLM-4.7", view.output_body)
        self.assertIn("Type `/model <name>` to switch the dev_builder model.", view.output_body)

    def test_process_repl_line_model_alias_switches_model(self) -> None:
        backend = TraeBackend(
            app_path=self.fixture["app_path"],
            support_dir=self.fixture["support_dir"],
            user_data_dir=self.fixture["user_data_dir"],
        )
        state = SessionState(
            workspace=str(self.root.resolve()),
            app_path=str(self.fixture["app_path"].resolve()),
            support_dir=str(self.fixture["support_dir"].resolve()),
            user_data_dir=str(self.fixture["user_data_dir"].resolve()),
        )
        app = AppContext(backend=backend, state=state, json_output=False)
        record = {"id": "fixture-active-session", "message_count": 0, "messages": []}
        view = InteractiveViewState(mode="chat")

        with mock.patch(
            "cli_anything.trae.trae_cli.render_interactive_session",
            return_value=None,
        ):
            exited = process_repl_line(
                app,
                record,
                view,
                alt_screen=True,
                line="/model GLM-4.7 --no-reload",
            )

        self.assertFalse(exited)
        self.assertEqual(view.output_title, "Model Switch")
        self.assertIn("Requested model: GLM-4.7", view.output_body)
        self.assertIn("Current model: GLM-4.7", view.output_body)
        self.assertIn("Status: switched", view.output_body)
        self.assertIn("Reload required: yes", view.output_body)
        current = backend.current_models(agent_type="dev_builder")
        self.assertEqual(
            current["current_models"]["dev_builder"]["model"]["display_name"],
            "GLM-4.7",
        )

    def test_run_interactive_prompt_records_working_and_run_events(self) -> None:
        backend = TraeBackend(
            app_path=self.fixture["app_path"],
            support_dir=self.fixture["support_dir"],
            user_data_dir=self.fixture["user_data_dir"],
        )
        state = SessionState(
            workspace=str(self.root.resolve()),
            app_path=str(self.fixture["app_path"].resolve()),
            support_dir=str(self.fixture["support_dir"].resolve()),
            user_data_dir=str(self.fixture["user_data_dir"].resolve()),
        )
        app = AppContext(backend=backend, state=state, json_output=False)
        record = {"id": "fixture-active-session", "message_count": 0, "messages": []}
        view = InteractiveViewState(mode="chat")
        payload = {
            "dispatch_method": "headless",
            "requested_dispatch_method": "auto",
            "dispatch_command": ["python3", "-m", "unittest", "sample", "-v"],
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
            "answer_text": "All tests passed.",
            "inspection": {
                "turn": {
                    "status": "completed",
                    "chat_model": "GLM-4.7",
                    "request_round_count": 2,
                    "progress_notice_count": 1,
                    "tool_run_count": 1,
                    "tool_runs": [
                        {
                            "command": "python3 -m unittest sample -v",
                            "exit_code": 0,
                            "result_log_excerpt": ["test_sample (suite) ... ok", "OK"],
                        }
                    ],
                }
            },
        }

        with mock.patch(
            "cli_anything.trae.trae_cli.execute_chat_request",
            return_value=payload,
        ) as mocked_execute:
            with mock.patch(
                "cli_anything.trae.trae_cli.should_use_alt_screen",
                return_value=True,
            ):
                with mock.patch(
                    "cli_anything.trae.trae_cli.render_interactive_session",
                    return_value=None,
                ) as mocked_render:
                    result = run_interactive_prompt(
                        app,
                        record,
                        "run tests",
                        view=view,
                    )

        self.assertEqual(result, "All tests passed.")
        mocked_execute.assert_called_once()
        self.assertGreaterEqual(mocked_render.call_count, 1)
        self.assertEqual(record["messages"][0]["role"], "user")
        self.assertEqual(record["messages"][0]["content"], "run tests")
        self.assertEqual(record["messages"][1]["role"], "event")
        self.assertTrue(str(record["messages"][1]["headline"]).startswith("Completed in "))
        ran_event = next(
            item for item in record["messages"] if item.get("role") == "event" and "Ran `" in str(item.get("headline"))
        )
        self.assertIn("python3 -m unittest sample -v", ran_event["headline"])
        self.assertEqual(record["messages"][-1]["role"], "assistant")
        self.assertEqual(record["messages"][-1]["content"], "All tests passed.")
        self.assertEqual(view.notice, "completed via headless")

    def test_process_repl_line_diff_shows_mocked_repo_summary(self) -> None:
        backend = TraeBackend(
            app_path=self.fixture["app_path"],
            support_dir=self.fixture["support_dir"],
            user_data_dir=self.fixture["user_data_dir"],
        )
        state = SessionState(
            workspace=str(self.root.resolve()),
            app_path=str(self.fixture["app_path"].resolve()),
            support_dir=str(self.fixture["support_dir"].resolve()),
            user_data_dir=str(self.fixture["user_data_dir"].resolve()),
        )
        app = AppContext(backend=backend, state=state, json_output=False)
        record = {"id": "fixture-active-session", "message_count": 0, "messages": []}
        view = InteractiveViewState()

        with mock.patch(
            "cli_anything.trae.trae_cli.collect_workspace_diff_payload",
            return_value={
                "workspace": str(self.root.resolve()),
                "repo_root": str(self.root.resolve()),
                "branch": "main",
                "is_repo": True,
                "has_changes": True,
                "status_lines": [" M cli.py", "?? memory.md"],
                "status_remaining": 0,
                "staged_count": 0,
                "unstaged_count": 1,
                "untracked_count": 1,
                "staged_stat": "",
                "unstaged_stat": " cli.py | 4 +++-",
            },
        ):
            with mock.patch(
                "cli_anything.trae.trae_cli.render_interactive_session",
                return_value=None,
            ):
                exited = process_repl_line(
                    app,
                    record,
                    view,
                    alt_screen=True,
                    line="/diff",
                )

        self.assertFalse(exited)
        self.assertEqual(view.output_title, "Diff")
        self.assertIn("Working tree: dirty", view.output_body)
        self.assertIn("Branch: main", view.output_body)
        self.assertIn("?? memory.md", view.output_body)

    def test_process_repl_line_approval_alias_shows_trust_context(self) -> None:
        backend = TraeBackend(
            app_path=self.fixture["app_path"],
            support_dir=self.fixture["support_dir"],
            user_data_dir=self.fixture["user_data_dir"],
        )
        state = SessionState(
            workspace=str(self.root.resolve()),
            app_path=str(self.fixture["app_path"].resolve()),
            support_dir=str(self.fixture["support_dir"].resolve()),
            user_data_dir=str(self.fixture["user_data_dir"].resolve()),
        )
        app = AppContext(
            backend=backend,
            state=state,
            json_output=False,
            auto_trust_workspace=True,
        )
        record = {"id": "fixture-active-session", "message_count": 0, "messages": []}
        view = InteractiveViewState()

        with mock.patch(
            "cli_anything.trae.trae_cli.render_interactive_session",
            return_value=None,
        ):
            exited = process_repl_line(
                app,
                record,
                view,
                alt_screen=True,
                line="/approval",
            )

        self.assertFalse(exited)
        self.assertEqual(view.output_title, "Approvals")
        self.assertIn("Per-command approvals: unavailable in traecli", view.output_body)
        self.assertIn("Auto trust flag: on", view.output_body)

    def test_rpc_transport_json(self) -> None:
        result = self.invoke("--json", "rpc", "transport")
        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["app_rpc"]["client_connected_service"], "ai-agent")
        self.assertEqual(payload["app_rpc"]["service_name"], "ai-agent")
        self.assertEqual(
            payload["app_rpc"]["local_impl"]["client_api"],
            "electron.ahaIpc.connect",
        )
        self.assertEqual(
            payload["app_rpc"]["node_socket_rule"]["socket_path"],
            "/tmp/aha/ai-agent.sock",
        )
        self.assertEqual(payload["sample_request"]["service"], "healthcheck")
        self.assertEqual(
            payload["sample_request"]["matched_ai_agent_request"]["method"],
            "ping",
        )
        self.assertEqual(payload["ckg"]["port"], 51002)
        self.assertEqual(payload["oauth_callback"]["port"], 17790)

    def test_rpc_request_json(self) -> None:
        mocked_payload = {
            "service_name": "ai-agent",
            "runtime_dir": "/tmp",
            "request_method": "request",
            "envelope": {
                "packet_type": "request",
                "session_id": "",
                "channel_id": "fixture-channel",
                "params": {
                    "service": "healthcheck",
                    "method": "ping",
                    "data": "",
                    "common_params": {},
                    "user_info": {
                        "name": "",
                        "token": "fixture-jwt-token",
                        "region": "",
                        "is_internal": False,
                        "user_id": "",
                        "scope": "",
                    },
                    "streamlined_common_params": {},
                    "client_info": {"connect_session_id": ""},
                },
            },
            "response": {
                "message": "success",
                "code": 0,
                "data": {"message": "pong"},
            },
        }

        with mock.patch(
            "cli_anything.trae.utils.trae_backend.TraeBackend.invoke_aha_rpc",
            return_value=mocked_payload,
        ):
            result = self.invoke(
                "--json",
                "rpc",
                "request",
                "healthcheck",
                "ping",
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["response"]["code"], 0)
        self.assertEqual(payload["response"]["data"]["message"], "pong")
        self.assertEqual(payload["envelope"]["params"]["service"], "healthcheck")
        self.assertEqual(payload["envelope"]["params"]["method"], "ping")
        self.assertEqual(payload["envelope"]["params"]["user_info"]["token"], "[redacted]")
        self.assertNotIn("fixture-jwt-token", result.output)

    def test_rpc_request_json_accepts_client_info_json(self) -> None:
        mocked_payload = {
            "service_name": "ai-agent",
            "runtime_dir": "/tmp",
            "request_method": "request",
            "envelope": {
                "packet_type": "request",
                "session_id": "",
                "channel_id": "fixture-channel",
                "params": {
                    "service": "chat",
                    "method": "chat",
                    "data": {"message": "fixture"},
                    "common_params": {},
                    "user_info": {
                        "name": "",
                        "token": "fixture-jwt-token",
                        "region": "",
                        "is_internal": False,
                        "user_id": "",
                        "scope": "",
                    },
                    "streamlined_common_params": {},
                    "client_info": {
                        "connect_session_id": "",
                        "project_id": "fixture-project-id",
                    },
                },
            },
            "response": {
                "message": "success",
                "code": 0,
                "data": {"event": "metadata"},
            },
        }

        with mock.patch(
            "cli_anything.trae.utils.trae_backend.TraeBackend.invoke_aha_rpc",
            return_value=mocked_payload,
        ) as mocked_rpc:
            result = self.invoke(
                "--json",
                "rpc",
                "request",
                "chat",
                "chat",
                "--data-json",
                '{"message":"fixture"}',
                "--client-info-json",
                '{"project_id":"fixture-project-id"}',
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(
            payload["envelope"]["params"]["client_info"]["project_id"],
            "fixture-project-id",
        )
        self.assertEqual(
            mocked_rpc.call_args.kwargs["client_info"],
            {"project_id": "fixture-project-id"},
        )

    def test_rpc_chat_json(self) -> None:
        mocked_payload = {
            "workspace": str(self.root.resolve()),
            "project_id": "fixture-project-id",
            "session_id": "fixture-session-id",
            "session_type": "inline_chat",
            "agent_type": "inline_chat",
            "client_info": {
                "connect_session_id": "",
                "project_id": "fixture-project-id",
            },
            "message_id": "fixture-message-id",
            "chat_data": {
                "agent_type": "inline_chat",
                "message_id": "fixture-message-id",
            },
            "chat_response": {"message": "success", "code": 0},
            "messages_response": {"message": "success", "code": 0},
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
                "user_message_context": {
                    "model_info": {
                        "encrypted_model_params": "fixture-secret-payload",
                        "session_token": "fixture-session-token",
                    }
                },
            },
            "answer_text": "fixture rpc answer",
            "answer_source": "message.content",
        }

        with mock.patch(
            "cli_anything.trae.utils.trae_backend.TraeBackend.send_rpc_chat",
            return_value=mocked_payload,
        ) as mocked_chat:
            result = self.invoke(
                "--json",
                "rpc",
                "chat",
                "收到回复我",
                "--client-info-json",
                '{"workspace_folder":"/tmp/project"}',
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["project_id"], "fixture-project-id")
        self.assertEqual(payload["session_id"], "fixture-session-id")
        self.assertEqual(payload["answer_text"], "fixture rpc answer")
        self.assertEqual(payload["assistant_status"], "completed")
        self.assertEqual(
            payload["latest_assistant_message"]["user_message_context"]["model_info"][
                "encrypted_model_params"
            ],
            "[redacted]",
        )
        self.assertEqual(
            payload["latest_assistant_message"]["user_message_context"]["model_info"][
                "session_token"
            ],
            "[redacted]",
        )
        self.assertNotIn("fixture-secret-payload", result.output)
        self.assertNotIn("fixture-session-token", result.output)
        self.assertEqual(
            mocked_chat.call_args.kwargs["client_info"],
            {"workspace_folder": "/tmp/project"},
        )

    def test_rpc_chat_passes_agent_type_option(self) -> None:
        mocked_payload = {
            "workspace": str(self.root.resolve()),
            "project_id": "fixture-project-id",
            "session_id": "fixture-session-id",
            "session_type": "side_chat",
            "agent_type": "builder_v3",
            "client_info": {
                "connect_session_id": "",
                "project_id": "fixture-project-id",
            },
            "message_id": "fixture-message-id",
            "chat_data": {
                "agent_type": "builder_v3",
                "message_id": "fixture-message-id",
            },
            "chat_response": {"message": "success", "code": 0},
            "messages_response": {"message": "success", "code": 0},
            "messages": [],
            "message_count": 0,
            "assistant_status": "completed",
            "latest_assistant_message": None,
            "answer_text": None,
            "answer_source": None,
        }

        with mock.patch(
            "cli_anything.trae.utils.trae_backend.TraeBackend.send_rpc_chat",
            return_value=mocked_payload,
        ) as mocked_chat:
            result = self.invoke(
                "--json",
                "rpc",
                "chat",
                "收到回复我",
                "--session-type",
                "side_chat",
                "--agent-type",
                "builder_v3",
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(mocked_chat.call_args.kwargs["agent_type"], "builder_v3")

    def test_rpc_export_chat_json(self) -> None:
        mocked_payload = {
            "session_id": "fixture-session-id",
            "connect_session_id": "fixture-connect",
            "connect_session_source": "logs",
            "guessed_connect_session": {"trace_id": "fixture-ok-trace"},
            "export_path": str(self.root / "fixture-chat.md"),
            "content": "# fixture export\n\nhello world\n",
            "request": {"response": {"message": "success", "code": 0}},
            "response": {"message": "success", "code": 0},
        }

        with mock.patch(
            "cli_anything.trae.utils.trae_backend.TraeBackend.export_chat_session",
            return_value=mocked_payload,
        ):
            result = self.invoke(
                "--json",
                "rpc",
                "export-chat",
                "fixture-session-id",
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["session_id"], "fixture-session-id")
        self.assertEqual(payload["connect_session_id"], "fixture-connect")
        self.assertIn("hello world", payload["content"])

    def test_rpc_transport_live_json(self) -> None:
        bundle_path = self.fixture["app_path"]
        ps_output = "\n".join(
            [
                f"18156 1 {bundle_path}/Contents/MacOS/Trae",
                f"18164 18156 {bundle_path}/Contents/Frameworks/Trae Helper (Plugin).app/Contents/MacOS/Trae Helper (Plugin) --vscode-crash-reporter-process-type=ai",
                f"18165 18156 {bundle_path}/Contents/Frameworks/Trae Helper (Plugin).app/Contents/MacOS/Trae Helper (Plugin) --vscode-crash-reporter-process-type=ckg",
            ]
        )
        unix_outputs = {
            18164: "f18\0tunix\0n->0xa1\0",
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

        with mock.patch(
            "cli_anything.trae.utils.trae_backend.TraeBackend._command_available",
            return_value=True,
        ):
            with mock.patch(
                "cli_anything.trae.utils.trae_backend.TraeBackend._run_probe_command",
                side_effect=fake_probe,
            ):
                result = self.invoke("--json", "rpc", "transport", "--live")

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertTrue(payload["live"]["ps_available"])
        self.assertEqual(payload["live"]["processes"][1]["role"], "ai-helper")
        self.assertEqual(
            payload["live"]["processes"][1]["sockets"]["anonymous_unix_socket_count"], 1
        )
        self.assertEqual(
            payload["live"]["processes"][2]["sockets"]["tcp_listeners"],
            ["127.0.0.1:51002"],
        )
        self.assertTrue(
            any("ckg-helper pid 18165 is listening on 127.0.0.1:51002" in note for note in payload["live"]["notes"])
        )


if __name__ == "__main__":
    unittest.main()
