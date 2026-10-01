"""缓存优先压缩的常量、错误类型、LangGraph 状态 schema 与压缩分区。

承载策略常量、SummaryToolCallError、CachePreservingSummarizationState（LangGraph 状态
schema，含 Annotated/PrivateStateAttr 运行期契约）与 CachePreservingPartition。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import (
    Annotated,
    NotRequired,
)

from deepagents.middleware.summarization import SummarizationState
from langchain.agents.middleware.types import PrivateStateAttr
from langchain_core.messages import AnyMessage

CACHE_PRESERVING_STRATEGY = "cache_preserving"
CACHE_REPLACEMENT_STRATEGY = "cache_replacement"
_MIN_CACHE_PREFIX_MESSAGES = 2
_PREFERRED_CACHE_PREFIX_MESSAGES = 4
_MAX_CACHE_PREFIX_MESSAGES = 8
_MAX_SUMMARY_OVERFLOW_RETRIES = 3
_TOOL_PAYLOAD_RETRY_MAX_CHARS = 4096
_TOOL_PAYLOAD_RETRY_TOTAL_CHARS = 8192
_MEDIA_BLOCK_MARKERS = {
    "document": "[document]",
    "file": "[document]",
    "image": "[image]",
    "image_url": "[image]",
    "input_file": "[document]",
    "input_image": "[image]",
}


class SummaryToolCallError(RuntimeError):
    """摘要模型违反纯文本约束并尝试调用工具。"""


class CachePreservingSummarizationState(SummarizationState):
    """缓存压缩额外维护一次性强制压缩标记。"""

    _force_cache_compaction: Annotated[NotRequired[bool], PrivateStateAttr]


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
