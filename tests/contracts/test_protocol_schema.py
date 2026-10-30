"""公开结构由 proto 核对，内部模型继续收紧业务约束。"""

from __future__ import annotations

import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from google.protobuf.descriptor import FileDescriptor
from google.protobuf.descriptor_pb2 import FieldDescriptorProto, FileDescriptorSet
from jsonschema import ValidationError, validate

from app.protocol.generated.boxteam.workspace.v2 import (
    public_pb2,
    session_interaction_pb2,
)
from app.protocol.openapi import SSE_JSON_DESCRIPTORS
from app.protocol.schema import assert_public_schema
from app.schemas.internal_v2.persistent_task import TaskConditionDTO
from app.schemas.internal_v2.sse import SSE_EVENT_MODELS


@pytest.mark.parametrize("name", SSE_JSON_DESCRIPTORS)
def test_same_shape_public_schema_matches_descriptor_and_keeps_business_constraints(name: str) -> None:
    if name in SSE_EVENT_MODELS:
        schema = SSE_EVENT_MODELS[name].model_json_schema(mode="serialization")
    else:
        root = SSE_EVENT_MODELS["SessionExecutionSseDTO"].model_json_schema(mode="serialization")
        schema = {**root["$defs"][name], "$defs": root["$defs"]}
    original = deepcopy(schema)
    assert_public_schema(schema, SSE_JSON_DESCRIPTORS[name])
    assert schema == original
    if name == "SseErrorDTO":
        with pytest.raises(ValidationError):
            validate({"message": ""}, schema)
        validate({"message": "原始错误"}, schema)


@pytest.mark.parametrize("mutation", ["missing", "extra", "scalar", "container", "nested"])
def test_public_schema_rejects_nested_field_and_type_drift(mutation: str) -> None:
    schema = SSE_EVENT_MODELS["WorkspaceFileChangeBatchDTO"].model_json_schema(mode="serialization")
    if mutation == "missing":
        del schema["properties"]["overflow"]
    elif mutation == "extra":
        schema["properties"]["unexpected"] = {"type": "string"}
    elif mutation == "scalar":
        schema["properties"]["event_id"]["type"] = "string"
    elif mutation == "container":
        schema["properties"]["changes"] = {"type": "object"}
    else:
        schema["$defs"]["WorkspaceDirectoryKeyDTO"]["properties"]["workspace_id"] = {"type": "integer"}
    with pytest.raises(ValueError, match="公开结构与 proto"):
        assert_public_schema(schema, SSE_JSON_DESCRIPTORS["WorkspaceFileChangeBatchDTO"])


def test_task_public_structure_accepts_business_union_without_erasing_constraints() -> None:
    schema = SSE_EVENT_MODELS["PersistentTaskListDTO"].model_json_schema(mode="serialization")
    original = deepcopy(schema)
    assert_public_schema(schema, public_pb2.DESCRIPTOR.message_types_by_name["PersistentTaskListDTO"])
    assert schema == original
    condition = {**schema["$defs"]["TaskConditionDTO"], "$defs": schema["$defs"]}
    # Pydantic 的脚本参数下限仍进入生成的公开 validator，不能被 proto 的基础数值类型抹掉。
    with pytest.raises(ValidationError):
        validate({"version": "1", "type_id": "builtin.script", "script": {
            "command": ["echo"], "timeout_seconds": 0,
            "output_limit_bytes": 1024, "memory_limit_mb": 16,
        }}, condition)
    # 字段间业务关系继续由内部模型执行，结构生成不替代业务验证。
    with pytest.raises(ValueError, match="判断器必须配置对应参数"):
        TaskConditionDTO.model_validate({"version": "1", "type_id": "builtin.script"})


@pytest.mark.parametrize("mutation", ["extra", "missing_everywhere", "missing_common", "branch_type"])
def test_business_union_still_rejects_public_field_drift(mutation: str) -> None:
    schema = SSE_EVENT_MODELS["PersistentTaskListDTO"].model_json_schema(mode="serialization")
    runtime = schema["$defs"]["RuntimeSourceActionDTO"]["properties"]
    generation = schema["$defs"]["SessionGenerationActionDTO"]["properties"]
    if mutation == "extra":
        runtime["unexpected"] = {"type": "string"}
    elif mutation == "missing_everywhere":
        del generation["generation"]
    elif mutation == "missing_common":
        del runtime["version"]
    else:
        runtime["target_uri"] = {"type": "integer"}
    with pytest.raises(ValueError, match="公开结构与 proto"):
        assert_public_schema(schema, public_pb2.DESCRIPTOR.message_types_by_name["PersistentTaskListDTO"])


@pytest.mark.parametrize("mutation", ["unbounded", "unknown", "const_unknown", "union_unknown"])
def test_public_enum_rejects_values_the_codec_cannot_encode(mutation: str) -> None:
    root = SSE_EVENT_MODELS["SessionExecutionSseDTO"].model_json_schema(mode="serialization")
    schema = {**root["$defs"]["JobProgressDTO"], "$defs": root["$defs"]}
    status: dict[str, object] = {"type": "string"}
    if mutation == "unknown":
        status["enum"] = ["completed", "invented"]
    elif mutation == "const_unknown":
        status["const"] = "invented"
    elif mutation == "union_unknown":
        status = {"anyOf": [{"type": "string", "const": "completed"},
                            {"type": "string", "const": "invented"}]}
    schema["properties"]["status"] = status
    with pytest.raises(ValueError, match=r"公开结构与 proto 枚举不一致: .*/status"):
        assert_public_schema(schema, SSE_JSON_DESCRIPTORS["JobProgressDTO"])


