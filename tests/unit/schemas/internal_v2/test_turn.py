from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.schemas.internal_v2.common import JobStatus
from app.schemas.internal_v2.turn import (
    TurnBaseDTO,
    TurnCursorDTO,
    TurnDetailBatchRequest,
    TurnSummaryDTO,
    TurnUserMessageSummaryDTO,
)


def _turn_fields() -> dict[str, object]:
    now = datetime.now(UTC)
    return {
        "turn_id": "job_1",
        "job_id": "job_1",
        "session_id": "session_1",
        "ordinal": 1,
        "revision": 1,
        "status": JobStatus.completed,
        "source_message_ids": ["message_1"],
        "merged_job_ids": [],
        "created_at": now,
        "updated_at": now,
    }


def test_turn_summary_has_stable_job_identity() -> None:
    summary = TurnSummaryDTO.model_validate(_turn_fields())

    assert summary.turn_id == summary.job_id
    assert summary.items_view == "summary"


def test_turn_rejects_different_turn_and_job_identity() -> None:
    values = _turn_fields()
    values["turn_id"] = "turn_other"

    with pytest.raises(ValidationError, match="Turn ID 必须等于实际执行 Job ID"):
        TurnBaseDTO.model_validate(values)


def test_turn_detail_batch_is_bounded_and_unique() -> None:
    with pytest.raises(ValidationError):
        TurnDetailBatchRequest(turn_ids=[f"job_{index}" for index in range(5)])
    with pytest.raises(ValidationError, match="turn_ids 不能重复"):
        TurnDetailBatchRequest(turn_ids=["job_1", "job_1"])


def test_turn_cursor_is_strictly_older_direction() -> None:
    cursor = TurnCursorDTO(
        session_id="session_1",
        projection_epoch=2,
        anchor_turn_id="job_2",
    )

    assert cursor.direction == "older"
    assert cursor.include_anchor is False


def test_turn_summary_rejects_unbounded_preview_and_lists() -> None:
    fields = _turn_fields()
    fields.update(
        {
            "response_preview": "x" * 1001,
            "source_message_ids": [f"message_{index}" for index in range(33)],
        }
    )

    with pytest.raises(ValidationError):
        TurnSummaryDTO.model_validate(fields)


def test_user_message_summary_has_no_content_metadata_or_data_url() -> None:
    message = TurnUserMessageSummaryDTO(
        message_id="message_1",
        preview="x" * 500,
        content_truncated=True,
        attachment_count=1,
        created_at=datetime.now(UTC),
    )

    payload = message.model_dump(mode="json")
    assert "content" not in payload
    assert "metadata" not in payload
    assert "attachments" not in payload
    assert "data_url" not in str(payload)
