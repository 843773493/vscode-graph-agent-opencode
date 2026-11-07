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
from app.schemas.gateway.extension_host import (
    ExtensionHostOpenRequest,
    ExtensionHostTargetRequest,
    ExtensionTargetDTO,
    ExtensionTargetListDTO,
    ExtensionTargetListRequest,
    ExtensionTargetViewDTO,
)
from app.schemas.gateway.extension_installation import (
    ExtensionGalleryInstallOptions,
    ExtensionInstallationGalleryDTO,
    ExtensionInstallationGalleryRequest,
    ExtensionInstallationInstallDTO,
    ExtensionInstallationInstallRequest,
    ExtensionInstallationInventoryDTO,
    ExtensionInstallationInventoryRequest,
    ExtensionInstallationMetadataRequest,
    ExtensionInstallationUninstallDTO,
    ExtensionInstallationUninstallRequest,
    ExtensionMetadataPatch,
    ExtensionUninstallResult,
    ExtensionValidation,
    InstalledExtension,
    ProfileExtensionInventory,
    VsixInstallResult,
)
from app.schemas.gateway.management_targets import (
    GatewayManagementDirectoryDTO,
    GatewayManagementTargetDTO,
)
from tests.contracts.api.openapi_snapshots import OPENAPI_SNAPSHOTS

PATH = "/api/gateway/extension-installations/inventory"
MODELS = (ExtensionInstallationInventoryRequest, ExtensionInstallationInventoryDTO,
          ProfileExtensionInventory, InstalledExtension, ExtensionValidation,
          ExtensionInstallationInstallRequest, ExtensionInstallationInstallDTO, VsixInstallResult,
          ExtensionInstallationUninstallRequest, ExtensionInstallationUninstallDTO, ExtensionUninstallResult,
          ExtensionGalleryInstallOptions, ExtensionInstallationGalleryRequest, ExtensionInstallationGalleryDTO,
          ExtensionMetadataPatch, ExtensionInstallationMetadataRequest,
          ExtensionTargetDTO, ExtensionTargetViewDTO, ExtensionTargetListRequest, ExtensionTargetListDTO,
          GatewayManagementDirectoryDTO, GatewayManagementTargetDTO)


@pytest.mark.parametrize("metadata", [False, True])
def test_gallery_and_metadata_proto_preserve_explicit_false_and_target(metadata: bool) -> None:
    target = {"protocol_version": 1, "gateway_id": "gateway_" + "a" * 32, "backend_id": str(uuid4()),
        "gateway_connection_id": "frozen-route", "extension_id": "publisher.probe", "version": "1.0.0"}
    if metadata:
        original = ExtensionInstallationMetadataRequest.model_validate({**target, "profile_id": "default-profile",
            "metadata": {"pinned": False, "pre_release": True}, "refresh_from_gallery": True})
    else:
        original = ExtensionInstallationGalleryRequest.model_validate({**target,
            "options": {"pinned": False, "install_given_version": True, "install_pre_release_version": False}})
    message = getattr(public_pb2, type(original).__name__)()
    json_format.ParseDict(original.model_dump(mode="json", exclude_none=True), message)
    restored = type(original).model_validate(json_format.MessageToDict(message, preserving_proto_field_name=True,
        always_print_fields_with_no_presence=True))
    assert restored == original
    options = message.metadata if metadata else message.options
    assert options.HasField("pinned") and options.pinned is False
    assert not options.HasField("is_machine_scoped")
    assert "workspace_ids" not in restored.model_dump()


