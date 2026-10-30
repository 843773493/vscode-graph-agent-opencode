"""Trace 的字段映射由 descriptor 推导，公开空值与时间语义保持不变。"""

from __future__ import annotations

from copy import deepcopy

import pytest

from app.protocol.codecs.workspace_events import trace_to_json, trace_to_proto
from app.protocol.generated.boxteam.workspace.v2 import trace_pb2

_OPTIONAL_FIELDS = ("part_id", "status", "tool_name", "step_id")


@pytest.fixture
def trace() -> dict[str, object]:
    return {
        "event_id": "evt_trace_codec",
        "session_id": "ses_019bef80066f7038ae8e88ccb2e5e2ea",
        "thread_id": "thr_019bef80066f7038ae8e88ccb2e5e2ea",
        "job_id": "job_trace_codec",
        "type": "tool_call_end",
        "phase": "tool",
        "title": "工具完成",
        "content": "",
        "part_id": "part_trace",
        "status": "completed",
        "tool_name": "read_file",
        "step_id": "step_trace",
        "skill_names": ["read", "review"],
        "timestamp": "2026-10-30T10:00:00.123456789+08:00",
        "raw": {"nested": {"status": "running"}, "values": [True, None, 2, "正文"]},
    }


def test_trace_wire_round_trip_preserves_all_fields_and_dynamic_body(trace: dict[str, object]) -> None:
    original = deepcopy(trace)
    encoded = trace_to_proto(trace)
    restored = trace_pb2.TraceEvent.FromString(encoded.SerializeToString())
    assert trace_to_json(restored) == {**trace, "timestamp": "2026-10-30T02:00:00.123456789Z"}
    assert trace == original


@pytest.mark.parametrize("state", ["omitted", "null", "empty"])
def test_trace_presence_preserves_null_and_empty_string_distinction(trace: dict[str, object], state: str) -> None:
    for name in _OPTIONAL_FIELDS:
        if state == "omitted":
            del trace[name]
        else:
            trace[name] = None if state == "null" else ""
    del trace["skill_names"]
    del trace["raw"]
    encoded = trace_to_proto(trace)
    assert all(encoded.HasField(name) == (state == "empty") for name in _OPTIONAL_FIELDS)
    assert trace_to_json(encoded) == {
        **trace,
        **dict.fromkeys(_OPTIONAL_FIELDS, "" if state == "empty" else None),
        "skill_names": [],
        "raw": {},
        "timestamp": "2026-10-30T02:00:00.123456789Z",
    }


@pytest.mark.parametrize("name", ["event_id", "session_id", "thread_id", "job_id", "type", "phase", "title", "content"])
@pytest.mark.parametrize("state", ["omitted", "wrong_type"])
def test_trace_required_string_fields_remain_strict(trace: dict[str, object], name: str, state: str) -> None:
    if state == "omitted":
        del trace[name]
    else:
        trace[name] = 42
    with pytest.raises(TypeError, match=f"Trace 事件缺少字段: {name}"):
        trace_to_proto(trace)


@pytest.mark.parametrize("name", _OPTIONAL_FIELDS)
def test_trace_optional_fields_reject_non_string_values(trace: dict[str, object], name: str) -> None:
    trace[name] = 42
    with pytest.raises(ValueError, match=f"Trace 事件字段类型错误: {name}"):
        trace_to_proto(trace)


@pytest.mark.parametrize("skills", [None, "read", ["read", 42]])
def test_trace_repeated_strings_reject_invalid_values(trace: dict[str, object], skills: object) -> None:
    trace["skill_names"] = skills
    with pytest.raises(ValueError, match="skill_names 必须是字符串数组"):
        trace_to_proto(trace)


@pytest.mark.parametrize("name", ["session_id", "thread_id"])
def test_trace_keeps_canonical_owner_validation(trace: dict[str, object], name: str) -> None:
    trace[name] = "invalid_owner"
    with pytest.raises(ValueError):
        trace_to_proto(trace)


@pytest.mark.parametrize(("name", "invalid", "message"), [
    ("raw", [], "raw 必须是对象"),
    ("timestamp", None, "timestamp 必须是 ISO 字符串"),
])
def test_trace_keeps_special_field_validation(
    trace: dict[str, object], name: str, invalid: object, message: str,
) -> None:
    trace[name] = invalid
    with pytest.raises(TypeError, match=message):
        trace_to_proto(trace)
