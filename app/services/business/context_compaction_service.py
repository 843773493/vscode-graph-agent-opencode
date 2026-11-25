from __future__ import annotations

from datetime import UTC, datetime

from app.abstractions.job_service import JobServiceProtocol
from app.agents.manual_compaction_checkpoint import ManualCompactionCheckpointAdapter
from app.core.shielded_cleanup import run_sync_shielded
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.runtime.context_compaction import AgentSummarizationCompactor
from app.schemas.internal_v2.session import SessionCompactResultDTO
from app.services.business.session_service import SessionService


class ContextCompactionService:
    """在 Thread 空闲窗口校验并登记手动压缩请求。"""

    def __init__(
        self,
        *,
        checkpoint_adapter: ManualCompactionCheckpointAdapter,
        job_service: JobServiceProtocol,
        session_service: SessionService,
        summarization_compactor: AgentSummarizationCompactor,
    ) -> None:
        self._checkpoint_adapter = checkpoint_adapter
        self._job_service = job_service
        self._session_service = session_service
        self._summarization_compactor = summarization_compactor

    async def request_compaction(
        self, session_id: str, *, thread_id: str | None = None,
    ) -> SessionCompactResultDTO:
        owner = MutationIntentOwner(
            session_id,
            await self._session_service.resolve_main_thread(session_id) if thread_id is None
            else await self._session_service.resolve_thread(session_id, thread_id),
        )
        return await self._job_service.run_thread_idle_operation(
            session_id, owner.thread_id, lambda: self._request_when_idle(owner),
        )

    async def _request_when_idle(self, owner: MutationIntentOwner) -> SessionCompactResultDTO:
        session_id = owner.session_id
        async with self._session_service.session_write_admission(session_id):
            session = await self._session_service.get(session_id)
        snapshot = await self._checkpoint_adapter.load_snapshot(owner)
        if snapshot is None:
            return SessionCompactResultDTO(
                session_id=session_id,
                thread_id=owner.thread_id,
                status="skipped",
                message="当前会话还没有可压缩的 checkpoint",
                before_message_count=0,
                effective_message_count_before=0,
                effective_message_count_after=0,
                summarized_message_count=0,
                retained_message_count=0,
                compacted_at=datetime.now(UTC),
            )

        ids = await run_sync_shielded(self._summarization_compactor.source_item_ids, owner)
        count = len(snapshot.raw_messages)
        if not ids:
            return SessionCompactResultDTO(
                session_id=session_id,
                thread_id=owner.thread_id,
                status="skipped",
                message="当前上下文没有可安全摘要的旧消息",
                before_message_count=count,
                effective_message_count_before=count,
                effective_message_count_after=count,
                summarized_message_count=0,
                retained_message_count=count,
                compacted_at=datetime.now(UTC),
            )

        handle = await self._summarization_compactor.request(
            owner, agent_id=session.current_agent_id, preferred_model=session.current_model, item_ids=ids, manual=True,
        )
        if handle is None:
            raise RuntimeError("compaction-capacity-busy: 后台摘要额度已满，本次手动请求未受理")
        return SessionCompactResultDTO(
            session_id=session_id,
            thread_id=owner.thread_id,
            task_id=handle.task_id,
            status="scheduled",
            message=(
                "已安排后台摘要；完整候选将在下一次模型请求通过预算核验后采用"
            ),
            before_message_count=count,
            effective_message_count_before=count,
            effective_message_count_after=count,
            summarized_message_count=0,
            retained_message_count=count,
            compacted_at=datetime.now(UTC),
        )
