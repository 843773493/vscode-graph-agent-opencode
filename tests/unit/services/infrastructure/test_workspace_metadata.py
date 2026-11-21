"""工作区身份与数据展示位置由原 owner 提供，展示位置不充当资源引用。"""

from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from app.schemas.internal_v2.workspace import WorkspaceContextDTO, WorkspaceDTO
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.workspace_service import WorkspaceService


@pytest.fixture
def metadata_service(empty_workspace_scope: tuple[str, Path]) -> WorkspaceService:
    _, workspace_root = empty_workspace_scope
    return WorkspaceService(
        config_service=Mock(spec=ConfigService), workspace_root=workspace_root,
    )


@pytest.mark.asyncio
async def test_workspace_owner_publishes_identity_and_its_data_display_location(metadata_service: WorkspaceService) -> None:
    workspace = (await metadata_service.get()).model_dump(mode="json")
    context = (await metadata_service.get_context()).model_dump(mode="json")
    assert workspace["workspace_id"] == context["workspace_id"] == metadata_service.workspace_id
    assert workspace["name"] == Path(metadata_service.root_path).name
    assert "root_path" not in workspace and "root_path" not in context
    assert "root" not in workspace["git"]
    assert workspace.pop("data_directory") == str(Path(metadata_service.root_path) / ".boxteam")
    assert metadata_service.root_path not in str(workspace) + str(context)


@pytest.mark.parametrize("model", [WorkspaceDTO, WorkspaceContextDTO])
def test_removed_root_field_is_rejected_instead_of_silently_dropped(model: type[WorkspaceDTO] | type[WorkspaceContextDTO]) -> None:
    payload = {"workspace_id": "workspace-unit", "root_path": "/private/workspace"}
    if model is WorkspaceDTO:
        payload["name"] = "项目"
    with pytest.raises(ValidationError, match="root_path"):
        model.model_validate(payload)


@pytest.mark.parametrize("model", [WorkspaceDTO, WorkspaceContextDTO])
def test_metadata_serialization_keeps_business_text_and_resource_uris(model: type[WorkspaceDTO] | type[WorkspaceContextDTO]) -> None:
    text = {"branch": "feature/metadata", "content": "/tmp/user-example", "command": ["/usr/bin/python", "/tmp/input"],
        "log_path": "/tmp/build.log", "cwd": r"C:\Users\owner\workspace",
        "message": r"C:\Users\owner\example", "source": "boxteam://workspace/workspace-unit/resources/files/root",
        "endpoint": "https://example.test/api", "changed_paths": ["/agent/provider"],
        "applied_paths": ["/ui/theme"], "deferred_paths": ["/backend/port"]}
    metadata = model.model_validate({"workspace_id": "workspace-unit", "git": text,
        **({"name": "/tmp/user-supplied-name"} if model is WorkspaceDTO else {})})
    payload = metadata.model_dump(mode="json")
    assert payload["git"] == text
    if model is WorkspaceDTO:
        assert payload["name"] == "/tmp/user-supplied-name"
