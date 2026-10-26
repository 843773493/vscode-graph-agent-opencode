from __future__ import annotations

import json
from pathlib import Path

import httpx
import jsonschema
import pytest
from fastapi import FastAPI

from app.api.config import router as config_router
from app.api.deps import get_config_service, verify_local_token
from app.core.trace_middleware import TraceMiddleware
from app.gateway.main import app as gateway_app
from app.main import app as workspace_app
from app.schemas.gateway.config import GatewayConfigEventDTO
from app.schemas.internal_v2.config import ConfigEventDTO
from app.services.infrastructure.config import ConfigRestartRequiredError
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore
from configs.diagnostics import diagnose_configuration
from configs.runtime import read_jsonc_object

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
    changed_paths = ["/ui/default_model", "/mcp/servers"]
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
        "/api/v1/workspaces/{workspace_id}/config/pending/resolve": "ConfigPendingHealthProofRequest",
        "/api/v1/workspaces/{workspace_id}/config/pending/discard": "ConfigPendingDiscardRequest",
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
            uri=None,
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


@pytest.fixture
async def pointer_event_owner(tmp_path: Path, test_config_path: str, request: pytest.FixtureRequest):
    config = read_jsonc_object(Path(test_config_path))
    config.pop("$schema", None)
    config["config_version"] = 2
    config.setdefault("ui", {})["max_concurrent_agents"] = 4
    config["mcp"] = {"servers": {"pointer_probe": {
        "transport": "stdio", "enabled": False, "command": "unused-pointer-probe",
        "env": {"ROOT/~": "before"},
    }}}
    config_path = tmp_path / "workspace.jsonc"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    workspace_root = tmp_path / "workspace"
    config_dir = Path.cwd() / "configs"
    inline_config_path = None
    reference_kind = getattr(request, "param", None)
    if reference_kind is not None:
        reference = "#/$defs/secret-marker" if reference_kind == "fragment" else (tmp_path / "secret-marker.json").as_uri()
        schema = read_jsonc_object(config_dir / "workspace_schema.jsonc")
        schema["allOf"] = [{
            "if": {"properties": {"ui": {"properties": {"max_concurrent_agents": {"const": 5}}}}},
            "then": {"$ref": reference},
        }]
        (tmp_path / "workspace_schema.jsonc").write_text(json.dumps(schema), encoding="utf-8")
        inline_config_path = tmp_path / "workspace_inline.jsonc"
        inline_config_path.write_bytes((config_dir / "workspace_inline.jsonc").read_bytes())
        config_dir = tmp_path
    store = WorkspaceStateStore(workspace_root=workspace_root)
    service = ConfigService(config_dir=config_dir, inline_config_path=inline_config_path, config_path=config_path,
        workspace_root=workspace_root, workspace_state_store=store)
    service.validate_workspace_config()
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.include_router(config_router, prefix="/api/v1")
    application.dependency_overrides[get_config_service] = lambda: service
    application.dependency_overrides[verify_local_token] = lambda: "pointer-contract"
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
            base_url="http://test") as client:
            yield service, store, client, config_path, config, workspace_root
    finally:
        store.close()


@pytest.mark.parametrize("pending_restart", [False, True])
async def test_real_config_diff_persists_and_serializes_json_pointers(pointer_event_owner, pending_restart: bool) -> None:
    service, store, client, config_path, config, workspace_root = pointer_event_owner
    # 配置值可以是原生路径；事件位置字段必须表达 JSON 键，保留 / 与 ~ 的 Pointer 转义。
    native_value = str(workspace_root / "native-target")
    config["mcp"]["servers"]["pointer_probe"]["env"]["ROOT/~"] = native_value
    config_path.write_text(json.dumps(config), encoding="utf-8")

    async def require_restart(*_snapshots: object) -> None:
        raise ConfigRestartRequiredError("配置候选需要重启", changed_sections=("mcp",))

    if pending_restart:
        with pytest.raises(ConfigRestartRequiredError):
            await service.reload(candidate_applier=require_restart)
    else:
        assert await service.reload()
    response = await client.get("/api/v1/config/events", headers={"X-Request-ID": "pointer-event"})
    assert response.status_code == 200, response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"] == "pointer-event"
    event, = response.json()["data"]["events"]
    expected = ["/mcp/servers/pointer_probe/env/ROOT~1~0"]
    assert event["changed_paths"] == expected
    assert event["applied_paths"] == ([] if pending_restart else expected)
    assert event["deferred_paths"] == (expected if pending_restart else [])
    assert event["result"] == ("restart_required" if pending_restart else "applied")
    assert str(config_path) not in response.text and native_value not in response.text
    # 关闭原连接后重开真正落库记录，排除只验证内存事件的情况。
    store.close()
    with WorkspaceStateStore(workspace_root=workspace_root) as reopened:
        persisted, = reopened.list_config_events(config_domain="workspace")
        assert persisted.changed_paths == tuple(expected)
        assert persisted.applied_paths == tuple(event["applied_paths"])
        assert persisted.deferred_paths == tuple(event["deferred_paths"])


