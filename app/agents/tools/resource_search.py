"""只读工作区发现与领域搜索；范围、队列及分页都归 Gateway。"""

from langchain_core.tools import BaseTool, StructuredTool, ToolException
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from app.abstractions.session_context import WorkspaceSessionContextAccessError
from app.agents.custom_tools import CustomToolFactoryContext
from app.core.trace_middleware import require_current_gateway_id
from app.schemas.gateway.search import (
    GatewaySearchScopeRequest,
    GatewaySearchWorkspaceOwnerDTO,
)
from app.schemas.search_federation import (
    FederatedSearchContinuationDTO,
    FederatedSearchEventDTO,
    FederatedSearchQueryDTO,
    FederatedSearchRequest,
)

_EVENTS = TypeAdapter(list[FederatedSearchEventDTO])


class DiscoverWorkspacesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(default="", max_length=512, description="按名称、ID 或 Gateway 导航归属发现工作区，不读取会话正文。")
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = None
    include_folders: bool = False


class SearchResourcesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    queries: tuple[FederatedSearchQueryDTO, ...] = Field(min_length=1, max_length=8, description=(
        "领域查询：catalog/thread 使用 name、fuzzy；message/tool_result/llm_request 使用 content、literal；"
        "file 支持 name/fuzzy 和 content/literal；context/content 保留原有效上下文，支持 literal/regex。"
        "文件条件放在 file_targets，上下文来源／大小写／字符预算放在 context，不扫描未选类别。"
    ))
    scope: GatewaySearchScopeRequest | None = Field(default=None, description=(
        "省略时只查发起工具所属工作区；selected 为明确 owner 集合，all 为当前 Gateway 全部登记工作区，"
        "resume 使用此前返回的冻结 scope。没有命中不扩大范围。"
    ))
    continuations: tuple[FederatedSearchContinuationDTO, ...] = ()
    max_results: int = Field(default=200, ge=0, le=1000)


def create_discover_workspaces_tool(context: CustomToolFactoryContext) -> BaseTool:
    async def discover_workspaces(
        query: str = "", limit: int = 50, cursor: str | None = None, include_folders: bool = False,
    ) -> str:
        try:
            result = await context.workspace_session_context_client.resource_search.discover_workspaces(
                query=query, limit=limit, cursor=cursor, include_folders=include_folders,
                request_id=context.invocation_context.require_operation_id("discover_workspaces"),
            )
        except WorkspaceSessionContextAccessError as error:
            raise ToolException(str(error)) from error
        return result.model_dump_json()

    return StructuredTool.from_function(
        coroutine=discover_workspaces, name="discover_workspaces", args_schema=DiscoverWorkspacesInput,
        description="只读发现当前 Gateway 已登记工作区，返回稳定 owner、导航归属和独立 cursor；不连接、启动或激活工作区。",
        handle_tool_error=True,
    )


def create_search_resources_tool(context: CustomToolFactoryContext) -> BaseTool:
    async def search_resources(
        queries: tuple[FederatedSearchQueryDTO, ...], scope: GatewaySearchScopeRequest | None = None,
        continuations: tuple[FederatedSearchContinuationDTO, ...] = (), max_results: int = 200,
    ) -> str:
        if scope is None:
            scope = GatewaySearchScopeRequest(mode="origin", origin=GatewaySearchWorkspaceOwnerDTO(
                gateway_id=require_current_gateway_id("search_resources"),
                workspace_id=context.session_context_query_service.workspace_id,
            ))
        request = FederatedSearchRequest(scope=scope, queries=queries, continuations=continuations, max_results=max_results)
        try:
            events = [event async for event in context.workspace_session_context_client.resource_search.search(
                request, request_id=context.invocation_context.require_operation_id("search_resources"),
            )]
        except WorkspaceSessionContextAccessError as error:
            raise ToolException(str(error)) from error
        return _EVENTS.dump_json(events).decode()

    return StructuredTool.from_function(
        coroutine=search_resources, name="search_resources", args_schema=SearchResourcesInput,
        description=(
            "在明确工作区范围只读搜索目录、Thread、文件、canonical 消息、工具结果、模型请求记录或有效上下文。"
            "返回原联邦 scope/page/branch/end 事件、规范引用和 owner 独立 cursor；只对原冻结范围的分支显式续查或重试。"
            "相关工作区先用 discover_workspaces 发现再明确选集合；精确单资源查询与读取使用 search_context/read_context。"
        ), handle_tool_error=True,
    )
