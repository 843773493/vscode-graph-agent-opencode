from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.services.mapping.trace_event_mapper import TraceEventMapper


def _event() -> dict[str, object]:
    return {
        "event_id": "evt_1",
        "job_id": "job_1",
        "timestamp": datetime(2026, 7, 14, tzinfo=timezone.utc),
        "type": "agent_start",
        "payload": {"agent_id": "default", "message": "start"},
    }


def test_mapper_uses_explicit_session_scope_and_preserves_ids() -> None:
    mapped = TraceEventMapper().map_one(_event(), session_id="ses_1")

    assert mapped is not None
    assert mapped.event_id == "evt_1"
    assert mapped.job_id == "job_1"
    assert mapped.session_id == "ses_1"


@pytest.mark.parametrize("field", ["event_id", "job_id", "timestamp"])
def test_mapper_rejects_missing_authoritative_fields(field: str) -> None:
    event = _event()
    event.pop(field)

    with pytest.raises((TypeError, ValueError), match=field):
        TraceEventMapper().map_one(event, session_id="ses_1")


def test_mapper_rejects_missing_session_scope() -> None:
    with pytest.raises(ValueError, match="session_id"):
        TraceEventMapper().map_one(_event())


def test_mapper_rejects_naive_timestamp() -> None:
    event = _event()
    event["timestamp"] = datetime(2026, 7, 14)

    with pytest.raises(ValueError, match="必须包含时区"):
        TraceEventMapper().map_one(event, session_id="ses_1")


def test_mapper_marks_failed_tool_result_as_failed() -> None:
    event = _event()
    event["type"] = "tool_call_end"
    event["part_id"] = "run_tool"
    event["payload"] = {
        "tool_name": "read_session_recent_text_messages",
        "result": "Gateway 工作区不存在: gw_typo",
        "status": "error",
        "failed": True,
    }

    mapped = TraceEventMapper().map_one(event, session_id="ses_1")

    assert mapped is not None
    assert mapped.title == "工具失败"
    assert mapped.status == "failed"
    assert mapped.content == "Gateway 工作区不存在: gw_typo"
