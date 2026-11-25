"""真实历史 HTTP 结构由 descriptor 核对，选择与 canonical 身份仍归业务模型。"""

from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from boxteam.workspace.v2 import public_pb2
from google.protobuf.descriptor_pb2 import FieldDescriptorProto, FileDescriptorSet
from google.protobuf.json_format import ParseDict
from pydantic import ValidationError
from pydantic.json_schema import JsonSchemaValue

from app.protocol.openapi import HTTP_JSON_DESCRIPTORS, assert_public_http_schemas
from app.protocol.schema import assert_public_schema, descriptor_pool_from_set
from app.schemas.internal_v2.turn import TurnHistoryLoadRequest, TurnResponseItemDTO
from app.schemas.internal_v2.turn_search import ThreadItemInspectionRequest
from tests.contracts.api.openapi_snapshots import OPENAPI_SNAPSHOTS

_ROOTS = ("SessionTurnBootstrapDTO", "TurnHistoryLoadRequest", "TurnHistoryPageDTO")


def test_execution_output_reference_uses_existing_content_protocol() -> None:
    request = ThreadItemInspectionRequest.model_validate({"operation": "content", "content_ref": {
        "kind": "execution_output", "session_id": "ses_019b99f62ca97699a68eacac2b7dda6e",
        "thread_id": "thr_019b99f62ca97699a68eacac2b7dda6f",
        "execution_id": "derived-execution", "item_id": "output-item", "content_hash": "sha256:actual",
    }})
    wire = ParseDict(request.model_dump(mode="json", exclude_none=True), public_pb2.ThreadItemInspectionRequest())
    assert wire.content_ref.execution_id == "derived-execution"
    assert wire.content_ref.content_hash == "sha256:actual"
    with pytest.raises(ValidationError, match="仅用于读取结果正文或格式"):
        ThreadItemInspectionRequest.model_validate({**request.model_dump(), "operation": "trace"})


def test_tool_result_marker_survives_public_schema_and_proto_without_execution_timing() -> None:
    """真实运行时JSON与生成协议均区分结果marker、声明完成和可空时序。"""
    assert_public_schema(TurnResponseItemDTO.model_json_schema(), public_pb2.TurnResponseItemDTO.DESCRIPTOR)
    item = TurnResponseItemDTO(id="result-item", kind="tool_result", projection="summary",
        status="completed", source={"message_sequence": 1, "item_id": "result-item", "item_sequence": 1,
                                    "created_at": "2026-11-23T00:00:00Z"},
        tool_call_id="call", tool_outcome="failure")
    encoded = ParseDict(item.model_dump(mode="json"), public_pb2.TurnResponseItemDTO())
    assert encoded.HasField("tool_outcome") and encoded.tool_outcome == "failure"
    assert encoded.status == "completed" and not encoded.HasField("tool_execution")
    without_marker = item.model_copy(update={"tool_outcome": None})
    assert not ParseDict(without_marker.model_dump(mode="json"), public_pb2.TurnResponseItemDTO()).HasField("tool_outcome")


