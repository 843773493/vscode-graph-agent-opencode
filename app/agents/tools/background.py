from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool, tool

from app.abstractions.background_message_bus import BackgroundMessageBusProtocol
from app.agents.tool_invocation_context import ToolInvocationContext
from app.core.background_task_contracts import (
    BackgroundTaskDescriptor,
    BackgroundToolResultIdentity,
)
from app.core.background_task_registry import BackgroundTaskRegistry
from app.domain.itemized.identity.item_projection import ItemProjectionIdentity
from app.domain.itemized.identity.tool_call_identity import provider_tool_call_id


def _thread(invocation_context: ToolInvocationContext, session_id: str) -> str:
    binding = invocation_context.thread_binding
    if binding is None or binding.session_id != session_id:
        raise RuntimeError("后台工具缺少匹配的精确 ThreadRuntimeBinding")
    return binding.thread_id


def create_system_time_emitter_tool(
    session_id: str,
    agent_id: str = "default",
    *,
    background_task_registry: BackgroundTaskRegistry,
    invocation_context: ToolInvocationContext,
) -> BaseTool:
    """创建 versioned 时间消息任务；恢复 identity 由持久 emitting call 派生。"""
    @tool("emit_system_time_messages")
    async def emit_system_time_messages(
        interval_seconds: float = 1.0,
        message_count: int = 5,
        source_id: str | None = None,
    ) -> dict[str, Any]:
        """按固定间隔向当前 thread 的后台邮箱发送系统时间。"""
        if interval_seconds <= 0 or message_count <= 0:
            raise ValueError("interval_seconds 和 message_count 必须大于 0")
        task_id = "bgt_" + invocation_context.require_operation_id("background_time_emitter").removeprefix("cop_")
        handle = await background_task_registry.spawn(
            session_id=session_id, thread_id=_thread(invocation_context, session_id),
            agent_id=agent_id, task_id=task_id,
            descriptor=BackgroundTaskDescriptor(
                kind="emit_system_time_messages", version=1,
                payload={"source_id": source_id or task_id,
                         "interval_seconds": interval_seconds, "message_count": message_count,
                         "started_at": invocation_context.require_operation_created_at().isoformat()},
            ),
        )
        return handle.to_dict()

    return emit_system_time_messages


def create_background_message_collection_tool(
    session_id: str,
    agent_id: str = "default",
    *,
    background_message_bus: BackgroundMessageBusProtocol,
    invocation_context: ToolInvocationContext,
) -> BaseTool:
    """创建按持久 consumption identity 恢复的精确 Thread 邮箱收集工具。"""
    @tool("collect_background_messages")
    async def collect_background_messages(
        source_id: str | None = None,
        after_message_id: str | None = None,
        timeout_seconds: int = 300,
        poll_interval_seconds: float = 1.0,
        stop_on_interrupt: bool = True,
    ) -> dict[str, Any]:
        """持续收集当前 thread 与 agent selector 的后台消息。"""
        batch = await background_message_bus.collect(
            session_id, _thread(invocation_context, session_id), agent_id,
            consumption_id=invocation_context.require_operation_id("background_collect"),
            source_id=source_id, after_message_id=after_message_id,
            timeout_seconds=timeout_seconds, poll_interval_seconds=poll_interval_seconds,
            stop_on_interrupt=stop_on_interrupt,
            tool_result_identity=BackgroundToolResultIdentity(
                model_call_id=invocation_context.require_model_call_id(),
                tool_call_id=provider_tool_call_id(
                    ItemProjectionIdentity(model_call_id=invocation_context.require_model_call_id()),
                    invocation_context.require_tool_call_id(),
                ),
            ),
        )
        # canonical ToolResult 提交后由 sink 确认；此处不能把尚未落盘的返回当成已处理。
        return batch.model_dump(mode="json")

    return collect_background_messages
