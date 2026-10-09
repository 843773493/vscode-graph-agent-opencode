"""冻结工作区文件写操作四个入口的统一错误映射契约。

create/paste/copy/upload 曾各自逐字重复同一套 except 块，现收敛为唯一
``_file_operation_error``。这里锁住映射结果不变，防止收敛时改动状态码。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api import workspace as workspace_api
from app.api.workspace import _file_operation_error
from app.schemas.internal_v2.workspace import (
    WorkspaceFileCopyRequest,
    WorkspaceFileCreateRequest,
    WorkspaceFilePasteRequest,
)
from app.schemas.internal_v2.workspace_file_operations import (
    WorkspaceFileMutationIntentDTO,
)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (FileNotFoundError("x"), 404),
        (FileExistsError("x"), 409),
        (PermissionError("x"), 403),
        (NotADirectoryError("x"), 400),
        (ValueError("x"), 400),
        (OSError("x"), 500),
    ],
)
def test_shared_file_operation_error_mapping(error: Exception, expected: int) -> None:
    assert _file_operation_error(error).status_code == expected


class _FailingWorkspaceService:
    workspace_id = "workspace_error_mapping"
    def __init__(self, error: Exception) -> None:
        self._error = error

    async def create_file_entry(self, **kwargs):
        raise self._error

    async def paste_file_entries(self, **kwargs):
        raise self._error

    async def copy_file_entry(self, **kwargs):
        raise self._error


@pytest.mark.asyncio
async def test_create_paste_copy_share_same_mapping(file_intent: WorkspaceFileMutationIntentDTO) -> None:
    service = _FailingWorkspaceService(FileExistsError("已存在"))

    with pytest.raises(HTTPException) as created:
        await workspace_api.create_workspace_file_entry(
            payload=WorkspaceFileCreateRequest(name="a", kind="file", intent=file_intent),
            path="",
            scope="workspace",
            _="local",
            request_id="req",
            workspace_service=service,
        )
    with pytest.raises(HTTPException) as pasted:
        await workspace_api.paste_workspace_file_entries(
            payload=WorkspaceFilePasteRequest(source_paths=["a"], intent=file_intent),
            path="",
            scope="workspace",
            _="local",
            request_id="req",
            workspace_service=service,
        )
    with pytest.raises(HTTPException) as copied:
        await workspace_api.copy_workspace_file_entry(
            payload=WorkspaceFileCopyRequest(source_path="a", intent=file_intent),
            path="",
            scope="workspace",
            _="local",
            request_id="req",
            workspace_service=service,
        )

    assert created.value.status_code == 409
    assert pasted.value.status_code == 409
    assert copied.value.status_code == 409


@pytest.fixture
def file_intent() -> WorkspaceFileMutationIntentDTO:
    return WorkspaceFileMutationIntentDTO(
        client_operation_id="op_00000000000040008000000000000001", client_sequence=1,
        expected_directory_revision=1,
        directory_key={"workspace_id": _FailingWorkspaceService.workspace_id,
                       "scope": "workspace", "root": "/error-mapping", "path": ""},
    )
