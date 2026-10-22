"""事件流结果和异常契约；消费者直接依赖这里，不通过 processor 转引。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from langgraph.types import Durability, StateSnapshot

from app.schemas.event import ModelTokenUsagePayload


class AgentEventSource(Protocol):
    """只暴露事件迭代能力，不暴露 public service 或 Agent 构建依赖。"""

    def astream_events(
        self, input_payload: dict[str, Any] | None, *, config: dict[str, Any],
        version: str, durability: Durability,
    ) -> AsyncIterator[dict[str, Any]]: ...

    async def aget_state(self, config: dict[str, Any]) -> StateSnapshot: ...


class AgentEventStreamTimeoutError(TimeoutError):
    """Agent 事件流在模型/工具事件出现前或模型响应期间未收敛。"""

    def __init__(self, message: str, *, code: str = "agent_event_timeout") -> None:
        self.code = code
        super().__init__(message)


class CanonicalItemObserverError(RuntimeError):
    """Saver 已提交精确 item 集合，随后 observer 失败；原异常仍必须向上传递。"""

    def __init__(
        self, observer_error: Exception, *, committed_item_ids: tuple[str, ...],
    ) -> None:
        self.observer_error = observer_error
        self.committed_item_ids = committed_item_ids
        super().__init__(f"canonical item 已提交但 observer 失败: items={committed_item_ids}: {observer_error}")


@dataclass(frozen=True, slots=True)
class SuccessfulToolCall:
    tool_name: str
    tool_args: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AgentEventStreamResult:
    final_text: str
    latest_model_content_blocks: tuple[dict[str, object], ...]
    last_tool_result_text: str
    successful_tool_calls: tuple[SuccessfulToolCall, ...] = ()
    completed_custom_tool_names: tuple[str, ...] = ()
    token_usage: ModelTokenUsagePayload = field(default_factory=ModelTokenUsagePayload)


@dataclass(frozen=True, slots=True)
class ToolEventDisplayContext:
    tool_name: str
    tool_args: dict[str, Any]
    invocation_tool_name: str | None
