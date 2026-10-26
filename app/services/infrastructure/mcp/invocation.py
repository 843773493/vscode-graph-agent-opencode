"""SDK 调用、受信资源使用与结果未知合同，不保存第二份调用历史。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal, TypeVar

import anyio
from langchain_core.tools import BaseTool
from pydantic import BaseModel

from app.core.shielded_cleanup import run_cleanup_shielded
from app.schemas.internal_v2.mcp import McpInvocationReceipt
from mcp import types
from mcp.client.session import ClientSession
from mcp.shared.exceptions import McpError
from mcp.shared.message import MessageMetadata
from mcp.shared.session import ProgressFnT

logger = logging.getLogger(__name__)
McpProgressCallback = Callable[[float, float | None], Awaitable[None]]
_PROGRESS: ContextVar[McpProgressCallback | None] = ContextVar("mcp_progress", default=None)
_Result = TypeVar("_Result", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class McpResourceUse:
    """由执行上下文声明的精确占用，不能从模型参数推测。"""

    holder_id: str
    operation_id: str
    session_id: str
    thread_id: str
    model_call_id: str


@dataclass(slots=True)
class ExtensionExecutionTarget:
    """单次准入的短期 handle；持久占用仍归原资源账本。"""

    tool: BaseTool
    started: bool = False
    result_confirmed: bool = False


_EXECUTION: ContextVar[ExtensionExecutionTarget | None] = ContextVar("extension_execution", default=None)


@contextmanager
def extension_execution_scope(execution: ExtensionExecutionTarget) -> Iterator[None]:
    token = _EXECUTION.set(execution)
    try:
        yield
    finally:
        _EXECUTION.reset(token)


def confirm_mcp_result() -> None:
    """只有 SDK 收到真实结果／协议错误，才能确认本次远端等待。"""
    execution = _EXECUTION.get()
    if execution is not None:
        execution.result_confirmed = True


@dataclass(slots=True)
class McpCallState:
    cancellation_delivery: Literal["not_requested", "sent", "failed"] = "not_requested"


_CALL: ContextVar[McpCallState | None] = ContextVar("mcp_call", default=None)


@contextmanager
def mcp_call_scope(state: McpCallState) -> Iterator[McpProgressCallback | None]:
    token = _CALL.set(state)
    try:
        # SDK 在接收 task 调用进度闭包，必须在原调用 task 捕获它。
        yield _PROGRESS.get()
    finally:
        _CALL.reset(token)


@contextmanager
def mcp_progress_scope(callback: McpProgressCallback | None) -> Iterator[None]:
    token = _PROGRESS.set(callback)
    try:
        yield
    finally:
        _PROGRESS.reset(token)


def mcp_cancelled_receipt(error: BaseException | None) -> McpInvocationReceipt | None:
    if isinstance(error, asyncio.CancelledError) and len(error.args) == 1 and isinstance(error.args[0], McpInvocationReceipt):
        return error.args[0]
    return None


class CancellableMcpClientSession(ClientSession):
    """复用 SDK 请求分配／读写，仅补原请求的取消通知。"""

    async def send_request(
        self, request: types.ClientRequest, result_type: type[_Result],
        request_read_timeout_seconds: timedelta | None = None,
        metadata: MessageMetadata = None, progress_callback: ProgressFnT | None = None,
    ) -> _Result:
        state = _CALL.get() if isinstance(request.root, types.CallToolRequest) else None
        # TODO: SDK 公开请求 handle 后移除对 _request_id 的读取；分配与 super
        # 首个 await 之间没有让出点，故并发调用各自取得原请求 ID，绝不按工具名取消。
        request_id = self._request_id
        try:
            return await super().send_request(
                request, result_type, request_read_timeout_seconds, metadata, progress_callback,
            )
        except (asyncio.CancelledError, McpError) as error:
            if state is not None and (isinstance(error, asyncio.CancelledError) or error.error.code == 408):
                async def notify() -> None:
                    state.cancellation_delivery = "failed"
                    try:
                        with anyio.fail_after(2):
                            await self.send_notification(types.ClientNotification(types.CancelledNotification(
                                params=types.CancelledNotificationParams(requestId=request_id, reason="caller_wait_ended"),
                            )))
                    except (OSError, TimeoutError, anyio.BrokenResourceError, anyio.ClosedResourceError):
                        # 原调用仍交付结果未知回执；取消投递失败不能覆盖其身份或伪报已停止。
                        logger.exception("MCP 取消通知投递失败: request_id=%s", request_id)
                    else:
                        state.cancellation_delivery = "sent"

                await run_cleanup_shielded(notify)
            raise
