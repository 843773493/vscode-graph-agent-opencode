"""宿主初始化的目标位置来自已认证绑定，file URI 只在目标机器解释。"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from google.protobuf import json_format
from pydantic import ValidationError

from app.core.resource_uri import resource_display_uri
from app.protocol.generated.boxteam.gateway.v2 import public_pb2
from app.protocol.schema import assert_public_schema
from app.schemas.gateway.extension_configuration import (
    ExtensionConfigurationContributionDTO,
    ExtensionConfigurationFileRequest,
    ExtensionConfigurationProfileFileRequest,
    ExtensionConfigurationProfileViewRequest,
    ExtensionConfigurationProfileWriteRequest,
    ExtensionConfigurationTargetRequest,
    ExtensionConfigurationViewDTO,
    ExtensionConfigurationViewRequest,
    ExtensionConfigurationWriteRequest,
    ExtensionWebHostContextDTO,
)
from app.schemas.gateway.extension_host import (
    ExtensionHostConfigurationResourceDTO,
    ExtensionHostConnectionDTO,
    ExtensionHostEnvironmentDTO,
    ExtensionHostFolderDTO,
    ExtensionHostInitializationDTO,
    ExtensionHostOpenRequest,
    ExtensionHostWorkspaceDTO,
)
from app.schemas.gateway.extension_installation import (
    InstalledExtension,
    ProfileExtensionInventory,
)
from app.schemas.gateway.extension_web import (
    ExtensionWebHostConnectionDTO,
)
from app.services.infrastructure.config.source_uri import extension_configuration_uri


@pytest.fixture
def host_binding(tmp_path: Path) -> ExtensionHostConnectionDTO:
    gateway = "gateway_" + "a" * 32
    workspace_ids = [str(uuid4()), str(uuid4())]
    backend_id, workspace_view_id, profile_id = str(uuid4()), str(uuid4()), "target-profile"
    return ExtensionHostConnectionDTO(protocol_version=1, gateway_id=gateway, backend_id=backend_id,
        workspace_ids=workspace_ids, view_id="explicit-view", client_id="authenticated-window", generation=str(uuid4()),
        request_id="original-request", initialization=ExtensionHostInitializationDTO(os="windows", parent_pid=123,
            environment=ExtensionHostEnvironmentDTO(app_root_uri="file:///C:/BoxTeam/runtime",
                global_storage_home_uri="file:///C:/BoxTeam/profile/globalStorage",
                workspace_storage_home_uri="file:///C:/BoxTeam/profile/workspaceStorage", profile_id=profile_id,
                remote_settings=ExtensionHostConfigurationResourceDTO(native_uri="file:///C:/BoxTeam/profile/settings.json",
                    resource_uri=extension_configuration_uri(gateway_id=gateway, backend_id=backend_id, profile_id=profile_id))),
            logs_location_uri=(tmp_path / "logs").as_uri(),
            workspace=ExtensionHostWorkspaceDTO(id=workspace_view_id, name="目标视图", folders=[
                ExtensionHostFolderDTO(workspace_id=workspace_id, name=f"工作区 {index}",
                    native_uri=f"file:///D:/project%20{index}", resource_uri=resource_display_uri(
                        scope="workspace", scope_id=workspace_id, kind="files", tail_segments=("root",)))
                for index, workspace_id in enumerate(workspace_ids)],
                configuration=ExtensionHostConfigurationResourceDTO(
                    native_uri="file:///C:/BoxTeam/views/target.code-workspace",
                    resource_uri=extension_configuration_uri(gateway_id=gateway, backend_id=backend_id, workspace_id=workspace_view_id)))))


def test_initialization_contract_and_wire_preserve_target_native_uris(host_binding: ExtensionHostConnectionDTO) -> None:
    for model in (ExtensionHostConnectionDTO, ExtensionHostInitializationDTO, ExtensionHostWorkspaceDTO,
                  ExtensionHostFolderDTO, ExtensionHostEnvironmentDTO, ExtensionHostConfigurationResourceDTO):
        assert_public_schema(model.model_json_schema(), public_pb2.DESCRIPTOR.message_types_by_name[model.__name__])
    encoded = json_format.ParseDict(host_binding.model_dump(mode="json"), public_pb2.ExtensionHostConnectionDTO())
    restored = ExtensionHostConnectionDTO.model_validate(json_format.MessageToDict(encoded,
        preserving_proto_field_name=True, always_print_fields_with_no_presence=True))
    assert restored == host_binding
    assert restored.initialization.workspace.folders[0].native_uri == "file:///D:/project%200"
    assert restored.initialization.environment.app_root_uri == "file:///C:/BoxTeam/runtime"


@pytest.mark.parametrize("empty", [False, True])
def test_web_context_contract_preserves_target_view_without_process_initialization(host_binding: ExtensionHostConnectionDTO, empty: bool) -> None:
    workspace = host_binding.initialization.workspace
    if empty:
        workspace = workspace.model_copy(update={"folders": [], "configuration": None})
    binding = ExtensionWebHostConnectionDTO(**host_binding.model_dump(exclude={"initialization", "workspace_ids"}),
        workspace_ids=[] if empty else host_binding.workspace_ids, profile_id="target-profile", extension_keys=["publisher.probe-1.0.0"],
        inventory=ProfileExtensionInventory(target_platform="linux-x64", profile_id="target-profile", extensions=[InstalledExtension(
            extension_key="publisher.probe-1.0.0", source="profile", description={}, is_valid=True, validations=[])]),
        context=ExtensionWebHostContextDTO(os="windows", remote_settings=host_binding.initialization.environment.remote_settings,
            workspace=workspace))
    for model in (ExtensionWebHostConnectionDTO, ExtensionWebHostContextDTO):
        assert_public_schema(model.model_json_schema(), public_pb2.DESCRIPTOR.message_types_by_name[model.__name__])
    encoded = json_format.ParseDict(binding.model_dump(mode="json"), public_pb2.ExtensionWebHostConnectionDTO())
    restored = ExtensionWebHostConnectionDTO.model_validate(json_format.MessageToDict(encoded,
        preserving_proto_field_name=True, always_print_fields_with_no_presence=True))
    assert restored == binding
    assert set(restored.context.model_dump()) == {"os", "remote_settings", "workspace"}
    assert restored.context.remote_settings.native_uri == "file:///C:/BoxTeam/profile/settings.json"
    if empty:
        with pytest.raises(ValidationError):
            ExtensionHostOpenRequest.model_validate(host_binding.model_dump(exclude={"initialization", "client_id", "generation", "request_id"})
                | {"workspace_ids": []})


@pytest.mark.parametrize("wrong", ["profile", "backend", "gateway", "view"])
def test_web_context_rejects_configuration_owner_mismatch(host_binding: ExtensionHostConnectionDTO, wrong: str) -> None:
    settings = host_binding.initialization.environment.remote_settings.model_dump()
    settings["resource_uri"] = extension_configuration_uri(gateway_id="gateway_" + "b" * 32 if wrong == "gateway" else host_binding.gateway_id,
        backend_id=str(uuid4()) if wrong == "backend" else host_binding.backend_id,
        profile_id="foreign-profile" if wrong == "profile" else "target-profile")
    workspace = host_binding.initialization.workspace.model_dump()
    if wrong == "view":
        workspace["id"] = str(uuid4())
    with pytest.raises(ValidationError):
        ExtensionWebHostConnectionDTO.model_validate({**host_binding.model_dump(exclude={"initialization"}),
            "profile_id": "target-profile", "extension_keys": ["publisher.probe-1.0.0"],
            "inventory": {"profile_id": "target-profile", "target_platform": "linux-x64", "extensions": [{"extension_key": "publisher.probe-1.0.0",
                "source": "profile", "description": {}, "is_valid": True, "validations": []}]},
            "context": {"os": "windows", "remote_settings": settings, "workspace": workspace}})


@pytest.mark.parametrize("invalid", ["reordered", "missing", "foreign-workspace", "foreign-gateway", "child", "native-scheme",
    "foreign-profile", "foreign-config-backend", "foreign-config-gateway", "foreign-config-view", "missing-multi-config", "unexpected-single-config"])
def test_initialization_rejects_view_and_locator_mismatch(host_binding: ExtensionHostConnectionDTO, invalid: str) -> None:
    payload = host_binding.model_dump()
    folders = payload["initialization"]["workspace"]["folders"]
    if invalid == "reordered":
        folders.reverse()
    elif invalid == "missing":
        del payload["initialization"]
    elif invalid == "foreign-workspace":
        folders[0]["resource_uri"] = folders[1]["resource_uri"]
    elif invalid == "foreign-gateway":
        folders[0]["resource_uri"] = resource_display_uri(scope="workspace", scope_id=host_binding.workspace_ids[0],
            kind="files", tail_segments=("root",), gateway_authority="gateway_" + "b" * 32)
    elif invalid == "child":
        folders[0]["resource_uri"] += "/nested"
    elif invalid == "native-scheme":
        folders[0]["native_uri"] = folders[0]["resource_uri"]
    elif invalid == "foreign-profile":
        payload["initialization"]["environment"]["profile_id"] = "another-profile"
    elif invalid == "foreign-config-backend":
        payload["initialization"]["environment"]["remote_settings"]["resource_uri"] = extension_configuration_uri(
            gateway_id=host_binding.gateway_id, backend_id=str(uuid4()), profile_id="target-profile")
    elif invalid == "foreign-config-gateway":
        payload["initialization"]["environment"]["remote_settings"]["resource_uri"] = extension_configuration_uri(
            gateway_id="gateway_" + "b" * 32, backend_id=host_binding.backend_id, profile_id="target-profile")
    elif invalid == "foreign-config-view":
        payload["initialization"]["workspace"]["id"] = str(uuid4())
    elif invalid == "missing-multi-config":
        payload["initialization"]["workspace"]["configuration"] = None
    elif invalid == "unexpected-single-config":
        payload["workspace_ids"] = payload["workspace_ids"][:1]
        payload["initialization"]["workspace"]["folders"] = folders[:1]
    with pytest.raises(ValidationError):
        ExtensionHostConnectionDTO.model_validate(payload)


@pytest.mark.parametrize("host_bound", [False, True])
def test_configuration_proto_preserves_distinct_settings_and_host_ownership(host_binding: ExtensionHostConnectionDTO, host_bound: bool) -> None:
    for model in (ExtensionConfigurationTargetRequest, ExtensionConfigurationContributionDTO, ExtensionConfigurationViewDTO, ExtensionConfigurationViewRequest,
                  ExtensionConfigurationFileRequest,
    ExtensionConfigurationProfileFileRequest,
    ExtensionConfigurationProfileViewRequest,
    ExtensionConfigurationProfileWriteRequest, ExtensionConfigurationWriteRequest):
        assert_public_schema(model.model_json_schema(), public_pb2.DESCRIPTOR.message_types_by_name[model.__name__])
    target = host_binding.model_dump(include={"protocol_version", "gateway_id", "backend_id", "workspace_ids", "view_id"})
    context = ExtensionWebHostContextDTO(os=host_binding.initialization.os,
        workspace=host_binding.initialization.workspace, remote_settings=host_binding.initialization.environment.remote_settings)
    extension = ExtensionConfigurationContributionDTO(extension_id="fixture.settings", name="settings", publisher="fixture",
        configuration={"properties": {"fixture.value": {"type": "string", "default": "value"}}},
        configuration_defaults={"[typescript]": {"editor.tabSize": 3}},
        capabilities={"untrustedWorkspaces": {"supported": "limited", "restrictedConfigurations": ["fixture.value"]}},
        enabled_api_proposals=[])
    view = ExtensionConfigurationViewDTO(**target, profile_id=host_binding.initialization.environment.profile_id, context=context, extensions=[extension])
    binding = {"generation": host_binding.generation} if host_bound else {"profile_id": view.profile_id}
    request_type = ExtensionConfigurationFileRequest if host_bound else ExtensionConfigurationProfileFileRequest
    request = request_type.model_validate({**target, **binding, "resource_uri": context.remote_settings.resource_uri})
    for original in (view, request):
        message = getattr(public_pb2, type(original).__name__)()
        json_format.ParseDict(original.model_dump(mode="json", exclude_none=True), message)
        restored = type(original).model_validate(json_format.MessageToDict(message, preserving_proto_field_name=True,
            always_print_fields_with_no_presence=True))
        assert restored == original
    encoded = getattr(public_pb2, request_type.__name__)()
    json_format.ParseDict(request.model_dump(mode="json", exclude_none=True), encoded)
    assert set(encoded.DESCRIPTOR.fields_by_name) & {"generation", "profile_id"} == ({"generation"} if host_bound else {"profile_id"})
    for invalid in ({}, {"generation": "host", "profile_id": view.profile_id}):
        with pytest.raises(ValidationError):
            (ExtensionConfigurationViewRequest if host_bound else ExtensionConfigurationProfileViewRequest).model_validate({**target, **invalid})
