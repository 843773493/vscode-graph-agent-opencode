from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Literal, TypeVar
from urllib.parse import urlparse

from langchain_core.tools import BaseTool, StructuredTool, ToolException
from pydantic import BaseModel, Field

from app.abstractions.session_context import (
    SessionContextRevisionChangedError,
    WorkspaceSessionContextAccessError,
)
from app.abstractions.session_target import SessionTarget
from app.agents.custom_tools import CustomToolFactoryContext
from app.schemas.internal_v2.session_context import (
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
    workspace_id: str | None = Field(
        default=None,
        description=(
            "可选目标工作区 ID；默认按 session 资源自动解析，只有会话 ID 冲突或"
            "目标不在当前工作区时才需要填写。"
        ),
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
    workspace_id: str | None = Field(
        default=None,
        description=(
            "可选目标工作区 ID；默认按 session 资源自动解析，只有会话 ID 冲突或"
            "目标不在当前工作区时才需要填写。"
        ),
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


@dataclass(frozen=True, slots=True)
class _ResourceRoute:
    kind: Literal["session", "workspace", "gateway"]
    session_id: str | None = None
    workspace_id: str | None = None


def _resource_route(
    resource: str,
    requested_workspace_id: str | None = None,
) -> _ResourceRoute:
    parsed = urlparse(resource)
    if parsed.scheme != "boxteam":
        raise ToolException("resource 必须使用 boxteam:// 资源地址")
    if parsed.netloc == "gateway":
        if requested_workspace_id is not None:
            raise ToolException("Gateway 资源不能同时提供 workspace_id")
        return _ResourceRoute(kind="gateway")
    try:
        from app.services.business.session_context_resource import (
            parse_session_context_resource,
        )

        parsed_resource = parse_session_context_resource(resource)
    except ValueError as error:
        raise ToolException(str(error)) from error
    if parsed_resource.kind == "session":
        if parsed_resource.session_id is None:
            raise ToolException(f"session 资源缺少 session_id: {resource}")
        if (
            parsed_resource.workspace_id is not None
            and requested_workspace_id is not None
            and parsed_resource.workspace_id != requested_workspace_id
        ):
            raise ToolException(
                "resource 中的 workspace_id 与独立 workspace_id 参数不一致"
            )
        return _ResourceRoute(
            kind="session",
            session_id=parsed_resource.session_id,
            workspace_id=parsed_resource.workspace_id or requested_workspace_id,
        )
    if (
        requested_workspace_id is not None
        and parsed_resource.workspace_id != requested_workspace_id
    ):
        raise ToolException(
            "resource 中的 workspace_id 与独立 workspace_id 参数不一致"
        )
    return _ResourceRoute(
        kind="workspace",
        workspace_id=parsed_resource.workspace_id,
    )


async def _resolve_session_target(
    context: CustomToolFactoryContext,
    route: _ResourceRoute,
) -> SessionTarget:
    if route.session_id is None:
        raise ToolException("session 资源缺少 session_id")
    resolver = getattr(context, "session_target_resolver", None)
    if resolver is None:
        return SessionTarget(
            session_id=route.session_id,
            workspace_id=route.workspace_id,
        )
    try:
        return await resolver.resolve_session(
            route.session_id,
            workspace_id=route.workspace_id,
        )
    except WorkspaceSessionContextAccessError as error:
        raise ToolException(str(error)) from error


def _resource_for_target(resource: str, target: SessionTarget) -> str:
    base, separator, selector = resource.partition("#")
    del base
    if target.workspace_id is None:
        routed = f"boxteam://session/{target.session_id}"
    else:
        routed = (
            f"boxteam://workspace/{target.workspace_id}/session/{target.session_id}"
        )
    return f"{routed}#{selector}" if separator else routed


async def _result_json(operation: Awaitable[ResultModel]) -> str:
    try:
        result = await operation
    except (WorkspaceSessionContextAccessError, SessionContextRevisionChangedError) as error:
        raise ToolException(str(error)) from error
    return result.model_dump_json()


def create_read_context_tool(context: CustomToolFactoryContext) -> BaseTool:
    async def read_context(
        resource: str,
        workspace_id: str | None = None,
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
        route = _resource_route(resource, workspace_id)
        if route.kind == "session":
            target = await _resolve_session_target(context, route)
            request_resource = _resource_for_target(resource, target)
        else:
            request_resource = resource
        request = SessionContextReadRequest(
            resource=request_resource,
            view=view,
            include=include or ["visible_text", "tool_summary"],
            recent_rounds=recent_rounds,
            include_initial_goal=include_initial_goal,
            cursor=cursor,
            limit=limit,
            max_chars=max_chars,
            expected_revision=expected_revision,
        )
        if route.kind == "session" and target.workspace_id is None:
            return await _result_json(
                context.session_context_query_service.read_context(request)
            )
        if route.kind == "gateway":
            return await _result_json(
                context.workspace_session_context_client.read_gateway_context(request)
            )
        if route.kind == "session":
            assert target.workspace_id is not None
            return await _result_json(
                context.workspace_session_context_client.read_context_in_workspace(
                    target.workspace_id,
                    request,
                )
            )
        assert route.workspace_id is not None
        return await _result_json(
            context.workspace_session_context_client.read_context_in_workspace(
                route.workspace_id,
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
        workspace_id: str | None = None,
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
        route = _resource_route(resource, workspace_id)
        if route.kind == "session":
            target = await _resolve_session_target(context, route)
            request_resource = _resource_for_target(resource, target)
        else:
            request_resource = resource
        request = SessionContextSearchRequest(
            resource=request_resource,
            query=query,
            sources=sources or ["effective_context"],
            match_mode=match_mode,
            case_sensitive=case_sensitive,
            max_results=max_results,
            max_chars=max_chars,
            cursor=cursor,
            expected_revision=expected_revision,
        )
        if route.kind == "session" and target.workspace_id is None:
            return await _result_json(
                context.session_context_query_service.search_context(request)
            )
        if route.kind == "gateway":
            return await _result_json(
                context.workspace_session_context_client.search_gateway_context(request)
            )
        if route.kind == "session":
            assert target.workspace_id is not None
            return await _result_json(
                context.workspace_session_context_client.search_context_in_workspace(
                    target.workspace_id,
                    request,
                )
            )
        assert route.workspace_id is not None
        return await _result_json(
            context.workspace_session_context_client.search_context_in_workspace(
                route.workspace_id,
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
