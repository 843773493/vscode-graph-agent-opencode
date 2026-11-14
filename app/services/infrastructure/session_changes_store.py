from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path
from typing import Literal

from app.abstractions.session_changes import StoredFileEdit
from app.core.atomic_fs import native_io_path
from app.core.path_utils import SafePathRoot, get_session_path_resolver
from app.core.trace_middleware import get_current_gateway_id
from app.schemas.internal_v2.session_changes import validate_change_file_reference

EDIT_INDEX_FILE = "index.jsonl"
REVIEWED_FILE = "reviewed.json"


class SessionChangesStore:
    """在工作区 `.boxteam` 内读写可人工检查的会话变更文件。"""

    def __init__(self, *, workspace_root: Path) -> None:
        self._workspace_root = workspace_root.resolve()
        self._path_resolver = get_session_path_resolver(
            self._workspace_root / ".boxteam" / "sessions"
        )

    def normalize_file_reference(self, file_path: str) -> str:
        return validate_change_file_reference(file_path, local_gateway_id=get_current_gateway_id())

    def save_edit(
        self,
        *,
        record: StoredFileEdit,
        before_content: str,
        after_content: str,
        diff_text: str,
    ) -> None:
        record = replace(record, file_path=self.normalize_file_reference(record.file_path))
        native_edit_dir = native_io_path(
            self._edit_dir(record.session_id, record.edit_id)
        )
        changes_dir = self._ensure_changes_dir(record.session_id)
        native_edit_dir.mkdir(parents=True, exist_ok=False)
        if record.before_exists:
            (native_edit_dir / "before.txt").write_text(before_content, encoding="utf-8")
        if record.after_exists:
            (native_edit_dir / "after.txt").write_text(after_content, encoding="utf-8")
        (native_edit_dir / "diff.patch").write_text(diff_text, encoding="utf-8")
        metadata = asdict(record)
        (native_edit_dir / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        index_file = native_io_path(changes_dir / EDIT_INDEX_FILE)
        with index_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(metadata, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")

    def read_records(self, session_id: str) -> list[StoredFileEdit]:
        changes_root = SafePathRoot(self._session_changes_dir(session_id))
        index_file = native_io_path(changes_root.path / EDIT_INDEX_FILE)
        try:
            handle = index_file.open("r", encoding="utf-8")
        except FileNotFoundError:
            return []
        records: list[StoredFileEdit] = []
        with handle:
            for line_number, line in enumerate(handle, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    data = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(
                        f"文件变更索引损坏: session_id={session_id}, line={line_number}"
                    ) from exc
                record = StoredFileEdit(**data)
                self._edit_path_in(changes_root, record.edit_id)
                if record.session_id != session_id:
                    raise ValueError(f"文件变更索引归属错误: session_id={session_id}, line={line_number}")
                records.append(replace(record, file_path=self.normalize_file_reference(record.file_path)))
        return records

    def read_reviewed_map(self, session_id: str) -> dict[str, bool]:
        reviewed_file = native_io_path(
            self._session_changes_dir(session_id) / REVIEWED_FILE
        )
        try:
            raw = json.loads(reviewed_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"文件审查状态损坏: session_id={session_id}") from exc
        if not isinstance(raw, dict):
            raise TypeError(f"文件审查状态格式错误: session_id={session_id}")
        return self._reviewed_references(raw)

    def _reviewed_references(self, reviewed: dict[str, bool]) -> dict[str, bool]:
        normalized: dict[str, bool] = {}
        for key, value in reviewed.items():
            uri = self.normalize_file_reference(key)
            if type(value) is not bool:
                raise TypeError("文件审查状态必须是布尔值")
            if uri in normalized:
                raise ValueError("文件审查状态含重复的规范 URI")
            normalized[uri] = value
        return normalized

    def save_reviewed_map(self, session_id: str, reviewed: dict[str, bool]) -> None:
        reviewed = self._reviewed_references(reviewed)
        target = self._ensure_changes_dir(session_id) / REVIEWED_FILE
        native_io_path(target).write_text(
            json.dumps(reviewed, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def edit_text_reader(self, session_id: str) -> Callable[[str, Literal["before", "after", "diff"]], str]:
        """在调用方的同一次会话读准入内复用 locator，不跨请求保存路径。"""
        changes_root = SafePathRoot(self._session_changes_dir(session_id))

        def read(edit_id: str, phase: Literal["before", "after", "diff"]) -> str:
            if phase not in {"before", "after", "diff"}:
                raise ValueError("文件变更内容必须为 before、after 或 diff")
            filename = "diff.patch" if phase == "diff" else f"{phase}.txt"
            file_path = self._edit_path_in(changes_root, edit_id, filename)
            try:
                with native_io_path(file_path).open(
                    encoding="utf-8", newline="" if phase == "diff" else None,
                ) as handle:
                    return handle.read()
            except FileNotFoundError as exc:
                raise RuntimeError(
                    f"文件变更内容缺失: session_id={session_id}, edit_id={edit_id}, phase={phase}"
                ) from exc

        return read

    def _session_changes_dir(self, session_id: str) -> Path:
        return self._path_resolver.resolve_session_node(session_id) / "changes"

    def _edit_dir(self, session_id: str, edit_id: str) -> Path:
        return self._edit_path_in(SafePathRoot(self._session_changes_dir(session_id)), edit_id)

    @staticmethod
    def _edit_path_in(changes_root: SafePathRoot, edit_id: str, *parts: str) -> Path:
        if re.fullmatch(r"edit_[0-9a-f]{12}", edit_id) is None:
            raise ValueError("文件变更 edit_id 格式错误")
        return changes_root.join("edits", edit_id, *parts)

    def _ensure_changes_dir(self, session_id: str) -> Path:
        changes_dir = self._session_changes_dir(session_id)
        native_io_path(changes_dir / "edits").mkdir(parents=True, exist_ok=True)
        return changes_dir
