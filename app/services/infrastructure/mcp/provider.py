"""原宿主的短期启动准入；不保存 resolve 参数，不拥有 MCP client。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Literal, Protocol

from app.schemas.gateway.mcp_definitions import (
    McpProviderHttpLaunchDTO,
    McpProviderStdioLaunchDTO,
)
from app.schemas.internal_v2.mcp import McpProviderDefinitionRefDTO


class McpProviderUnavailable(RuntimeError):
    """来源引用仍保留，但原宿主或原定义已不能准入。"""


class McpProviderLaunchError(RuntimeError):
    """原 SDK 失败的安全诊断，不携带一次性 launch 或响应正文。"""


def is_provider_unavailable(error: BaseException) -> bool:
    if isinstance(error, BaseExceptionGroup):
        return bool(error.exceptions) and all(is_provider_unavailable(item) for item in error.exceptions)
    return isinstance(error, McpProviderUnavailable)


def _error_types(error: BaseException) -> str:
    if isinstance(error, BaseExceptionGroup):
        return ",".join(_error_types(item) for item in error.exceptions)
    return type(error).__name__


_private_launch: ContextVar[bool] = ContextVar("mcp_provider_private_launch", default=False)


class _ProviderLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if _private_launch.get():
            # SDK 和 HTTP wire 可能记录原 header、响应正文及 Pydantic 输入。
            error = record.exc_info[1] if record.exc_info else None
            record.msg = "MCP Provider SDK: event=%s error_types=%s"
            record.args = (record.funcName, _error_types(error) if error else "none")
            record.exc_info = record.exc_text = record.stack_info = None
        return True


_provider_log_filter = _ProviderLogFilter()
for _logger_name in ("", "mcp.client.session", "mcp.shared.session", "mcp.client.stdio", "mcp.client.streamable_http",
                     "httpx", "httpcore.connection", "httpcore.http11", "httpcore.http2", "httpcore.proxy", "httpcore.socks"):
    logging.getLogger(_logger_name).addFilter(_provider_log_filter)


@contextmanager
def provider_launch_diagnostics(private: bool) -> Iterator[None]:
    if not private:
        yield
        return
    token = _private_launch.set(True)
    try:
        yield
    except Exception as error:
        if is_provider_unavailable(error):
            raise
        # 第三方异常可能含原 launch 秘密，禁止原件进入持久错误链。
        raise McpProviderLaunchError(f"MCP Provider SDK 连接失败: error_types=[{_error_types(error)}]") from None
    finally:
        _private_launch.reset(token)


@dataclass(frozen=True, slots=True)
class McpProviderAdmission:
    launch: McpProviderStdioLaunchDTO | McpProviderHttpLaunchDTO = field(repr=False)
    invalidated: asyncio.Event
    check: Callable[[], None]
    committed: asyncio.Event = field(default_factory=asyncio.Event)


class McpDefinitionResolver(Protocol):
    def check_mcp_definition(self, reference: McpProviderDefinitionRefDTO, *, workspace_id: str,
                             transport: Literal["stdio", "streamable_http"]) -> None: ...

    async def resolve_mcp_definition(self, reference: McpProviderDefinitionRefDTO, *, workspace_id: str,
                                     transport: Literal["stdio", "streamable_http"]) -> McpProviderAdmission: ...