@pytest.mark.parametrize(("path", "method", "request_model", "response_model"), [
    ("/{session_id}/bootstrap", "get", None, "SessionTurnBootstrapDTO"),
    ("/{session_id}/history", "post", "TurnHistoryLoadRequest", "TurnHistoryPageDTO"),
    ("/{session_id}/threads/{thread_id}/history", "post", "TurnHistoryLoadRequest", "TurnHistoryPageDTO"),
])
def test_history_contract_follows_live_routes(
    http_openapi: JsonSchemaValue, path: str, method: str, request_model: str | None, response_model: str,
) -> None:
    original = deepcopy(http_openapi)
    operation = http_openapi["paths"]["/api/v1/workspaces/{workspace_id}/sessions" + path][method]
    if request_model is not None:
        assert operation["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith("/" + request_model)
        assert request_model in HTTP_JSON_DESCRIPTORS
    response = operation["responses"]["200"]["content"]["application/json"]["schema"]
    envelope = http_openapi["components"]["schemas"][response["$ref"].rsplit("/", 1)[1]]
    assert envelope["properties"]["data"]["anyOf"][0]["$ref"].endswith("/" + response_model)
    assert response_model in HTTP_JSON_DESCRIPTORS
    assert_public_http_schemas(http_openapi)
    assert http_openapi == original


@pytest.mark.parametrize("model", _ROOTS)
def test_history_contract_requires_roots(http_openapi: JsonSchemaValue, model: str) -> None:
    del http_openapi["components"]["schemas"][model]
    with pytest.raises(ValueError, match=f"公开 HTTP 结构未被导出: {model}"):
        assert_public_http_schemas(http_openapi)


@pytest.mark.parametrize(("model", "field", "shape"), [
    ("SessionTurnBootstrapDTO", "thread_id", {"type": "integer"}),
    ("TurnHistoryLoadRequest", "tool_call_ids", {"type": "array", "items": {"type": "integer"}}),
    ("TurnHistoryPageDTO", "internal_executions", {"type": "object"}),
    ("TurnResponseSourceDTO", "logical_item_ordinal", {"type": "string"}),
    ("InternalExecutionDisplayItemDTO", "item_sequence", {"type": "string"}),
    ("InternalDisplayProvenanceDTO", "source_thread_id", {"type": "integer"}),
])
def test_history_contract_rejects_nested_shape_drift(
    http_openapi: JsonSchemaValue, model: str, field: str, shape: JsonSchemaValue,
) -> None:
    http_openapi["components"]["schemas"][model]["properties"][field] = shape
    with pytest.raises(ValueError, match="公开结构与 proto 类型不一致") as caught:
        assert_public_http_schemas(http_openapi)
    assert "/" + field in str(caught.value)


@pytest.mark.parametrize("model", (*_ROOTS, "TurnResponseSourceDTO", "InternalExecutionDisplayItemDTO", "InternalDisplayProvenanceDTO"))
def test_history_contract_checks_candidate_descriptor(
    http_openapi: JsonSchemaValue, http_descriptor_bundle: FileDescriptorSet, model: str,
) -> None:
    message = next(message for file in http_descriptor_bundle.file for message in file.message_type if message.name == model)
    message.field.add(name="new_history_field", number=100, type=FieldDescriptorProto.TYPE_STRING)
    pool = descriptor_pool_from_set(http_descriptor_bundle.SerializeToString())
    with pytest.raises(ValueError, match="公开结构与 proto 字段不一致") as caught:
        assert_public_http_schemas(http_openapi, pool)
    assert "new_history_field" in str(caught.value) and model in str(caught.value)


@pytest.mark.parametrize("model", ["SessionTurnBootstrapDTO", "InternalDisplayProvenanceDTO"])
def test_history_export_rejects_drift_before_publishing(
    tmp_path: Path, http_descriptor_bundle: FileDescriptorSet, model: str,
) -> None:
    message = next(message for file in http_descriptor_bundle.file for message in file.message_type if message.name == model)
    message.field.add(name="new_history_field", number=100, type=FieldDescriptorProto.TYPE_STRING)
    descriptor_set = tmp_path / "candidate.binpb"
    descriptor_set.write_bytes(http_descriptor_bundle.SerializeToString())
    output = tmp_path / "generated"
    for name in OPENAPI_SNAPSHOTS:
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"original snapshot\n")
    command = [sys.executable, "-m", "scripts.export_openapi_snapshot", "--output-root", str(output),
               "--descriptor-set", str(descriptor_set)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    (tmp_path / "export.stdout.log").write_text(result.stdout, encoding="utf-8")
    (tmp_path / "export.stderr.log").write_text(result.stderr, encoding="utf-8")
    (tmp_path / "export.json").write_text(json.dumps({"command": command, "exit_code": result.returncode}, indent=2) + "\n")
    assert result.returncode != 0, result.stdout + result.stderr
    assert "公开结构与 proto 字段不一致" in result.stderr and "new_history_field" in result.stderr
    assert model in result.stderr
    for name in OPENAPI_SNAPSHOTS:
        assert (output / name).read_bytes() == b"original snapshot\n"


@pytest.mark.parametrize("payload", [
    {"direction": "around"},
    {"turn_ids": ["turn", "turn"]},
    {"execution_id": "execution", "turn_ids": ["turn"]},
    {"anchor_context": {"model_call_id": "call", "anchor": "before_model_call"}},
    {"direction": "around", "anchor_turn_id": "turn", "context_cursor": "cursor",
     "anchor_context": {"model_call_id": "call", "anchor": "before_model_call"}},
])
def test_history_business_selection_still_rejects_invalid_combinations(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        TurnHistoryLoadRequest.model_validate(payload)


def test_history_canonical_identity_remains_a_business_constraint() -> None:
    payload = {"id": "part", "kind": "text", "projection": "streaming", "source": {"message_sequence": 0}}
    assert TurnResponseItemDTO.model_validate(payload).projection == "streaming"
    with pytest.raises(ValidationError, match="缺少 canonical item identity"):
        TurnResponseItemDTO.model_validate({**payload, "projection": "detail"})
    canonical = {"message_sequence": 0, "item_id": "item", "item_sequence": 1, "created_at": "2026-11-01T00:00:00Z"}
    assert TurnResponseItemDTO.model_validate({**payload, "id": "item", "projection": "detail", "source": canonical}).source.item_id == "item"
    with pytest.raises(ValidationError, match="live streaming Item 不得伪造"):
        TurnResponseItemDTO.model_validate({**payload, "source": canonical})
