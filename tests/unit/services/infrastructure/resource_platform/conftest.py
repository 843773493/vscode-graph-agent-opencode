"""资源观察平台单元测试的公共依赖 fixture。"""

from __future__ import annotations

import pytest

from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)


@pytest.fixture
def resource_registry() -> ResourceRegistry:
    return ResourceRegistry()
