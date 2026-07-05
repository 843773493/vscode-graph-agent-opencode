from __future__ import annotations

from dataclasses import dataclass

from deepagents.middleware.summarization import create_summarization_middleware
from langchain_core.messages import AnyMessage, BaseMessage

from app.agents.agent_factory import build_runtime_for_agent
from app.agents.summarization_paths import apply_boxteam_summarization_paths
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.context_history_store import ContextHistoryStore


@dataclass(slots=True)
class ContextCompactionPlan:
    messages: list[AnyMessage]
    effective_messages: list[AnyMessage]
    cutoff: int
    to_summarize: list[AnyMessage]
    preserved_messages: list[AnyMessage]
    summary: str
    summary_message: AnyMessage
    state_cutoff: int
    history_file_path: str


@dataclass(slots=True)
class ContextCompactionCheck:
    messages: list[AnyMessage]
    effective_messages: list[AnyMessage]
    cutoff: int


class AgentSummarizationCompactor:
    def __init__(
        self,
        *,
        config_service: ConfigService,
        history_store: ContextHistoryStore,
    ) -> None:
        self._config_service = config_service
        self._history_store = history_store

    async def check(
        self,
        *,
        agent_id: str,
        raw_messages: list[object],
        event: object,
    ) -> ContextCompactionCheck:
        messages = self._as_any_messages(raw_messages)
        summarization = self._build_summarization(agent_id)
        effective_messages = summarization._apply_event_to_messages(messages, event)
        cutoff = summarization._determine_cutoff_index(effective_messages)
        return ContextCompactionCheck(
            messages=messages,
            effective_messages=effective_messages,
            cutoff=cutoff,
        )

    async def build_plan(
        self,
        *,
        session_id: str,
        agent_id: str,
        raw_messages: list[object],
        event: object,
    ) -> ContextCompactionPlan:
        check = await self.check(
            agent_id=agent_id,
            raw_messages=raw_messages,
            event=event,
        )
        if check.cutoff <= 0:
            raise ValueError("上下文未超过可压缩窗口，不能构建压缩计划")

        summarization = self._build_summarization(agent_id)
        to_summarize, preserved_messages = summarization._partition_messages(
            check.effective_messages,
            check.cutoff,
        )
        summary = await summarization._acreate_summary(to_summarize)
        history_file_path = await self._history_store.offload_history(
            session_id=session_id,
            messages=to_summarize,
        )
        summary_message = summarization._build_new_messages_with_path(
            summary,
            history_file_path,
        )[0]
        state_cutoff = summarization._compute_state_cutoff(event, check.cutoff)
        return ContextCompactionPlan(
            messages=check.messages,
            effective_messages=check.effective_messages,
            cutoff=check.cutoff,
            to_summarize=to_summarize,
            preserved_messages=preserved_messages,
            summary=summary,
            summary_message=summary_message,
            state_cutoff=state_cutoff,
            history_file_path=history_file_path,
        )

    def _build_summarization(self, agent_id: str):
        runtime = build_runtime_for_agent(
            agent_id=agent_id,
            config_service=self._config_service,
        )
        summarization = create_summarization_middleware(
            runtime["model"],
            self._history_store.backend,
        )
        apply_boxteam_summarization_paths(summarization)
        return summarization

    @staticmethod
    def _as_any_messages(raw_messages: list[object]) -> list[AnyMessage]:
        messages: list[AnyMessage] = []
        for index, message in enumerate(raw_messages):
            if not isinstance(message, BaseMessage):
                raise TypeError(
                    f"checkpoint messages[{index}] 类型不受支持: {type(message).__name__}"
                )
            messages.append(message)
        return messages
