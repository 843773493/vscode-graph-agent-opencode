"""冻结 Node Debug 公开 schema，防止 owner 与审计字段再次漂移。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.main import app

SNAPSHOTS = (
    "src/clients/web/openapi.json",
    "src/clients/web/src/types/openapi/index.json",
)


@pytest.fixture(params=("live", *SNAPSHOTS))
def openapi_document(request: pytest.FixtureRequest):
    if request.param == "live":
        return app.openapi()
    return json.loads((Path.cwd() / request.param).read_text(encoding="utf-8"))


def test_node_debug_state_requires_explicit_thread_owner(openapi_document) -> None:
    schema = openapi_document["components"]["schemas"]["NodeDebugStateDTO"]

    assert "thread_id" in schema["required"]
    assert schema["properties"]["thread_id"] == {
        "type": "string",
        "minLength": 1,
        "title": "Thread Id",
    }


def test_node_debug_action_record_exposes_extension_binding_audit(
    openapi_document,
) -> None:
    schemas = openapi_document["components"]["schemas"]
    action_schema = schemas["NodeDebugActionRecordDTO"]
    binding_schema = schemas["ExtensionCatalogBindingAuditDTO"]

    assert action_schema["properties"]["extension_catalog_binding"] == {
        "anyOf": [
            {"$ref": "#/components/schemas/ExtensionCatalogBindingAuditDTO"},
            {"type": "null"},
        ]
    }
    assert binding_schema["required"] == [
        "binding_id",
        "binding_hash",
        "catalog_revision",
        "generation",
        "provider_binding_identity",
        "target_id",
        "target_schema_hash",
    ]


@pytest.mark.parametrize("snapshot", SNAPSHOTS)
def test_node_debug_openapi_snapshot_matches_routes(snapshot: str) -> None:
    document = json.loads((Path.cwd() / snapshot).read_text(encoding="utf-8"))
    assert document == app.openapi(), f"需运行 bun run gen:openapi 更新 {snapshot}"
