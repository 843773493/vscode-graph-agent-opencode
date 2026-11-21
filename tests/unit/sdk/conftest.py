"""SDK 单测只隔离 Home，沿用根装具的进程保护和临时目录。"""

from pathlib import Path

import pytest

from tests.harness.python.run_context import TestRunContext


@pytest.fixture(autouse=True)
def setup_test_config(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    # SDK 不读取产品配置，无需为每个用例复制 Gateway／Workspace 配置和 schema。
    context = TestRunContext.from_test_file(Path(request.node.path))
    home = context.boxteam_home_for_node(request.node.nodeid)
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BOXTEAM_HOME", str(home))
