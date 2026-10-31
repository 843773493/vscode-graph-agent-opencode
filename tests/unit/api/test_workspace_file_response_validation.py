"""文件内部校验不回显原输入；multipart意图拒绝仍是写入前的400。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api import workspace
from app.api.deps import (
    get_workspace_file_watch_service,
    get_workspace_service,
    verify_local_token,
)
from app.core import failures
from app.core.trace_middleware import TraceMiddleware
from app.schemas.internal_v2.workspace import WorkspaceFileReferencesDTO
from app.schemas.internal_v2.workspace_file_operations import (
    WorkspaceFileOperationReceiptDTO,
)
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.workspace_file_watch_service import (
    WorkspaceFileWatchService,
)
from app.services.infrastructure.workspace_service import WorkspaceService

WORKSPACE_ID = "workspace-file-validation"
ROOT = f"boxteam://workspace/{WORKSPACE_ID}/resources/files/root"
BASE = "/api/v1/workspace/files"
REQUEST_ID = "req-file-validation"
OPERATION_ID = "op_" + "1" * 32
INTENT = {
    "client_operation_id": OPERATION_ID, "client_sequence": 1, "expected_directory_revision": 1,
    "directory_key": {"workspace_id": WORKSPACE_ID, "scope": "workspace", "root": ROOT, "path": ROOT},
}
NATIVE_PATHS = ["/private/file-validation-secret", r"C:\x\file-validation-secret", r"\\target\x\file-validation-secret"]
ENDPOINTS = [
    ("POST", "/references", {"paths": ["entry.txt"]}, "file_references"),
    ("POST", "/events", {"paths": []}, "resolve_watch_roots"),
    ("GET", "", None, "list_files"),
    ("GET", "/content", None, "get_file_content"),
    ("PUT", "/content", {"content": "new", "expected_revision": "1" * 64}, "update_file_content"),
    ("GET", "/raw", None, "prepare_file_download"),
    ("GET", "/download", None, "prepare_file_download"),
    ("POST", "/entries", {"intent": INTENT, "name": "entry.txt", "kind": "file"}, "create_file_entry"),
    ("POST", "/paste", {"intent": INTENT, "source_paths": ["source.txt"]}, "paste_file_entries"),
    ("POST", "/copy", {"intent": INTENT, "source_path": "source.txt"}, "copy_file_entry"),
    ("POST", "/upload", None, "upload_file_entries"),
    ("GET", "/operations", None, "status"),
    ("POST", "/reveal", None, "reveal_file_entry"),
]


@pytest.fixture
def boundary_client() -> Iterator[tuple[TestClient, Mock, Mock]]:
    from app.main import app as workspace_app

    service = Mock(spec=WorkspaceService, workspace_id=WORKSPACE_ID)
    service.file_paths = Mock(workspace=Path("/explicit-unit-placeholder"), workspace_uri=ROOT)
    watcher = Mock(spec=WorkspaceFileWatchService)
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.include_router(workspace.router, prefix="/api/v1")
    for error_type in (HTTPException, RequestValidationError):
        application.add_exception_handler(error_type, workspace_app.exception_handlers[error_type])
    application.dependency_overrides[verify_local_token] = lambda: "local"
    application.dependency_overrides[get_workspace_service] = lambda: service
    application.dependency_overrides[get_workspace_file_watch_service] = lambda: watcher
    with TestClient(application, raise_server_exceptions=False, headers={"X-Request-ID": REQUEST_ID}) as client:
        yield client, service, watcher


def _request(client: TestClient, method: str, suffix: str, payload: dict[str, object] | None):
    if suffix == "/upload":
        return _upload(client, json.dumps(INTENT))
    params = {"path": "entry.txt"}
    if suffix == "/operations":
        params = {"operation_id": OPERATION_ID, "expected_workspace_id": WORKSPACE_ID}
    return client.request(method, BASE + suffix, params=params, json=payload)


def _upload(client: TestClient, intent: str):
    return client.post(BASE + "/upload", data={"intent": intent, "relative_paths": "entry.txt"},
                       files={"files": ("entry.txt", b"original content", "text/plain")})


def _operation(service: Mock, watcher: Mock, operation: str) -> Mock:
    if operation == "resolve_watch_roots":
        return watcher.resolve_watch_roots
    if operation == "status":
        return service.file_operations.status
    return getattr(service, operation)


@pytest.mark.parametrize(("method", "suffix", "payload", "operation"), ENDPOINTS)
@pytest.mark.parametrize("native", NATIVE_PATHS)
def test_internal_file_validation_keeps_original_diagnostic(
    boundary_client: tuple[TestClient, Mock, Mock], method: str, suffix: str,
    payload: dict[str, object] | None, operation: str, native: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, service, watcher = boundary_client
    with pytest.raises(ValidationError) as captured:
        WorkspaceFileReferencesDTO.model_validate({"uris": [{native: native}]})
    failure = captured.value
    cause = RuntimeError("内部来源 " + native)
    failure.__cause__ = cause
    target = _operation(service, watcher, operation)
    target.side_effect = failure
    response = _request(client, method, suffix, payload)
    assert response.status_code == 500, response.text
    assert response.json() == {
        "code": 500, "message": "请求处理失败: stage=http_request, error=ValidationError",
        "data": None, "request_id": REQUEST_ID,
    }
    assert response.headers["X-Request-ID"] == REQUEST_ID
    assert "file-validation-secret" not in response.text
    assert failure.__cause__ is cause
    diagnostics = [record for record in caplog.records if record.exc_info and record.exc_info[1] is failure]
    assert len(diagnostics) == 1 and "file-validation-secret" in caplog.text
    target.assert_called_once()


@pytest.mark.parametrize(("method", "suffix", "payload", "operation"), ENDPOINTS)
def test_file_business_input_rejection_keeps_400(
    boundary_client: tuple[TestClient, Mock, Mock], method: str, suffix: str,
    payload: dict[str, object] | None, operation: str,
) -> None:
    client, service, watcher = boundary_client
    target = _operation(service, watcher, operation)
    target.side_effect = ValueError("原文件参数不可用")
    response = _request(client, method, suffix, payload)
    assert response.status_code == 400, response.text
    assert response.json() == {"detail": "原文件参数不可用", "request_id": REQUEST_ID}
    assert response.headers["X-Request-ID"] == REQUEST_ID
    target.assert_called_once()


@pytest.mark.parametrize("shape", ["json", "sequence", "directory", "unknown-field"])
@pytest.mark.parametrize("native", NATIVE_PATHS)
def test_multipart_input_validation_stays_400_before_service_and_can_retry(
    boundary_client: tuple[TestClient, Mock, Mock], shape: str, native: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, service, _ = boundary_client
    invalid = deepcopy(INTENT)
    if shape == "sequence":
        invalid["client_sequence"] = {native: native}
    elif shape == "directory":
        invalid["directory_key"]["path"] = native
    elif shape == "unknown-field":
        invalid[native] = native
    raw = "{\"" + native if shape == "json" else json.dumps(invalid)
    response = _upload(client, raw)
    assert response.status_code == 400, response.text
    assert "stage=file-upload-intent, error=ValidationError" in response.json()["detail"]
    assert "file-validation-secret" not in response.text and "input_value" not in response.text
    assert response.json()["request_id"] == response.headers["X-Request-ID"] == REQUEST_ID
    service.upload_file_entries.assert_not_called()
    diagnostics = [record for record in caplog.records if record.exc_info and isinstance(record.exc_info[1], ValidationError)]
    assert len(diagnostics) == 1
    original = diagnostics[0].exc_info
    assert original is not None and isinstance(original[1], ValidationError)
    assert "file-validation-secret" in repr(original[1].errors())
    service.upload_file_entries.return_value = WorkspaceFileOperationReceiptDTO(
        operation_id=OPERATION_ID, client_sequence=1, queue_seq=1, kind="upload",
        state="queued", receipt_revision=1, effect="none",
    )
    healthy = _upload(client, json.dumps(INTENT))
    assert healthy.status_code == 202, healthy.text
    assert healthy.json()["data"]["operation_id"] == OPERATION_ID
    assert healthy.json()["request_id"] == healthy.headers["X-Request-ID"] == REQUEST_ID
    service.upload_file_entries.assert_called_once()
    assert service.upload_file_entries.call_args.kwargs["intent"].model_dump() == {
        **INTENT, "depends_on": [], "created_by_operation_id": None, "relative_child_path": "",
        "directory_revision_from_operation_id": None, "client_ref": None,
    }


def test_multipart_input_log_failure_does_not_replace_original_400(
    boundary_client: tuple[TestClient, Mock, Mock], monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, service, _ = boundary_client
    original_errors: list[ValidationError] = []
    sink_error = OSError(5, "日志不可写", "/private/file-validation-secret/service.log")

    def fail_log(*args: object, **kwargs: object) -> None:
        error = kwargs["exc_info"]
        assert isinstance(error, ValidationError)
        original_errors.append(error)
        raise sink_error

    monkeypatch.setattr(failures.logger, "error", fail_log)
    response = _upload(client, json.dumps({**INTENT, NATIVE_PATHS[0]: "invalid"}))
    assert response.status_code == 400, response.text
    assert "diagnostic_sink_error=OSError" in response.json()["detail"]
    assert "file-validation-secret" not in response.text
    service.upload_file_entries.assert_not_called()
    assert len(original_errors) == 1
    assert str(sink_error) in "\n".join(original_errors[0].__notes__)


@pytest.mark.parametrize("native", NATIVE_PATHS)
def test_real_file_response_validation_can_retry_without_modifying_source(
    boundary_client: tuple[TestClient, Mock, Mock], empty_workspace_scope: tuple[str, Path],
    monkeypatch: pytest.MonkeyPatch, native: str, caplog: pytest.LogCaptureFixture,
) -> None:
    client, _, _ = boundary_client
    workspace_id, root = empty_workspace_scope
    source = root / "entry.txt"
    content = b"original content\n"
    source.write_bytes(content)
    config = Mock(spec=ConfigService)
    config.get_workspace_preview_max_bytes.return_value = 1024
    config.get_workspace_preview_binary_sample_bytes.return_value = 1024
    service = WorkspaceService(config_service=config, workspace_root=root)
    language = Mock(return_value={native: native})
    monkeypatch.setattr(service, "_guess_language", language)
    assert isinstance(client.app, FastAPI)
    client.app.dependency_overrides[get_workspace_service] = lambda: service
    uri = f"boxteam://workspace/{workspace_id}/resources/files/root/entry.txt"
    response = client.get(BASE + "/content", params={"path": uri})
    assert response.status_code == 500, response.text
    assert response.json()["message"] == "请求处理失败: stage=http_request, error=ValidationError"
    assert response.json()["request_id"] == response.headers["X-Request-ID"] == REQUEST_ID
    assert "file-validation-secret" not in response.text and source.read_bytes() == content
    diagnostics = [record for record in caplog.records if record.exc_info and isinstance(record.exc_info[1], ValidationError)]
    assert len(diagnostics) == 1 and "file-validation-secret" in caplog.text
    language.return_value = "plaintext"
    healthy = client.get(BASE + "/content", params={"path": uri})
    assert healthy.status_code == 200, healthy.text
    assert healthy.json()["data"]["path"] == uri
    assert healthy.json()["data"]["content"] == content.decode()
    assert healthy.json()["data"]["revision"] == hashlib.sha256(content).hexdigest()
    assert source.read_bytes() == content
    service.close()
