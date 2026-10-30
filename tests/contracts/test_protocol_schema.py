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

from app.protocol.openapi import SSE_JSON_DESCRIPTORS
from app.protocol.schema import assert_public_schema
from app.schemas.internal_v2.sse import SSE_EVENT_MODELS


@pytest.mark.parametrize("name", SSE_JSON_DESCRIPTORS)
def test_same_shape_public_schema_matches_descriptor_and_keeps_business_constraints(name: str) -> None:
    schema = SSE_EVENT_MODELS[name].model_json_schema(mode="serialization")
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


def test_export_checks_fresh_proto_descriptor_before_publishing(tmp_path: Path) -> None:
    """模拟 proto 已新增字段而 Python binding 未更新，不能用旧 binding 自证一致。"""
    bundle = FileDescriptorSet()
    seen: set[str] = set()

    def collect(file: FileDescriptor) -> None:
        if file.name in seen:
            return
        seen.add(file.name)
        for dependency in file.dependencies:
            collect(dependency)
        file.CopyToProto(bundle.file.add())

    collect(SSE_JSON_DESCRIPTORS["TraceEventDTO"].file)
    trace = next(message for file in bundle.file for message in file.message_type if message.name == "TraceEvent")
    trace.field.add(name="new_public_field", number=100, type=FieldDescriptorProto.TYPE_STRING)
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
