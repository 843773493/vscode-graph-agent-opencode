"""wait 工具把软件调用身份与有界 selector 交给持久观察 owner。"""

from __future__ import annotations

from typing import Annotated

from langchain_core.tools import BaseTool, tool
from pydantic import Field

from app.abstractions.communication import SessionCommunicationProtocol
from app.agents.tool_invocation_context import ToolInvocationContext
from app.services.business.communication.wait import (
    DEFAULT_WAIT_TIMEOUT_SECONDS,
    WaitSelector,
    WaitUntil,
)


def create_wait_for_session_tool(
    session_id: str, *, communication_service: SessionCommunicationProtocol,
    invocation_context: ToolInvocationContext,
) -> BaseTool:
    @tool("wait_for_session")
    async def wait_for_session(
        target_session_id: str, communication_id: str | None = None,
        job_id: str | None = None, turn_id: str | None = None,
        until: WaitUntil = "terminal", timeout_seconds: Annotated[int, Field(ge=1, le=300)] = DEFAULT_WAIT_TIMEOUT_SECONDS,
    ) -> dict[str, object]:
        """有界等待目标 main thread；selector 至多一个，默认60秒，允许1–300秒。

        无 selector 时只观察首次快照中已存在的身份；恢复沿用原 deadline。
        """
        selectors = [WaitSelector(kind, value) for kind, value in (("communication", communication_id), ("job", job_id), ("turn", turn_id)) if value is not None]
        if len(selectors) > 1:
            raise ValueError("wait selector 至多传一个（communication_id|job_id|turn_id）")
        binding = invocation_context.thread_binding
        if binding is None or binding.session_id != session_id:
            raise RuntimeError("wait 缺少精确 source thread binding")
        return await communication_service.wait(
            session_id=binding.session_id, thread_id=binding.thread_id,
            source_call_id=invocation_context.require_operation_id("wait"),
            source_call_created_at=invocation_context.require_operation_created_at(),
            target_session_id=target_session_id, selector=selectors[0] if selectors else None,
            until=until, timeout_seconds=timeout_seconds,
        )
    return wait_for_session
