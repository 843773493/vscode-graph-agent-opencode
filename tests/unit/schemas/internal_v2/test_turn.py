from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.schemas.internal_v2.common import JobStatus
from app.schemas.internal_v2.turn import (
    InternalExecutionDisplayPartDTO,
    InternalExecutionHistoryDTO,
    TurnBaseDTO,
    TurnCursorDTO,
    TurnDetailBatchRequest,
    TurnDetailDTO,
    TurnHistoryPageDTO,
    TurnSummaryDTO,
    TurnThinkingBlockDTO,
    TurnUserMessageSummaryDTO,
)


def _turn_fields() -> dict[str, object]:
    now = datetime.now(UTC)
    return {
        "turn_id": "job_1",
        "job_id": "job_1",
        "session_id": "session_1",
        "ordinal": 1,
        "root_logical_item_ordinal": 0,
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


def test_turn_cursor_freezes_owner_and_both_ordinal_lanes() -> None:
    cursor = TurnCursorDTO(
        session_id="session_1",
        thread_id="thread_1",
        checkpoint_ns="",
        rollout_id="rollout_1",
        projection_epoch=2,
        view_id="view_1",
        history_view_revision=0,
        turn_read_upper_bound=3,
        logical_item_read_upper_bound=0,
        direction="before",
        query_mode="linear",
        effective_include=("internal",),
        turn_ordinal=2,
        logical_item_ordinal=0,
    )

    assert cursor.direction == "before"
    assert cursor.turn_ordinal == 2
    assert cursor.logical_item_ordinal == 0


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


def test_turn_details_preserve_more_than_32_thinking_blocks() -> None:
    fields = _turn_fields()
    blocks = [
        TurnThinkingBlockDTO(kind="summary", text=f"block-{index}")
        for index in range(33)
    ]

    summary = TurnSummaryDTO.model_validate({**fields, "thinking_blocks": blocks})
    detail = TurnDetailDTO.model_validate({**fields, "thinking_blocks": blocks})

    assert len(summary.thinking_blocks) == 33
    assert len(detail.thinking_blocks) == 33


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


def test_internal_execution_history_exposes_only_typed_display_projection() -> None:
    now = datetime.now(UTC)
    display_part = InternalExecutionDisplayPartDTO(
        kind="generated_session_result",
        text="生成会话已返回结果。",
        item_id="item_return",
        item_sequence=12,
        logical_item_ordinal=3,
        created_at=now,
        provenance={"source_session_id": "session_generated"},
    )
    execution = InternalExecutionHistoryDTO(
        execution_id="execution_1",
        job_id="job_1",
        session_id="session_main",
        thread_id="thread_main",
        status="completed",
        created_at=now,
        updated_at=now,
        display_parts=[display_part],
    )

    payload = execution.model_dump(mode="json")
    assert set(payload) == {
        "execution_id",
        "job_id",
        "session_id",
        "thread_id",
        "status",
        "created_at",
        "updated_at",
        "display_parts",
        "response_parts",
    }
    assert payload["display_parts"] == [
        {
            "kind": "generated_session_result",
            "text": "生成会话已返回结果。",
            "item_id": "item_return",
            "item_sequence": 12,
            "logical_item_ordinal": 3,
            "created_at": now.isoformat().replace("+00:00", "Z"),
            "provenance": {
                "source_session_id": "session_generated",
                "source_thread_id": None,
                "delegation_id": None,
                "source_execution_id": None,
                "source_item_id": None,
            },
        }
    ]
    assert "metadata" not in payload
    assert "prompt" not in payload
    assert "raw_record" not in payload

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        InternalExecutionHistoryDTO(
            execution_id="execution_1",
            job_id="job_1",
            session_id="session_main",
            thread_id="thread_main",
            logical_item_ordinal=3,
            status="completed",
            created_at=now,
            updated_at=now,
            display_parts=[display_part],
        )


def test_internal_execution_history_rejects_control_payload_and_duplicate_item_ids() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        InternalExecutionHistoryDTO(
            execution_id="execution_1",
            job_id="job_1",
            session_id="session_main",
            thread_id="thread_main",
            status="completed",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            display_parts=[],
            metadata={"branch_result": "private branch body"},
        )

    display_part = {
        "kind": "generated_session_result",
        "text": "委派任务已完成。",
        "item_id": "item_duplicate",
        "item_sequence": 10,
        "logical_item_ordinal": 4,
        "created_at": datetime.now(UTC),
        "provenance": {},
    }
    entry = {
        "execution_id": "execution_1",
        "job_id": "job_1",
        "session_id": "session_main",
        "thread_id": "thread_main",
        "status": "completed",
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
        "display_parts": [display_part],
    }
    with pytest.raises(ValidationError, match="display item_id 不能重复"):
        TurnHistoryPageDTO(
            items=[],
            projection_epoch=1,
            internal_executions=[entry, {**entry, "execution_id": "execution_2"}],
        )