@pytest.mark.parametrize(("model", "field", "patch"), [
    (ExtensionInstallationGalleryRequest, "options", {"donot_verify_signature": True}),
    (ExtensionInstallationMetadataRequest, "metadata", {"id": "forged-gallery-id"}),
    (ExtensionInstallationMetadataRequest, "metadata", {"pinned": "false"}),
])
def test_native_installation_options_do_not_accept_unowned_fields(model, field, patch) -> None:
    payload = {"protocol_version": 1, "gateway_id": "gateway_" + "a" * 32, "backend_id": str(uuid4()),
        "extension_id": "publisher.probe", "version": "1.0.0", field: patch}
    if model is ExtensionInstallationMetadataRequest:
        payload["profile_id"] = "default-profile"
    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_management_directory_and_view_keep_separate_workspace_contracts(gateway_openapi: JsonSchemaValue) -> None:
    schema = gateway_openapi["components"]["schemas"]
    assert "/api/gateway/management/gateways" in gateway_openapi["paths"]
    assert "/api/gateway/extension-targets" in gateway_openapi["paths"]
    for model in (ExtensionTargetDTO, ExtensionTargetListRequest, ExtensionTargetListDTO,
                  ExtensionInstallationInventoryRequest, ExtensionInstallationInventoryDTO,
                  ExtensionInstallationUninstallRequest, ExtensionInstallationUninstallDTO):
        assert "workspace_ids" not in schema[model.__name__]["properties"]
    for model in (ExtensionHostTargetRequest, ExtensionTargetViewDTO,
                  ExtensionInstallationInstallRequest, ExtensionInstallationInstallDTO):
        assert schema[model.__name__]["properties"]["workspace_ids"]["minItems"] == 1
    assert ExtensionHostOpenRequest.model_json_schema()["properties"]["workspace_ids"]["minItems"] == 1
    for model in (ExtensionInstallationInventoryDTO, ExtensionInstallationUninstallDTO, ExtensionInstallationInstallDTO):
        assert "gateway_connection_id" not in schema[model.__name__]["properties"]


def test_target_list_proto_preserves_stable_domain_without_workspace() -> None:
    gateway_id, backend_id = "gateway_" + "a" * 32, str(uuid4())
    original = ExtensionTargetListDTO.model_validate({"protocol_version": 1, "gateway_id": gateway_id,
        "targets": [{"protocol_version": 1, "gateway_id": gateway_id, "backend_id": backend_id,
                     "capabilities": {"installation_manage": True, "host_manage": False}}]})
    wire = json_format.ParseDict(original.model_dump(mode="json"), public_pb2.ExtensionTargetListDTO())
    restored = ExtensionTargetListDTO.model_validate(json_format.MessageToDict(wire,
        preserving_proto_field_name=True, always_print_fields_with_no_presence=True))
    assert restored == original and "workspace_ids" not in restored.targets[0].model_dump()
    with pytest.raises(ValidationError):
        ExtensionHostTargetRequest.model_validate({"protocol_version": 1, "gateway_id": gateway_id, "workspace_ids": []})


