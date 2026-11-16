from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from langchain_core.messages import AnyMessage, BaseMessage

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
        workspace_root: Path,
    ) -> None:
        self._config_service = config_service
        self._workspace_root = workspace_root

    async def check(
        self,
        *,
        agent_id: str,
        raw_messages: list[object],
        event: object,
    ) -> ContextCompactionCheck:
        messages = self._as_any_messages(raw_messages)
        summarization = await run_sync_shielded(self._build_summarization, agent_id)

        from app.agents.summarization.planning import build_safe_compaction_partition
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
        from app.agents.summarization.factory import (
            create_cache_preserving_summarization_middleware,
        )
        from app.agents.summarization.preflight import NoDurableOwnerCompactionPreflight
        from app.agents.workspace_backend import build_workspace_backend
        from app.runtime.agent_runtime import prepare_agent_model

        model_setup = prepare_agent_model(
            agent_id=agent_id,
            config_service=self._config_service,
        )
        summarization = create_cache_preserving_summarization_middleware(
            model_setup.model,
            build_workspace_backend(self._workspace_root),
            # 预览装配没有 durable Saver owner；端口仅在真实触发压缩时
            # 才会被调用，按合成装配合同给显式失败端口，不静默跳过。
            compaction_preflight=NoDurableOwnerCompactionPreflight(),
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
