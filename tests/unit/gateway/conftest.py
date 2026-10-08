"""Gateway 单元测试通过 fixture 显式装配完整 Backend 合同。"""

from collections.abc import Callable

import pytest

from tests.support.gateway_managed_backends import ManagedBackendGraph, backend_graph


@pytest.fixture
def managed_backend_factory() -> Callable[..., ManagedBackendGraph]:
    return backend_graph
