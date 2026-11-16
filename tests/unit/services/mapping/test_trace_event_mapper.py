from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services.mapping.trace_event_mapper import trace_event_to_dto

_THREAD_ID = "thr_00000000700070008000000000000001"


def _trace_event() -> dict[str, object]:
    return {
        "event_id": "evt_1",
        "job_id": "job_1",
        "timestamp": datetime(2026, 7, 14, tzinfo=UTC),
        "type": "agent_start",
        "payload": {"agent_id": "default", "message": "start"},
    }


def test_mapper_uses_explicit_session_scope_and_preserves_ids() -> None:
    mapped = trace_event_to_dto(
        trace_event=_trace_event(),
        session_id="ses_1",
        thread_id=_THREAD_ID,
    )

    assert mapped is not None
    assert mapped.event_id == "evt_1"
    assert mapped.job_id == "job_1"
    assert mapped.session_id == "ses_1"
    assert mapped.thread_id == _THREAD_ID


@pytest.mark.parametrize("field", ["event_id", "job_id", "timestamp"])
def test_mapper_rejects_missing_authoritative_fields(field: str) -> None:
    trace_event = _trace_event()
    trace_event.pop(field)

    with pytest.raises((TypeError, ValueError), match=field):
        trace_event_to_dto(
            trace_event, session_id="ses_1", thread_id=_THREAD_ID
        )


def test_mapper_rejects_missing_session_scope() -> None:
    with pytest.raises(ValueError, match="session_id"):
        trace_event_to_dto(_trace_event(), thread_id=_THREAD_ID)


def test_mapper_rejects_empty_thread_scope() -> None:
    with pytest.raises(ValueError):
        trace_event_to_dto(_trace_event(), session_id="ses_1", thread_id="")


def test_mapper_rejects_naive_timestamp() -> None:
    trace_event = _trace_event()
    trace_event["timestamp"] = datetime(2026, 7, 14)  # noqa: DTZ001 - 明确验证缺少时区时拒绝。

    with pytest.raises(ValueError, match="必须包含时区"):
        trace_event_to_dto(
            trace_event, session_id="ses_1", thread_id=_THREAD_ID
        )


def test_mapper_marks_failed_tool_result_as_failed() -> None:
    trace_event = _trace_event()
    trace_event["type"] = "tool_call_end"
    trace_event["part_id"] = "run_tool"
    trace_event["payload"] = {
        "tool_name": "read_context",
        "result": "Gateway 工作区不存在: gw_typo",
        "status": "error",
        "failed": True,
    }

    mapped = trace_event_to_dto(
        trace_event, session_id="ses_1", thread_id=_THREAD_ID
    )

    assert mapped is not None
    assert mapped.title == "工具失败"
    assert mapped.status == "failed"
    assert mapped.content == "Gateway 工作区不存在: gw_typo"


def test_mapper_preserves_model_failure_details() -> None:
    trace_event = _trace_event()
    trace_event["type"] = "model_failed"
    trace_event["payload"] = {
        "provider_id": "primary",
        "model": "big-pickle",
        "error_type": "RateLimitError",
        "error": "Rate limit exceeded",
    }

    mapped = trace_event_to_dto(
        trace_event, session_id="ses_1", thread_id=_THREAD_ID
    )

    assert mapped is not None
    assert mapped.phase == "llm"
    assert mapped.title == "模型调用失败"
    assert mapped.status == "failed"
    assert mapped.content == (
        "primary/big-pickle：RateLimitError: Rate limit exceeded"
    )


def test_mapper_exposes_process_exit_as_execution_lost() -> None:
    trace_event = _trace_event()
    trace_event["type"] = "session_interrupted"
    trace_event["payload"] = {
        "phase": "process_exit",
        "code": "execution_lost",
        "message": "工作区后端重启，无法安全续接原 AgentLoop 执行",
    }

    mapped = trace_event_to_dto(
        trace_event, session_id="ses_1", thread_id=_THREAD_ID
    )

    assert mapped is not None
    assert mapped.phase == "session"
    assert mapped.title == "执行丢失"
    assert mapped.status == "failed"
    assert mapped.content == "工作区后端重启，无法安全续接原 AgentLoop 执行"


def test_trace_event_mapper_maps_tool_events():
    event = trace_event_to_dto(
        {
            "event_id": "evt_tool",
            "job_id": "job_1",
            "step_id": "step_1",
            "timestamp": "2024-03-09T12:00:00+00:00",
            "type": "tool_call_end",
            "payload": {"tool_name": "search_files"},
        },
        session_id="ses_1",
        thread_id="thr_019bef80066f7038ae8e88ccb2e5e2ea",
    )

    assert event is not None
    assert event.type == "tool_call_end"
    assert event.phase == "tool"
    assert event.title == "工具返回"
    assert event.content == "工具 search_files 已返回结果"
    assert event.tool_name == "search_files"
    assert event.status == "completed"


def test_trace_event_mapper_maps_tool_skill_names():
    event = trace_event_to_dto(
        {
            "event_id": "evt_tool_skill",
            "job_id": "job_1",
            "step_id": "step_1",
            "timestamp": "2024-03-09T12:00:00+00:00",
            "type": "tool_call_start",
            "payload": {
                "tool_name": "test_tool_2",
                "args": {},
                "skill_names": ["test-tool-2"],
            },
        },
        session_id="ses_1",
        thread_id="thr_019bef80066f7038ae8e88ccb2e5e2ea",
    )

    assert event is not None
    assert event.type == "tool_call_start"
    assert event.tool_name == "test_tool_2"
    assert event.skill_names == ["test-tool-2"]