@pytest.mark.parametrize("status", [
    {"type": "string", "enum": ["completed", "failed"]},
    {"type": "string", "const": "completed"},
    {"anyOf": [{"type": "string", "const": "completed"}, {"type": "string", "const": "failed"}]},
])
def test_public_enum_keeps_narrower_business_constraints(status: dict[str, object]) -> None:
    root = SSE_EVENT_MODELS["SessionExecutionSseDTO"].model_json_schema(mode="serialization")
    schema = {**root["$defs"]["JobProgressDTO"], "$defs": root["$defs"]}
    schema["properties"]["status"] = status
    original = deepcopy(schema)
    assert_public_schema(schema, SSE_JSON_DESCRIPTORS["JobProgressDTO"])
    assert schema == original


@pytest.mark.parametrize("mutation", ["renamed", "removed"])
def test_export_rejects_fresh_proto_enum_drift_before_publishing(
    tmp_path: Path, protocol_descriptor_bundle: FileDescriptorSet, mutation: str,
) -> None:
    enum = next(enum for file in protocol_descriptor_bundle.file for enum in file.enum_type
                if enum.name == "JobStatus")
    completed = next(value for value in enum.value if value.name == "JOB_STATUS_COMPLETED")
    if mutation == "renamed":
        completed.name = "JOB_STATUS_FINISHED"
    else:
        enum.value.remove(completed)
    descriptor_set = tmp_path / "fresh.binpb"
    descriptor_set.write_bytes(protocol_descriptor_bundle.SerializeToString())
    output = tmp_path / "generated"
    result = subprocess.run(
        [sys.executable, "-m", "scripts.export_sse_runtime_schemas", "--output-root", str(output),
         "--descriptor-set", str(descriptor_set)],
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "公开结构与 proto 枚举不一致" in result.stderr
    assert "completed" in result.stderr and "/status" in result.stderr
    assert not output.exists()


@pytest.mark.parametrize("message_name", [
    "TraceEvent", "WorkspaceObservationFrameDTO", "TaskActionDTO", "JobProgress",
    "JobStepProgress", "MessageObservation", "SessionError", "TraceObserved",
    "WorkspaceFileChangeBatch", "WorkspaceDirectoryKeyDTO", "WorkspaceFileNodeDTO",
])
def test_export_checks_fresh_proto_descriptor_before_publishing(
    tmp_path: Path, message_name: str, protocol_descriptor_bundle: FileDescriptorSet,
) -> None:
    """模拟 proto 已新增字段而 Python binding 未更新，不能用旧 binding 自证一致。"""
    bundle = protocol_descriptor_bundle
    message = next(message for file in bundle.file for message in file.message_type if message.name == message_name)
    message.field.add(name="new_public_field", number=100, type=FieldDescriptorProto.TYPE_STRING)
    descriptor_set = tmp_path / "fresh.binpb"
    descriptor_set.write_bytes(bundle.SerializeToString())
    output = tmp_path / "generated"
    result = subprocess.run(
        [sys.executable, "-m", "scripts.export_sse_runtime_schemas", "--output-root", str(output),
         "--descriptor-set", str(descriptor_set)],
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode != 0 and "new_public_field" in result.stderr
    assert "公开结构与 proto 字段不一致" in result.stderr
    assert not output.exists()


@pytest.fixture
def protocol_descriptor_bundle() -> FileDescriptorSet:
    bundle = FileDescriptorSet()
    seen: set[str] = set()

    def collect(file: FileDescriptor) -> None:
        if file.name in seen:
            return
        seen.add(file.name)
        for dependency in file.dependencies:
            collect(dependency)
        file.CopyToProto(bundle.file.add())

    for descriptor in SSE_JSON_DESCRIPTORS.values():
        collect(descriptor.file)
    collect(session_interaction_pb2.DESCRIPTOR)
    return bundle


@pytest.mark.parametrize(("mutation", "error"), [
    ("added", "new.event"), ("removed", "trace.observed"), ("renamed", "trace.changed"),
    ("detached", "trace.observed"), ("wrong_message", "boxteam.workspace.v2.SessionError"),
    ("scalar", "trace.observed"), ("status_wrong_message", "session.status.changed"),
])
def test_export_rejects_event_union_drift_before_publishing(
    tmp_path: Path, protocol_descriptor_bundle: FileDescriptorSet, mutation: str, error: str,
) -> None:
    bundle = protocol_descriptor_bundle
    message = next(message for file in bundle.file for message in file.message_type
                   if message.name == "SessionExecutionEvent")
    field = next(field for field in message.field if field.name == "trace_observed")
    if mutation == "added":
        message.field.add(name="new_event", number=100, type=FieldDescriptorProto.TYPE_MESSAGE,
                          type_name=".boxteam.workspace.v2.JobProgress", oneof_index=0)
    elif mutation == "removed":
        message.field.remove(field)
    elif mutation == "renamed":
        field.name = "trace_changed"
    elif mutation == "detached":
        field.ClearField("oneof_index")
    elif mutation == "wrong_message":
        field.type_name = ".boxteam.workspace.v2.SessionError"
    elif mutation == "status_wrong_message":
        field = next(field for field in message.field if field.name == "session_status_changed")
        field.type_name = ".boxteam.workspace.v2.JobProgress"
    else:
        field.type = FieldDescriptorProto.TYPE_STRING
        field.ClearField("type_name")
    descriptor_set = tmp_path / "fresh.binpb"
    descriptor_set.write_bytes(bundle.SerializeToString())
    output = tmp_path / "generated"
    result = subprocess.run(
        [sys.executable, "-m", "scripts.export_sse_runtime_schemas", "--output-root", str(output),
         "--descriptor-set", str(descriptor_set)],
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert error in result.stderr
    assert not output.exists()
