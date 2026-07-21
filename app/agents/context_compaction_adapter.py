from __future__ import annotations

from dataclasses import dataclass

from langchain_core.messages import AnyMessage, BaseMessage

from app.agents.agent_factory import build_runtime_for_agent
from app.agents.cache_preserving_summarization import (
    apply_summarization_event,
    build_safe_compaction_partition,
    create_cache_preserving_summarization_middleware,
)
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.context_history_store import ContextHistoryStore


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
        effective_messages = apply_summarization_event(messages, event)
        partition = build_safe_compaction_partition(
            summarization,
            effective_messages,
            event,
        )
        cutoff = (
            len(partition.prefix_messages) + len(partition.messages_to_summarize)
            if partition is not None
            else 0
        )
        return ContextCompactionCheck(
            messages=messages,
            effective_messages=effective_messages,
            cutoff=cutoff,
        )

    def _build_summarization(self, agent_id: str):
        runtime = build_runtime_for_agent(
            agent_id=agent_id,
            config_service=self._config_service,
        )
        summarization = create_cache_preserving_summarization_middleware(
            runtime["model"],
            self._history_store.backend,
        )
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
