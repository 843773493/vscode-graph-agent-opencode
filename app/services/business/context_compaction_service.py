from __future__ import annotations

from datetime import UTC, datetime

from app.agents.context_checkpoint_store import ContextCompactionCheckpointStore
from app.agents.context_compaction_adapter import AgentSummarizationCompactor
from app.schemas.public_v2.session import SessionCompactResultDTO
from app.services.business.session_service import SessionService


class ContextCompactionService:
    def __init__(
        self,
        *,
        checkpoint_store: ContextCompactionCheckpointStore,
        session_service: SessionService,
        summarization_compactor: AgentSummarizationCompactor,
    ) -> None:
        self._checkpoint_store = checkpoint_store
        self._session_service = session_service
        self._summarization_compactor = summarization_compactor

    async def compact(self, session_id: str) -> SessionCompactResultDTO:
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
                message="当前上下文未超过可压缩窗口",
                before_message_count=len(check.messages),
                effective_message_count_before=len(check.effective_messages),
                effective_message_count_after=len(check.effective_messages),
                summarized_message_count=0,
                retained_message_count=len(check.effective_messages),
                compacted_at=datetime.now(UTC),
            )

        plan = await self._summarization_compactor.build_plan(
            session_id=session_id,
            agent_id=session.current_agent_id,
            raw_messages=checkpoint.raw_messages,
            event=checkpoint.event,
        )
        await self._checkpoint_store.save_summarization_event(
            checkpoint=checkpoint,
            cutoff_index=plan.state_cutoff,
            summary_message=plan.summary_message,
            history_file_path=plan.history_file_path,
        )

        effective_after_count = 1 + len(plan.preserved_messages)
        return SessionCompactResultDTO(
            session_id=session_id,
            status="compacted",
            message=f"已压缩 {len(plan.to_summarize)} 条较早上下文",
            before_message_count=len(plan.messages),
            effective_message_count_before=len(plan.effective_messages),
            effective_message_count_after=effective_after_count,
            summarized_message_count=len(plan.to_summarize),
            retained_message_count=len(plan.preserved_messages),
            summary=plan.summary,
            history_file_path=plan.history_file_path,
            compacted_at=datetime.now(UTC),
        )
