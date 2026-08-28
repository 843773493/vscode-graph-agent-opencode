from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Awaitable, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from app.core.turn_execution_scope import (
    TurnExecutionScope,
    get_current_turn_execution_scope,
    reset_current_turn_execution_scope,
    set_current_turn_execution_scope,
)
from app.services.infrastructure.resource_manager import (
    ResourceManager,
    resource_refs_from_tool_payload,
)


class ToolInvocationContext:
    """由 Agent 后端注入的单次工具调用上下文，不属于模型参数。"""

    def __init__(
        self,
        *,
        tool_timeout_seconds: float | None = None,
        resource_manager: ResourceManager | None = None,
    ) -> None:
        self._tool_call_id: contextvars.ContextVar[str | None] = (
            contextvars.ContextVar("agent_tool_call_id", default=None)
        )
        if tool_timeout_seconds is not None and tool_timeout_seconds <= 0:
            raise ValueError("tool_timeout_seconds 必须大于 0")
        self.tool_timeout_seconds = tool_timeout_seconds
        self.resource_manager = resource_manager

    def set_tool_call_id(
        self,
        tool_call_id: str,
    ) -> contextvars.Token[str | None]:
        if not tool_call_id:
            raise ValueError("tool_call_id 不能为空")
        return self._tool_call_id.set(tool_call_id)

    def reset_tool_call_id(
        self,
        token: contextvars.Token[str | None],
    ) -> None:
        self._tool_call_id.reset(token)

    def require_tool_call_id(self) -> str:
        tool_call_id = self._tool_call_id.get()
        if not tool_call_id:
            raise RuntimeError("当前工具执行上下文缺少 tool_call_id")
        return tool_call_id


class ToolInvocationContextMiddleware(AgentMiddleware):
    """在统一执行层注入调用身份，业务工具通过闭包读取。"""

    def __init__(self, context: ToolInvocationContext) -> None:
        self._context = context

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[object]],
    ) -> ToolMessage | Command[object]:
        token = self._bind(request)
        try:
            return handler(request)
        finally:
            self._context.reset_tool_call_id(token)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[
            [ToolCallRequest],
            Awaitable[ToolMessage | Command[object]],
        ],
    ) -> ToolMessage | Command[object]:
        token = self._bind(request)
        parent_scope = get_current_turn_execution_scope()
        tool_scope = (
            parent_scope.child(
                f"tool-{request.tool_call.get('id')}",
                timeout_seconds=self._context.tool_timeout_seconds,
            )
            if parent_scope is not None
            else None
        )
        scope_token = (
            set_current_turn_execution_scope(tool_scope)
            if tool_scope is not None
            else None
        )
        resource_leases = self._acquire_resource_leases(
            request,
            parent_scope=parent_scope,
        )
        task = asyncio.create_task(handler(request))
        abort_hook_id = (
            tool_scope.register_abort(lambda _reason: _cancel_task(task))
            if tool_scope is not None
            else None
        )
        try:
            if self._context.tool_timeout_seconds is None:
                return await task
            try:
                return await asyncio.wait_for(task, self._context.tool_timeout_seconds)
            except TimeoutError as error:
                if tool_scope is not None:
                    await tool_scope.cancel("scope_deadline_exceeded")
                tool_call_id = request.tool_call.get("id")
                if not isinstance(tool_call_id, str) or not tool_call_id:
                    raise RuntimeError("工具局部超时缺少 tool_call_id") from error
                return ToolMessage(
                    content="工具执行超过局部超时，结果未确认",
                    tool_call_id=tool_call_id,
                    status="error",
                )
        except asyncio.CancelledError:
            if tool_scope is not None:
                tool_scope.raise_if_cancelled()
            raise
        finally:
            if tool_scope is not None and abort_hook_id is not None:
                tool_scope.remove_abort(abort_hook_id)
            if scope_token is not None:
                reset_current_turn_execution_scope(scope_token)
            if tool_scope is not None:
                await tool_scope.close()
            keep_leases_for_reconcile = bool(
                tool_scope is not None
                and tool_scope.cancellation_signal.is_cancelled
                and tool_scope.cancellation_signal.reason
                in {"user_requested", "execution_lost"}
            )
            if not keep_leases_for_reconcile:
                for lease_id in resource_leases:
                    self._context.resource_manager.release(lease_id)
                    if parent_scope is not None:
                        parent_scope.remove_lease(lease_id)
            self._context.reset_tool_call_id(token)

    def _acquire_resource_leases(
        self,
        request: ToolCallRequest,
        *,
        parent_scope: TurnExecutionScope | None,
    ) -> list[str]:
        manager = self._context.resource_manager
        if manager is None or parent_scope is None:
            return []
        resource_refs = _resource_refs_from_tool_call(request)
        lease_ids: list[str] = []
        operation_id = str(request.tool_call["id"])
        try:
            for resource_id, kind in resource_refs:
                manager.register_external(
                    resource_id=resource_id,
                    kind=kind,
                    created_by_turn_id=parent_scope.turn_stream_id,
                )
                lease = manager.acquire_operation(
                    resource_id=resource_id,
                    turn_stream_id=parent_scope.turn_stream_id,
                    operation_id=operation_id,
                )
                parent_scope.add_lease(lease.lease_id)
                lease_ids.append(lease.lease_id)
        except Exception:
            for lease_id in lease_ids:
                manager.release(lease_id, reason="operation_setup_failed")
                parent_scope.remove_lease(lease_id)
            raise
        return lease_ids

    def _bind(
        self,
        request: ToolCallRequest,
    ) -> contextvars.Token[str | None]:
        tool_call_id = request.tool_call.get("id")
        if not isinstance(tool_call_id, str) or not tool_call_id:
            tool_name = request.tool_call.get("name")
            raise RuntimeError(
                "工具调用缺少 tool_call_id，无法建立后端调用上下文: "
                f"tool_name={tool_name!r}"
            )
        return self._context.set_tool_call_id(tool_call_id)


async def _cancel_task(task: asyncio.Task[object]) -> None:
    if not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def _resource_refs_from_tool_call(
    request: ToolCallRequest,
) -> tuple[tuple[str, str], ...]:
    return resource_refs_from_tool_payload(
        request.tool_call.get("name"),
        request.tool_call.get("args"),
    )


__all__ = [
    "ToolInvocationContext",
    "ToolInvocationContextMiddleware",
]
