"""快速访问页面共用端口租约，按各测试文件的隔离块选择并保留绑定。"""

import errno
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
    probes: list[dict[str, int | bool]] = []
    for port in range(block.port(20), block.end + 1):
        lease = E2EProcessLease(context.runtime_root_for_node(request.node.nodeid) / "gateway-port.json", (port,))
        try:
            lease.acquire()
        except RuntimeError as error:
            cause = error.__cause__
            if not isinstance(cause, OSError) or (
                cause.errno != errno.EADDRINUSE and getattr(cause, "winerror", None) != 10048
            ):
                raise
            probes.append({"port": port, "available": False})
            continue
        probes.append({"port": port, "available": True})
        try:
            (context.artifacts_dir / "port-allocation.json").write_text(json.dumps({
                "block": {"start": block.start, "end": block.end}, "gateway": port, "probes": probes,
            }, indent=2) + "\n", encoding="utf-8")
            yield port
        finally:
            lease.release()
        return
    raise RuntimeError(f"快速访问测试端口块没有空闲端口: {block}, probes={probes}")
