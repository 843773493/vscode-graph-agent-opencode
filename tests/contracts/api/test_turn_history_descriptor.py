"""真实历史 HTTP 结构由 descriptor 核对，选择与 canonical 身份仍归业务模型。"""

from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from google.protobuf.descriptor_pb2 import FieldDescriptorProto, FileDescriptorSet
from pydantic import ValidationError
from pydantic.json_schema import JsonSchemaValue

from app.protocol.openapi import HTTP_JSON_DESCRIPTORS, assert_public_http_schemas
from app.protocol.schema import descriptor_pool_from_set
from app.schemas.internal_v2.turn import TurnHistoryLoadRequest, TurnResponsePartDTO
from tests.contracts.api.openapi_snapshots import OPENAPI_SNAPSHOTS

_ROOTS = ("SessionTurnBootstrapDTO", "TurnHistoryLoadRequest", "TurnHistoryPageDTO")


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
    ("InternalExecutionDisplayPartDTO", "item_sequence", {"type": "string"}),
    ("InternalDisplayProvenanceDTO", "source_thread_id", {"type": "integer"}),
])
def test_history_contract_rejects_nested_shape_drift(
    http_openapi: JsonSchemaValue, model: str, field: str, shape: JsonSchemaValue,
) -> None:
    http_openapi["components"]["schemas"][model]["properties"][field] = shape
    with pytest.raises(ValueError, match="公开结构与 proto 类型不一致") as caught:
        assert_public_http_schemas(http_openapi)
    assert "/" + field in str(caught.value)


@pytest.mark.parametrize("model", (*_ROOTS, "TurnResponseSourceDTO", "InternalExecutionDisplayPartDTO", "InternalDisplayProvenanceDTO"))
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
])
def test_history_business_selection_still_rejects_invalid_combinations(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        TurnHistoryLoadRequest.model_validate(payload)


def test_history_canonical_identity_remains_a_business_constraint() -> None:
    payload = {"part_id": "part", "kind": "text", "projection": "streaming", "source": {"message_sequence": 0}}
    assert TurnResponsePartDTO.model_validate(payload).projection == "streaming"
    with pytest.raises(ValidationError, match="缺少 canonical item identity"):
        TurnResponsePartDTO.model_validate({**payload, "projection": "detail"})
    canonical = {"message_sequence": 0, "item_id": "item", "item_sequence": 1, "created_at": "2026-11-01T00:00:00Z"}
    assert TurnResponsePartDTO.model_validate({**payload, "projection": "detail", "source": canonical}).source.item_id == "item"
    with pytest.raises(ValidationError, match="live streaming part 不得伪造"):
        TurnResponsePartDTO.model_validate({**payload, "source": canonical})
