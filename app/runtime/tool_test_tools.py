"""工具能力测试的实际工具装配；登记用例时不加载 Agent 框架。"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool
    from langgraph.prebuilt.tool_node import ToolRuntime

    from app.tool_testing.sandbox import ToolTestFilePaths


def build_apply_patch_test_tool(file_paths: ToolTestFilePaths) -> BaseTool:
    from app.agents.tools.apply_patch import create_apply_patch_tool
    from app.services.infrastructure.workspace_files.text_owner import (
        WorkspaceTextMutationOwner,
    )

    return create_apply_patch_tool(text_mutations=WorkspaceTextMutationOwner(paths=file_paths))


def build_edit_file_test_tool(file_paths: ToolTestFilePaths) -> tuple[BaseTool, ToolRuntime]:
    from deepagents.middleware.filesystem import FilesystemMiddleware
    from langchain_core.tools import StructuredTool
    from langgraph.prebuilt.tool_node import ToolRuntime

    from app.agents.workspace_backend import build_workspace_backend
    from app.agents.workspace_filesystem_tools import (
        configure_workspace_filesystem_tools,
    )
    from app.services.infrastructure.resource_platform.sources.resource_navigation import (
        ResourceNavigation,
    )
    from app.services.infrastructure.workspace_files.text_owner import (
        WorkspaceTextMutationOwner,
    )

    workspace_root, workspace_id = file_paths.workspace, file_paths.workspace_id
    middleware = FilesystemMiddleware(backend=build_workspace_backend(workspace_root))
    navigation = ResourceNavigation(
        workspace_root=workspace_root,
        workspace_id=workspace_id,
        file_paths=file_paths,
    )
    configure_workspace_filesystem_tools(
        middleware,
        workspace_root=workspace_root,
        resource_navigation=navigation,
        text_mutations=WorkspaceTextMutationOwner(paths=file_paths),
    )
    matches = [tool for tool in middleware.tools if tool.name == "edit_file"]
    if len(matches) != 1:
        raise RuntimeError(
            "FilesystemMiddleware 中 edit_file 工具数量异常: "
            f"expected=1, actual={len(matches)}"
        )
    tool = matches[0]
    if not isinstance(tool, StructuredTool):
        raise TypeError(
            "FilesystemMiddleware 的 edit_file 不是 StructuredTool: "
            f"actual={type(tool).__name__}"
        )
    if tool.func is None or tool.coroutine is None:
        raise RuntimeError("FilesystemMiddleware 的 edit_file 缺少同步或异步实现")

    runtime = ToolRuntime(
        state={},
        context=None,
        config={},
        stream_writer=lambda _: None,
        tool_call_id="model-tool-test-edit-file",
        store=None,
        tools=[tool],
    )
    return tool, runtime
