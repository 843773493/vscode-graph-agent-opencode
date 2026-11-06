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
from app.schemas.gateway.extension_host import (
    ExtensionHostConnectionDTO,
    ExtensionHostEnvironmentDTO,
    ExtensionHostFolderDTO,
    ExtensionHostInitializationDTO,
    ExtensionHostWorkspaceDTO,
)


@pytest.fixture
def host_binding(tmp_path: Path) -> ExtensionHostConnectionDTO:
    gateway = "gateway_" + "a" * 32
    workspace_ids = [str(uuid4()), str(uuid4())]
    return ExtensionHostConnectionDTO(protocol_version=1, gateway_id=gateway, backend_id=str(uuid4()),
        workspace_ids=workspace_ids, view_id="explicit-view", client_id="authenticated-window", generation=str(uuid4()),
        request_id="original-request", initialization=ExtensionHostInitializationDTO(os="windows", parent_pid=123,
            environment=ExtensionHostEnvironmentDTO(app_root_uri="file:///C:/BoxTeam/runtime",
                global_storage_home_uri="file:///C:/BoxTeam/profile/globalStorage",
                workspace_storage_home_uri="file:///C:/BoxTeam/profile/workspaceStorage"),
            logs_location_uri=(tmp_path / "logs").as_uri(),
            workspace=ExtensionHostWorkspaceDTO(id=str(uuid4()), name="目标视图", folders=[
                ExtensionHostFolderDTO(workspace_id=workspace_id, name=f"工作区 {index}",
                    native_uri=f"file:///D:/project%20{index}", resource_uri=resource_display_uri(
                        scope="workspace", scope_id=workspace_id, kind="files", tail_segments=("root",),
                        gateway_authority=gateway)) for index, workspace_id in enumerate(workspace_ids)])))


def test_initialization_contract_and_wire_preserve_target_native_uris(host_binding: ExtensionHostConnectionDTO) -> None:
    for model in (ExtensionHostConnectionDTO, ExtensionHostInitializationDTO, ExtensionHostWorkspaceDTO,
                  ExtensionHostFolderDTO, ExtensionHostEnvironmentDTO):
        assert_public_schema(model.model_json_schema(), public_pb2.DESCRIPTOR.message_types_by_name[model.__name__])
    encoded = json_format.ParseDict(host_binding.model_dump(mode="json"), public_pb2.ExtensionHostConnectionDTO())
    restored = ExtensionHostConnectionDTO.model_validate(json_format.MessageToDict(encoded,
        preserving_proto_field_name=True, always_print_fields_with_no_presence=True))
    assert restored == host_binding
    assert restored.initialization.workspace.folders[0].native_uri == "file:///D:/project%200"
    assert restored.initialization.environment.app_root_uri == "file:///C:/BoxTeam/runtime"


@pytest.mark.parametrize("invalid", ["reordered", "missing", "foreign-workspace", "foreign-gateway", "child", "native-scheme"])
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
        folders[0]["resource_uri"] = folders[0]["resource_uri"].replace(host_binding.gateway_id, "gateway_" + "b" * 32)
    elif invalid == "child":
        folders[0]["resource_uri"] += "/nested"
    else:
        folders[0]["native_uri"] = folders[0]["resource_uri"]
    with pytest.raises(ValidationError):
        ExtensionHostConnectionDTO.model_validate(payload)