def test_inventory_contract_follows_actual_read_only_route(gateway_openapi: JsonSchemaValue) -> None:
    operation = gateway_openapi["paths"][PATH]["post"]
    assert operation["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith("/ExtensionInstallationInventoryRequest")
    response = operation["responses"]["200"]["content"]["application/json"]["schema"]
    envelope = gateway_openapi["components"]["schemas"][response["$ref"].rsplit("/", 1)[1]]
    assert envelope["properties"]["data"]["anyOf"][0]["$ref"].endswith("/ExtensionInstallationInventoryDTO")
    install_path = "/api/gateway/extension-installations/install"
    uninstall_path = "/api/gateway/extension-installations/uninstall"
    assert {path for path in gateway_openapi["paths"] if path.startswith("/api/gateway/extension-installations")} == {
        PATH, install_path, uninstall_path, "/api/gateway/extension-installations/install-gallery",
        "/api/gateway/extension-installations/metadata"}
    install = gateway_openapi["paths"][install_path]["post"]
    assert install["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith("/ExtensionInstallationInstallRequest")
    uninstall = gateway_openapi["paths"][uninstall_path]["post"]
    assert uninstall["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith("/ExtensionInstallationUninstallRequest")
    assert set(gateway_openapi["paths"][PATH]) == {"post"}
    openapi.assert_gateway_http_schemas(gateway_openapi)
    for model in MODELS:
        assert_public_schema(model.model_json_schema(), public_pb2.DESCRIPTOR.message_types_by_name[model.__name__])


@pytest.mark.parametrize("invalid", ["profile_id", "extension_id", "version", "remove", "cleanUp"])
def test_profile_uninstall_requires_exact_selection_and_has_no_physical_delete_switch(invalid: str) -> None:
    payload = {"protocol_version": 1, "gateway_id": "gateway_" + "a" * 32,
        "backend_id": str(uuid4()),
        "profile_id": "default-profile", "extension_id": "publisher.probe", "version": "1.0.0"}
    request = ExtensionInstallationUninstallRequest.model_validate(payload)
    assert request.extension_id == "publisher.probe" and request.version == "1.0.0"
    if invalid in {"remove", "cleanUp"}:
        payload[invalid] = True
    else:
        payload.pop(invalid)
    with pytest.raises(ValidationError):
        ExtensionInstallationUninstallRequest.model_validate(payload)


def test_profile_uninstall_receipt_proto_preserves_selection_and_complete_inventory() -> None:
    original = ExtensionInstallationUninstallDTO(protocol_version=1, gateway_id="gateway_" + "a" * 32,
        backend_id=str(uuid4()),
        uninstalled=ExtensionUninstallResult(id="publisher.probe", version="1.0.0", profile_id="default-profile"),
        inventory=ProfileExtensionInventory(target_platform="linux-x64", profile_id="default-profile", extensions=[]))
    wire = json_format.ParseDict(original.model_dump(mode="json"), public_pb2.ExtensionInstallationUninstallDTO())
    restored = ExtensionInstallationUninstallDTO.model_validate(json_format.MessageToDict(wire,
        preserving_proto_field_name=True, always_print_fields_with_no_presence=True))
    assert restored == original


@pytest.mark.parametrize("source", ["foreign-gateway", "foreign-workspace", "directory", "native", "private-kind"])
def test_vsix_install_source_must_belong_to_explicit_target(source: str) -> None:
    gateway_id, workspace_id = "gateway_" + "a" * 32, str(uuid4())
    uri = f"boxteam://{gateway_id}/workspace/{workspace_id}/resources/files/root/probe.vsix"
    payload = {"protocol_version": 1, "gateway_id": gateway_id, "backend_id": str(uuid4()),
        "workspace_ids": [workspace_id], "source_uri": uri}
    valid = ExtensionInstallationInstallRequest.model_validate(payload)
    assert valid.source_uri == uri
    if source == "foreign-gateway":
        payload["source_uri"] = uri.replace(gateway_id, "gateway_" + "b" * 32)
    elif source == "foreign-workspace":
        payload["source_uri"] = uri.replace(workspace_id, str(uuid4()))
    elif source == "directory":
        payload["source_uri"] = uri.rsplit("/", 1)[0]
    elif source == "native":
        payload["source_uri"] = "/tmp/probe.vsix"
    else:
        payload["source_uri"] = f"boxteam://{gateway_id}/workspace/{workspace_id}/resources/config/workspace"
    with pytest.raises(ValidationError):
        ExtensionInstallationInstallRequest.model_validate(payload)


def test_inventory_proto_preserves_original_description_and_invalid_entry() -> None:
    request = ExtensionInstallationInventoryRequest(protocol_version=1, gateway_id="gateway_" + "a" * 32,
        backend_id=str(uuid4()))
    original = ExtensionInstallationInventoryDTO(**request.model_dump(exclude={"gateway_connection_id"}), inventory=ProfileExtensionInventory(target_platform="linux-x64",
        profile_id="default-profile", extensions=[InstalledExtension(source="profile", extension_key="publisher.probe-1.0.0", is_valid=False,
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


@pytest.mark.parametrize("reused", [False, True])
def test_install_result_preserves_explicit_reuse_disposition(reused: bool) -> None:
    original = VsixInstallResult(id="publisher.probe", version="1.0.0", profile_id="default-profile", reused=reused)
    wire = json_format.ParseDict(original.model_dump(mode="json"), public_pb2.VsixInstallResult())
    restored = VsixInstallResult.model_validate(json_format.MessageToDict(wire,
        preserving_proto_field_name=True, always_print_fields_with_no_presence=True))
    assert restored == original and restored.reused is reused
    with pytest.raises(ValidationError, match="reused"):
        VsixInstallResult.model_validate(original.model_dump(exclude={"reused"}))


@pytest.mark.parametrize("invalid", ["missing-backend", "null-backend", "view", "duplicate", "boolean-version"])
def test_inventory_request_requires_precise_backend_and_workspace_roles(invalid: str) -> None:
    payload = {"protocol_version": 1, "gateway_id": "gateway_" + "a" * 32, "backend_id": str(uuid4())}
    if invalid == "missing-backend":
        del payload["backend_id"]
    elif invalid == "null-backend":
        payload["backend_id"] = None
    elif invalid == "view":
        payload["view_id"] = "window-a"
    elif invalid == "duplicate":
        payload["workspace_ids"] = [str(uuid4())] * 2
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
