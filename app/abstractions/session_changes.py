from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from app.schemas.internal_v2.session_changes import SessionFileChangeKind


@dataclass(frozen=True, slots=True)
class ObservedFileEdit:
    """文件 owner 为同一次工具编辑提供的前后状态，不代表当前磁盘内容。"""

    file_path: str
    before_exists: bool
    before_content: str
    after_exists: bool
    after_content: str


@dataclass(frozen=True, slots=True)
class StoredFileEdit:
    edit_id: str
    session_id: str
    turn_id: str | None
    tool_call_id: str
    execution_id: str
    tool_name: str
    file_path: str
    kind: SessionFileChangeKind
    additions: int
    deletions: int
    created_at: str
    before_exists: bool
    after_exists: bool


@dataclass(frozen=True, slots=True)
class AggregatedFileChange:
    file_path: str
    kind: SessionFileChangeKind
    latest_edit_id: str
    tool_call_ids: tuple[str, ...]
    execution_ids: tuple[str, ...]
    turn_ids: tuple[str, ...]
    before_content: str | None
    after_content: str | None
    diff_text: str


class SessionChangesStoreProtocol(Protocol):
    def normalize_file_reference(self, file_path: str) -> str: ...

    def save_edit(
        self,
        *,
        record: StoredFileEdit,
        before_content: str,
        after_content: str,
        diff_text: str,
    ) -> None: ...

    def read_records(self, session_id: str) -> list[StoredFileEdit]: ...

    def read_reviewed_map(self, session_id: str) -> dict[str, bool]: ...

    def save_reviewed_map(self, session_id: str, reviewed: dict[str, bool]) -> None: ...

    def edit_text_reader(self, session_id: str) -> Callable[[str, Literal["before", "after", "diff"]], str]: ...


class SessionChangesRecorderProtocol(Protocol):
    async def record_tool_file_edit(
        self,
        *,
        session_id: str,
        turn_id: str | None,
        tool_call_id: str,
        execution_id: str,
        tool_name: str,
        edit: ObservedFileEdit,
    ) -> StoredFileEdit | None: ...
