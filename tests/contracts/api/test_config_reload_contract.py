from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.gateway.main import app as gateway_app
from app.main import app as workspace_app
from app.schemas.gateway import GatewayConfigEventDTO
from app.schemas.internal_v2.config import ConfigEventDTO
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore
from configs.diagnostics import diagnose_configuration

_ACTIVATION_SCOPES = {
    "current",
    "next_job",
    "next_session",
    "restart_workspace",
    "restart_gateway",
    "mixed",
    "unknown",
}


def test_workspace_and_gateway_event_schemas_expose_activation_scope() -> None:
    workspace_schema = workspace_app.openapi()["components"]["schemas"][
        "ConfigEventDTO"
    ]
    gateway_schema = gateway_app.openapi()["components"]["schemas"][
        "GatewayConfigEventDTO"
    ]

    assert set(workspace_schema["properties"]["activation_scope"]["enum"]) == (
        _ACTIVATION_SCOPES
    )
    assert set(gateway_schema["properties"]["activation_scope"]["enum"]) == (
        _ACTIVATION_SCOPES
    )


def test_workspace_config_update_contract_requires_explicit_cas_scope() -> None:
    schema = workspace_app.openapi()["components"]["schemas"]["ConfigUpdateRequest"]

    assert set(schema["required"]) >= {
        "config_layer",
        "scope",
        "base_layer_revision",
        "base_layer_digest",
        "expected_active_revision",
        "expected_active_digest",
        "idempotency_key",
    }
    assert schema["properties"]["config_layer"]["const"] == "runtime_override"
    assert schema["properties"]["scope"]["const"] == "workspace"


def test_workspace_config_update_contract_rejects_partial_cas_pairs() -> None:
    from pydantic import ValidationError

    from app.schemas.internal_v2.config import ConfigUpdateRequest

    with pytest.raises(ValidationError, match="必须同时提供"):
        ConfigUpdateRequest(
            config_layer="runtime_override",
            scope="workspace",
            base_layer_revision=1,
            base_layer_digest=None,
            expected_active_revision=None,
            expected_active_digest=None,
            idempotency_key="partial-cas-contract",
        )


def test_pending_event_contract_never_reports_applied_paths() -> None:
    changed_paths = ["/ui/theme", "/logger"]
    workspace_event = ConfigEventDTO(
        event_seq=1,
        event_id="event-workspace-pending",
        config_domain="workspace",
        source="watcher",
        result="restart_required",
        activation_scope="restart_workspace",
        changed_paths=changed_paths,
        applied_paths=[],
        deferred_paths=changed_paths,
        occurred_at="2026-09-03T00:00:00+00:00",
    )
    gateway_event = GatewayConfigEventDTO(
        event_seq=1,
        event_id="event-gateway-pending",
        config_domain="gateway",
        source="watcher",
        result="restart_required",
        activation_scope="restart_gateway",
        changed_paths=changed_paths,
        applied_paths=[],
        deferred_paths=changed_paths,
        occurred_at="2026-09-03T00:00:00+00:00",
    )

    assert workspace_event.applied_paths == []
    assert workspace_event.deferred_paths == workspace_event.changed_paths
    assert gateway_event.applied_paths == []
    assert gateway_event.deferred_paths == gateway_event.changed_paths


def test_recovery_routes_expose_strict_proof_and_discard_contracts() -> None:
    workspace_openapi = workspace_app.openapi()
    gateway_openapi = gateway_app.openapi()

    expected_routes = {
        "/api/v1/config/pending/resolve": "ConfigPendingHealthProofRequest",
        "/api/v1/config/pending/discard": "ConfigPendingDiscardRequest",
    }
    for path, schema_name in expected_routes.items():
        request_body = workspace_openapi["paths"][path]["post"]["requestBody"]
        assert request_body["content"]["application/json"]["schema"]["$ref"].endswith(
            f"/{schema_name}"
        )

    expected_gateway_routes = {
        "/api/gateway/config/resolve-restart": "GatewayConfigPendingHealthProofRequest",
        "/api/gateway/config/discard-restart": "GatewayConfigPendingDiscardRequest",
    }
    for path, schema_name in expected_gateway_routes.items():
        request_body = gateway_openapi["paths"][path]["post"]["requestBody"]
        assert request_body["content"]["application/json"]["schema"]["$ref"].endswith(
            f"/{schema_name}"
        )

    for schema_name in (
        "ConfigPendingHealthProofRequest",
        "GatewayConfigPendingHealthProofRequest",
    ):
        schema = (
            workspace_openapi["components"]["schemas"][schema_name]
            if schema_name.startswith("Config")
            else gateway_openapi["components"]["schemas"][schema_name]
        )
        assert "payload" not in schema["properties"]
        assert "api_key" not in json.dumps(schema).lower()
    gateway_proof_schema = gateway_openapi["components"]["schemas"][
        "GatewayConfigPendingHealthProofRequest"
    ]
    assert "gateway_id" in gateway_proof_schema["required"]


def test_configuration_diagnostics_redact_pending_payload_and_keep_secret_reference(
    tmp_path,
) -> None:
    workspace_root = tmp_path / "workspace"
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        store.create_pending_config_candidate(
            config_domain="workspace",
            candidate_id="diagnostic-candidate",
            idempotency_key="diagnostic-retry",
            payload={"api_key": "literal-secret-must-not-appear"},
            source_baseline={},
            candidate_digest="candidate-digest",
            effective_digest="effective-digest",
            target_generation="workspace-generation",
            fencing_token="fencing-token",
            state="candidate_validated",
            secret_bindings={
                "/api_key": {
                    "secret_ref": "env:BOXTEAM_TEST_API_KEY",
                    "secret_version": "version-1",
                    "binding_digest": "binding-digest",
                }
            },
        )
        source_event = store.append_config_source_journal(
            source_key="shared-user-workspace",
            source_event_id="diagnostic-source-event",
            source_path=tmp_path / "workspace.jsonc",
            presence="present",
            layer_revision=1,
            layer_digest="source-digest",
            previous_digest=None,
            origin="file-watcher",
            fanout_id="fanout:diagnostic-source-event",
        )
        store.record_config_source_fanout(
            source_key="shared-user-workspace",
            source_generation=source_event.source_generation,
            workspace_id="workspace-diagnostic",
            status="applied",
            layer_revision=1,
            layer_digest="source-digest",
            result="applied",
        )
        result = diagnose_configuration(
            config_root=tmp_path / "config-root",
            gateway_inline_path=Path("configs/gateway_inline.jsonc"),
            gateway_schema_path=Path("configs/gateway_schema.jsonc"),
            workspace_inline_path=Path("configs/workspace_inline.jsonc"),
            workspace_schema_path=Path("configs/workspace_schema.jsonc"),
            gateway_sqlite_path=tmp_path / "gateway.sqlite",
            workspace_root=workspace_root,
        )
        serialized = json.dumps(result, ensure_ascii=False)
        assert "literal-secret-must-not-appear" not in serialized
        pending = result["workspace"]["sqlite"]["pending"]
        assert pending[0]["secret_bindings"]["/api_key"]["secret_ref"] == (
            "env:BOXTEAM_TEST_API_KEY"
        )
        assert "payload_json" not in pending[0]
        fanout = result["workspace"]["sqlite"]["source_fanout"]
        assert fanout[0]["workspace_id"] == "workspace-diagnostic"
        assert fanout[0]["result"] == "applied"
    finally:
        store.close()
