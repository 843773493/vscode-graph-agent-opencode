from __future__ import annotations

from datetime import UTC, datetime

from app.abstractions.job_service import JobServiceProtocol
from app.agents.context_checkpoint_store import ContextCompactionCheckpointStore
from app.agents.context_compaction_adapter import AgentSummarizationCompactor
from app.schemas.public_v2.session import SessionCompactResultDTO
from app.services.business.session_service import SessionService


class ContextCompactionService:
    def __init__(
        self,
        *,
        checkpoint_store: ContextCompactionCheckpointStore,
        job_service: JobServiceProtocol,
        session_service: SessionService,
        summarization_compactor: AgentSummarizationCompactor,
    ) -> None:
        self._checkpoint_store = checkpoint_store
        self._job_service = job_service
        self._session_service = session_service
        self._summarization_compactor = summarization_compactor

    async def compact(self, session_id: str) -> SessionCompactResultDTO:
        return await self._job_service.run_session_idle_operation(
            session_id,
            lambda: self._compact_idle(session_id),
        )

    async def _compact_idle(self, session_id: str) -> SessionCompactResultDTO:
        session = await self._session_service.get(session_id)
        checkpoint = await self._checkpoint_store.load(session_id)
        if checkpoint is None:
            return SessionCompactResultDTO(
                session_id=session_id,
                status="skipped",
                message="当前会话还没有可压缩的 checkpoint",
                before_message_count=0,
                effective_message_count_before=0,
                effective_message_count_after=0,
                summarized_message_count=0,
                retained_message_count=0,
                compacted_at=datetime.now(UTC),
            )

        check = await self._summarization_compactor.check(
            agent_id=session.current_agent_id,
            raw_messages=checkpoint.raw_messages,
            event=checkpoint.event,
        )
        if check.cutoff <= 0:
            return SessionCompactResultDTO(
                session_id=session_id,
                status="skipped",
                message="当前上下文没有可安全摘要的旧消息",
                before_message_count=len(check.messages),
                effective_message_count_before=len(check.effective_messages),
                effective_message_count_after=len(check.effective_messages),
                summarized_message_count=0,
                retained_message_count=len(check.effective_messages),
                compacted_at=datetime.now(UTC),
            )

        await self._checkpoint_store.save_compaction_request(
            checkpoint=checkpoint,
        )

        return SessionCompactResultDTO(
            session_id=session_id,
            status="scheduled",
            message=(
                "已安排上下文压缩，将在下一次完整模型请求前执行；"
                "优先保持缓存，找不到安全前缀时会替换旧前缀"
            ),
            before_message_count=len(check.messages),
            effective_message_count_before=len(check.effective_messages),
            effective_message_count_after=len(check.effective_messages),
            summarized_message_count=0,
            retained_message_count=len(check.effective_messages),
            compacted_at=datetime.now(UTC),
        )
