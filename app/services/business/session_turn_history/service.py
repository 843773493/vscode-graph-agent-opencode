from __future__ import annotations

import asyncio

from app.abstractions.job_service import JobServiceProtocol
from app.abstractions.turn_history import (
    TurnEventProjectorProtocol,
    TurnHistoryEventSourceProtocol,
    TurnHistoryStoreProtocol,
    TurnSessionLookupProtocol,
)
from app.schemas.public_v2.common import JobStatus
from app.schemas.public_v2.turn import (
    SessionTurnBootstrapDTO,
    TurnDetailBatchDTO,
    TurnJobSummaryDTO,
    TurnPageDTO,
)

from .migration import SessionTurnHistoryMigrator

_RECENT_MIGRATION_EVENT_LIMIT = 128
_RECENT_MIGRATION_BYTE_LIMIT = 256 * 1024
_BOOTSTRAP_ACTIVE_JOB_LIMIT = 8


class SessionTurnHistoryService:
    """协调 Turn bootstrap、分页、详情和既有会话显式迁移。"""

    def __init__(
        self,
        *,
        store: TurnHistoryStoreProtocol,
        projector: TurnEventProjectorProtocol,
        trace_event_store: TurnHistoryEventSourceProtocol,
        session_service: TurnSessionLookupProtocol,
        job_service: JobServiceProtocol,
        migrator: SessionTurnHistoryMigrator,
    ) -> None:
        self._store = store
        self._projector = projector
        self._trace_event_store = trace_event_store
        self._session_service = session_service
        self._job_service = job_service
        self._migrator = migrator

    async def bootstrap(
        self,
        session_id: str,
    ) -> tuple[SessionTurnBootstrapDTO, bool]:
        session = await self._session_service.get(session_id)
        needs_completion = await asyncio.to_thread(
            self._ensure_recent_projection,
            session_id,
        )
        pending = await self._job_service.list_pending_summaries(
            session_id,
            limit=_BOOTSTRAP_ACTIVE_JOB_LIMIT,
        )
        active_jobs: list[TurnJobSummaryDTO] = []
        if pending.active_job_id is not None:
            active_job = await self._job_service.get(pending.active_job_id)
            active_jobs.append(
                TurnJobSummaryDTO(
                    job_id=active_job.job_id,
                    message_id=active_job.message_id,
                    status=active_job.status,
                    updated_at=active_job.updated_at,
                )
            )
        active_jobs.extend(
            TurnJobSummaryDTO(
                job_id=request.job_id,
                message_id=request.message_id,
                status=JobStatus.queued,
                updated_at=request.updated_at,
            )
            for request in pending.requests
        )
        active_page = active_jobs[:_BOOTSTRAP_ACTIVE_JOB_LIMIT]
        active_job_count = pending.request_count + (
            1 if pending.active_job_id is not None else 0
        )
        latest_page = await asyncio.to_thread(
            self._store.list_summaries,
            session_id,
            limit=1,
            cursor=None,
        )
        status = await asyncio.to_thread(self._store.projection_status, session_id)
        if status not in {"ready", "partial"}:
            raise RuntimeError(
                f"Turn projection status 非法: session_id={session_id}, status={status}"
            )
        return (
            SessionTurnBootstrapDTO(
                session=session,
                latest_turn=latest_page.items[0] if latest_page.items else None,
                active_job_id=pending.active_job_id,
                active_jobs=active_page,
                active_job_count=active_job_count,
                active_jobs_truncated=len(active_page) < active_job_count,
                projection_state=status,
                older_cursor=(latest_page.next_cursor if status == "ready" else None),
                event_cursor=await asyncio.to_thread(
                    self._store.event_cursor,
                    session_id,
                ),
                projection_epoch=latest_page.projection_epoch,
            ),
            needs_completion,
        )

    async def list_turns(
        self,
        session_id: str,
        *,
        limit: int,
        cursor: str | None,
    ) -> tuple[TurnPageDTO, bool]:
        await self._session_service.get(session_id)
        needs_completion = await asyncio.to_thread(
            self._ensure_recent_projection,
            session_id,
        )
        page = await asyncio.to_thread(
            self._store.list_summaries,
            session_id,
            limit=limit,
            cursor=cursor,
        )
        return page, needs_completion

    async def get_details(
        self,
        session_id: str,
        turn_ids: list[str],
    ) -> tuple[TurnDetailBatchDTO, bool]:
        await self._session_service.get(session_id)
        needs_completion = await asyncio.to_thread(
            self._ensure_recent_projection,
            session_id,
        )
        details = await asyncio.to_thread(
            self._store.get_details,
            session_id,
            turn_ids,
        )
        return details, needs_completion

    async def complete_migration(self, session_id: str) -> None:
        await self._migrator.complete(session_id)

    def _ensure_recent_projection(self, session_id: str) -> bool:
        return self._projector.synchronize(
            session_id,
            lambda: self._ensure_recent_projection_locked(session_id),
        )

    def _ensure_recent_projection_locked(self, session_id: str) -> bool:
        if self._store.projection_exists(
            session_id
        ) and self._store.history_initialized(session_id):
            if self._store.projection_status(session_id) == "partial":
                return True
            recovery = self._trace_event_store.read_turn_recovery_batch(
                session_id,
                after_event_id=self._store.event_cursor(session_id),
                max_events=_RECENT_MIGRATION_EVENT_LIMIT,
                max_bytes=_RECENT_MIGRATION_BYTE_LIMIT,
            )
            if not recovery.complete:
                self._store.set_projection_status(session_id, "partial")
                return True
            for indexed_event in recovery.events:
                self._projector.record_event(
                    session_id,
                    indexed_event.event,
                    source_offset=indexed_event.source_offset,
                )
            return False

        batch = self._trace_event_store.read_turn_bootstrap_batch(
            session_id,
            max_events=_RECENT_MIGRATION_EVENT_LIMIT,
            max_bytes=_RECENT_MIGRATION_BYTE_LIMIT,
        )
        has_older_events = (
            batch.has_older_events
            or not batch.index_available
            or self._migrator.has_checkpoint_history(session_id)
        )
        recent_events = [indexed.event for indexed in batch.events]
        if recent_events:
            latest_created_index = next(
                (
                    index
                    for index in range(len(recent_events) - 1, -1, -1)
                    if recent_events[index].type == "job_created"
                ),
                None,
            )
            if latest_created_index is None:
                # Trace tail 可能从超长 Turn 中间开始，不能凭 job_id 生成幽灵 Turn。
                has_older_events = True
            else:
                latest_created_event = recent_events[latest_created_index]
                latest_job_id = latest_created_event.job_id
                latest_job_events = [
                    event
                    for event in recent_events[latest_created_index:]
                    if event.job_id == latest_job_id
                ]
                has_older_events = (
                    has_older_events
                    or latest_created_index > 0
                    or any(
                        event.job_id != latest_job_id
                        for event in recent_events[latest_created_index:]
                    )
                )
                indexed_by_id = {
                    indexed.event.event_id: indexed for indexed in batch.events
                }
                for event in latest_job_events:
                    if event.type == "job_merged" and any(
                        self._store.get_turn(session_id, merged_job_id) is None
                        for merged_job_id in event.payload.merged_job_ids
                    ):
                        has_older_events = True
                        continue
                    indexed = indexed_by_id[event.event_id]
                    self._projector.record_event(
                        session_id,
                        event,
                        source_offset=indexed.source_offset,
                    )
        else:
            self._store.projection_epoch(session_id)
        self._store.set_projection_status(
            session_id,
            "partial" if has_older_events else "ready",
        )
        if not has_older_events:
            self._store.mark_history_initialized(session_id)
        return has_older_events
