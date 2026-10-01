"""由 BaseChatModel + backend + preflight 端口构造缓存优先压缩中间件。"""

from __future__ import annotations

from deepagents.backends.protocol import BACKEND_TYPES
from deepagents.middleware.summarization import compute_summarization_defaults
from langchain.chat_models import BaseChatModel as RuntimeBaseChatModel
from langchain_core.language_models.chat_models import BaseChatModel

from app.agents.summarization.middleware import CachePreservingSummarizationMiddleware
from app.services.infrastructure.rollout_context.checkpoint.boundary.compaction_boundary_adapter import (
    CompactionPreflightPort,
)


def create_cache_preserving_summarization_middleware(
    model: BaseChatModel,
    backend: BACKEND_TYPES,
    *,
    compaction_preflight: CompactionPreflightPort,
) -> CachePreservingSummarizationMiddleware:
    if not isinstance(model, RuntimeBaseChatModel):
        raise TypeError("缓存优先压缩需要 BaseChatModel 实例")
    defaults = compute_summarization_defaults(model)
    return CachePreservingSummarizationMiddleware(
        model=model,
        backend=backend,
        trigger=defaults["trigger"],
        keep=defaults["keep"],
        trim_tokens_to_summarize=None,
        truncate_args_settings=defaults["truncate_args_settings"],
        compaction_preflight=compaction_preflight,
    )
