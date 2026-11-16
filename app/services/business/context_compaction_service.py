from __future__ import annotations

from datetime import UTC, datetime

from app.abstractions.job_service import JobServiceProtocol
from app.agents.manual_compaction_checkpoint import ManualCompactionCheckpointAdapter
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

    async def request_compaction(self, session_id: str) -> SessionCompactResultDTO:
        owner = MutationIntentOwner(
            session_id, await self._session_service.resolve_main_thread(session_id),
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
            raw_messages=snapshot.raw_messages,
            event=snapshot.summarization_event,
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

        # Thread 准入与 preparation 保护读取窗口；Session 短写只覆盖发布，
        # 避免 main 的读取/判断阻塞 sibling 的 context-control lease。
        async with self._session_service.session_write_admission(session_id):
            await self._checkpoint_adapter.request_compaction(
                snapshot=snapshot,
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
