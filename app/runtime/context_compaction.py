from __future__ import annotations

from dataclasses import dataclass
from functools import partial

from langchain_core.messages import AnyMessage, BaseMessage
from langchain_core.messages.utils import count_tokens_approximately

from app.agents.provider_api_mode import parse_provider_api_mode
from app.agents.summarization.planning import (
    build_safe_compaction_partition,
    compaction_retention,
    compaction_retention_cutoff,
)
from app.agents.summarization.projection import apply_summarization_event
from app.core.shielded_cleanup import run_sync_shielded
from app.services.infrastructure.config_service import ConfigService


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
    ) -> None:
        self._config_service = config_service

    async def check(
        self,
        *,
        agent_id: str,
        raw_messages: list[object],
        event: object,
    ) -> ContextCompactionCheck:
        messages = self._as_any_messages(raw_messages)
        snapshot = self._config_service.get_snapshot()
        with self._config_service.use_snapshot(snapshot):
            runtime_config = self._config_service.get_agent_runtime_config(agent_id)
            selected = runtime_config["models"][0]
            mode = parse_provider_api_mode(selected)
        return await run_sync_shielded(
            self._check, messages, event, mode.model_info.max_input_tokens,
        )

    @staticmethod
    def _check(
        messages: list[AnyMessage], event: object, max_input_tokens: int | None,
    ) -> ContextCompactionCheck:
        effective_messages = apply_summarization_event(messages, event)
        # 当前执行适配器均为 LiteLLM，沿用其 LangChain 默认计数，不能按 wire 协议另猜系数。
        token_counter = partial(
            count_tokens_approximately, use_usage_metadata_scaling=True,
        )
        partition = build_safe_compaction_partition(
            effective_messages, event,
            summarize_end=compaction_retention_cutoff(
                effective_messages, keep=compaction_retention(max_input_tokens),
                max_input_tokens=max_input_tokens, token_counter=token_counter,
                partial_token_counter=partial(token_counter, use_usage_metadata_scaling=False),
            ),
        )
        cutoff = len(partition.prefix_messages) + len(partition.messages_to_summarize) if partition is not None else 0
        return ContextCompactionCheck(messages=messages, effective_messages=effective_messages, cutoff=cutoff)

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
