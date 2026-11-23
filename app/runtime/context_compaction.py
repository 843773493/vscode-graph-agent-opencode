from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from boxteam_plugin_sdk.lifecycle import LifetimeScope, run_cleanup_shielded
from langchain_core.messages import AnyMessage, BaseMessage

from app.agents.summarization.projection import apply_summarization_event
from app.core.shielded_cleanup import run_sync_shielded
from app.schemas.internal_v2.model_reference import ModelReferenceDTO
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
        snapshot = self._config_service.get_snapshot()
        scope = LifetimeScope("compaction-check")
        try:
            with self._config_service.use_snapshot(snapshot):
                runtime_config = self._config_service.get_agent_runtime_config(agent_id)
                selected = runtime_config["models"][0]
                provider = await self._config_service.resolve_llm_model(ModelReferenceDTO(
                    provider_id=selected["provider_id"], model_id=selected["model_id"],
                ))
                result = await run_sync_shielded(self._check, provider, runtime_config, messages, event, scope)
        except BaseException as check_error:
            try:
                await run_cleanup_shielded(scope.close)
            except BaseException as close_error:  # noqa: BLE001 — 清理取消也须保留原检查错误。
                raise BaseExceptionGroup("压缩检查与模型释放均失败", [check_error, close_error]) from None
            raise
        await run_cleanup_shielded(scope.close)
        return result

    def _check(
        self, provider: dict[str, Any], runtime_config: dict[str, Any],
        messages: list[AnyMessage], event: object, scope: LifetimeScope,
    ) -> ContextCompactionCheck:
        from app.agents.summarization.factory import (
            create_cache_preserving_summarization_middleware,
        )
        from app.agents.summarization.planning import build_safe_compaction_partition
        from app.agents.summarization.preflight import NoDurableOwnerCompactionPreflight
        from app.agents.workspace_backend import build_workspace_backend
        from app.runtime.provider_model import build_model_from_provider

        model = build_model_from_provider(provider, runtime_config)
        scope.register(model.aclose, label="compaction-model")
        summarization = create_cache_preserving_summarization_middleware(
            model, build_workspace_backend(self._workspace_root),
            compaction_preflight=NoDurableOwnerCompactionPreflight(),
        )
        effective_messages = apply_summarization_event(messages, event)
        partition = build_safe_compaction_partition(summarization, effective_messages, event)
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
