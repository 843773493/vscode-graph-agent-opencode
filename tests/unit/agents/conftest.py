"""模型文件工具使用真实共享文件 owner；测试仅显式装配依赖。"""

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from deepagents.middleware.filesystem import FilesystemMiddleware

from app.abstractions.resource_file_content import ResourceFileReader
from app.agents.workspace_filesystem_tools import configure_workspace_filesystem_tools
from app.core.trace_middleware import reset_current_gateway_id, set_current_gateway_id
from app.core.workspace_identity import load_or_create_workspace_id
from app.services.infrastructure.resource_platform.sources.resource_navigation import (
    ResourceNavigation,
)
from app.services.infrastructure.workspace_files.paths import WorkspaceFilePaths


@pytest.fixture
def configure_filesystem_tools(text_mutations_factory) -> Iterator[Callable[..., ResourceNavigation]]:
    token = set_current_gateway_id("gateway_0123456789abcdef0123456789abcdef")

    def configure(
        middleware: FilesystemMiddleware, *, workspace_root: Path, project_root: Path | None = None,
        artifact_reader: ResourceFileReader | None = None,
    ) -> ResourceNavigation:
        workspace_id = load_or_create_workspace_id(workspace_root)
        navigation = ResourceNavigation(
            workspace_root=workspace_root, workspace_id=workspace_id, project_root=project_root,
            file_paths=WorkspaceFilePaths(workspace=workspace_root, workspace_id=workspace_id, bindings=None),
        )
        configure_workspace_filesystem_tools(middleware, workspace_root=workspace_root, resource_navigation=navigation,
            text_mutations=text_mutations_factory(workspace_root), artifact_reader=artifact_reader)
        return navigation

    try:
        yield configure
    finally:
        reset_current_gateway_id(token)
