"""真实消息流边界按 descriptor 转换枚举，不按字段名猜测枚举类别。"""

from __future__ import annotations

from copy import deepcopy
from typing import cast

import pytest
from google.protobuf import struct_pb2, timestamp_pb2
from google.protobuf.descriptor import Descriptor
from google.protobuf.descriptor_pb2 import FieldDescriptorProto, FileDescriptorProto
from google.protobuf.descriptor_pool import DescriptorPool

from app.protocol.codecs.json import event_payload_fields
from app.protocol.codecs.message_stream import (
    message_stream_to_json,
    message_stream_to_proto,
)
from app.protocol.generated.boxteam.workspace.message.v1 import message_stream_pb2


@pytest.fixture
def event() -> dict[str, object]:
    return {"event_id": "evt_descriptor", "session_id": "ses_019bef80066f7038ae8e88ccb2e5e2ea",
            "thread_id": "thr_00000000400070008000000000000002", "turn_id": "turn_descriptor",
            "turn_stream_id": "stream_descriptor", "event_seq": 1}


def test_fresh_descriptor_adds_payload_without_another_dispatch_entry() -> None:
    file = FileDescriptorProto()
    message_stream_pb2.DESCRIPTOR.CopyToProto(file)
    envelope = next(message for message in file.message_type if message.name == "MessageStreamEvent")
    envelope.field.add(
        name="model_paused", number=40, label=FieldDescriptorProto.LABEL_OPTIONAL,
        type=FieldDescriptorProto.TYPE_MESSAGE, type_name=".boxteam.workspace.message.v1.ModelStarted",
        oneof_index=next(index for index, oneof in enumerate(envelope.oneof_decl) if oneof.name == "payload"),
    )
    pool = DescriptorPool()
    for dependency in (struct_pb2.DESCRIPTOR, timestamp_pb2.DESCRIPTOR):
        pool.AddSerializedFile(dependency.serialized_pb)
    pool.Add(file)
    descriptor = pool.FindMessageTypeByName("boxteam.workspace.message.v1.MessageStreamEvent")
    assert "model_paused" not in message_stream_pb2.MessageStreamEvent.DESCRIPTOR.fields_by_name
    field = event_payload_fields(descriptor)["model.paused"]
    assert field.containing_type is descriptor
    assert field.name == "model_paused"
    assert field.message_type is not None and field.message_type.name == "ModelStarted"


@pytest.mark.parametrize("aliases", [
    {"no_such_payload": ("unknown.event",)},
    {"stream_opened": ("model.started",)},
    {"stream_opened": ()},
    {"stream_opened": ("stream.opened", "stream.opened")},
    {"stream_opened": ("",)},
])
def test_payload_aliases_reject_stale_missing_or_conflicting_tags(aliases: dict[str, tuple[str, ...]]) -> None:
    with pytest.raises(ValueError):
        event_payload_fields(cast(Descriptor, message_stream_pb2.MessageStreamEvent.DESCRIPTOR), aliases=aliases)


@pytest.mark.parametrize(("event_type", "payload_field"), [
    ("tool_call", "tool_call"), ("tool_call.delta", "tool_call_delta"),
    ("tool_call.completed", "tool_call_completed"),
    ("activity.started", "activity"), ("activity.updated", "activity"),
    ("activity.completed", "activity"), ("activity.failed", "activity"),
])
def test_business_event_aliases_keep_original_wire_branch(
    event: dict[str, object], event_type: str, payload_field: str,
) -> None:
    payload = {"activity_id": "activity_alias"} if event_type.startswith("activity.") else {"tool_call_id": "call_alias"}
    wire = message_stream_to_proto({**event, "type": event_type, "payload": payload})
    assert wire.WhichOneof("payload") == payload_field
    decoded = message_stream_to_json(message_stream_pb2.MessageStreamEvent.FromString(wire.SerializeToString()))
    assert decoded["type"] == event_type


@pytest.mark.parametrize("event_type", ["tool.call", "tool.call.delta", "activity"])
def test_mechanical_names_do_not_replace_business_aliases(event: dict[str, object], event_type: str) -> None:
    with pytest.raises(ValueError, match="事件类型未注册"):
        message_stream_to_proto({**event, "type": event_type, "payload": {}})


@pytest.mark.parametrize(("event_type", "field_name", "descriptor"), [
    ("stream.opened", "status", message_stream_pb2.StreamStatus.DESCRIPTOR),
    ("model.completed", "outcome", message_stream_pb2.ModelCallOutcome.DESCRIPTOR),
    ("block.delta", "operation", message_stream_pb2.BlockDeltaOperation.DESCRIPTOR),
    ("tool.completed", "status", message_stream_pb2.ToolExecutionStatus.DESCRIPTOR),
    ("tool.completed", "outcome", message_stream_pb2.ToolExecutionOutcome.DESCRIPTOR),
    ("activity.updated", "status", message_stream_pb2.ActivityStatus.DESCRIPTOR),
    ("activity.updated", "outcome", message_stream_pb2.ActivityOutcome.DESCRIPTOR),
])
def test_all_declared_public_enum_values_round_trip(event, event_type, field_name, descriptor) -> None:
    prefix = descriptor.values_by_number[0].name.removesuffix("UNSPECIFIED")
    for enum_value in descriptor.values:
        if enum_value.number == 0:
            continue
        name = enum_value.name.removeprefix(prefix).lower()
        source = {**event, "type": event_type, "payload": {field_name: name}}
        encoded = message_stream_to_proto(source)
        payload = getattr(encoded, encoded.WhichOneof("payload"))
        assert getattr(payload, field_name) == enum_value.number
        assert message_stream_to_json(encoded)["payload"][field_name] == name


def test_snapshot_recurses_into_enum_fields_without_touching_dynamic_body_or_source(event) -> None:
    detail = {"status": "running", "outcome": "success", "nested": {"status": "completed"}}
    source = {**event, "type": "stream.snapshot", "payload": {
        "stream_status": "open", "snapshot_seq": 1,
        "activities": [{"status": "waiting", "outcome": "execution_lost", "detail": detail}],
        "tool_executions": [{"status": "completed", "outcome": "outcome_unknown"}],
        "tool_calls": [{"status": "running", "arguments": detail}],
    }}
    original = deepcopy(source)
    encoded = message_stream_to_proto(source)
    assert encoded.stream_snapshot.activities[0].status == message_stream_pb2.ACTIVITY_STATUS_WAITING
    decoded = message_stream_to_json(encoded)["payload"]
    assert decoded["activities"][0]["status"] == "waiting"
    assert decoded["activities"][0]["outcome"] == "execution_lost"
    assert decoded["activities"][0]["detail"] == detail
    assert decoded["tool_executions"][0]["outcome"] == "outcome_unknown"
    assert decoded["tool_calls"][0]["status"] == "running"
    assert decoded["tool_calls"][0]["arguments"] == detail
    assert source == original
