from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.schemas.public_v2.session_changes import (
    SessionChangesetDTO,
    SessionFileChangeKind,
)


@dataclass(frozen=True, slots=True)
class FileEditSnapshot:
    file_path: str
    real_path: Path
    existed: bool
    content: str


@dataclass(frozen=True, slots=True)
class StoredFileEdit:
    edit_id: str
    session_id: str
    turn_id: str
    tool_call_id: str
    tool_name: str
    file_path: str
    kind: SessionFileChangeKind
    additions: int
    deletions: int
    created_at: str
    before_exists: bool
    after_exists: bool
    before_file: str | None
    after_file: str | None
    diff_file: str


@dataclass(frozen=True, slots=True)
class AggregatedFileChange:
    file_path: str
    kind: SessionFileChangeKind
    additions: int
    deletions: int
    latest_edit_id: str
    tool_call_ids: tuple[str, ...]
    turn_ids: tuple[str, ...]
    before_content: str | None
    after_content: str | None


class SessionChangesStoreProtocol(Protocol):
    def resolve_file_path(self, file_path: str) -> tuple[str, Path]: ...

    def read_text_if_exists(self, real_path: Path) -> str: ...

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

    def read_relative_text(self, session_id: str, relative_path: str | None) -> str: ...

    def save_changeset_file(
        self,
        *,
        session_id: str,
        changeset_id: str,
        file_path: str,
        before_content: str | None,
        after_content: str | None,
        diff_text: str,
    ) -> tuple[str | None, str | None, str]: ...

    def save_changeset_summary(self, changeset: SessionChangesetDTO) -> None: ...


class SessionChangesRecorderProtocol(Protocol):
    def capture_before(self, file_path: str) -> FileEditSnapshot: ...

    def build_snapshot(
        self,
        *,
        file_path: str,
        existed: bool,
        content: str,
    ) -> FileEditSnapshot: ...

    async def record_tool_file_edit(
        self,
        *,
        session_id: str,
        turn_id: str,
        tool_call_id: str,
        tool_name: str,
        before: FileEditSnapshot,
    ) -> StoredFileEdit | None: ...
