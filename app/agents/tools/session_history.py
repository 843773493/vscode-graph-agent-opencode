from __future__ import annotations

from collections.abc import Awaitable
from typing import Literal, TypeVar

from langchain_core.tools import BaseTool, StructuredTool, ToolException
from pydantic import BaseModel, Field

from app.abstractions.session_context import (
    SessionContextRevisionChangedError,
    WorkspaceSessionContextAccessError,
)
from app.agents.custom_tools import CustomToolFactoryContext
from app.core.session_control_communication_ledger.observations import (
    RemoteObservationError,
)
from app.core.trace_middleware import get_current_gateway_id, require_current_gateway_id
from app.schemas.internal_v2.session_context import (
    SessionContextInclude,
    SessionContextMatchMode,
    SessionContextReadRequest,
    SessionContextSearchRequest,
    SessionContextSearchSource,
)
from app.services.business.session_context_resource import (
    ParsedSessionContextResource,
    gateway_session_inventory_identity,
    gateway_session_inventory_uri,
    parse_session_context_ref,
    session_resource_identity,
)
from app.services.infrastructure.resource_platform.virtual_resources.values import (
    validate_resource_identity,
)

ResultModel = TypeVar("ResultModel", bound=BaseModel)


class ReadContextInput(BaseModel):
    resource_identity: str = Field(description="目标上下文资源的稳定身份")
    uri: str = Field(description="目标上下文资源的 URI 地址")
    scope: Literal["workspace", "user", "gateway", "inline"] = Field(
        description="与 URI 一致的资源作用域"
    )
    view: Literal[
        "overview", "messages", "records", "information", "inventory", "assembly", "assemblies"
    ] = Field(default="overview", description="读取视图；默认返回低成本概览。")
    record_index: int | None = Field(default=None, ge=0)
    assembly_ref: str | None = Field(default=None, description="冻结 assembly 的 ResourceIdentity")
    include: list[SessionContextInclude] = Field(
        default_factory=lambda: ["visible_text", "tool_summary"],
        description="显式展开的内容类型。reasoning 和工具载荷默认不返回。",
    )
    recent_rounds: int = Field(default=3, ge=1, le=50)
    include_initial_goal: bool = True
    cursor: str | None = None
    limit: int = Field(default=20, ge=1, le=200)
    max_chars: int = Field(default=16_384, ge=1_024, le=65_536)
    revision: str | None = None


class SearchContextInput(BaseModel):
    resource_identity: str = Field(description="搜索范围的稳定身份")
    uri: str = Field(description="搜索范围的 URI 地址")
    scope: Literal["workspace", "user", "gateway", "inline"] = Field(
        description="与 URI 一致的资源作用域"
    )
    query: str = Field(min_length=1, description="要搜索的文本或正则表达式。")
    sources: list[SessionContextSearchSource] = Field(
        default_factory=lambda: ["effective_context"]
    )
    match_mode: SessionContextMatchMode = "literal"
    case_sensitive: bool = False
    max_results: int = Field(default=20, ge=1, le=200)
    max_chars: int = Field(default=16_384, ge=1_024, le=65_536)
    cursor: str | None = None
    revision: str | None = None


def _request_identity(
    *, resource_identity: str, uri: str, scope: str
) -> ParsedSessionContextResource:
    try:
        resource = parse_session_context_ref(
            resource_identity=resource_identity,
            uri=uri,
            scope=scope,
            local_gateway_id=get_current_gateway_id(),
        )
    except ValueError as error:
        raise ToolException(str(error)) from error
    return resource


def _validate_target_identity(
    *,
    resource_identity: str,
    uri: str,
    scope: str,
    backend_workspace_id: str,
    session_id: str,
) -> None:
    resource = parse_session_context_ref(
        resource_identity=resource_identity, uri=uri, scope=scope,
    )
    if (
        resource.resource_identity != session_resource_identity(backend_workspace_id, session_id)
        or resource.scope != "workspace"
        or resource.scope_id != backend_workspace_id
        or resource.session_id != session_id
    ):
        raise ToolException(
            "会话上下文资源身份与当前 session/backend workspace 不一致: "
            f"session_id={session_id!r}, backend_workspace_id={backend_workspace_id!r}"
        )


async def _result_json(operation: Awaitable[ResultModel]) -> str:
    try:
        result = await operation
    except (WorkspaceSessionContextAccessError, SessionContextRevisionChangedError, RemoteObservationError) as error:
        raise ToolException(str(error)) from error
    return result.model_dump_json()


