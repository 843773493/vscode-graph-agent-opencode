"""内部文件 DTO 在构造后损坏时，普通 HTTP 不能将其作为成功引用发布。"""

from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app.schemas.internal_v2.workspace import WorkspaceFileReferencesDTO
from tests.unit.api.test_workspace_file_response_validation import (
    BASE,
    NATIVE_PATHS,
    REQUEST_ID,
    ROOT,
)
from tests.unit.api.test_workspace_file_response_validation import (
    boundary_client as boundary_client,  # noqa: PLC0414 - 复用真实路由与Trace边界，隔离文件服务。
)


@pytest.mark.parametrize("native", NATIVE_PATHS)
def test_updated_file_reference_fails_before_success_response(
    boundary_client: tuple[TestClient, Mock, Mock], native: str, caplog: pytest.LogCaptureFixture,
) -> None:
    client, service, _watcher = boundary_client
    references = WorkspaceFileReferencesDTO(uris=[ROOT])
    references.uris.append(native)
    service.file_references.return_value = references
    response = client.post(BASE + "/references", json={"paths": ["entry.txt"]})
    assert response.status_code == 500, response.text
    assert response.json()["request_id"] == response.headers["X-Request-ID"] == REQUEST_ID
    assert "PydanticSerializationError" in response.json()["message"]
    assert native not in response.text and "file-validation-secret" not in response.text
    assert references.uris == [ROOT, native]
    assert any(record.exc_info and type(record.exc_info[1]).__name__ == "PydanticSerializationError"
               for record in caplog.records)
    service.file_references.return_value = WorkspaceFileReferencesDTO(uris=[ROOT])
    restored = client.post(BASE + "/references", json={"paths": ["entry.txt"]})
    assert restored.status_code == 200 and restored.json()["data"]["uris"] == [ROOT]
