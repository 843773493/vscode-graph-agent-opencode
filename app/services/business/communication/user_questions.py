"""主 Agent 给用户留存问题；回答始终经普通 Composer 发送。"""

from __future__ import annotations

from app.core.shielded_cleanup import run_sync_shielded
from app.schemas.internal_v2.user_question import (
    UserQuestionPageDTO,
    UserQuestionSummaryDTO,
)
from app.services.business.session_service import SessionService
from app.services.infrastructure.events.event_channel_service import (
    EventChannel,
    EventChannelService,
    EventChannelSpec,
    channel_name,
)
from app.services.infrastructure.rollout_context.execution.user_questions import (
    UserQuestionStore,
)


class UserQuestionService:
    def __init__(self, *, sessions: SessionService, store: UserQuestionStore, events: EventChannelService) -> None:
        self.sessions = sessions
        self.store = store
        self.events = events

    def channel(self, session_id: str, thread_id: str) -> EventChannel:
        return self.events.ensure_channel(EventChannelSpec(
            name=channel_name("user.questions", f"{session_id}:{thread_id}"), overflow_policy="gap", max_queue_size=8,
        ))

    async def assert_main_agent(self, session_id: str, thread_id: str) -> None:
        # 身份来自后端 catalog，模型不能通过参数指定接收者或假冒主 Agent。
        if thread_id != await self.sessions.resolve_main_thread(session_id):
            raise ValueError("仅主 Agent 可以给用户留言；子 Agent 请经父 Agent 转达")

    async def summary(self, session_id: str, thread_id: str) -> UserQuestionSummaryDTO:
        async with self.sessions.session_read_admission(session_id):
            await self.sessions.resolve_thread(session_id, thread_id)
            return await run_sync_shielded(self.store.summary, session_id, thread_id)

    async def page(self, session_id: str, thread_id: str, *, before: int | None, limit: int) -> UserQuestionPageDTO:
        async with self.sessions.session_read_admission(session_id):
            await self.sessions.resolve_thread(session_id, thread_id)
            return await run_sync_shielded(self.store.page, session_id, thread_id, before=before, limit=limit)

    async def create(self, session_id: str, thread_id: str, *, question_id: str, markdown: str,
                     model_call_id: str, turn_id: str | None) -> str:
        if not markdown.strip() or len(markdown.encode("utf-8")) > 64 * 1024:
            raise ValueError("留言须为非空 Markdown，且不超过 64 KiB")
        async with self.sessions.session_write_admission(session_id):
            await self.assert_main_agent(session_id, thread_id)
            result = await run_sync_shielded(self.store.create, session_id, thread_id,
                question_id=question_id, markdown=markdown, model_call_id=model_call_id, turn_id=turn_id)
            channel = self.events.find_channel(channel_name("user.questions", f"{session_id}:{thread_id}"))
            if channel is not None:
                channel.publish(True)
            return result

    async def delete(self, session_id: str, thread_id: str, question_id: str) -> UserQuestionSummaryDTO:
        async with self.sessions.session_write_admission(session_id):
            await self.sessions.resolve_thread(session_id, thread_id)
            summary = await run_sync_shielded(self.store.delete, session_id, thread_id, question_id)
            channel = self.events.find_channel(channel_name("user.questions", f"{session_id}:{thread_id}"))
            if channel is not None:
                channel.publish(True)
            return summary
