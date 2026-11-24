"""由 BaseChatModel + backend + preflight 端口构造缓存优先压缩中间件。"""

from __future__ import annotations

from deepagents.backends.protocol import BACKEND_TYPES
from langchain.chat_models import BaseChatModel as RuntimeBaseChatModel
from langchain_core.language_models.chat_models import BaseChatModel

from app.agents.summarization.middleware import CachePreservingSummarizationMiddleware
from app.agents.summarization.planning import compaction_retention
from app.domain.context_compaction import CompactionPolicy
from app.services.infrastructure.rollout_context.checkpoint.boundary.compaction_boundary_adapter import (
    CompactionPreflightPort,
)


def create_cache_preserving_summarization_middleware(
    model: BaseChatModel,
    backend: BACKEND_TYPES,
    *,
    compaction_preflight: CompactionPreflightPort,
    policy: CompactionPolicy,
    automatic_enabled: bool,
) -> CachePreservingSummarizationMiddleware:
    if not isinstance(model, RuntimeBaseChatModel):
        raise TypeError("缓存优先压缩需要 BaseChatModel 实例")
    capacity = (model.profile or {}).get("max_input_tokens")
    # TODO: 后台候选接通唯一准备链后，由该链消费 prepare/target/apply_policy，
    # 并移除此处按初始模型触发的阈值；当前仅接通自动开关和应用阈值。
    return CachePreservingSummarizationMiddleware(
        model=model,
        backend=backend,
        trigger=("fraction", policy.apply_fraction) if capacity is not None else None,
        keep=compaction_retention(capacity),
        trim_tokens_to_summarize=None,
        compaction_preflight=compaction_preflight,
        automatic_enabled=automatic_enabled and capacity is not None,
    )
