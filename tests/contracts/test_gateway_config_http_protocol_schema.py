"""Gateway配置HTTP以descriptor核结构，保留原来源、pending和错误语义。"""

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

from app.core.path_utils import (
    get_user_gateway_config_path,
    get_user_gateway_local_config_path,
    get_user_gateway_schema_path,
)
from app.core.trace_middleware import TraceMiddleware
from app.gateway.auth import verify_gateway_token
from app.gateway.config import GatewayConfigReloadService, load_gateway_config
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.routes.health_config import router
from app.protocol import openapi
from app.protocol.generated.boxteam.gateway.v2 import public_pb2
from app.protocol.schema import descriptor_pool_from_set
from app.schemas.gateway.config import (
    GatewayConfigPendingDiscardRequest,
    GatewayConfigPendingHealthProofRequest,
    GatewayConfigReloadStatusDTO,
    GatewayConfigSourceDTO,
)
from configs.runtime import read_jsonc_object
from tests.contracts.api.openapi_snapshots import OPENAPI_SNAPSHOTS

ROOTS = (
    "GatewayConfigSourcesDTO", "GatewayConfigReloadStatusDTO", "GatewayConfigEventsDTO",
    "GatewayConfigPendingHealthProofRequest", "GatewayConfigPendingDiscardRequest",
)


@pytest.mark.parametrize(("path", "method", "response", "request_model"), [
    ("sources", "get", "GatewayConfigSourcesDTO", None),
    ("reload-status", "get", "GatewayConfigReloadStatusDTO", None),
    ("events", "get", "GatewayConfigEventsDTO", None),
    ("retry-restart", "post", "GatewayConfigReloadStatusDTO", None),
    ("resolve-restart", "post", "GatewayConfigReloadStatusDTO", "GatewayConfigPendingHealthProofRequest"),
    ("discard-restart", "post", "GatewayConfigReloadStatusDTO", "GatewayConfigPendingDiscardRequest"),
])
def test_configuration_schema_follows_actual_routes(
    gateway_openapi: JsonSchemaValue, path: str, method: str, response: str, request_model: str | None,
) -> None:
    original = deepcopy(gateway_openapi)
    operation = gateway_openapi["paths"]["/api/gateway/config/" + path][method]
    reference = operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    envelope = gateway_openapi["components"]["schemas"][reference.rsplit("/", 1)[1]]
    assert envelope["properties"]["data"]["anyOf"][0]["$ref"].endswith("/" + response)
    if request_model is not None:
        body = operation["requestBody"]["content"]["application/json"]["schema"]
        assert body["$ref"].endswith("/" + request_model)
    assert response in openapi.GATEWAY_HTTP_JSON_DESCRIPTORS
    openapi.assert_gateway_http_schemas(gateway_openapi)
    assert gateway_openapi == original


@pytest.mark.parametrize("model", ROOTS)
def test_configuration_schema_requires_exported_roots(gateway_openapi: JsonSchemaValue, model: str) -> None:
    del gateway_openapi["components"]["schemas"][model]
    with pytest.raises(ValueError, match=f"公开 HTTP 结构未被导出: {model}"):
        openapi.assert_gateway_http_schemas(gateway_openapi)


@pytest.mark.parametrize(("model", "field", "shape"), [
    ("GatewayConfigSourcesDTO", "policy_manifest", {"type": "object"}),
    ("GatewayConfigSourceDTO", "uri", {"type": "integer"}),
    ("GatewayConfigReloadStatusDTO", "active_revision", {"type": "string"}),
    ("GatewayConfigReloadStatusDTO", "layer_digests", {"type": "array"}),
    ("GatewayConfigReloadStatusDTO", "deferred_paths", {"type": "object"}),
    ("GatewayConfigEventsDTO", "events", {"type": "object"}),
    ("GatewayConfigEventDTO", "pending_revision", {"type": "string"}),
    ("GatewayConfigPendingHealthProofRequest", "consumer_health_digests", {"type": "object", "additionalProperties": {"type": "integer"}}),
    ("GatewayConfigPendingDiscardRequest", "expected_active_revision", {"type": "string"}),
])
def test_configuration_schema_rejects_nested_shape_drift(
    gateway_openapi: JsonSchemaValue, model: str, field: str, shape: JsonSchemaValue,
) -> None:
    gateway_openapi["components"]["schemas"][model]["properties"][field] = shape
    with pytest.raises(ValueError, match="公开结构与 proto 类型不一致"):
        openapi.assert_gateway_http_schemas(gateway_openapi)


@pytest.mark.parametrize("model", (*ROOTS, "GatewayConfigSourceDTO", "GatewayConfigEventDTO"))
def test_configuration_schema_uses_candidate_descriptor(
    gateway_openapi: JsonSchemaValue, http_descriptor_bundle: FileDescriptorSet, model: str,
) -> None:
    message = next(m for f in http_descriptor_bundle.file for m in f.message_type if m.name == model)
    message.field.add(name="new_configuration_field", number=100, type=FieldDescriptorProto.TYPE_STRING)
    pool = descriptor_pool_from_set(http_descriptor_bundle.SerializeToString())
    with pytest.raises(ValueError, match="公开结构与 proto 字段不一致") as caught:
        openapi.assert_gateway_http_schemas(gateway_openapi, pool)
    assert model in str(caught.value) and "new_configuration_field" in str(caught.value)


