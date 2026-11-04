"""联邦与工作区公开结构从descriptor核对，内部配置及生命周期继续各自负责。"""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Callable, Iterator
from copy import deepcopy
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from google.protobuf.descriptor_pb2 import FieldDescriptorProto, FileDescriptorSet
from google.protobuf.json_format import ParseDict
from pydantic import ValidationError
from pydantic.json_schema import JsonSchemaValue

from app.core.path_utils import get_user_gateway_config_path
from app.gateway.auth import verify_federation_token
from app.gateway.config import GatewayConfigReloadService
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.federation import FEDERATION_PROTOCOL_VERSION, RemoteGatewayConnection
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.routes import federation, workspaces
from app.gateway.routes._shared import get_registry
from app.protocol import openapi
from app.protocol.generated.boxteam.gateway.v2 import public_pb2
from app.protocol.schema import descriptor_pool_from_set
from app.schemas.gateway.federation import FederationProtocolManifestDTO
from app.schemas.gateway.lifecycle import GatewayRuntimeGroupRequest
from app.schemas.gateway.workspaces import GatewayWorkspaceDTO
from configs.runtime import read_jsonc_object
from tests.contracts.api.openapi_snapshots import OPENAPI_SNAPSHOTS
from tests.contracts.test_gateway_config_http_protocol_schema import (
    configuration_owner as configuration_owner,  # noqa: PLC0414 - 复用真实配置owner装具
)

ROOTS = ("FederationProtocolManifestDTO", "GatewayWorkspaceListDTO",
         "GatewayRuntimeStateResultDTO", "GatewayRuntimeRestartResultDTO")


@pytest.mark.parametrize(("path", "method", "response"), [
    ("/federation/manifest", "get", "FederationProtocolManifestDTO"),
    ("/workspaces", "get", "GatewayWorkspaceListDTO"),
    ("/workspaces/{workspace_id}/runtime/start", "post", "GatewayRuntimeStateResultDTO"),
    ("/workspaces/{workspace_id}/runtime/stop", "post", "GatewayRuntimeStateResultDTO"),
    ("/workspaces/{workspace_id}/runtime/restart-safe", "post", "GatewayRuntimeRestartResultDTO"),
    ("/workspaces/{workspace_id}/runtime/restart-force", "post", "GatewayRuntimeRestartResultDTO"),
])
def test_federation_schema_follows_actual_routes(
    gateway_openapi: JsonSchemaValue, path: str, method: str, response: str,
) -> None:
    original = deepcopy(gateway_openapi)
    operation = gateway_openapi["paths"]["/api/gateway" + path][method]
    reference = operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    envelope = gateway_openapi["components"]["schemas"][reference.rsplit("/", 1)[1]]
    assert envelope["properties"]["data"]["anyOf"][0]["$ref"].endswith("/" + response)
    assert response in openapi.GATEWAY_HTTP_JSON_DESCRIPTORS
    openapi.assert_gateway_http_schemas(gateway_openapi)
    assert gateway_openapi == original


@pytest.mark.parametrize("model", ROOTS)
def test_federation_schema_requires_exported_roots(gateway_openapi: JsonSchemaValue, model: str) -> None:
    del gateway_openapi["components"]["schemas"][model]
    with pytest.raises(ValueError, match=f"公开 HTTP 结构未被导出: {model}"):
        openapi.assert_gateway_http_schemas(gateway_openapi)


@pytest.mark.parametrize(("model", "field", "shape"), [
    ("FederationProtocolManifestDTO", "config_event_cursor", {"type": "string"}),
    ("FederationProtocolManifestDTO", "config_reload_restart_required", {"type": "integer"}),
    ("GatewayWorkspaceListDTO", "items", {"type": "object"}),
    ("GatewayWorkspaceDTO", "remote_gateways", {"type": "string"}),
    ("GatewayRemoteConnectionSummaryDTO", "config_event_cursor", {"type": "string"}),
    ("GatewayRemoteConnectionSummaryDTO", "restart_required", {"type": "integer"}),
    ("GatewayRuntimeRestartResultDTO", "workspaces", {"type": "string"}),
])
def test_federation_schema_rejects_nested_shape_drift(
    gateway_openapi: JsonSchemaValue, model: str, field: str, shape: JsonSchemaValue,
) -> None:
    gateway_openapi["components"]["schemas"][model]["properties"][field] = shape
    with pytest.raises(ValueError, match="公开结构与 proto 类型不一致"):
        openapi.assert_gateway_http_schemas(gateway_openapi)


