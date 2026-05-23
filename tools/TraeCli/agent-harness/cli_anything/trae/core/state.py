from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class SessionSnapshot:
    workspace: Optional[str] = None
    app_path: Optional[str] = None
    support_dir: Optional[str] = None
    user_data_dir: Optional[str] = None
    json_output: bool = False
    last_headless_session_id: Optional[str] = None
    current_cli_session_id: Optional[str] = None
    current_cdp_target_id: Optional[str] = None


@dataclass
class SessionState:
    workspace: Optional[str] = None
    app_path: Optional[str] = None
    support_dir: Optional[str] = None
    user_data_dir: Optional[str] = None
    json_output: bool = False
    last_headless_session_id: Optional[str] = None
    current_cli_session_id: Optional[str] = None
    current_cdp_target_id: Optional[str] = None
    last_backend_command: list[str] = field(default_factory=list)
    _undo_stack: list[SessionSnapshot] = field(default_factory=list)
    _redo_stack: list[SessionSnapshot] = field(default_factory=list)

    def snapshot(self) -> SessionSnapshot:
        return SessionSnapshot(
            workspace=self.workspace,
            app_path=self.app_path,
            support_dir=self.support_dir,
            user_data_dir=self.user_data_dir,
            json_output=self.json_output,
            last_headless_session_id=self.last_headless_session_id,
            current_cli_session_id=self.current_cli_session_id,
            current_cdp_target_id=self.current_cdp_target_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self.snapshot()),
            "last_backend_command": list(self.last_backend_command),
            "undo_depth": len(self._undo_stack),
            "redo_depth": len(self._redo_stack),
        }

    def apply(self, snapshot: SessionSnapshot) -> None:
        self.workspace = snapshot.workspace
        self.app_path = snapshot.app_path
        self.support_dir = snapshot.support_dir
        self.user_data_dir = snapshot.user_data_dir
        self.json_output = snapshot.json_output
        self.last_headless_session_id = snapshot.last_headless_session_id
        self.current_cli_session_id = snapshot.current_cli_session_id
        self.current_cdp_target_id = snapshot.current_cdp_target_id

    def _push_undo(self) -> None:
        self._undo_stack.append(self.snapshot())
        self._redo_stack.clear()

    def set_workspace(self, workspace: str) -> str:
        self._push_undo()
        self.workspace = str(Path(workspace).expanduser().resolve())
        self.current_cdp_target_id = None
        return self.workspace

    def set_paths(
        self,
        *,
        app_path: Optional[str] = None,
        support_dir: Optional[str] = None,
        user_data_dir: Optional[str] = None,
    ) -> None:
        self._push_undo()
        if app_path is not None:
            self.app_path = str(Path(app_path).expanduser().resolve())
        if support_dir is not None:
            self.support_dir = str(Path(support_dir).expanduser().resolve())
        if user_data_dir is not None:
            self.user_data_dir = str(Path(user_data_dir).expanduser().resolve())
        self.current_cdp_target_id = None

    def note_backend_command(self, command: list[str]) -> None:
        self.last_backend_command = list(command)

    def undo(self) -> bool:
        if not self._undo_stack:
            return False
        self._redo_stack.append(self.snapshot())
        self.apply(self._undo_stack.pop())
        return True

    def redo(self) -> bool:
        if not self._redo_stack:
            return False
        self._undo_stack.append(self.snapshot())
        self.apply(self._redo_stack.pop())
        return True
