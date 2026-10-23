from __future__ import annotations

import pytest

from app.main import app
from tests.contracts.api.openapi_snapshots import (
    OPENAPI_SNAPSHOTS,
    load_openapi_snapshot,
)


@pytest.fixture(scope="session", params=("live", *OPENAPI_SNAPSHOTS))
def openapi_document(request: pytest.FixtureRequest) -> dict[str, object]:
    """每个测试进程按 live 与离线镜像各读取一次只读 OpenAPI 文档。"""
    if request.param == "live":
        return app.openapi()
    return load_openapi_snapshot(request.param)
