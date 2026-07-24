from __future__ import annotations

from collections.abc import Awaitable
from typing import Literal, TypeVar
from urllib.parse import urlparse

from langchain_core.tools import BaseTool, StructuredTool, ToolException
from pydantic import BaseModel, Field

from app.abstractions.session_context import (
    SessionContextRevisionChangedError,
    WorkspaceSessionContextAccessError,
)
from app.agents.custom_tools import CustomToolFactoryContext
from app.schemas.public_v2.session_context import (
    SessionContextReadRequest,
    SessionContextSearchRequest,
)


ResultModel = TypeVar("ResultModel", bound=BaseModel)
ContextInclude = Literal[
    "visible_text",
    "reasoning",
    "tool_summary",
    "tool_calls",
    "tool_results",
    "system",
    "raw_record",
]


class ReadContextInput(BaseModel):
    resource: str = Field(
        description=(
            "BoxTeam 上下文资源，例如 boxteam://session/{session_id}、"
            "boxteam://workspace/{workspace_id}/sessions 或 boxteam://gateway/workspaces。"
        )
    )
    view: Literal["overview", "messages", "records", "information", "inventory"] = (
        Field(default="overview", description="读取视图；默认返回低成本概览。")
    )
    include: list[ContextInclude] = Field(
        default_factory=lambda: ["visible_text", "tool_summary"],
        description="显式展开的内容类型。reasoning 和工具载荷默认不返回。",
    )
    recent_rounds: int = Field(default=3, ge=1, le=20)
    include_initial_goal: bool = True
    cursor: str | None = None
    limit: int = Field(default=20, ge=1, le=200)
    max_chars: int = Field(default=16_384, ge=1_024, le=65_536)
    expected_revision: str | None = None


class SearchContextInput(BaseModel):
    resource: str = Field(
        description=(
            "搜索范围；只有 boxteam://gateway 才会搜索全部已注册工作区。"
        )
    )
    query: str = Field(min_length=1, description="要搜索的文本或正则表达式。")
    sources: list[
        Literal["effective_context", "session_catalog", "session_information"]
    ] = Field(default_factory=lambda: ["effective_context"])
    match_mode: Literal["literal", "regex"] = "literal"
    case_sensitive: bool = False
    max_results: int = Field(default=20, ge=1, le=200)
    max_chars: int = Field(default=16_384, ge=1_024, le=65_536)
    cursor: str | None = None
    expected_revision: str | None = None


def _resource_route(resource: str) -> tuple[Literal["local", "workspace", "gateway"], str | None]:
    parsed = urlparse(resource)
    if parsed.scheme != "boxteam":
        raise ToolException("resource 必须使用 boxteam:// 资源地址")
    if parsed.netloc == "session":
        return "local", None
    if parsed.netloc == "gateway":
        return "gateway", None
    if parsed.netloc != "workspace":
        raise ToolException(f"不支持的上下文资源: {resource}")
    workspace_id = parsed.path.strip("/").partition("/")[0].strip()
    if not workspace_id:
        raise ToolException("workspace 资源缺少 Gateway 工作区 ID")
    return "workspace", workspace_id


async def _result_json(operation: Awaitable[ResultModel]) -> str:
    try:
        result = await operation
    except (WorkspaceSessionContextAccessError, SessionContextRevisionChangedError) as error:
        raise ToolException(str(error)) from error
    return result.model_dump_json()


def create_read_context_tool(context: CustomToolFactoryContext) -> BaseTool:
    async def read_context(
        resource: str,
        view: Literal[
            "overview", "messages", "records", "information", "inventory"
        ] = "overview",
        include: list[ContextInclude] | None = None,
        recent_rounds: int = 3,
        include_initial_goal: bool = True,
        cursor: str | None = None,
        limit: int = 20,
        max_chars: int = 16_384,
        expected_revision: str | None = None,
    ) -> str:
        request = SessionContextReadRequest(
            resource=resource,
            view=view,
            include=include or ["visible_text", "tool_summary"],
            recent_rounds=recent_rounds,
            include_initial_goal=include_initial_goal,
            cursor=cursor,
            limit=limit,
            max_chars=max_chars,
            expected_revision=expected_revision,
        )
        route, workspace_id = _resource_route(resource)
        if route == "local":
            return await _result_json(
                context.session_context_query_service.read_context(request)
            )
        if route == "gateway":
            return await _result_json(
                context.workspace_session_context_client.read_gateway_context(request)
            )
        assert workspace_id is not None
        return await _result_json(
            context.workspace_session_context_client.read_context_in_workspace(
                workspace_id,
                request,
            )
        )

    return StructuredTool.from_function(
        coroutine=read_context,
        name="read_context",
        description=(
            "像 read 一样读取当前 Session、指定 Gateway 工作区或 Gateway inventory。"
            "默认返回首个用户目标、最近 3 轮可见文本和执行概览；详细内容需显式 include。"
        ),
        args_schema=ReadContextInput,
        handle_tool_error=True,
    )


def create_search_context_tool(context: CustomToolFactoryContext) -> BaseTool:
    async def search_context(
        resource: str,
        query: str,
        sources: list[
            Literal["effective_context", "session_catalog", "session_information"]
        ] | None = None,
        match_mode: Literal["literal", "regex"] = "literal",
        case_sensitive: bool = False,
        max_results: int = 20,
        max_chars: int = 16_384,
        cursor: str | None = None,
        expected_revision: str | None = None,
    ) -> str:
        request = SessionContextSearchRequest(
            resource=resource,
            query=query,
            sources=sources or ["effective_context"],
            match_mode=match_mode,
            case_sensitive=case_sensitive,
            max_results=max_results,
            max_chars=max_chars,
            cursor=cursor,
            expected_revision=expected_revision,
        )
        route, workspace_id = _resource_route(resource)
        if route == "local":
            return await _result_json(
                context.session_context_query_service.search_context(request)
            )
        if route == "gateway":
            return await _result_json(
                context.workspace_session_context_client.search_gateway_context(request)
            )
        assert workspace_id is not None
        return await _result_json(
            context.workspace_session_context_client.search_context_in_workspace(
                workspace_id,
                request,
            )
        )

    return StructuredTool.from_function(
        coroutine=search_context,
        name="search_context",
        description=(
            "像 grep 一样搜索 Session、工作区或显式指定的整个 Gateway 上下文。"
            "返回短预览和可供 read_context 展开的 locator，不返回大段原始内容。"
        ),
        args_schema=SearchContextInput,
        handle_tool_error=True,
    )
