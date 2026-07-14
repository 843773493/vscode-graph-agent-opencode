from __future__ import annotations

from datetime import datetime, timezone

from app.abstractions.session_changes import (
    FileEditSnapshot,
    SessionChangesStoreProtocol,
    StoredFileEdit,
)
from app.schemas.public_v2.session_changes import (
    SessionChangesSummaryDTO,
    SessionChangesetDTO,
    SessionChangesetListDTO,
    SessionChangesetListItemDTO,
    SessionFileChangeDTO,
    SessionFileReviewResultDTO,
)
from app.services.business.session_changes_logic import (
    aggregate_records,
    build_edit_id,
    build_unified_diff,
    changeset_kind_and_turn,
    count_diff_lines,
    detect_change_kind,
    preview,
    sorted_turn_ids,
)
from app.services.business.session_service import SessionService


class SessionChangesService:
    """编排会话文件变更的记录、聚合与审查状态。"""

    def __init__(
        self,
        *,
        session_service: SessionService,
        store: SessionChangesStoreProtocol,
    ) -> None:
        self._session_service = session_service
        self._store = store

    def capture_before(self, file_path: str) -> FileEditSnapshot:
        virtual_path, real_path = self._store.resolve_file_path(file_path)
        return FileEditSnapshot(
            file_path=virtual_path,
            real_path=real_path,
            existed=real_path.exists(),
            content=self._store.read_text_if_exists(real_path),
        )

    def build_snapshot(
        self,
        *,
        file_path: str,
        existed: bool,
        content: str,
    ) -> FileEditSnapshot:
        virtual_path, real_path = self._store.resolve_file_path(file_path)
        return FileEditSnapshot(
            file_path=virtual_path,
            real_path=real_path,
            existed=existed,
            content=content,
        )

    async def record_tool_file_edit(
        self,
        *,
        session_id: str,
        turn_id: str,
        tool_call_id: str,
        execution_id: str,
        tool_name: str,
        before: FileEditSnapshot,
    ) -> StoredFileEdit | None:
        await self._session_service.get(session_id)
        after_content = self._store.read_text_if_exists(before.real_path)
        after_exists = before.real_path.exists()
        if before.existed == after_exists and before.content == after_content:
            return None

        diff_text = build_unified_diff(
            file_path=before.file_path,
            before_content=before.content if before.existed else "",
            after_content=after_content if after_exists else "",
        )
        additions, deletions = count_diff_lines(diff_text)
        edit_id = build_edit_id(
            turn_id=turn_id,
            tool_call_id=tool_call_id,
            file_path=before.file_path,
        )
        record = StoredFileEdit(
            edit_id=edit_id,
            session_id=session_id,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
            execution_id=execution_id,
            tool_name=tool_name,
            file_path=before.file_path,
            kind=detect_change_kind(
                before_exists=before.existed,
                after_exists=after_exists,
            ),
            additions=additions,
            deletions=deletions,
            created_at=datetime.now(timezone.utc).isoformat(),
            before_exists=before.existed,
            after_exists=after_exists,
            before_file=f"edits/{edit_id}/before.txt" if before.existed else None,
            after_file=f"edits/{edit_id}/after.txt" if after_exists else None,
            diff_file=f"edits/{edit_id}/diff.patch",
        )
        self._store.save_edit(
            record=record,
            before_content=before.content,
            after_content=after_content,
            diff_text=diff_text,
        )
        return record

    async def list_changesets(self, session_id: str) -> SessionChangesetListDTO:
        await self._session_service.get(session_id)
        records = self._store.read_records(session_id)
        all_changeset = self._build_changeset(
            session_id=session_id,
            changeset_id="all",
            records=records,
        )
        items = [
            SessionChangesetListItemDTO(
                changeset_id="all",
                label="All Changes",
                description="显示本会话内文件工具产生的全部可审查变更",
                change_kind="all",
                is_default=True,
                summary=all_changeset.summary,
            )
        ]
        for turn_id in sorted_turn_ids(records):
            changeset_id = f"turn:{turn_id}"
            changeset = self._build_changeset(
                session_id=session_id,
                changeset_id=changeset_id,
                records=[record for record in records if record.turn_id == turn_id],
            )
            items.append(
                SessionChangesetListItemDTO(
                    changeset_id=changeset_id,
                    label="This Turn",
                    description=f"显示任务 {turn_id} 中产生的文件变更",
                    change_kind="turn",
                    turn_id=turn_id,
                    summary=changeset.summary,
                )
            )
        return SessionChangesetListDTO(session_id=session_id, items=items)

    async def get_changeset(
        self,
        *,
        session_id: str,
        changeset_id: str,
    ) -> SessionChangesetDTO:
        await self._session_service.get(session_id)
        return self._build_changeset(
            session_id=session_id,
            changeset_id=changeset_id,
            records=self._records_for_changeset(session_id, changeset_id),
            persist=True,
        )

    async def set_file_reviewed(
        self,
        *,
        session_id: str,
        file_path: str,
        reviewed: bool,
    ) -> SessionFileReviewResultDTO:
        await self._session_service.get(session_id)
        normalized_path, _ = self._store.resolve_file_path(file_path)
        reviewed_map = self._store.read_reviewed_map(session_id)
        if reviewed:
            reviewed_map[normalized_path] = True
        else:
            reviewed_map.pop(normalized_path, None)
        self._store.save_reviewed_map(session_id, reviewed_map)
        return SessionFileReviewResultDTO(
            session_id=session_id,
            file_path=normalized_path,
            reviewed=reviewed,
        )

    def _records_for_changeset(
        self,
        session_id: str,
        changeset_id: str,
    ) -> list[StoredFileEdit]:
        records = self._store.read_records(session_id)
        if changeset_id == "all":
            return records
        if changeset_id.startswith("turn:"):
            turn_id = changeset_id.removeprefix("turn:")
            if not turn_id:
                raise ValueError("turn changeset 缺少 turn_id")
            return [record for record in records if record.turn_id == turn_id]
        raise ValueError(f"不支持的 changeset_id: {changeset_id}")

    def _build_changeset(
        self,
        *,
        session_id: str,
        changeset_id: str,
        records: list[StoredFileEdit],
        persist: bool = False,
    ) -> SessionChangesetDTO:
        reviewed_map = self._store.read_reviewed_map(session_id)
        changes = aggregate_records(
            records,
            read_relative_text=lambda path: self._store.read_relative_text(
                session_id,
                path,
            ),
        )
        files: list[SessionFileChangeDTO] = []
        for change in changes:
            diff_text = build_unified_diff(
                file_path=change.file_path,
                before_content=change.before_content or "",
                after_content=change.after_content or "",
            )
            additions, deletions = count_diff_lines(diff_text)
            if additions == 0 and deletions == 0:
                continue
            before_file: str | None = None
            after_file: str | None = None
            diff_file = ""
            if persist:
                before_file, after_file, diff_file = self._store.save_changeset_file(
                    session_id=session_id,
                    changeset_id=changeset_id,
                    file_path=change.file_path,
                    before_content=change.before_content,
                    after_content=change.after_content,
                    diff_text=diff_text,
                )
            files.append(
                SessionFileChangeDTO(
                    file_path=change.file_path,
                    kind=change.kind,
                    additions=additions,
                    deletions=deletions,
                    reviewed=reviewed_map.get(change.file_path) is True,
                    latest_edit_id=change.latest_edit_id,
                    tool_call_ids=list(change.tool_call_ids),
                    execution_ids=list(change.execution_ids),
                    turn_ids=list(change.turn_ids),
                    before_file=before_file,
                    after_file=after_file,
                    diff_file=diff_file,
                    diff_text=diff_text,
                    before_preview=preview(change.before_content),
                    after_preview=preview(change.after_content),
                )
            )

        summary = SessionChangesSummaryDTO(
            files=len(files),
            additions=sum(file.additions for file in files),
            deletions=sum(file.deletions for file in files),
        )
        change_kind, turn_id = changeset_kind_and_turn(changeset_id)
        changeset = SessionChangesetDTO(
            session_id=session_id,
            changeset_id=changeset_id,
            label="This Turn" if change_kind == "turn" else "All Changes",
            description=(
                f"显示任务 {turn_id} 中产生的文件变更"
                if change_kind == "turn"
                else "显示本会话内文件工具产生的全部可审查变更"
            ),
            change_kind=change_kind,
            turn_id=turn_id,
            summary=summary,
            files=files,
            generated_at=datetime.now(timezone.utc),
        )
        if persist:
            self._store.save_changeset_summary(changeset)
        return changeset
