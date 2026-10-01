"""钉死事件 payload 的默认时间戳必须带时区。"""
from __future__ import annotations

from app.schemas.event import SessionInterruptedPayload


def test_session_interrupted_payload_default_timestamp_is_timezone_aware() -> None:
    """缺省也不得回退到 naive：事件时间戳必须含时区（BaseEvent 同款不变式）。"""
    payload = SessionInterruptedPayload(session_id="s", phase="text")
    assert payload.interrupted_at.tzinfo is not None
    assert payload.model_dump_json().count('Z"') == 1
