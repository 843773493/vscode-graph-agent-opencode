"""缓存优先压缩分区与事件构造。

按稳定性挑选缓存前缀切点，计算各入口共用的安全压缩分区，并把分区固化为
_summarization_event；安全性判定委托唯一 compaction preflight。"""

from __future__ import annotations

from collections.abc import (
    Mapping,
    Sequence,
)

# TODO: DeepAgents 暴露公共的压缩扩展基类后，改用公共 API，避免依赖私有实现类。
from deepagents.middleware.summarization import _DeepAgentsSummarizationMiddleware
from langchain_core.messages import (
    AnyMessage,
    HumanMessage,
)

from app.agents.summarization.projection import (
    _event_prefix_messages,
    effective_cutoff_to_state_cutoff,
    replacement_effective_cutoff_to_state_cutoff,
)
from app.agents.summarization.state import (
    _MAX_CACHE_PREFIX_MESSAGES,
    _MIN_CACHE_PREFIX_MESSAGES,
    _PREFERRED_CACHE_PREFIX_MESSAGES,
    CACHE_PRESERVING_STRATEGY,
    CachePreservingPartition,
)
from app.services.infrastructure.rollout_context.checkpoint.boundary.compaction_boundary_adapter import (
    prefix_has_open_tool_group,
)


def _prefix_cutoff_candidates(
    messages: list[AnyMessage],
    summarize_end: int,
) -> list[int]:
    """按稳定性排序的缓存前缀切点候选。

    缓存前缀应停在一轮对话结束处；HumanMessage 起始的轮次边界优先，
    其余位置降序兜底。安全性一律由唯一 compaction preflight port 判定
    （调度预览路径用内存闭合初筛），本函数不做任何配对判断。
    """
    if summarize_end <= _MIN_CACHE_PREFIX_MESSAGES:
        return []
    preferred_minimum = (
        _PREFERRED_CACHE_PREFIX_MESSAGES
        if summarize_end > _PREFERRED_CACHE_PREFIX_MESSAGES
        else _MIN_CACHE_PREFIX_MESSAGES
    )
    target = min(
        _MAX_CACHE_PREFIX_MESSAGES,
        max(preferred_minimum, summarize_end // 4),
        summarize_end - 1,
    )
    return [
        *(
            index
            for index in range(target, 0, -1)
            if isinstance(messages[index], HumanMessage)
        ),
        *(
            index
            for index in range(target, 0, -1)
            if not isinstance(messages[index], HumanMessage)
        ),
    ]


def build_cache_preserving_partition(
    summarization: _DeepAgentsSummarizationMiddleware,
    effective_messages: list[AnyMessage],
    event: object,
    summarize_end: int,
    *,
    prefix_cutoff_candidates: Sequence[int] | None = None,
) -> CachePreservingPartition | None:
    """保留已经发送过的前缀，只摘要中段并继续保留近期尾部。"""
    if (
        isinstance(event, Mapping)
        and event.get("strategy") == CACHE_PRESERVING_STRATEGY
    ):
        prefix_messages = _event_prefix_messages(event)
        middle_start = len(prefix_messages)
        # effective_messages 中紧随稳定前缀的是上一次摘要，也要滚入新摘要。
    else:
        candidates = (
            prefix_cutoff_candidates
            if prefix_cutoff_candidates is not None
            # 调度预览合同：没有 preflight 信息时用内存闭合初筛，
            # 最终 durable 安全门在 model-call 路径的 preflight port。
            else [
                index
                for index in _prefix_cutoff_candidates(
                    effective_messages,
                    summarize_end,
                )
                if not prefix_has_open_tool_group(effective_messages, index)
            ]
        )
        middle_start = next(iter(candidates), 0)
        if middle_start == 0:
            return None
        prefix_messages = list(effective_messages[:middle_start])

    if summarize_end <= middle_start:
        return None
    messages_to_summarize = list(effective_messages[middle_start:summarize_end])
    if not messages_to_summarize:
        return None
    state_cutoff = effective_cutoff_to_state_cutoff(event, summarize_end)
    return CachePreservingPartition(
        prefix_messages=prefix_messages,
        messages_to_summarize=messages_to_summarize,
        preserved_messages=list(effective_messages[summarize_end:]),
        state_cutoff=state_cutoff,
    )


def build_safe_compaction_partition(
    summarization: _DeepAgentsSummarizationMiddleware,
    effective_messages: list[AnyMessage],
    event: object,
    *,
    prefix_cutoff_candidates: Sequence[int] | None = None,
) -> CachePreservingPartition | None:
    """统一计算自动、HTTP 与工具入口使用的安全压缩分区。"""
    summarize_end = summarization._determine_cutoff_index(effective_messages)
    replacement_required = summarize_end <= 0
    if replacement_required:
        if len(effective_messages) <= 1:
            return None
        summarize_end = summarization._find_safe_cutoff_point(
            effective_messages,
            len(effective_messages) - 1,
        )
        if summarize_end <= 0:
            return None

    partition = None
    if not replacement_required:
        partition = build_cache_preserving_partition(
            summarization,
            effective_messages,
            event,
            summarize_end,
            prefix_cutoff_candidates=prefix_cutoff_candidates,
        )
    if partition is not None:
        return partition

    # 没有可保留的完整轮次边界时，最终允许替换旧前缀，避免超限会话永久卡死。
    if (
        isinstance(event, Mapping)
        and event.get("strategy") == CACHE_PRESERVING_STRATEGY
    ):
        summarize_end = max(
            summarize_end,
            len(_event_prefix_messages(event)) + 1,
        )
    return CachePreservingPartition(
        prefix_messages=[],
        messages_to_summarize=list(effective_messages[:summarize_end]),
        preserved_messages=list(effective_messages[summarize_end:]),
        state_cutoff=replacement_effective_cutoff_to_state_cutoff(
            event,
            summarize_end,
        ),
    )


def build_cache_preserving_event(
    partition: CachePreservingPartition,
    *,
    summary_message: AnyMessage,
    file_path: str,
    strategy: str = CACHE_PRESERVING_STRATEGY,
) -> dict[str, object]:
    event: dict[str, object] = {
        "strategy": strategy,
        "cutoff_index": partition.state_cutoff,
        "cache_prefix_messages": partition.prefix_messages,
        "summary_message": summary_message,
        "file_path": file_path,
    }
    cutoff_message = (
        partition.messages_to_summarize[-1] if partition.messages_to_summarize else None
    )
    if cutoff_message is not None:
        message_id = cutoff_message.id
        if not isinstance(message_id, str) or not message_id:
            metadata = cutoff_message.response_metadata or {}
            candidate = metadata.get("message_id")
            message_id = candidate if isinstance(candidate, str) else None
        if message_id:
            event["cutoff_message_id"] = message_id
    return event
