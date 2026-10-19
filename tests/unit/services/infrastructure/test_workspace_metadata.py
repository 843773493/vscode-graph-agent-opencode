"""工作区元数据只返回业务身份；位置只能留在本地 owner。"""

from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import ValidationError
from pydantic_core import PydanticSerializationError

from app.schemas.internal_v2.workspace import WorkspaceContextDTO, WorkspaceDTO
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.workspace_service import WorkspaceService


@pytest.fixture
def metadata_service(tmp_path: Path) -> WorkspaceService:
    root = tmp_path / "metadata-workspace"
    root.mkdir()
    return WorkspaceService(config_service=Mock(spec=ConfigService), workspace_root=root)


@pytest.mark.asyncio
async def test_workspace_owner_publishes_identity_without_duplicate_locations(metadata_service: WorkspaceService) -> None:
    workspace = (await metadata_service.get()).model_dump(mode="json")
    context = (await metadata_service.get_context()).model_dump(mode="json")
    assert workspace["workspace_id"] == context["workspace_id"] == metadata_service.workspace_id
    assert workspace["name"] == "metadata-workspace"
    assert "root_path" not in workspace and "root_path" not in context
    assert "root" not in workspace["git"]
    assert metadata_service.root_path not in str(workspace) + str(context)


@pytest.mark.parametrize("model", [WorkspaceDTO, WorkspaceContextDTO])
def test_removed_root_field_is_rejected_instead_of_silently_dropped(model: type[WorkspaceDTO] | type[WorkspaceContextDTO]) -> None:
    payload = {"workspace_id": "workspace-unit", "root_path": "/private/workspace"}
    if model is WorkspaceDTO:
        payload["name"] = "项目"
    with pytest.raises(ValidationError, match="root_path"):
        model.model_validate(payload)


@pytest.mark.parametrize("path", ["/private/workspace", r"C:\Users\owner\workspace", r"\\host\share\workspace", r"\rooted\workspace"])
@pytest.mark.parametrize("model", [WorkspaceDTO, WorkspaceContextDTO])
def test_serialization_rejects_nested_real_paths_after_mutation(path: str, model: type[WorkspaceDTO] | type[WorkspaceContextDTO]) -> None:
    metadata = model.model_validate({"workspace_id": "workspace-unit", **({"name": "项目"} if model is WorkspaceDTO else {})})
    metadata.git["nested"] = {"locations": [path]}
    with pytest.raises(PydanticSerializationError, match=r"git.nested.locations\[0\]"):
        metadata.model_dump_json()


@pytest.mark.parametrize("field", ["root", "root_path", "source_path", "directory", "working_directory", "cwd"])
def test_serialization_rejects_structured_location_fields(field: str) -> None:
    metadata = WorkspaceDTO(workspace_id="workspace-unit", name="项目", runtime={field: "/private/workspace"})
    with pytest.raises(PydanticSerializationError, match=rf"runtime\.{field}"):
        metadata.model_dump(mode="json")


@pytest.mark.parametrize("model", [WorkspaceDTO, WorkspaceContextDTO])
def test_metadata_serialization_keeps_business_text_and_resource_uris(model: type[WorkspaceDTO] | type[WorkspaceContextDTO]) -> None:
    text = {"branch": "feature/metadata", "content": "/tmp/user-example", "command": ["/usr/bin/python", "/tmp/input"],
        "message": r"C:\Users\owner\example", "source": "boxteam://workspace/workspace-unit/resources/files/root",
        "endpoint": "https://example.test/api", "changed_paths": ["/agent/provider"],
        "applied_paths": ["/ui/theme"], "deferred_paths": ["/backend/port"]}
    metadata = model.model_validate({"workspace_id": "workspace-unit", "git": text,
        **({"name": "/tmp/user-supplied-name"} if model is WorkspaceDTO else {})})
    payload = metadata.model_dump(mode="json")
    assert payload["git"] == text
    if model is WorkspaceDTO:
        assert payload["name"] == "/tmp/user-supplied-name"
