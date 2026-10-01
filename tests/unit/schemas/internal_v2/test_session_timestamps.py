"""钉死 Session 中断/压缩结果 DTO 的默认时间戳必须带时区。"""
from __future__ import annotations

from app.schemas.internal_v2.session import (
    SessionCompactResultDTO,
    SessionInterruptResultDTO,
)


def test_interrupt_result_default_timestamp_is_timezone_aware() -> None:
    """缺省也不得回退到 naive：事件时间戳必须含时区（对齐 event schema 不变式）。"""
    result = SessionInterruptResultDTO(
        session_id="s",
        job_id="j",
        status="cancelling",
        interrupt_request_id="i",
        phase="text",
    )
    assert result.interrupted_at.tzinfo is not None
    assert result.model_dump_json().endswith('Z"}')


def test_compact_result_default_timestamp_is_timezone_aware() -> None:
    result = SessionCompactResultDTO(
        session_id="s",
        status="skipped",
        message="m",
        before_message_count=0,
        effective_message_count_before=0,
        effective_message_count_after=0,
        summarized_message_count=0,
        retained_message_count=0,
    )
    assert result.compacted_at.tzinfo is not None
    assert result.model_dump_json().endswith('Z"}')
