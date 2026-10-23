"""Gateway 单元测试通过 fixture 显式装配完整 Backend 合同。"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from tests.support.gateway_managed_backends import ManagedBackendGraph


@pytest.fixture
def managed_backend_factory() -> Callable[..., ManagedBackendGraph]:
    from tests.support.gateway_managed_backends import backend_graph

    return backend_graph
