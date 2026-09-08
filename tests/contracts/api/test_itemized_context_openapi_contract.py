"""冻结上下文 view 的公开 OpenAPI 与离线镜像契约，不启动应用 lifespan。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.main import app

VIEWS = [
    "overview",
    "messages",
    "records",
    "information",
    "inventory",
    "assembly",
    "assemblies",
]
SNAPSHOTS = (
    "src/clients/web/openapi.json",
    "src/clients/web/src/types/openapi/index.json",
)


@pytest.fixture(params=("live", *SNAPSHOTS))
def openapi_document(request: pytest.FixtureRequest):
    if request.param == "live":
        return app.openapi()
    return json.loads((Path.cwd() / request.param).read_text(encoding="utf-8"))


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


@pytest.mark.parametrize("snapshot", SNAPSHOTS)
def test_offline_openapi_is_generated_from_current_routes(snapshot: str) -> None:
    document = json.loads((Path.cwd() / snapshot).read_text(encoding="utf-8"))
    assert document == app.openapi(), f"需运行 bun run gen:openapi 更新 {snapshot}"