@pytest.mark.parametrize("model", (*ROOTS, "GatewayWorkspaceDTO", "GatewayRemoteConnectionSummaryDTO"))
def test_federation_schema_uses_candidate_descriptor(
    gateway_openapi: JsonSchemaValue, http_descriptor_bundle: FileDescriptorSet, model: str,
) -> None:
    message = next(m for f in http_descriptor_bundle.file for m in f.message_type if m.name == model)
    message.field.add(name="new_federation_field", number=100, type=FieldDescriptorProto.TYPE_STRING)
    pool = descriptor_pool_from_set(http_descriptor_bundle.SerializeToString())
    with pytest.raises(ValueError, match="公开结构与 proto 字段不一致") as caught:
        openapi.assert_gateway_http_schemas(gateway_openapi, pool)
    assert model in str(caught.value) and "new_federation_field" in str(caught.value)


@pytest.mark.parametrize("model", ["FederationProtocolManifestDTO", "GatewayRemoteConnectionSummaryDTO"])
def test_federation_export_rejects_drift_before_publishing(
    tmp_path: Path, http_descriptor_bundle: FileDescriptorSet, model: str,
    openapi_output: Path, openapi_export: Callable[[Path, Path], None],
) -> None:
    message = next(m for f in http_descriptor_bundle.file for m in f.message_type if m.name == model)
    message.field.add(name="new_federation_field", number=100, type=FieldDescriptorProto.TYPE_STRING)
    descriptor = tmp_path / "candidate.binpb"
    descriptor.write_bytes(http_descriptor_bundle.SerializeToString())
    with pytest.raises(ValueError, match="公开结构与 proto 字段不一致") as caught:
        openapi_export(openapi_output, descriptor)
    assert "new_federation_field" in str(caught.value)
    assert model in str(caught.value)
    assert all((openapi_output / name).read_bytes() == b"original snapshot\n" for name in OPENAPI_SNAPSHOTS)


@pytest.fixture
def federated_gateway(
    configuration_owner: tuple[FastAPI, GatewayConfigReloadService], tmp_path: Path,
) -> Iterator[tuple[FastAPI, GatewayConfigReloadService, GatewayWorkspaceRegistry]]:
    application, config_owner = configuration_owner
    store = application.state.gateway_state
    assert isinstance(store, GatewayStateStore)
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json", state_store=store)
    application.include_router(federation.router)
    application.include_router(workspaces.router)
    application.dependency_overrides[verify_federation_token] = lambda: "manifest-contract"
    application.dependency_overrides[get_registry] = lambda: registry
    try:
        yield application, config_owner, registry
    finally:
        registry.close()