@pytest.mark.parametrize("damage", ["value", "key", "dynamic"])
async def test_workspace_reload_status_rejects_without_echoing_config(
    pointer_event_owner, monkeypatch: pytest.MonkeyPatch, damage: str,
) -> None:
    service, store, client, config_path, config, _workspace_root = pointer_event_owner
    snapshot = service.get_snapshot()
    active = store.get_active_config_snapshot("workspace")
    events = service.list_config_events()
    marker = str(config_path.parent / "secret-marker")
    original = config_path.read_bytes()
    if damage == "value":
        config["ui"]["max_concurrent_agents"] = marker
    elif damage == "key":
        config[marker] = "unknown"
    else:
        config["mcp"]["servers"]["pointer_probe"]["env"] = {marker: 7}
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(ValueError, match="配置验证失败") as failure:
        await service.reload()
    assert isinstance(failure.value.__cause__, jsonschema.ValidationError)
    assert "secret-marker" in str(failure.value.__cause__)
    response = await client.get("/api/v1/config/reload-status", headers={"X-Request-ID": "invalid-workspace"})
    assert response.status_code == 200, response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"] == "invalid-workspace"
    status = response.json()["data"]
    assert status["healthy"] is False
    assert status["reason"] == "invalid_config"
    assert status["last_error"] == f"ValueError: {failure.value}"
    assert "secret-marker" not in response.text
    assert str(config_path.parent) not in response.text
    public_config = await client.get("/api/v1/config")
    assert public_config.status_code == 200, public_config.text
    assert "secret-marker" not in public_config.text
    assert str(config_path.parent) not in public_config.text
    assert service.get_snapshot() is snapshot
    assert store.get_active_config_snapshot("workspace") == active
    assert store.get_pending_config_candidate(config_domain="workspace") is None
    assert service.list_config_events() == events

    config_path.write_bytes(original)
    await service.reload()
    recovered = (await client.get("/api/v1/config/reload-status")).json()["data"]
    assert recovered["healthy"] is True
    assert recovered["last_error"] is None
    assert store.get_active_config_snapshot("workspace") == active


@pytest.mark.parametrize("pointer_event_owner", ["fragment", "file"], indirect=True)
async def test_workspace_reload_status_never_echoes_unresolved_schema_reference(pointer_event_owner) -> None:
    service, store, client, config_path, config, _workspace_root = pointer_event_owner
    active = store.get_active_config_snapshot("workspace")
    events = service.list_config_events()
    original = config_path.read_bytes()
    config["ui"]["max_concurrent_agents"] = 5
    config_path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValueError, match="stage=schema_reference") as failure:
        await service.reload()
    assert failure.value.__cause__ is not None
    assert "secret-marker" in str(failure.value.__cause__)
    response = await client.get("/api/v1/config/reload-status")
    assert response.status_code == 200, response.text
    status = response.json()["data"]
    assert status["healthy"] is False
    assert status["reason"] == "invalid_config"
    assert status["last_error"] == f"ValueError: {failure.value}"
    assert "secret-marker" not in response.text
    assert str(config_path.parent) not in response.text
    assert store.get_active_config_snapshot("workspace") == active
    assert store.get_pending_config_candidate(config_domain="workspace") is None
    assert service.list_config_events() == events
    config_path.write_bytes(original)
    await service.reload()
    recovered = (await client.get("/api/v1/config/reload-status")).json()["data"]
    assert recovered["healthy"] is True
    assert recovered["last_error"] is None
    assert store.get_active_config_snapshot("workspace") == active


