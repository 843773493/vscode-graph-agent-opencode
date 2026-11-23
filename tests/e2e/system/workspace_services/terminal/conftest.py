from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.harness.python.run_context import TestRunContext
from tests.harness.python.terminal_plugin import (
    TerminalPluginGateway,
    terminal_plugin_gateway_for_workspace,
)
from tests.support.ports import TestFilePorts


@pytest.fixture(scope="module")
async def terminal_plugin_gateway(
    request: pytest.FixtureRequest,
    e2e_workspace_root_path: str,
    e2e_workspace_config_path: str,
    e2e_service_ports: TestFilePorts,
    prepared_developer_tools_package: Path,
) -> AsyncIterator[TerminalPluginGateway]:
    del e2e_workspace_config_path
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    artifacts = context.run_root / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    async with terminal_plugin_gateway_for_workspace(
        Path(e2e_workspace_root_path),
        artifacts,
        e2e_service_ports.port(48),
        context.boxteam_home,
        prepared_package_root=prepared_developer_tools_package,
    ) as gateway:
        yield gateway
