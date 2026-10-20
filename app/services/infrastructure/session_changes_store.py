from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from app.abstractions.session_changes import StoredFileEdit
from app.core.path_utils import get_session_path_resolver, safe_join
from app.core.resource_uri import parse_resource_uri
from app.core.trace_middleware import get_current_gateway_id
from app.schemas.internal_v2.session_changes import SessionChangesetDTO

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
        resource = parse_resource_uri(file_path, local_gateway_id=get_current_gateway_id())
        if resource.kind != "files" or resource.scope not in {"workspace", "gateway"} or len(resource.tail_segments) < 2:
            raise ValueError("文件变更只能引用明确的 Workspace/Gateway 文件 URI")
        return resource.display_uri

    def save_edit(
        self,
        *,
        record: StoredFileEdit,
        before_content: str,
        after_content: str,
        diff_text: str,
    ) -> None:
        changes_dir = self._ensure_changes_dir(record.session_id)
        edit_dir = changes_dir / "edits" / record.edit_id
        edit_dir.mkdir(parents=True, exist_ok=False)
        if record.before_exists:
            (edit_dir / "before.txt").write_text(before_content, encoding="utf-8")
        if record.after_exists:
            (edit_dir / "after.txt").write_text(after_content, encoding="utf-8")
        (edit_dir / "diff.patch").write_text(diff_text, encoding="utf-8")
        metadata = asdict(record)
        (edit_dir / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        with (changes_dir / EDIT_INDEX_FILE).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(metadata, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")

    def read_records(self, session_id: str) -> list[StoredFileEdit]:
        index_file = self._session_changes_dir(session_id) / EDIT_INDEX_FILE
        if not index_file.exists():
            return []
        records: list[StoredFileEdit] = []
        with index_file.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    data = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(
                        f"文件变更索引损坏: {index_file}:{line_number}"
                    ) from exc
                records.append(StoredFileEdit(**data))
        return records

    def read_reviewed_map(self, session_id: str) -> dict[str, bool]:
        reviewed_file = self._session_changes_dir(session_id) / REVIEWED_FILE
        if not reviewed_file.exists():
            return {}
        try:
            raw = json.loads(reviewed_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"文件审查状态损坏: {reviewed_file}") from exc
        if not isinstance(raw, dict):
            raise TypeError(f"文件审查状态格式错误: {reviewed_file}")
        return {str(key): value is True for key, value in raw.items()}

    def save_reviewed_map(self, session_id: str, reviewed: dict[str, bool]) -> None:
        target = self._ensure_changes_dir(session_id) / REVIEWED_FILE
        target.write_text(
            json.dumps(reviewed, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def read_relative_text(self, session_id: str, relative_path: str | None) -> str:
        if not relative_path:
            return ""
        file_path = safe_join(self._session_changes_dir(session_id), relative_path)
        if not file_path.exists():
            raise RuntimeError(f"文件变更内容缺失: {file_path}")
        return file_path.read_text(encoding="utf-8")

    def save_changeset_file(
        self,
        *,
        session_id: str,
        changeset_id: str,
        file_path: str,
        before_content: str | None,
        after_content: str | None,
        diff_text: str,
    ) -> tuple[str | None, str | None, str]:
        changeset_name = self._safe_name(changeset_id)
        stem = self._file_stem(file_path)
        file_dir = self._ensure_changes_dir(session_id) / "changesets" / changeset_name / stem
        file_dir.mkdir(parents=True, exist_ok=True)
        (file_dir / "diff.patch").write_text(diff_text, encoding="utf-8")
        before_file = None
        if before_content is not None:
            before_file = f"changesets/{changeset_name}/{stem}/before.txt"
            (file_dir / "before.txt").write_text(before_content, encoding="utf-8")
        after_file = None
        if after_content is not None:
            after_file = f"changesets/{changeset_name}/{stem}/after.txt"
            (file_dir / "after.txt").write_text(after_content, encoding="utf-8")
        return before_file, after_file, f"changesets/{changeset_name}/{stem}/diff.patch"

    def save_changeset_summary(self, changeset: SessionChangesetDTO) -> None:
        target = (
            self._ensure_changes_dir(changeset.session_id)
            / "changesets"
            / self._safe_name(changeset.changeset_id)
            / "summary.json"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(changeset.model_dump_json(indent=2), encoding="utf-8")

    def _session_changes_dir(self, session_id: str) -> Path:
        return self._path_resolver.resolve_session_node(session_id) / "changes"

    def _ensure_changes_dir(self, session_id: str) -> Path:
        changes_dir = self._session_changes_dir(session_id)
        (changes_dir / "edits").mkdir(parents=True, exist_ok=True)
        (changes_dir / "changesets").mkdir(parents=True, exist_ok=True)
        return changes_dir

    @staticmethod
    def _safe_name(value: str) -> str:
        return "".join(ch if ch.isalnum() or ch in ".-_" else "_" for ch in value)

    @classmethod
    def _file_stem(cls, file_path: str) -> str:
        digest = hashlib.sha256(file_path.encode("utf-8")).hexdigest()[:12]
        return f"{digest}_{cls._safe_name(Path(file_path).name or 'file')}"
