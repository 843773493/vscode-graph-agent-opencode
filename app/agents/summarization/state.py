"""缓存优先压缩的纯状态、策略常量与错误类型。"""

from __future__ import annotations

from dataclasses import dataclass

from langchain_core.messages import AnyMessage

CACHE_PRESERVING_STRATEGY = "cache_preserving"
CACHE_REPLACEMENT_STRATEGY = "cache_replacement"
_MIN_CACHE_PREFIX_MESSAGES = 2
_PREFERRED_CACHE_PREFIX_MESSAGES = 4
_MAX_CACHE_PREFIX_MESSAGES = 8
_MAX_SUMMARY_OVERFLOW_RETRIES = 3


class SummaryToolCallError(RuntimeError):
    """摘要模型违反纯文本约束并尝试调用工具。"""


class SummaryTextError(ValueError):
    """摘要响应没有可见正文。"""


class SummaryGenerationError(RuntimeError):
    """第三方摘要器明确返回生成失败。"""


class CompactionReductionError(RuntimeError):
    """生成的摘要没有缩短本次上下文。"""


@dataclass(slots=True)
class CachePreservingPartition:
    prefix_messages: list[AnyMessage]
    messages_to_summarize: list[AnyMessage]
    preserved_messages: list[AnyMessage]
    state_cutoff: int
    # overflow retry 中段切点经唯一 compaction preflight 过滤后的安全子集
    # （中段坐标）；由 model-call 路径写入，调度预览路径保持默认空集。
    safe_retry_middle_boundaries: frozenset[int] = frozenset()

    @property
    def effective_messages(self) -> list[AnyMessage]:
        return [*self.prefix_messages, *self.preserved_messages]
