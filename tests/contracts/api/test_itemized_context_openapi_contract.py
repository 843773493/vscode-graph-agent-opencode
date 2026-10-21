"""冻结上下文 view 的公开 OpenAPI 与离线镜像契约，不启动应用 lifespan。"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.agents.tools.session_history import ReadContextInput, SearchContextInput
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
        assert set(schema["required"]) >= {"resource_identity", "uri", "scope"}
        assert "resource" not in schema["properties"]
        assert "assembly_ref" in schema["properties"]
        operation = openapi_document["paths"]["/api/v1/workspaces/{workspace_id}/context/read"]["post"]
        assert operation["requestBody"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/SessionContextReadRequest"
        }
    else:
        assert "view" in schema["required"]


@pytest.mark.parametrize("snapshot", OPENAPI_SNAPSHOTS)
def test_offline_openapi_is_generated_from_current_routes(snapshot: str) -> None:
    document = load_openapi_snapshot(snapshot)
    assert document == app.openapi(), f"需运行 bun run gen:openapi 更新 {snapshot}"


@pytest.fixture
def gateway_context_skill_schemas() -> dict[str, dict[str, object]]:
    path = Path.cwd() / "resources/skills/gateway-context/SKILL.md"
    content = path.read_text()
    assert "allowed-tools: read_context, search_context" in content
    contracts = [json.loads(block) for block in re.findall(r"```json\n([\s\S]*?)\n```", content)]
    assert [contract["tool_name"] for contract in contracts] == ["read_context", "search_context"]
    return {contract["tool_name"]: contract["arguments_schema"] for contract in contracts}


def _schema_constraints(value: object) -> object:
    if isinstance(value, dict):
        return {key: _schema_constraints(part) for key, part in value.items() if key not in {"title", "description"}}
    if isinstance(value, list):
        return [_schema_constraints(part) for part in value]
    return value


@pytest.mark.parametrize("tool_name,input_type", (("read_context", ReadContextInput), ("search_context", SearchContextInput)))
def test_builtin_gateway_context_skill_matches_real_tool_input_contract(
    gateway_context_skill_schemas, tool_name, input_type,
) -> None:
    # 模型读到的发行包参数合同必须与真实工具保持一致，防止旧字段诱发错误调用。
    assert _schema_constraints(gateway_context_skill_schemas[tool_name]) == _schema_constraints(input_type.model_json_schema())


def test_sealed_resource_projection_schema_is_closed(openapi_document) -> None:
    schemas = openapi_document["components"]["schemas"]
    resource = schemas["SessionContextResourceDTO"]
    assert resource["additionalProperties"] is False
    assert set(resource["properties"]) == {"display_uri", "resource_kind", "owner_scope", "facet", "revision", "availability", "provenance_ref"}
    assert set(resource["required"]) == set(resource["properties"])
    assert {"type": "null"} in resource["properties"]["display_uri"]["anyOf"]
    assert schemas["SessionContextItemDTO"]["properties"]["resource_refs"]["items"] == {"$ref": "#/components/schemas/SessionContextResourceDTO"}
    assert schemas["SessionContextProvenanceRefDTO"]["additionalProperties"] is False
