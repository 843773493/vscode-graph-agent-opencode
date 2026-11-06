from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from functools import cache

from app.abstractions.session_changes import (
    FileEditSnapshot,
    SessionChangesStoreProtocol,
    StoredFileEdit,
)
from app.core.key_lock_pool import KeyLockPool
from app.core.shielded_cleanup import run_sync_shielded
from app.schemas.internal_v2.session_changes import (
    SessionChangesetDTO,
    SessionChangesetListDTO,
    SessionChangesetListItemDTO,
    SessionChangesSummaryDTO,
    SessionFileChangeDTO,
    SessionFileReviewResultDTO,
)
from app.services.business.session_changes_logic import (
    aggregate_records,
    build_edit_id,
    build_unified_diff,
    changeset_kind_and_turn,
    changeset_records,
    count_diff_lines,
    detect_change_kind,
    preview_session_file_content,
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
        # 同会话的文件与审查状态仍串行处理，取消须等待 worker 结算。
        self._operation_locks = KeyLockPool()

    async def record_tool_file_edit(
        self,
        *,
        session_id: str,
        turn_id: str | None,
        tool_call_id: str,
        execution_id: str,
        tool_name: str,
        before: FileEditSnapshot,
    ) -> StoredFileEdit | None:
        def record() -> StoredFileEdit | None:
            file_path = self._store.normalize_file_reference(before.file_path)
            after_content = before.after_content
            after_exists = before.after_exists
            if before.existed == after_exists and before.content == after_content:
                return None

            diff_text = build_unified_diff(
                file_path=file_path,
                before_content=before.content if before.existed else "",
                after_content=after_content if after_exists else "",
            )
            additions, deletions = count_diff_lines(diff_text)
            edit_id = build_edit_id(
                turn_id=turn_id,
                execution_id=execution_id,
                tool_call_id=tool_call_id,
                file_path=file_path,
            )
            record = StoredFileEdit(
                edit_id=edit_id,
                session_id=session_id,
                turn_id=turn_id,
                tool_call_id=tool_call_id,
                execution_id=execution_id,
                tool_name=tool_name,
                file_path=file_path,
                kind=detect_change_kind(
                    before_exists=before.existed,
                    after_exists=after_exists,
                ),
                additions=additions,
                deletions=deletions,
                created_at=datetime.now(UTC).isoformat(),
                before_exists=before.existed,
                after_exists=after_exists,
            )
            self._store.save_edit(
                record=record,
                before_content=before.content,
                after_content=after_content,
                diff_text=diff_text,
            )
            return record

        # 原执行 owner 保护工具结果结算，不重新申请 active 准入。
        async with self._operation_locks.lock_for(session_id):
            await self._session_service.get(session_id)
            return await run_sync_shielded(record)

    async def list_changesets(self, session_id: str) -> SessionChangesetListDTO:
        def read() -> SessionChangesetListDTO:
            records = self._store.read_records(session_id)
            # 同一次列表里的 all/Turn 可消费相同快照，只复用纯 diff 计算。
            build_diff = cache(build_unified_diff)
            items: list[SessionChangesetListItemDTO] = []
            for changeset_id in (
                "all",
                *(f"turn:{turn_id}" for turn_id in sorted_turn_ids(records)),
            ):
                changeset = self._build_changeset(
                    session_id=session_id,
                    changeset_id=changeset_id,
                    records=changeset_records(records, changeset_id),
                    build_diff=build_diff,
                )
                items.append(
                    SessionChangesetListItemDTO(
                        changeset_id=changeset_id,
                        label=changeset.label,
                        description=changeset.description,
                        change_kind=changeset.change_kind,
                        is_default=changeset.change_kind == "all",
                        turn_id=changeset.turn_id,
                        summary=changeset.summary,
                    )
                )
            return SessionChangesetListDTO(session_id=session_id, items=items)

        async with (
            self._session_service.session_read_admission(session_id),
            self._operation_locks.lock_for(session_id),
        ):
            await self._session_service.get(session_id)
            return await run_sync_shielded(read)

    async def get_changeset(
        self,
        *,
        session_id: str,
        changeset_id: str,
    ) -> SessionChangesetDTO:
        def read() -> SessionChangesetDTO:
            return self._build_changeset(
                session_id=session_id,
                changeset_id=changeset_id,
                records=changeset_records(
                    self._store.read_records(session_id),
                    changeset_id,
                ),
                persist=True,
            )

        async with (
            self._session_service.session_read_admission(session_id),
            self._operation_locks.lock_for(session_id),
        ):
            await self._session_service.get(session_id)
            return await run_sync_shielded(read)

    async def set_file_reviewed(
        self,
        *,
        session_id: str,
        file_path: str,
        reviewed: bool,
    ) -> SessionFileReviewResultDTO:
        def update() -> SessionFileReviewResultDTO:
            normalized_path = self._store.normalize_file_reference(file_path)
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

        async with (
            self._session_service.session_read_admission(session_id),
            self._operation_locks.lock_for(session_id),
        ):
            await self._session_service.get(session_id)
            return await run_sync_shielded(update)

    def _build_changeset(
        self,
        *,
        session_id: str,
        changeset_id: str,
        records: list[StoredFileEdit],
        persist: bool = False,
        build_diff: Callable[..., str] = build_unified_diff,
    ) -> SessionChangesetDTO:
        reviewed_map = self._store.read_reviewed_map(session_id)
        changes = aggregate_records(
            records,
            read_edit_text=lambda edit_id, phase: self._store.read_edit_text(
                session_id,
                edit_id,
                phase,
            ),
        )
        files: list[SessionFileChangeDTO] = []
        for change in changes:
            diff_text = build_diff(
                file_path=change.file_path,
                before_content=change.before_content or "",
                after_content=change.after_content or "",
            )
            additions, deletions = count_diff_lines(diff_text)
            if additions == 0 and deletions == 0:
                continue
            if persist:
                self._store.save_changeset_file(
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
                    diff_text=diff_text,
                    before_preview=preview_session_file_content(change.before_content),
                    after_preview=preview_session_file_content(change.after_content),
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
            generated_at=datetime.now(UTC),
        )
        if persist:
            self._store.save_changeset_summary(changeset)
        return changeset
