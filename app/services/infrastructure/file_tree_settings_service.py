from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pydantic import ValidationError

from app.core.resource_uri import parse_resource_uri
from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.schemas.internal_v2.workspace import (
    FileTreeShortcutDTO,
    FileTreeShortcutSource,
    SessionFileTreeSettingsDTO,
)
from app.services.infrastructure.workspace_files.paths import WorkspaceFilePaths


class FileTreeSettingsService:
    """持久化会话级快捷路径，并显式管理工作区默认值。"""

    def __init__(
        self,
        *,
        workspace_root: Path,
        path_resolver: SessionCatalogPathResolver,
        file_paths: WorkspaceFilePaths,
    ) -> None:
        self._workspace_root = workspace_root.resolve()
        self._path_resolver = path_resolver
        self._file_paths = file_paths
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
        if self._exists(settings_path):
            raise FileExistsError(f"新会话快捷路径快照已存在: session_id={session_id}, artifact=file_tree_shortcuts")
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
            settings_path = (
                self._path_resolver.resolve_session_node(node.node_id)
                / "ui"
                / "file-tree-shortcuts.json"
            )
            if self._exists(settings_path):
                raw = self._read_settings_payload(settings_path)
                if raw.get("version") == 2:
                    continue
                shortcuts = self._parse_shortcuts(
                    raw.get("shortcuts"), source="session"
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

    def _normalize_path(self, path: str) -> str:
        return self._file_paths.resolve(path, allow_empty=False)[1]

    def _normalize_directory(self, path: str) -> str:
        normalized = self._normalize_path(path)
        candidate = self._file_paths.native(normalized)
        with self._access("directory_stat", uri=normalized):
            exists = candidate.exists()
            is_directory = candidate.is_dir() if exists else False
        if not exists:
            raise FileNotFoundError(f"快捷路径不存在: {normalized}")
        if not is_directory:
            raise NotADirectoryError(f"快捷路径不是目录: {normalized}")
        return normalized

    @staticmethod
    def _default_label(path: str) -> str:
        parsed = parse_resource_uri(path)
        return parsed.tail_segments[-1] if len(parsed.tail_segments) > 1 else "目录"

    @classmethod
    def _upsert(
        cls,
        shortcuts: list[FileTreeShortcutDTO],
        path: str,
        label: str | None,
        *,
        source: FileTreeShortcutSource,
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

    def _read_shortcuts(
        self,
        path: Path,
        *,
        source: FileTreeShortcutSource,
    ) -> list[FileTreeShortcutDTO]:
        if not self._exists(path):
            return []
        raw = self._read_settings_payload(path)
        return self._parse_shortcuts(
            raw.get("shortcuts"), source=source
        )

    def _read_session_settings(
        self,
        path: Path,
    ) -> tuple[list[FileTreeShortcutDTO], list[FileTreeShortcutDTO]]:
        if not self._exists(path):
            raise FileNotFoundError("会话快捷路径快照不存在: artifact=file_tree_shortcuts")
        raw = self._read_settings_payload(path)
        if raw.get("version") != 2:
            raise ValueError("会话快捷路径快照版本错误: artifact=file_tree_shortcuts")
        return (
            self._parse_shortcuts(
                raw.get("shortcuts"), source="session"
            ),
            self._parse_shortcuts(
                raw.get("workspace_shortcuts"), source="workspace"
            ),
        )

    def _read_settings_payload(self, path: Path) -> dict[str, object]:
        with self._access("read"):
            raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise TypeError("文件树快捷路径配置必须为对象: artifact=file_tree_shortcuts")
        return raw

    def _parse_shortcuts(
        self,
        raw: object,
        *,
        source: FileTreeShortcutSource,
    ) -> list[FileTreeShortcutDTO]:
        if not isinstance(raw, list):
            raise TypeError("文件树快捷路径配置必须为列表: artifact=file_tree_shortcuts")
        if any(not isinstance(item, dict) for item in raw):
            raise TypeError("文件树快捷路径配置项必须为对象: artifact=file_tree_shortcuts")
        with self._access("validate"):
            return [
                FileTreeShortcutDTO.model_validate({**item, "source": source})
                for item in raw
            ]

    def _write_shortcuts(
        self,
        path: Path,
        shortcuts: list[FileTreeShortcutDTO],
    ) -> None:
        payload = {
            "version": 1,
            "shortcuts": [
                {"path": item.path, "label": item.label} for item in shortcuts
            ],
        }
        self._write_payload(path, payload)

    def _write_session_settings(
        self,
        path: Path,
        session_shortcuts: list[FileTreeShortcutDTO],
        workspace_shortcuts: list[FileTreeShortcutDTO],
    ) -> None:
        payload = {
            "version": 2,
            "shortcuts": [
                {"path": item.path, "label": item.label} for item in session_shortcuts
            ],
            "workspace_shortcuts": [
                {"path": item.path, "label": item.label} for item in workspace_shortcuts
            ],
        }
        self._write_payload(path, payload)

    def _write_payload(self, path: Path, payload: dict[str, object]) -> None:
        with self._access("write"):
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

    def _exists(self, path: Path) -> bool:
        with self._access("stat"):
            return path.exists()

    @contextmanager
    def _access(self, stage: str, *, uri: str | None = None) -> Iterator[None]:
        """仅在存储边界发布稳定诊断，物理位置和原始校验值保留在原因链。"""
        try:
            yield
        except (OSError, ValidationError, json.JSONDecodeError, UnicodeDecodeError) as error:
            detail = (
                f"workspace_id={self._file_paths.workspace_id}, artifact=file_tree_shortcuts, "
                f"stage={stage}, error={type(error).__name__}"
            )
            if uri is not None:
                detail += f", uri={uri}"
            if isinstance(error, OSError):
                raise type(error)(error.errno, f"{detail}, errno={error.errno}") from error
            if isinstance(error, ValidationError):
                rules = sorted({item["type"] for item in error.errors(include_input=False)})
                detail += f", error_count={error.error_count()}, rules={','.join(rules)}"
            elif isinstance(error, json.JSONDecodeError):
                detail += f", line={error.lineno}, column={error.colno}"
            else:
                detail += f", start={error.start}, end={error.end}"
            raise ValueError(detail) from error
