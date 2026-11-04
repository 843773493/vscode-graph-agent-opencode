"""快速访问页面共用端口租约，按各测试文件的隔离块选择并保留绑定。"""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.harness.python.run_context import TestRunContext
from tests.support.e2e_control.harness import E2EProcessLease
from tests.support.ports import integration_port_block_for_file


@pytest.fixture
def search_gateway_port(request: pytest.FixtureRequest) -> Iterator[int]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    block = integration_port_block_for_file(Path(request.node.path))
    lease, probes = E2EProcessLease.acquire_available(
        context.runtime_root_for_node(request.node.nodeid) / "gateway-port.json", range(block.port(20), block.end + 1),
    )
    port = lease.ports[0]
    try:
        (context.artifacts_dir / "port-allocation.json").write_text(json.dumps({
            "block": {"start": block.start, "end": block.end}, "gateway": port,
            "probes": [{"port": candidate, "available": available} for candidate, available in probes],
        }, indent=2) + "\n", encoding="utf-8")
        yield port
    finally:
        lease.release()
