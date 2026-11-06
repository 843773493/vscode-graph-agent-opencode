"""目标库存的公开协议、真实 OpenAPI 与原描述 Struct 保持同形。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import pytest
from google.protobuf import json_format
from google.protobuf.descriptor_pb2 import FieldDescriptorProto, FileDescriptorSet
from pydantic import ValidationError
from pydantic.json_schema import JsonSchemaValue

from app.protocol import openapi
from app.protocol.generated.boxteam.gateway.v2 import public_pb2
from app.protocol.schema import assert_public_schema, descriptor_pool_from_set
from app.schemas.gateway.extension_installation import (
    ExtensionInstallationInventoryDTO,
    ExtensionInstallationInventoryRequest,
    ExtensionValidation,
    InstalledExtension,
    ProfileExtensionInventory,
)
from tests.contracts.api.openapi_snapshots import OPENAPI_SNAPSHOTS

PATH = "/api/gateway/extension-installations/inventory"
MODELS = (ExtensionInstallationInventoryRequest, ExtensionInstallationInventoryDTO,
          ProfileExtensionInventory, InstalledExtension, ExtensionValidation)


def test_inventory_contract_follows_actual_read_only_route(gateway_openapi: JsonSchemaValue) -> None:
    operation = gateway_openapi["paths"][PATH]["post"]
    assert operation["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith("/ExtensionInstallationInventoryRequest")
    response = operation["responses"]["200"]["content"]["application/json"]["schema"]
    envelope = gateway_openapi["components"]["schemas"][response["$ref"].rsplit("/", 1)[1]]
    assert envelope["properties"]["data"]["anyOf"][0]["$ref"].endswith("/ExtensionInstallationInventoryDTO")
    assert [path for path in gateway_openapi["paths"] if path.startswith("/api/gateway/extension-installations")] == [PATH]
    assert set(gateway_openapi["paths"][PATH]) == {"post"}
    openapi.assert_gateway_http_schemas(gateway_openapi)
    for model in MODELS:
        assert_public_schema(model.model_json_schema(), public_pb2.DESCRIPTOR.message_types_by_name[model.__name__])


def test_inventory_proto_preserves_original_description_and_invalid_entry() -> None:
    request = ExtensionInstallationInventoryRequest(protocol_version=1, gateway_id="gateway_" + "a" * 32,
        backend_id=str(uuid4()), workspace_ids=[str(uuid4())])
    original = ExtensionInstallationInventoryDTO(**request.model_dump(), inventory=ProfileExtensionInventory(
        profile_id="default-profile", extensions=[InstalledExtension(source="profile", is_valid=False,
            description={"identifier": {"value": "publisher.probe"}, "version": "1.0.0", "isBuiltin": False,
                "extensionLocation": {"$mid": 1, "scheme": "file", "path": "/backend/extensions/probe"},
                "custom": {"rootPath": "C:\\backend\\probe", "values": [None, True, 42, {"file": "arbitrary.json"}]}},
            validations=[ExtensionValidation(severity=3, message="原 manifest 缺失")])]))
    wire = json_format.ParseDict(original.model_dump(mode="json"), public_pb2.ExtensionInstallationInventoryDTO())
    restored = ExtensionInstallationInventoryDTO.model_validate(json_format.MessageToDict(wire,
        preserving_proto_field_name=True, always_print_fields_with_no_presence=True))
    assert restored == original
    assert wire.inventory.extensions[0].description.fields["extensionLocation"].struct_value.fields["path"].string_value == "/backend/extensions/probe"
    assert restored.inventory.extensions[0].is_valid is False


@pytest.mark.parametrize("invalid", ["missing-backend", "null-backend", "view", "duplicate", "boolean-version"])
def test_inventory_request_requires_precise_backend_and_workspace_roles(invalid: str) -> None:
    payload = {"protocol_version": 1, "gateway_id": "gateway_" + "a" * 32, "backend_id": str(uuid4()), "workspace_ids": [str(uuid4())]}
    if invalid == "missing-backend":
        del payload["backend_id"]
    elif invalid == "null-backend":
        payload["backend_id"] = None
    elif invalid == "view":
        payload["view_id"] = "window-a"
    elif invalid == "duplicate":
        payload["workspace_ids"] *= 2
    else:
        payload["protocol_version"] = True
    with pytest.raises(ValidationError):
        ExtensionInstallationInventoryRequest.model_validate(payload)


@pytest.mark.parametrize("severity", [True, -1, 4, "3"])
def test_inventory_validation_uses_exact_original_severity(severity: object) -> None:
    with pytest.raises(ValidationError):
        ExtensionValidation.model_validate({"severity": severity, "message": "原诊断"})


@pytest.mark.parametrize("model", [value.__name__ for value in MODELS])
def test_inventory_rejects_candidate_descriptor_drift(
    gateway_openapi: JsonSchemaValue, http_descriptor_bundle: FileDescriptorSet, model: str,
) -> None:
    message = next(message for file in http_descriptor_bundle.file if file.package == "boxteam.gateway.v2"
                   for message in file.message_type if message.name == model)
    message.field.add(name="new_inventory_field", number=100, type=FieldDescriptorProto.TYPE_STRING)
    pool = descriptor_pool_from_set(http_descriptor_bundle.SerializeToString())
    with pytest.raises(ValueError, match="公开结构与 proto 字段不一致") as caught:
        openapi.assert_gateway_http_schemas(gateway_openapi, pool)
    assert "new_inventory_field" in str(caught.value) and model in str(caught.value)


@pytest.mark.parametrize("model", ["ExtensionInstallationInventoryRequest", "InstalledExtension"])
def test_inventory_export_rejects_root_or_nested_drift_before_publishing(
    tmp_path: Path, http_descriptor_bundle: FileDescriptorSet, model: str,
    openapi_output: Path, openapi_export: Callable[[Path, Path], None],
) -> None:
    message = next(message for file in http_descriptor_bundle.file if file.package == "boxteam.gateway.v2"
                   for message in file.message_type if message.name == model)
    message.field.add(name="new_inventory_field", number=100, type=FieldDescriptorProto.TYPE_STRING)
    descriptor_set = tmp_path / "candidate.binpb"
    descriptor_set.write_bytes(http_descriptor_bundle.SerializeToString())
    with pytest.raises(ValueError, match="公开结构与 proto 字段不一致") as caught:
        openapi_export(openapi_output, descriptor_set)
    assert "new_inventory_field" in str(caught.value) and model in str(caught.value)
    assert all((openapi_output / name).read_bytes() == b"original snapshot\n" for name in OPENAPI_SNAPSHOTS)
