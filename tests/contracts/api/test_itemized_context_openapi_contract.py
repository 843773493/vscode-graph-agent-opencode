"""冻结上下文 view 的公开 OpenAPI 与离线镜像契约，不启动应用 lifespan。"""

from __future__ import annotations

import pytest

from app.main import app
from tests.contracts.api.openapi_snapshots import (
    OPENAPI_SNAPSHOTS,
    load_openapi_snapshot,
)

VIEWS = [
    "overview",
    "messages",
    "records",
    "information",
    "inventory",
    "assembly",
    "assemblies",
]
@pytest.fixture(params=("live", *OPENAPI_SNAPSHOTS))
def openapi_document(request: pytest.FixtureRequest):
    if request.param == "live":
        return app.openapi()
    return load_openapi_snapshot(request.param)


@pytest.mark.parametrize(
    "model", ("SessionContextReadRequest", "SessionContextReadResultDTO")
)
def test_context_read_views_match_in_live_and_offline_openapi(
    openapi_document, model: str
) -> None:
    schema = openapi_document["components"]["schemas"][model]
    assert schema["properties"]["view"]["enum"] == VIEWS
    assert schema["properties"]["view"]["type"] == "string"
    if model == "SessionContextReadRequest":
        # 默认读取仍是 active overview，冻结查询必须显式选择。
        assert schema["properties"]["view"]["default"] == "overview"
        assert "#assembly=<assembly_id>" in schema["properties"]["resource"][
            "description"
        ]
        operation = openapi_document["paths"]["/api/v1/context/read"]["post"]
        assert operation["requestBody"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/SessionContextReadRequest"
        }
    else:
        assert "view" in schema["required"]


@pytest.mark.parametrize("snapshot", OPENAPI_SNAPSHOTS)
def test_offline_openapi_is_generated_from_current_routes(snapshot: str) -> None:
    document = load_openapi_snapshot(snapshot)
    assert document == app.openapi(), f"需运行 bun run gen:openapi 更新 {snapshot}"
