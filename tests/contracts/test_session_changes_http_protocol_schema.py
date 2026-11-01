"""会话变更列表、详情和审查回执复用 proto 的公开结构核对。"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from pathlib import Path

import pytest
from google.protobuf.descriptor_pb2 import FieldDescriptorProto, FileDescriptorSet
from pydantic import ValidationError
from pydantic.json_schema import JsonSchemaValue

from app.protocol.openapi import HTTP_JSON_DESCRIPTORS, assert_public_http_schemas
from app.protocol.schema import descriptor_pool_from_set
from app.schemas.internal_v2.session_changes import (
    SessionFileChangeDTO,
    SessionFileReviewRequest,
    SessionFileReviewResultDTO,
)
from tests.contracts.api.openapi_snapshots import OPENAPI_SNAPSHOTS


@pytest.mark.parametrize(("suffix", "method", "request_model", "response_model"), [
    ("", "get", None, "SessionChangesetListDTO"),
    ("/{changeset_id}", "get", None, "SessionChangesetDTO"),
    ("/{changeset_id}/review", "post", "SessionFileReviewRequest", "SessionFileReviewResultDTO"),
])
def test_changes_http_roots_follow_actual_routes(
    http_openapi: JsonSchemaValue, suffix: str, method: str, request_model: str | None, response_model: str,
) -> None:
    original = deepcopy(http_openapi)
    prefix = "/api/v1/workspaces/{workspace_id}/sessions/{session_id}/changesets"
    operation = http_openapi["paths"][prefix + suffix][method]
    if request_model is not None:
        request = operation["requestBody"]["content"]["application/json"]["schema"]
        assert request["$ref"].endswith("/" + request_model)
        assert request_model in HTTP_JSON_DESCRIPTORS
    response = operation["responses"]["200"]["content"]["application/json"]["schema"]
    schemas = http_openapi["components"]["schemas"]
    envelope = schemas[response["$ref"].rsplit("/", 1)[1]]
    assert envelope["properties"]["data"]["anyOf"][0]["$ref"].endswith("/" + response_model)
    assert response_model in HTTP_JSON_DESCRIPTORS
    assert_public_http_schemas(http_openapi)
    assert http_openapi == original


@pytest.mark.parametrize(("model", "field", "shape"), [
    ("SessionChangesetListDTO", "items", {"type": "object"}),
    ("SessionChangesetListItemDTO", "change_kind", {"type": "integer"}),
    ("SessionChangesSummaryDTO", "additions", {"type": "string"}),
    ("SessionChangesetDTO", "generated_at", {"type": "integer"}),
    ("SessionFileChangeDTO", "file_path", {"type": "integer"}),
    ("SessionFileChangeDTO", "tool_call_ids", {"type": "array", "items": {"type": "integer"}}),
    ("SessionFileChangeDTO", "diff_text", {"type": "object"}),
    ("SessionFileReviewRequest", "reviewed", {"type": "string"}),
    ("SessionFileReviewResultDTO", "file_path", {"type": "integer"}),
])
def test_changes_http_recursively_rejects_request_and_response_drift(
    http_openapi: JsonSchemaValue, model: str, field: str, shape: JsonSchemaValue,
) -> None:
    http_openapi["components"]["schemas"][model]["properties"][field] = shape
    with pytest.raises(ValueError, match="公开结构与 proto 类型不一致") as captured:
        assert_public_http_schemas(http_openapi)
    assert "/" + field in str(captured.value)


@pytest.mark.parametrize("model", [
    "SessionChangesetListDTO", "SessionChangesetListItemDTO", "SessionChangesSummaryDTO",
    "SessionChangesetDTO", "SessionFileChangeDTO", "SessionFileReviewRequest", "SessionFileReviewResultDTO",
])
def test_changes_http_checks_candidate_descriptor_for_all_members(
    http_openapi: JsonSchemaValue, http_descriptor_bundle: FileDescriptorSet, model: str,
) -> None:
    message = next(message for file in http_descriptor_bundle.file for message in file.message_type if message.name == model)
    message.field.add(name="new_public_field", number=100, type=FieldDescriptorProto.TYPE_STRING)
    pool = descriptor_pool_from_set(http_descriptor_bundle.SerializeToString())
    with pytest.raises(ValueError, match="公开结构与 proto 字段不一致") as captured:
        assert_public_http_schemas(http_openapi, pool)
    assert "new_public_field" in str(captured.value)


def test_changes_structure_checks_preserve_uri_and_business_validation(http_openapi: JsonSchemaValue) -> None:
    original = deepcopy(http_openapi)
    assert_public_http_schemas(http_openapi)
    assert http_openapi == original
    uri = "boxteam://workspace/ws-changes/resources/files/root/src/example.py"
    value = {"file_path": uri, "kind": "edit", "latest_edit_id": "edit_1",
             "diff_text": "-/native/body\n+/native/changed\n", "before_preview": "/native/body"}
    change = SessionFileChangeDTO.model_validate(value)
    assert change.file_path == uri and change.diff_text == value["diff_text"]
    assert change.before_preview == "/native/body" and change.reviewed is False
    assert SessionFileReviewRequest(file_path=uri, reviewed=True).reviewed is True
    result = {"session_id": "session", "file_path": uri, "reviewed": False}
    assert SessionFileReviewResultDTO.model_validate(result).reviewed is False
    for invalid in ("/etc/hosts", r"C:\Windows\System32\drivers\etc\hosts"):
        with pytest.raises(ValidationError):
            SessionFileChangeDTO.model_validate({**value, "file_path": invalid})
        with pytest.raises(ValidationError):
            SessionFileReviewResultDTO.model_validate({**result, "file_path": invalid})
    with pytest.raises(ValidationError):
        SessionFileChangeDTO.model_validate({**value, "kind": "unknown"})
    with pytest.raises(ValidationError):
        SessionFileReviewRequest.model_validate({"file_path": uri})


@pytest.mark.parametrize("model", ["SessionChangesetDTO", "SessionFileChangeDTO"])
def test_changes_candidate_drift_stops_export_before_snapshot_publication(
    http_descriptor_bundle: FileDescriptorSet, tmp_path: Path, model: str,
    openapi_output: Path, openapi_export: Callable[[Path, Path], None],
) -> None:
    message = next(message for file in http_descriptor_bundle.file for message in file.message_type if message.name == model)
    message.field.add(name="new_public_field", number=100, type=FieldDescriptorProto.TYPE_STRING)
    descriptor = tmp_path / "candidate.binpb"
    descriptor.write_bytes(http_descriptor_bundle.SerializeToString())
    with pytest.raises(ValueError, match="公开结构与 proto 字段不一致") as caught:
        openapi_export(openapi_output, descriptor)
    assert "new_public_field" in str(caught.value)
    assert model in str(caught.value)
    assert all((openapi_output / name).read_bytes() == b"original snapshot\n" for name in OPENAPI_SNAPSHOTS)
