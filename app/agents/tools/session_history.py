from __future__ import annotations

from collections.abc import Awaitable
from typing import TypeVar

from langchain_core.tools import BaseTool, StructuredTool, ToolException
from pydantic import BaseModel, Field

from app.abstractions.session_context import WorkspaceSessionContextAccessError
from app.agents.custom_tools import CustomToolFactoryContext


ResultModel = TypeVar("ResultModel", bound=BaseModel)


class SessionTargetInput(BaseModel):
    workspace_id: str | None = Field(
        default=None,
        description=(
            "目标 Gateway 工作区 ID；省略时读取当前工作区，"
            "跨工作区读取时必须显式提供"
        ),
    )
    session_id: str = Field(description="要读取的目标会话 ID。")


class ReadSessionRecentTextMessagesInput(SessionTargetInput):
    """读取另一个会话最近文本消息的参数。"""

    rounds: int = Field(
        default=5,
        ge=1,
        le=50,
        description="最近用户轮次数，默认 5。",
    )


class GrepSessionContextJsonlInput(SessionTargetInput):
    """搜索另一个会话当前有效上下文 JSONL 的参数。"""

    pattern: str = Field(description="Python 正则表达式。")
    case_sensitive: bool = Field(default=False, description="是否区分大小写。")
    max_matches: int = Field(default=20, ge=1, le=200, description="最多返回的匹配行数。")
    expected_snapshot_id: str | None = Field(
        default=None,
        description="上一步返回的 snapshot_id；用于检测上下文是否已变化。",
    )


class ReadSessionContextJsonlInput(SessionTargetInput):
    """按行读取另一个会话当前有效上下文 JSONL 的参数。"""

    line_start: int = Field(default=1, ge=1, description="起始行号，从 1 开始。")
    line_count: int = Field(default=20, ge=1, le=200, description="最多读取的行数。")
    max_chars_per_line: int = Field(
        default=4000,
        ge=200,
        le=20000,
        description="每行最多返回字符数，避免单条工具记录占用过多上下文。",
    )
    expected_snapshot_id: str | None = Field(
        default=None,
        description="上一步返回的 snapshot_id；用于检测上下文是否已变化。",
    )


def _normalized_workspace_id(workspace_id: str | None) -> str | None:
    if workspace_id is None:
        return None
    normalized = workspace_id.strip()
    if not normalized:
        raise ToolException(
            "workspace_id 不能是空字符串；请改用已注册的 Gateway 工作区 ID，"
            "读取当前工作区时请省略该字段"
        )
    return normalized


async def _workspace_result_json(operation: Awaitable[ResultModel]) -> str:
    """把模型可修正的跨工作区访问错误转成失败 ToolMessage。"""
    try:
        result = await operation
    except WorkspaceSessionContextAccessError as error:
        raise ToolException(str(error)) from error
    return result.model_dump_json()


def create_read_session_recent_text_messages_tool(
    context: CustomToolFactoryContext,
) -> BaseTool:
    """创建读取当前或指定工作区 session 最近文本消息的扩展工具。"""

    async def read_session_recent_text_messages(
        session_id: str,
        rounds: int = 5,
        workspace_id: str | None = None,
    ) -> str:
        target_workspace_id = _normalized_workspace_id(workspace_id)
        if target_workspace_id is None:
            result = await context.session_context_query_service.recent_text(
                session_id,
                rounds=rounds,
            )
        else:
            return await _workspace_result_json(
                context.workspace_session_context_client.recent_text_in_workspace(
                    target_workspace_id,
                    session_id,
                    rounds=rounds,
                )
            )
        return result.model_dump_json()

    return StructuredTool.from_function(
        coroutine=read_session_recent_text_messages,
        name="read_session_recent_text_messages",
        description=(
            "读取当前工作区或指定 Gateway 工作区中另一个 session 的当前有效上下文，"
            "返回最近 N 轮用户消息及其间的模型 text 消息。默认 N=5。"
        ),
        args_schema=ReadSessionRecentTextMessagesInput,
        handle_tool_error=True,
    )


def create_grep_session_context_jsonl_tool(
    context: CustomToolFactoryContext,
) -> BaseTool:
    """创建搜索当前或指定工作区 session 上下文 JSONL 的扩展工具。"""

    async def grep_session_context_jsonl(
        session_id: str,
        pattern: str,
        case_sensitive: bool = False,
        max_matches: int = 20,
        expected_snapshot_id: str | None = None,
        workspace_id: str | None = None,
    ) -> str:
        target_workspace_id = _normalized_workspace_id(workspace_id)
        if target_workspace_id is None:
            result = await context.session_context_query_service.grep(
                session_id,
                pattern=pattern,
                case_sensitive=case_sensitive,
                max_matches=max_matches,
                expected_snapshot_id=expected_snapshot_id,
            )
        else:
            return await _workspace_result_json(
                context.workspace_session_context_client.grep_in_workspace(
                    target_workspace_id,
                    session_id,
                    pattern=pattern,
                    case_sensitive=case_sensitive,
                    max_matches=max_matches,
                    expected_snapshot_id=expected_snapshot_id,
                )
            )
        return result.model_dump_json()

    return StructuredTool.from_function(
        coroutine=grep_session_context_jsonl,
        name="grep_session_context_jsonl",
        description=(
            "像 grep 文件一样用正则搜索当前工作区或指定 Gateway 工作区中另一个 "
            "session 的当前有效模型上下文 JSONL。可传 expected_snapshot_id 检测变化。"
        ),
        args_schema=GrepSessionContextJsonlInput,
        handle_tool_error=True,
    )


def create_read_session_context_jsonl_tool(
    context: CustomToolFactoryContext,
) -> BaseTool:
    """创建读取当前或指定工作区 session 上下文 JSONL 的扩展工具。"""

    async def read_session_context_jsonl(
        session_id: str,
        line_start: int = 1,
        line_count: int = 20,
        max_chars_per_line: int = 4000,
        expected_snapshot_id: str | None = None,
        workspace_id: str | None = None,
    ) -> str:
        target_workspace_id = _normalized_workspace_id(workspace_id)
        if target_workspace_id is None:
            result = await context.session_context_query_service.read_lines(
                session_id,
                line_start=line_start,
                line_count=line_count,
                max_chars_per_line=max_chars_per_line,
                expected_snapshot_id=expected_snapshot_id,
            )
        else:
            return await _workspace_result_json(
                context.workspace_session_context_client.read_lines_in_workspace(
                    target_workspace_id,
                    session_id,
                    line_start=line_start,
                    line_count=line_count,
                    max_chars_per_line=max_chars_per_line,
                    expected_snapshot_id=expected_snapshot_id,
                )
            )
        return result.model_dump_json()

    return StructuredTool.from_function(
        coroutine=read_session_context_jsonl,
        name="read_session_context_jsonl",
        description=(
            "像 read 文件一样按行读取当前工作区或指定 Gateway 工作区中另一个 "
            "session 的当前有效模型上下文 JSONL。可传 expected_snapshot_id 检测变化。"
        ),
        args_schema=ReadSessionContextJsonlInput,
        handle_tool_error=True,
    )