@pytest.mark.parametrize("pending", [False, True])
async def test_real_federation_http_serialization_survives_generated_consumers(
    federated_gateway: tuple[FastAPI, GatewayConfigReloadService, GatewayWorkspaceRegistry],
    tmp_path: Path, pending: bool,
) -> None:
    application, config_owner, registry = federated_gateway
    # 本测试只核查询序列化；远端发现结果按明确装具登记，不声称SSH或重启验收。
    connection = RemoteGatewayConnection(
        connection_id="rgw_contract", name="Remote", host="remote.example", port=22,
        username="developer", private_key_path=None, ssh_config_host=None,
        remote_gateway_port=8014, remote_gateway_id="gateway_00000000000040008000000000000002",
        protocol_version=FEDERATION_PROTOCOL_VERSION, source_owner="manual",
        remote_config_event_cursor=0 if pending else None,
        remote_config_state="pending_restart" if pending else None,
        remote_restart_required=pending, remote_candidate_ref="candidate-remote" if pending else None,
    )
    registry.apply_remote_projection_snapshot(connection=connection, projections=(WorkspaceTarget(
        gateway_id="gateway_00000000000040008000000000000002",
        workspace_id="00000000-0000-4000-8000-000000000002", name="Remote workspace", root_path=None,
        backend_url="http://remote.invalid", connection_kind="remote_gateway",
        owner="remote_projection",
        connection_id="rgw_contract", remote_gateway_connection_ids=("rgw_contract",),
    ),))
    path = get_user_gateway_config_path()
    payload = read_jsonc_object(path)
    logging = payload.setdefault("logging", {})
    assert isinstance(logging, dict)
    runtime = logging.setdefault("runtime", {})
    assert isinstance(runtime, dict)
    runtime["level"] = "debug"
    path.write_text(json.dumps(payload))
    await config_owner.reload()
    samples = {}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        for endpoint, name in (("federation/manifest", "FederationProtocolManifestDTO"),
                               ("workspaces?check_health=false", "GatewayWorkspaceListDTO")):
            response = await client.get("/api/gateway/" + endpoint, headers={"X-Request-ID": "federation-" + name})
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["request_id"] == response.headers["X-Request-ID"] == "federation-" + name
            samples[name] = body["data"]
            message = ParseDict(body["data"], getattr(public_pb2, name)())
            assert type(message).FromString(message.SerializeToString()) == message
    manifest = samples["FederationProtocolManifestDTO"]
    assert manifest["config_event_cursor"] > 0 and manifest["config_reload_state"] == "active"
    assert manifest["config_reload_restart_required"] is False
    workspace = samples["GatewayWorkspaceListDTO"]["items"][0]
    remote, = workspace["remote_gateways"]
    assert remote["config_event_cursor"] == (0 if pending else None)
    assert remote["restart_required"] is pending
    assert remote["config_reload_state"] == ("pending_restart" if pending else None)
    assert remote["candidate_ref"] == ("candidate-remote" if pending else None)
    assert workspace["workspace_id"] == "00000000-0000-4000-8000-000000000002"
    assert workspace["gateway_id"] == remote["gateway_id"] == "gateway_00000000000040008000000000000002"
    assert "backend_workspace_id" not in workspace
    manifest_message = ParseDict(manifest, public_pb2.FederationProtocolManifestDTO())
    assert manifest_message.HasField("config_reload_restart_required")
    assert manifest_message.config_reload_restart_required is False
    workspace_message = ParseDict(workspace, public_pb2.GatewayWorkspaceDTO())
    remote_message, = workspace_message.remote_gateways
    assert remote_message.HasField("config_event_cursor") is pending
    assert remote_message.HasField("restart_required")
    assert remote_message.restart_required is pending
    receipt = tmp_path / "http.json"
    receipt.write_text(json.dumps(samples, ensure_ascii=False))
    command = ["bun", "tests/contracts/gateway_config_consumer.mjs", str(receipt)]
    consumer = await asyncio.to_thread(subprocess.run, command, capture_output=True, text=True, timeout=30, check=False)
    (tmp_path / "consumer.stdout.log").write_text(consumer.stdout)
    (tmp_path / "consumer.stderr.log").write_text(consumer.stderr)
    (tmp_path / "consumer.json").write_text(json.dumps({"command": command, "exit_code": consumer.returncode}) + "\n")
    assert consumer.returncode == 0, consumer.stdout + consumer.stderr


def test_federation_structure_keeps_business_validation() -> None:
    with pytest.raises(ValidationError):
        FederationProtocolManifestDTO.model_validate({"protocol_version": 1, "gateway_id": "gateway", "federation_depth": 1})
    with pytest.raises(ValidationError):
        FederationProtocolManifestDTO(protocol_version=1, gateway_id="gateway", config_event_cursor=-1)
    with pytest.raises(ValidationError):
        GatewayRuntimeGroupRequest(expected_backend_generation="generation")
    with pytest.raises(ValidationError):
        GatewayWorkspaceDTO.model_validate({"workspace_id": "workspace", "name": "name", "backend_url": "http://backend",
            "connection_kind": "transitive_gateway", "status": "ready", "runtime_action": "probe_external_backend", "checked_at": "now"})
