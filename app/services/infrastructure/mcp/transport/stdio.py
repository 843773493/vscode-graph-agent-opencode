"""唯一 stdio 传输；协议解析和序列化始终交给官方 SDK 类型。"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from anyio.streams.text import TextReceiveStream
from pydantic import ValidationError

from app.core.child_process import OwnedChildProcess
from app.core.shielded_cleanup import run_cleanup_shielded
from mcp import StdioServerParameters
from mcp.client.stdio import get_default_environment
from mcp.os.win32.utilities import get_windows_executable_command
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage


class McpProtocolValidationError(ValueError):
    """官方 SDK 类型校验失败，只交付错误数量，避免持久化消息原值。"""


@asynccontextmanager
async def mcp_stdio_transport(parameters: StdioServerParameters, process: OwnedChildProcess,
                              on_started: Callable[[], Awaitable[None]], *, unset_env: tuple[str, ...] = ()) -> AsyncIterator[tuple[
                                  MemoryObjectReceiveStream[SessionMessage | Exception], MemoryObjectSendStream[SessionMessage]]]:
    incoming, read = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    write, outgoing = anyio.create_memory_object_stream[SessionMessage](0)

    async def receive() -> None:
        assert process.process is not None and process.process.stdout is not None
        async with incoming:
            pending = ""
            async for chunk in TextReceiveStream(process.process.stdout, encoding=parameters.encoding,
                                                  errors=parameters.encoding_error_handler):
                lines = (pending + chunk).split("\n")
                pending = lines.pop()
                for line in lines:
                    try:
                        message = SessionMessage(JSONRPCMessage.model_validate_json(line))
                    except ValidationError as error:
                        raise McpProtocolValidationError(f"MCP JSON-RPC 类型校验失败: count={error.error_count()}") from None
                    else:
                        await incoming.send(message)

    async def send() -> None:
        assert process.process is not None and process.process.stdin is not None
        async with outgoing:
            async for message in outgoing:
                payload = message.message.model_dump_json(by_alias=True, exclude_none=True)
                await process.process.stdin.send((payload + "\n").encode(parameters.encoding, parameters.encoding_error_handler))

    try:
        async with anyio.create_task_group() as group:
            try:
                command = get_windows_executable_command(parameters.command) if sys.platform == "win32" else parameters.command
                environment = {**get_default_environment(), **(parameters.env or {})}
                for key in unset_env:
                    environment.pop(key, None)
                await process.start([command, *parameters.args], env=environment, cwd=parameters.cwd)
                await on_started()
                group.start_soon(receive)
                group.start_soon(send)
                yield read, write
            finally:
                group.cancel_scope.cancel()
                # Windows 文件管道读取可能仍在 worker 中等待；先关原进程，才能排空读任务。
                with anyio.CancelScope(shield=True):
                    await run_cleanup_shielded(process.close)
    finally:
        read.close()
        write.close()
        incoming.close()
        outgoing.close()
