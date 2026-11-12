from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.harness.python.run_context import TestRunContext
from tests.harness.python.terminal_plugin import (
    TerminalPluginGateway,
    terminal_plugin_gateway_for_workspace,
)


@pytest.fixture(scope="module")
async def terminal_plugin_gateway(
    request: pytest.FixtureRequest,
    e2e_workspace_root_path: str,
    e2e_workspace_config_path: str,
    e2e_backend_port: int,
    prepared_developer_tools_package: Path,
) -> AsyncIterator[TerminalPluginGateway]:
    del e2e_workspace_config_path
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    async with terminal_plugin_gateway_for_workspace(
        Path(e2e_workspace_root_path),
        context.artifacts_dir,
        e2e_backend_port + 95,
        context.boxteam_home,
        prepared_package_root=prepared_developer_tools_package,
    ) as gateway:
        yield gateway
