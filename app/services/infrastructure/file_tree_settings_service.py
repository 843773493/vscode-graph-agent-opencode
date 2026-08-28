from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Literal

from app.core.session_paths import SessionPathResolver
from app.schemas.internal_v2.workspace import (
    FileTreeShortcutDTO,
    SessionFileTreeSettingsDTO,
)


class FileTreeSettingsService:
    """持久化会话级快捷路径，并显式管理工作区默认值。"""

    def __init__(
        self,
        *,
        workspace_root: Path,
        path_resolver: SessionPathResolver,
    ) -> None:
        self._workspace_root = workspace_root.resolve()
        self._path_resolver = path_resolver
        self._workspace_settings_path = (
            self._workspace_root / ".boxteam" / "ui" / "file-tree-shortcuts.json"
        )
        self._snapshot_defaults_for_existing_sessions()

    def get(self, session_id: str) -> SessionFileTreeSettingsDTO:
        session_path = self._session_settings_path(session_id)
        session_shortcuts, workspace_shortcuts = self._read_session_settings(
            session_path
        )
        default_shortcuts = self._read_shortcuts(
            self._workspace_settings_path, source="workspace"
        )
        effective_by_path = {
            shortcut.path: shortcut for shortcut in workspace_shortcuts
        }
        for shortcut in session_shortcuts:
            effective_by_path.setdefault(shortcut.path, shortcut)
        return SessionFileTreeSettingsDTO(
            session_id=session_id,
            session_shortcuts=session_shortcuts,
            workspace_shortcuts=workspace_shortcuts,
            default_shortcuts=default_shortcuts,
            effective_shortcuts=list(effective_by_path.values()),
        )

    def handle_session_change(self, action: str, session_id: str) -> None:
        if action == "create":
            self.initialize_session(session_id)

    def initialize_session(self, session_id: str) -> None:
        settings_path = self._session_settings_path(session_id)
        if settings_path.exists():
            raise FileExistsError(f"新会话快捷路径快照已存在: {settings_path}")
        defaults = self._read_shortcuts(
            self._workspace_settings_path, source="workspace"
        )
        self._write_session_settings(settings_path, [], defaults)

    def add_session_shortcut(
        self,
        session_id: str,
        *,
        path: str,
        label: str | None,
    ) -> SessionFileTreeSettingsDTO:
        normalized_path = self._normalize_directory(path)
        settings_path = self._session_settings_path(session_id)
        shortcuts, inherited = self._read_session_settings(settings_path)
        self._write_session_settings(
            settings_path,
            self._upsert(shortcuts, normalized_path, label, source="session"),
            inherited,
        )
        return self.get(session_id)

    def remove_session_shortcut(
        self,
        session_id: str,
        *,
        path: str,
    ) -> SessionFileTreeSettingsDTO:
        normalized_path = self._normalize_path(path)
        settings_path = self._session_settings_path(session_id)
        shortcuts, inherited = self._read_session_settings(settings_path)
        remaining_shortcuts = [
            item for item in shortcuts if item.path != normalized_path
        ]
        remaining_inherited = [
            item for item in inherited if item.path != normalized_path
        ]
        if len(remaining_shortcuts) == len(shortcuts) and len(
            remaining_inherited
        ) == len(inherited):
            raise KeyError(f"会话快捷路径不存在: {normalized_path}")
        self._write_session_settings(
            settings_path,
            remaining_shortcuts,
            remaining_inherited,
        )
        return self.get(session_id)

    def apply_to_workspace(
        self,
        session_id: str,
        *,
        path: str,
        label: str | None = None,
    ) -> SessionFileTreeSettingsDTO:
        normalized_path = self._normalize_directory(path)
        session_settings = self.get(session_id)
        source = next(
            (
                item
                for item in session_settings.effective_shortcuts
                if item.path == normalized_path
            ),
            None,
        )
        workspace_shortcuts = self._read_shortcuts(
            self._workspace_settings_path,
            source="workspace",
        )
        self._write_shortcuts(
            self._workspace_settings_path,
            self._upsert(
                workspace_shortcuts,
                normalized_path,
                source.label if source is not None else label,
                source="workspace",
            ),
        )
        return self.get(session_id)

    def remove_workspace_shortcut(
        self,
        session_id: str,
        *,
        path: str,
    ) -> SessionFileTreeSettingsDTO:
        normalized_path = self._normalize_path(path)
        shortcuts = self._read_shortcuts(
            self._workspace_settings_path,
            source="workspace",
        )
        remaining = [item for item in shortcuts if item.path != normalized_path]
        if len(remaining) == len(shortcuts):
            raise KeyError(f"工作区快捷路径不存在: {normalized_path}")
        self._write_shortcuts(self._workspace_settings_path, remaining)
        return self.get(session_id)

    def _snapshot_defaults_for_existing_sessions(self) -> None:
        defaults = self._read_shortcuts(
            self._workspace_settings_path, source="workspace"
        )
        for node in self._path_resolver.list_nodes():
            if node.kind != "session":
                continue
            settings_path = node.path / "ui" / "file-tree-shortcuts.json"
            if settings_path.exists():
                raw = self._read_settings_payload(settings_path)
                if raw.get("version") == 2:
                    continue
                shortcuts = self._parse_shortcuts(
                    raw.get("shortcuts"), source="session", path=settings_path
                )
            else:
                shortcuts = []
            self._write_session_settings(settings_path, shortcuts, defaults)

    def _session_settings_path(self, session_id: str) -> Path:
        try:
            session_node = self._path_resolver.resolve_session_node(session_id)
        except KeyError as error:
            raise KeyError(f"会话不存在: {session_id}") from error
        return session_node / "ui" / "file-tree-shortcuts.json"

    @staticmethod
    def _normalize_path(path: str) -> str:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            raise ValueError(f"快捷路径必须是绝对路径: {path}")
        return str(candidate.resolve())

    def _normalize_directory(self, path: str) -> str:
        normalized = self._normalize_path(path)
        candidate = Path(normalized)
        if not candidate.exists():
            raise FileNotFoundError(f"快捷路径不存在: {normalized}")
        if not candidate.is_dir():
            raise NotADirectoryError(f"快捷路径不是目录: {normalized}")
        return normalized

    @staticmethod
    def _default_label(path: str) -> str:
        candidate = Path(path)
        return candidate.name or candidate.anchor or path

    @classmethod
    def _upsert(
        cls,
        shortcuts: list[FileTreeShortcutDTO],
        path: str,
        label: str | None,
        *,
        source: Literal["session", "workspace"],
    ) -> list[FileTreeShortcutDTO]:
        normalized_label = (label or "").strip() or cls._default_label(path)
        updated = [item for item in shortcuts if item.path != path]
        updated.append(
            FileTreeShortcutDTO(
                path=path,
                label=normalized_label,
                source=source,
            )
        )
        return updated

    @staticmethod
    def _read_shortcuts(
        path: Path,
        *,
        source: Literal["session", "workspace"],
    ) -> list[FileTreeShortcutDTO]:
        if not path.exists():
            return []
        raw = FileTreeSettingsService._read_settings_payload(path)
        return FileTreeSettingsService._parse_shortcuts(
            raw.get("shortcuts"), source=source, path=path
        )

    @staticmethod
    def _read_session_settings(
        path: Path,
    ) -> tuple[list[FileTreeShortcutDTO], list[FileTreeShortcutDTO]]:
        if not path.exists():
            raise FileNotFoundError(f"会话快捷路径快照不存在: {path}")
        raw = FileTreeSettingsService._read_settings_payload(path)
        if raw.get("version") != 2:
            raise ValueError(f"会话快捷路径快照版本错误: {path}")
        return (
            FileTreeSettingsService._parse_shortcuts(
                raw.get("shortcuts"), source="session", path=path
            ),
            FileTreeSettingsService._parse_shortcuts(
                raw.get("workspace_shortcuts"), source="workspace", path=path
            ),
        )

    @staticmethod
    def _read_settings_payload(path: Path) -> dict[str, object]:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise TypeError(f"文件树快捷路径配置格式错误: {path}")
        return raw

    @staticmethod
    def _parse_shortcuts(
        raw: object,
        *,
        source: Literal["session", "workspace"],
        path: Path,
    ) -> list[FileTreeShortcutDTO]:
        if not isinstance(raw, list):
            raise TypeError(f"文件树快捷路径配置格式错误: {path}")
        if any(not isinstance(item, dict) for item in raw):
            raise TypeError(f"文件树快捷路径配置项格式错误: {path}")
        return [
            FileTreeShortcutDTO.model_validate({**item, "source": source})
            for item in raw
        ]

    @staticmethod
    def _write_shortcuts(
        path: Path,
        shortcuts: list[FileTreeShortcutDTO],
    ) -> None:
        payload = {
            "version": 1,
            "shortcuts": [
                {"path": item.path, "label": item.label} for item in shortcuts
            ],
        }
        FileTreeSettingsService._write_payload(path, payload)

    @staticmethod
    def _write_session_settings(
        path: Path,
        session_shortcuts: list[FileTreeShortcutDTO],
        workspace_shortcuts: list[FileTreeShortcutDTO],
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 2,
            "shortcuts": [
                {"path": item.path, "label": item.label} for item in session_shortcuts
            ],
            "workspace_shortcuts": [
                {"path": item.path, "label": item.label} for item in workspace_shortcuts
            ],
        }
        FileTreeSettingsService._write_payload(path, payload)

    @staticmethod
    def _write_payload(path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = temporary.name
                json.dump(payload, temporary, ensure_ascii=False, indent=2)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, path)
            temporary_path = None
        finally:
            if temporary_path is not None and os.path.exists(temporary_path):
                os.unlink(temporary_path)