@pytest.mark.parametrize("model", ["GatewayConfigReloadStatusDTO", "GatewayConfigEventDTO"])
def test_configuration_export_rejects_drift_before_publishing(
    tmp_path: Path, http_descriptor_bundle: FileDescriptorSet, model: str,
    openapi_output: Path, openapi_export: Callable[[Path, Path], None],
) -> None:
    message = next(m for f in http_descriptor_bundle.file for m in f.message_type if m.name == model)
    message.field.add(name="new_configuration_field", number=100, type=FieldDescriptorProto.TYPE_STRING)
    descriptor = tmp_path / "candidate.binpb"
    descriptor.write_bytes(http_descriptor_bundle.SerializeToString())
    with pytest.raises(ValueError, match="公开结构与 proto 字段不一致") as caught:
        openapi_export(openapi_output, descriptor)
    assert "new_configuration_field" in str(caught.value)
    assert model in str(caught.value)
    assert all((openapi_output / name).read_bytes() == b"original snapshot\n" for name in OPENAPI_SNAPSHOTS)


@pytest.fixture
def configuration_owner(tmp_path: Path) -> Iterator[tuple[FastAPI, GatewayConfigReloadService]]:
    # autouse装具提供隔离HOME和完整配置；不启动Gateway生命周期或复用开发服务。
    with GatewayStateStore(path=tmp_path / "gateway.sqlite") as store:
        config = load_gateway_config(state_store=store)
        owner = GatewayConfigReloadService(
            state_store=store, config=config, config_path=get_user_gateway_config_path(),
            local_config_path=get_user_gateway_local_config_path(),
            schema_path=get_user_gateway_schema_path(),
        )
        owner.initialize_active_snapshot()
        application = FastAPI()
        application.add_middleware(TraceMiddleware)
        application.include_router(router)
        application.state.gateway_state = store
        application.state.gateway_config_reload = owner
        application.dependency_overrides[verify_gateway_token] = lambda: "configuration-contract"
        yield application, owner


async def test_real_configuration_http_serialization_survives_all_generated_consumers(
    configuration_owner: tuple[FastAPI, GatewayConfigReloadService], tmp_path: Path,
) -> None:
    application, owner = configuration_owner
    path = get_user_gateway_config_path()
    payload = read_jsonc_object(path)
    logging = payload.setdefault("logging", {})
    assert isinstance(logging, dict)
    runtime = logging.setdefault("runtime", {})
    assert isinstance(runtime, dict)
    runtime["level"] = "debug"
    path.write_text(json.dumps(payload))
    await owner.reload()
    samples = {}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        for endpoint, name in (("sources", "GatewayConfigSourcesDTO"),
                               ("reload-status", "GatewayConfigReloadStatusDTO"),
                               ("events", "GatewayConfigEventsDTO")):
            response = await client.get("/api/gateway/config/" + endpoint, headers={"X-Request-ID": "configuration-" + endpoint})
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["request_id"] == response.headers["X-Request-ID"] == "configuration-" + endpoint
            samples[name] = body["data"]
            message = ParseDict(body["data"], getattr(public_pb2, name)())
            assert type(message).FromString(message.SerializeToString()) == message
    assert samples["GatewayConfigSourcesDTO"]["policy_manifest"]
    assert samples["GatewayConfigSourcesDTO"]["sources"]
    status = samples["GatewayConfigReloadStatusDTO"]
    assert status["state"] == "active" and status["active_revision"] is not None
    assert status["layer_digests"] and status["pending_revision"] is None
    events = samples["GatewayConfigEventsDTO"]["events"]
    assert events and events[-1]["result"] == "applied" and events[-1]["applied_paths"]
    receipt = tmp_path / "http.json"
    receipt.write_text(json.dumps(samples, ensure_ascii=False))
    command = ["bun", "tests/contracts/gateway_config_consumer.mjs", str(receipt)]
    consumer = await asyncio.to_thread(subprocess.run, command, capture_output=True, text=True, timeout=30, check=False)
    (tmp_path / "consumer.stdout.log").write_text(consumer.stdout)
    (tmp_path / "consumer.stderr.log").write_text(consumer.stderr)
    (tmp_path / "consumer.json").write_text(json.dumps({"command": command, "exit_code": consumer.returncode}) + "\n")
    assert consumer.returncode == 0, consumer.stdout + consumer.stderr


def test_configuration_structure_keeps_business_validation() -> None:
    status = GatewayConfigReloadStatusDTO(reason="recovery_required", layer_digests={"user": None})
    message = ParseDict(status.model_dump(mode="json"), public_pb2.GatewayConfigReloadStatusDTO())
    assert message.layer_digests["user"].WhichOneof("kind") == "null_value"
    with pytest.raises(ValidationError):
        GatewayConfigReloadStatusDTO.model_validate({"reason": "silently_recovered"})
    with pytest.raises(ValidationError):
        GatewayConfigSourceDTO(uri="/private/gateway.jsonc", layer="user", precedence=1, loaded=True)
    with pytest.raises(ValidationError):
        GatewayConfigPendingDiscardRequest(expected_active_revision=-1, expected_active_digest="digest")
    with pytest.raises(ValidationError, match="extra_forbidden"):
        GatewayConfigPendingHealthProofRequest.model_validate({"unexpected": True})
