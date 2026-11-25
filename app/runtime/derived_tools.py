"""派生只读工具按需装配，复用原文件权限、调用身份与结果存储。"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from langchain.agents.middleware import AgentMiddleware
    from langchain_core.tools import BaseTool

    from app.abstractions.resource_file_content import ResourceFileReader
    from app.domain.itemized.derived_execution import FrozenDerivedInput
    from app.services.infrastructure.resource_platform.sources.resource_navigation import (
        ResourceNavigation,
    )
    from app.services.infrastructure.tool_output_store import ToolOutputStore
    from app.services.infrastructure.workspace_files.text_owner import (
        WorkspaceTextMutationOwner,
    )


def build_derived_tools(
    frozen: FrozenDerivedInput, *, workspace_root: Path, resource_navigation: ResourceNavigation,
    text_mutations: WorkspaceTextMutationOwner, artifact_reader: ResourceFileReader,
    output_store: ToolOutputStore,
) -> tuple[tuple[BaseTool, ...], tuple[AgentMiddleware, ...]]:
    from app.agents.middleware_assembly import build_workspace_filesystem_middleware
    from app.agents.tool_invocation_context import (
        ThreadRuntimeBinding,
        ToolInvocationContext,
        ToolInvocationContextMiddleware,
    )
    from app.agents.tool_output_middleware import ToolOutputMiddleware
    from app.agents.workspace_backend import build_workspace_backend

    if not frozen.allowed_tool_ids:
        return (), ()
    if frozen.gateway_id is None:
        raise ValueError("derived-tool-context-unsupported: 文件工具缺少首次 Gateway 身份")
    filesystem = build_workspace_filesystem_middleware(
        backend=build_workspace_backend(workspace_root), workspace_root=workspace_root,
        resource_navigation=resource_navigation, text_mutations=text_mutations,
        artifact_reader=artifact_reader, permissions=None,
    )
    tools = tuple(tool for tool in filesystem.tools if tool.name in frozen.allowed_tool_ids)
    if {tool.name for tool in tools} != set(frozen.allowed_tool_ids):
        raise ValueError("derived-tool-context-unsupported: 冻结工具不在原文件工具装配中")
    context = ToolInvocationContext(thread_binding=ThreadRuntimeBinding(frozen.session_id, frozen.thread_id))
    return tools, (ToolInvocationContextMiddleware(context),
                   ToolOutputMiddleware(session_id=frozen.session_id, store=output_store))
