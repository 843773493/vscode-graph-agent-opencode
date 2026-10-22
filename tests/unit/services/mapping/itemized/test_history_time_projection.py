"""区分来源消息时间、Turn 索引时间及其序列化表示。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from app.schemas.internal_v2.turn import TurnDetailDTO
from app.services.mapping.itemized.history_turns.detail import build_detail
from app.services.mapping.itemized.history_turns.projection import summary


@pytest.fixture
def history_projection() -> dict[str, object]:
    return {
        "status": "running",
        "created_at": "2026-10-04T12:00:00.123456+02:00",
        "updated_at": "2026-10-04T12:00:02.124456+02:00",
        "activity_stats": {"item_count": 2, "first_item_sequence": 1, "last_item_sequence": 2},
    }


@pytest.fixture
def history_detail() -> Callable[[list[BaseMessage], dict[str, object]], TurnDetailDTO]:
    def build(messages: list[BaseMessage], projection: dict[str, object]) -> TurnDetailDTO:
        return build_detail(
            "session-time", 1, messages,
            turn_id="turn-time", thread_id="thr_00000000700070008000000000000001",
            job_id="job-time", root_logical_item_ordinal=1,
            source_view_id="view-time", source_view_revision=1,
            projection=projection, include_final=False,
        )

    return build


@pytest.mark.parametrize("message_time", [
    "2026-10-04T18:00:01.987654+08:00",
    datetime.fromisoformat("2026-10-04T18:00:01.987654+08:00"),
])
def test_detail_and_summary_keep_message_offset_and_projection_clock(
    history_detail, history_projection: dict[str, object], message_time: str | datetime,
) -> None:
    message = HumanMessage(
        content="原消息", id="root-message", response_metadata={"created_at": message_time},
    )
    detail = history_detail([message], history_projection)
    expected = datetime.fromisoformat("2026-10-04T18:00:01.987654+08:00")
    assert detail.created_at == expected
    assert detail.created_at.utcoffset() == timedelta(hours=8)
    assert detail.created_at.microsecond == 987654
    assert detail.user_messages[0].created_at == expected
    assert detail.updated_at == datetime.fromisoformat(str(history_projection["updated_at"]))
    assert detail.updated_at.utcoffset() == timedelta(hours=2)
    assert detail.completed_at == detail.updated_at
    assert detail.status == "running"
    assert detail.activity_stats.duration_ms == 2001
    projected = summary(detail)
    assert projected.created_at == detail.created_at
    assert projected.updated_at == detail.updated_at
    assert projected.user_messages[0].created_at == expected
    assert projected.model_dump(mode="json")["created_at"] == "2026-10-04T18:00:01.987654+08:00"


@pytest.mark.parametrize("message_time", [
    "2026-10-04T10:00:01.987654",
    datetime.fromisoformat("2026-10-04T10:00:01.987654"),
])
def test_naive_message_time_uses_utc_without_losing_microseconds(
    history_detail, history_projection: dict[str, object], message_time: str | datetime,
) -> None:
    detail = history_detail([
        HumanMessage(content="消息", response_metadata={"created_at": message_time}),
    ], history_projection)
    assert detail.created_at == datetime(2026, 10, 4, 10, 0, 1, 987654, tzinfo=UTC)
    assert detail.user_messages[0].created_at == detail.created_at


@pytest.mark.parametrize("field", ["created_at", "updated_at"])
@pytest.mark.parametrize("invalid_time", [None, "", "invalid-date", "2026-10-04T12:00:00"])
def test_valid_message_time_does_not_bypass_invalid_projection_time(
    history_detail, history_projection: dict[str, object], field: str, invalid_time: object,
) -> None:
    history_projection[field] = invalid_time
    message = HumanMessage(
        content="消息", response_metadata={"created_at": "2026-10-04T10:00:00+00:00"},
    )
    with pytest.raises(ValueError):
        history_detail([message], history_projection)


def test_invalid_nonempty_message_date_does_not_fall_back_to_valid_projection(
    history_detail, history_projection: dict[str, object],
) -> None:
    # 使用无用户消息的加载范围，避免后续 user_message 再次校验掩盖错误回退。
    message = AIMessage(content="正文", response_metadata={"created_at": "invalid-date"})
    with pytest.raises(ValueError):
        history_detail([message], history_projection)


def test_metadata_only_history_uses_projection_clock_and_keeps_source_text(
    history_detail, history_projection: dict[str, object],
) -> None:
    original = dict(history_projection)
    detail = history_detail([], history_projection)
    assert detail.created_at == datetime.fromisoformat(str(original["created_at"]))
    assert detail.created_at.utcoffset() == timedelta(hours=2)
    assert detail.created_at.microsecond == 123456
    assert detail.updated_at == datetime.fromisoformat(str(original["updated_at"]))
    assert detail.activity_stats.duration_ms == 2001
    assert detail.user_messages == []
    assert history_projection == original
