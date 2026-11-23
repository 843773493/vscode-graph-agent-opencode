"""快速访问页面共用端口租约，使用公共服务端口计划。"""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.harness.python.run_context import TestRunContext
from tests.support.ports import integration_ports_for_file


@pytest.fixture
def search_gateway_port(request: pytest.FixtureRequest) -> Iterator[int]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    port = integration_ports_for_file(Path(request.node.path)).port(20)
    (context.artifacts_dir / "port-allocation.json").write_text(
        json.dumps({"gateway": port}) + "\n", encoding="utf-8",
    )
    yield port
