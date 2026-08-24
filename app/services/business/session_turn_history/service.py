"""协调 rollout-backed 会话历史与活动 Job 状态。"""
from __future__ import annotations

from app.abstractions.job_service import JobServiceProtocol
from app.abstractions.turn_history import TurnSessionLookupProtocol
from app.core.history_loading import HistoryLoadingConfig
from app.core.rollout_checkpoint_saver import RolloutCheckpointSaver
from app.schemas.public_v2.common import JobStatus
from app.schemas.public_v2.turn import (
    SessionTurnBootstrapDTO,
    TurnHistoryLoadRequest,
    TurnHistoryPageDTO,
    TurnJobSummaryDTO,
)
from app.services.infrastructure.trace_event_store import TraceEventStore

_BOOTSTRAP_ACTIVE_JOB_LIMIT = 8


class SessionTurnHistoryService:
    """历史内容只从 rollout 读取，活动 Job 仍由 JobService 提供。"""

    def __init__(
        self,
        *,
        checkpointer: RolloutCheckpointSaver,
        session_service: TurnSessionLookupProtocol,
        job_service: JobServiceProtocol,
        trace_event_store: TraceEventStore,
    ) -> None:
        self._checkpointer = checkpointer
        self._session_service = session_service
        self._job_service = job_service
        self._trace_event_store = trace_event_store

    async def bootstrap(
        self,
        session_id: str,
        *,
        history_loading: HistoryLoadingConfig | None = None,
    ) -> SessionTurnBootstrapDTO:
        session = await self._session_service.get(session_id)
        latest, older_cursor, projection_epoch = self._checkpointer.bootstrap_history(
            session_id,
            policy=history_loading,
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
        active_job_count = pending.request_count + (
            1 if pending.active_job_id is not None else 0
        )
        active_page = active_jobs[:_BOOTSTRAP_ACTIVE_JOB_LIMIT]
        return SessionTurnBootstrapDTO(
            session=session,
            latest_turn=latest,
            active_job_id=pending.active_job_id,
            active_jobs=active_page,
            active_job_count=active_job_count,
            active_jobs_truncated=len(active_page) < active_job_count,
            projection_state="ready",
            older_cursor=older_cursor,
            event_cursor=self._trace_event_store.latest_event_cursor(session_id),
            projection_epoch=projection_epoch,
        )

    async def load_history(
        self,
        session_id: str,
        request: TurnHistoryLoadRequest,
        *,
        history_loading: HistoryLoadingConfig | None = None,
    ) -> TurnHistoryPageDTO:
        await self._session_service.get(session_id)
        page = self._checkpointer.load_history(
            session_id,
            request,
            policy=history_loading,
        )
        return page


__all__ = ["SessionTurnHistoryService"]