@pytest.mark.parametrize("damage", [
    "factory_import", "factory_attribute", "factory_callable", "factory_runtime",
    "factory_syntax", "factory_os", "factory_path", "name",
    "unknown_field", "skill", "tool_id", "duplicate", "custom_type", "agents_type", "agent_key",
])
async def test_workspace_custom_tool_preflight_keeps_native_paths_out_of_status(
    pointer_event_owner, monkeypatch: pytest.MonkeyPatch, damage: str,
) -> None:
    service, store, client, config_path, config, _workspace_root = pointer_event_owner
    snapshot = service.get_snapshot()
    active = store.get_active_config_snapshot("workspace")
    events = service.list_config_events()
    original = config_path.read_bytes()
    marker = str(config_path.parent / "private-preflight-marker")
    spec = {"name": "preflight_probe", "factory": "json:loads"}
    custom = [spec]
    if damage == "factory_import":
        spec["factory"] = "missing_preflight_probe:factory"
    elif damage == "factory_attribute":
        spec["factory"] = "json:missing_preflight_probe"
    elif damage == "factory_callable":
        spec["factory"] = "json:__doc__"
    elif damage in {"factory_runtime", "factory_syntax", "factory_os"}:
        cause_type = {
            "factory_runtime": "RuntimeError", "factory_syntax": "SyntaxError", "factory_os": "OSError",
        }[damage]
        module_path = _workspace_root / "preflight_failure.py"
        module_path.write_text(f"raise {cause_type}({marker!r})\n", encoding="utf-8")
        monkeypatch.syspath_prepend(str(_workspace_root))
        spec["factory"] = "preflight_failure:create"
    elif damage == "factory_path":
        spec["factory"] = marker
    elif damage == "name":
        spec["name"] = marker
    elif damage == "unknown_field":
        spec[marker] = "unknown"
    elif damage == "skill":
        spec["skills"] = [marker]
    elif damage == "tool_id":
        custom = [{"tool_id": marker}]
    elif damage == "duplicate":
        custom.append(dict(spec))
    config["agents"]["default"]["tools"] = {"custom": marker if damage == "custom_type" else custom}
    if damage == "agents_type":
        config["agents"] = marker
    elif damage == "agent_key":
        config["agents"] = {marker: {"tools": {"custom": [{
            "name": "preflight_probe", "factory": "missing_preflight_probe:factory",
        }]}}}
    config_path.write_text(json.dumps(config), encoding="utf-8")
    rejected = config_path.read_bytes()

    with pytest.raises((TypeError, ValueError)) as failure:
        await service.reload()
    assert marker not in str(failure.value) and str(config_path.parent) not in str(failure.value)
    if damage not in {"custom_type", "agents_type"}:
        assert failure.value.__cause__ is not None
    if damage in {"factory_runtime", "factory_syntax", "factory_os"}:
        assert f"cause_type={cause_type}" in str(failure.value)
        assert type(failure.value.__cause__).__name__ == cause_type
        assert marker in str(failure.value.__cause__)
    if damage == "duplicate":
        assert "重复扩展工具名: preflight_probe" in str(failure.value.__cause__)
    response = await client.get("/api/v1/config/reload-status", headers={"X-Request-ID": "preflight-error"})
    assert response.status_code == 200, response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"] == "preflight-error"
    status = response.json()["data"]
    assert status["healthy"] is False and status["reason"] == "invalid_config"
    assert status["last_error"] == f"{type(failure.value).__name__}: {failure.value}"
    public = await client.get("/api/v1/config")
    assert public.status_code == 200, public.text
    for body in (response.text, public.text):
        assert marker not in body and str(config_path.parent) not in body
    assert service.get_snapshot() is snapshot
    assert store.get_active_config_snapshot("workspace") == active
    assert store.get_pending_config_candidate(config_domain="workspace") is None
    assert service.list_config_events() == events
    assert config_path.read_bytes() == rejected

    config_path.write_bytes(original)
    await service.reload()
    recovered = (await client.get("/api/v1/config/reload-status")).json()["data"]
    assert recovered["healthy"] is True and recovered["last_error"] is None
    assert store.get_active_config_snapshot("workspace") == active
