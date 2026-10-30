"""真实消息流边界按 descriptor 转换枚举，不按字段名猜测枚举类别。"""

from __future__ import annotations

from copy import deepcopy

import pytest

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