def create_read_context_tool(context: CustomToolFactoryContext) -> BaseTool:
    async def read_context(
        resource_identity: str,
        uri: str,
        scope: Literal["workspace", "user", "gateway", "inline"],
        view: Literal[
            "overview", "messages", "records", "information", "inventory", "assembly", "assemblies"
        ] = "overview",
        record_index: int | None = None,
        assembly_ref: str | None = None,
        include: list[SessionContextInclude] | None = None,
        recent_rounds: int = 3,
        include_initial_goal: bool = True,
        cursor: str | None = None,
        limit: int = 20,
        max_chars: int = 16_384,
        revision: str | None = None,
    ) -> str:
        resource = _request_identity(
            resource_identity=resource_identity,
            uri=uri,
            scope=scope,
        )
        identity, canonical_uri, canonical_scope = resource.resource_identity, resource.uri, scope
        request = SessionContextReadRequest(
            resource_identity=identity,
            uri=canonical_uri,
            scope=canonical_scope,
            view=view,
            record_index=record_index,
            assembly_ref=validate_resource_identity(assembly_ref) if assembly_ref is not None else None,
            include=include or ["visible_text", "tool_summary"],
            recent_rounds=recent_rounds,
            include_initial_goal=include_initial_goal,
            cursor=cursor,
            limit=limit,
            max_chars=max_chars,
            revision=revision,
        )

        if resource.gateway_authority is not None and resource.kind == "workspace_sessions":
            return await _result_json(
                context.workspace_session_context_client.read_resource_context(request)
            )
        if resource.scope == "gateway":
            gateway_id = require_current_gateway_id("read_context")
            expected_identity = gateway_session_inventory_identity(gateway_id)
            expected_uri = gateway_session_inventory_uri(gateway_id)
            if (
                resource.kind != "workspace_sessions"
                or resource.resource_identity != expected_identity
                or resource.uri != expected_uri
            ):
                raise ToolException("Gateway inventory URI 必须绑定当前真实 gateway_id")
            return await _result_json(
                context.workspace_session_context_client.read_gateway_context(request)
            )

        if resource.kind == "workspace_sessions":
            if resource.scope_id == context.session_context_query_service.workspace_id:
                return await _result_json(
                    context.session_context_query_service.read_context(request)
                )
            return await _result_json(
                context.workspace_session_context_client.read_resource_context(request)
            )

        if resource.kind != "session" or resource.session_id is None:
            raise ToolException(f"read_context 不支持该资源 kind: {resource.uri!r}")
        _validate_target_identity(
            resource_identity=identity,
            uri=canonical_uri,
            scope=canonical_scope,
            backend_workspace_id=resource.scope_id,
            session_id=resource.session_id,
        )
        service = context.session_communication_service
        binding = context.invocation_context.thread_binding
        if service is None or binding is None or binding.session_id != context.session_id:
            raise RuntimeError("Session read 缺少持久通信 owner 与精确 source thread")
        return await _result_json(service.read(
            session_id=binding.session_id, thread_id=binding.thread_id,
            source_call_id=context.invocation_context.require_operation_id("read"),
            source_call_created_at=context.invocation_context.require_operation_created_at(), request=request,
        ))

    return StructuredTool.from_function(
        coroutine=read_context,
        name="read_context",
        description=(
            "像 read 一样读取由 resource_identity、URI 和 scope 指定的 Session、"
            "工作区 inventory 或 Gateway inventory。视图选择和 revision 均为独立字段。"
        ),
        args_schema=ReadContextInput,
        handle_tool_error=True,
    )


def create_search_context_tool(context: CustomToolFactoryContext) -> BaseTool:
    async def search_context(
        resource_identity: str,
        uri: str,
        scope: Literal["workspace", "user", "gateway", "inline"],
        query: str,
        sources: list[SessionContextSearchSource] | None = None,
        match_mode: SessionContextMatchMode = "literal",
        case_sensitive: bool = False,
        max_results: int = 20,
        max_chars: int = 16_384,
        cursor: str | None = None,
        revision: str | None = None,
    ) -> str:
        resource = _request_identity(
            resource_identity=resource_identity,
            uri=uri,
            scope=scope,
        )
        identity, canonical_uri, canonical_scope = resource.resource_identity, resource.uri, scope
        request = SessionContextSearchRequest(
            resource_identity=identity,
            uri=canonical_uri,
            scope=canonical_scope,
            query=query,
            sources=sources or ["effective_context"],
            match_mode=match_mode,
            case_sensitive=case_sensitive,
            max_results=max_results,
            max_chars=max_chars,
            cursor=cursor,
            revision=revision,
        )

        if resource.gateway_authority is not None:
            return await _result_json(
                context.workspace_session_context_client.search_resource_context(request)
            )
        if resource.scope == "gateway":
            gateway_id = require_current_gateway_id("search_context")
            if (
                resource.kind != "workspace_sessions"
                or resource.resource_identity != gateway_session_inventory_identity(gateway_id)
                or resource.uri != gateway_session_inventory_uri(gateway_id)
            ):
                raise ToolException("Gateway inventory URI 必须绑定当前真实 gateway_id")
            return await _result_json(
                context.workspace_session_context_client.search_gateway_context(request)
            )

        if resource.kind == "workspace_sessions":
            if resource.scope_id == context.session_context_query_service.workspace_id:
                return await _result_json(
                    context.session_context_query_service.search_context(request)
                )
            return await _result_json(
                context.workspace_session_context_client.search_resource_context(request)
            )

        if resource.kind != "session" or resource.session_id is None:
            raise ToolException(f"search_context 不支持该资源 kind: {resource.uri!r}")
        _validate_target_identity(
            resource_identity=identity,
            uri=canonical_uri,
            scope=canonical_scope,
            backend_workspace_id=resource.scope_id,
            session_id=resource.session_id,
        )
        if resource.scope_id == context.session_context_query_service.workspace_id:
            return await _result_json(
                context.session_context_query_service.search_context(request)
            )
        return await _result_json(
            context.workspace_session_context_client.search_resource_context(request)
        )

    return StructuredTool.from_function(
        coroutine=search_context,
        name="search_context",
        description=(
            "像 grep 一样搜索结构化资源范围；返回短预览和可供 read_context 再读取的身份、"
            "URI、scope 与独立 revision。"
        ),
        args_schema=SearchContextInput,
        handle_tool_error=True,
    )
