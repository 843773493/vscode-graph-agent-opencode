"""AGENTS 发现复用原 proto 资源导航响应，不增加第二份来源 DTO。"""

import pytest
from pydantic.json_schema import JsonSchemaValue

from app.protocol.openapi import HTTP_JSON_DESCRIPTORS, assert_public_http_schemas
from tests.contracts.api.openapi_snapshots import (
    OPENAPI_SNAPSHOTS,
    load_openapi_snapshot,
)


def test_agent_spec_discovery_exports_original_navigation_response(http_openapi: JsonSchemaValue) -> None:
    operation = http_openapi["paths"]["/api/v1/workspaces/{workspace_id}/resources/agent-spec"]["get"]
    assert "requestBody" not in operation
    response = operation["responses"]["200"]["content"]["application/json"]["schema"]
    envelope = http_openapi["components"]["schemas"][response["$ref"].rsplit("/", 1)[1]]
    assert envelope["properties"]["data"]["anyOf"][0]["$ref"].endswith("/ResourceNavigationResultDTO")
    assert "ResourceNavigationResultDTO" in HTTP_JSON_DESCRIPTORS
    assert_public_http_schemas(http_openapi)


@pytest.mark.parametrize("snapshot", OPENAPI_SNAPSHOTS)
def test_agent_spec_discovery_offline_snapshots_match_routes(snapshot: str, http_openapi: JsonSchemaValue) -> None:
    assert load_openapi_snapshot(snapshot) == http_openapi, f"需运行 bun run gen:openapi 更新 {snapshot}"
