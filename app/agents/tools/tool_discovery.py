"""固定的任务级发现入口；只读取调用所属的冻结扩展目录。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Self

from langchain_core.tools import BaseTool, StructuredTool, ToolException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agents.policy.tool_capability import ToolCapabilityPolicy
from app.agents.tool_identity import TOOL_DISCOVERY_NAME
from app.services.infrastructure.mcp.extension_catalog import (
    ExtensionCatalogBindingError,
)
from app.services.infrastructure.mcp.tool_definitions import (
    FrozenToolCatalog,
    FrozenToolDefinition,
)
from app.services.infrastructure.mcp.tool_search import (
    ToolSearchConflict,
    ToolSearchStorageError,
    search_tool_catalog,
)
from app.services.infrastructure.rollout_context.activation.common import (
    ResourceActivationStoreError,
)
from app.services.infrastructure.tool_output_store import ToolOutputReference


class SearchToolsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str | None = Field(default=None, min_length=1, max_length=512, description="任务描述；与 tool_ids 必须且只能提供一项。")
    tool_ids: list[str] | None = Field(default=None, min_length=1, max_length=100, description="精确工具 ID，按给定顺序逐项读取；与 query 互斥。")
    limit: int = Field(default=5, ge=1, le=20, strict=True)
    max_response_bytes: int = Field(default=32768, ge=2048, le=262144, strict=True)
    cursor: str | None = Field(default=None, min_length=1, max_length=1024)

    @model_validator(mode="after")
    def validate_selector(self) -> Self:
        if (self.query is None) == (self.tool_ids is None):
            raise ValueError("query 与非空 tool_ids 必须且只能提供一项")
        if self.tool_ids is not None and any(not name.strip() or len(name) > 256 for name in self.tool_ids):
            raise ValueError("tool_ids 每项必须是 1–256 字符的非空工具 ID")
        return self


def create_search_tools_tool(
    *, catalog_resolver: Callable[[], FrozenToolCatalog],
    policy_resolver: Callable[[FrozenToolCatalog], Mapping[str, ToolCapabilityPolicy]],
    definition_writer: Callable[[FrozenToolDefinition, dict[str, object]], Awaitable[ToolOutputReference]],
    response_byte_limit: int,
) -> BaseTool:
    async def search_tools(
        query: str | None = None, tool_ids: list[str] | None = None,
        limit: int = 5, max_response_bytes: int = 32768, cursor: str | None = None,
    ) -> str:
        try:
            catalog = catalog_resolver()
            return await search_tool_catalog(
                catalog, policy_resolver(catalog), query=query, tool_ids=tool_ids,
                limit=limit, max_response_bytes=min(max_response_bytes, response_byte_limit), cursor=cursor,
                materialize=definition_writer,
            )
        except (ExtensionCatalogBindingError, ResourceActivationStoreError, ToolSearchConflict, ToolSearchStorageError) as error:
            raise ToolException(str(error)) from error

    return StructuredTool.from_function(
        coroutine=search_tools, name=TOOL_DISCOVERY_NAME, args_schema=SearchToolsInput,
        description="按任务 query 搜索扩展工具，或用 tool_ids 读取完整定义。二者互斥。结果包含完整参数契约、策略、版本和实际调用入口；needs_read 须先用普通文件工具读取 read_path 的完整定义。只发现，不执行。续页须保持原查询和响应限制。",
        handle_tool_error=True,
    )
