"""冻结 Node Debug 公开 schema，防止 owner 与审计字段再次漂移。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.main import app
from app.schemas.internal_v2.node_debug import NodeDebugStateDTO
from tests.contracts.api.openapi_snapshots import (
    OPENAPI_SNAPSHOTS,
    load_openapi_snapshot,
)


def test_node_debug_state_requires_explicit_thread_owner(openapi_document) -> None:
    schema = openapi_document["components"]["schemas"]["NodeDebugStateDTO"]

    assert "thread_id" in schema["required"]
    assert schema["properties"]["thread_id"] == {
        "type": "string",
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


@pytest.mark.parametrize("snapshot", OPENAPI_SNAPSHOTS)
def test_node_debug_openapi_snapshot_matches_routes(snapshot: str) -> None:
    document = load_openapi_snapshot(snapshot)
    assert document == app.openapi(), f"需运行 bun run gen:openapi 更新 {snapshot}"


@pytest.mark.parametrize("thread_id", ("", "session_legacy", "not-a-thread"))
def test_node_debug_state_rejects_noncanonical_thread_owner(thread_id: str) -> None:
    with pytest.raises(ValidationError, match="thread_id"):
        NodeDebugStateDTO(session_id="ses_019be93d195b7ce48842923ced187ecb", thread_id=thread_id, status="idle")
