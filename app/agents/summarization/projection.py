"""把压缩事件（_summarization_event）投影为模型可见消息与状态边界。

事件读取与边界映射的纯函数；不做任何配对或分区决策。"""

from __future__ import annotations

from collections.abc import Mapping

from langchain_core.messages import (
    AnyMessage,
    BaseMessage,
)

from app.agents.summarization.state import CACHE_PRESERVING_STRATEGY


def _event_int(event: Mapping[str, object], key: str) -> int:
    value = event.get(key)
    if not isinstance(value, int) or value < 0:
        raise TypeError(f"_summarization_event.{key} 必须是非负整数，实际值: {value!r}")
    return value


def _event_prefix_messages(event: Mapping[str, object]) -> list[AnyMessage]:
    value = event.get("cache_prefix_messages")
    if not isinstance(value, list):
        raise TypeError("cache_preserving 压缩事件缺少 cache_prefix_messages 列表")
    for index, message in enumerate(value):
        if not isinstance(message, BaseMessage):
            raise TypeError(
                "cache_prefix_messages 中出现不支持的消息类型: "
                f"index={index}, type={type(message).__name__}"
            )
    return list(value)


def apply_summarization_event(
    messages: list[AnyMessage],
    event: object,
) -> list[AnyMessage]:
    """把压缩事件投影为模型实际可见的消息。"""
    if event is None:
        return list(messages)
    if not isinstance(event, Mapping):
        raise TypeError("_summarization_event 必须是 mapping")

    cutoff = _event_int(event, "cutoff_index")
    if cutoff > len(messages):
        raise ValueError(
            "_summarization_event.cutoff_index 超过消息数量: "
            f"cutoff={cutoff}, messages={len(messages)}"
        )
    summary_message = event.get("summary_message")
    if not isinstance(summary_message, BaseMessage):
        raise TypeError("_summarization_event.summary_message 必须是消息对象")

    if event.get("strategy") == CACHE_PRESERVING_STRATEGY:
        prefix = _event_prefix_messages(event)
        return [*prefix, summary_message, *messages[cutoff:]]
    return [summary_message, *messages[cutoff:]]


def effective_cutoff_to_state_cutoff(
    event: object,
    effective_cutoff: int,
) -> int:
    """将模型投影中的边界转换成原始 checkpoint 消息边界。"""
    if event is None:
        return effective_cutoff
    if not isinstance(event, Mapping):
        raise TypeError("_summarization_event 必须是 mapping")

    previous_cutoff = _event_int(event, "cutoff_index")
    if event.get("strategy") == CACHE_PRESERVING_STRATEGY:
        prefix_count = len(_event_prefix_messages(event))
        dynamic_start = prefix_count + 1
        if effective_cutoff < dynamic_start:
            raise ValueError(
                "新的压缩边界位于缓存稳定前缀内部，无法保持缓存: "
                f"effective_cutoff={effective_cutoff}, prefix_count={prefix_count}"
            )
        return previous_cutoff + effective_cutoff - dynamic_start

    if effective_cutoff < 1:
        raise ValueError("已有摘要后的压缩边界必须至少越过摘要消息")
    return previous_cutoff + effective_cutoff - 1


def replacement_effective_cutoff_to_state_cutoff(
    event: object,
    effective_cutoff: int,
) -> int:
    """允许替换稳定前缀时，将投影边界映射回原始 checkpoint。"""
    if event is None:
        return effective_cutoff
    if not isinstance(event, Mapping):
        raise TypeError("_summarization_event 必须是 mapping")

    previous_cutoff = _event_int(event, "cutoff_index")
    if event.get("strategy") == CACHE_PRESERVING_STRATEGY:
        prefix_count = len(_event_prefix_messages(event))
        if effective_cutoff <= prefix_count:
            raise ValueError(
                "替换压缩必须同时吞并旧稳定前缀后的摘要消息: "
                f"effective_cutoff={effective_cutoff}, prefix_count={prefix_count}"
            )
        return previous_cutoff + effective_cutoff - prefix_count - 1
    if effective_cutoff < 1:
        raise ValueError("已有摘要后的替换边界必须至少越过摘要消息")
    return previous_cutoff + effective_cutoff - 1
