"""可见消息分页游标必须绑定实际 thread 身份（OpenSpec 8.6）。"""

from __future__ import annotations

import pytest

from app.schemas.internal_v2.common import MessageRole
from app.schemas.internal_v2.message import MessageDTO
from app.services.business.session_turn_history.visible_page import visible_message_page

SESSION_ID = "ses_2f5c1a8b0d7e44e2a91c3d5f6007b8a4"
CHECKPOINT_ID = "ckpt_visible_page_thread_identity"
MESSAGE_TIME = "2026-07-14T00:00:00+00:00"


def _message(message_id: str, thread_id: str) -> MessageDTO:
    return MessageDTO(
        message_id=message_id,
        session_id=SESSION_ID,
        thread_id=thread_id,
        role=MessageRole.user,
        content=message_id,
        created_at=MESSAGE_TIME,
        updated_at=MESSAGE_TIME,
    )


def _page(thread_id: str, *, limit: int = 1, cursor: str | None = None):
    messages = [
        _message("msg_1", thread_id),
        _message("msg_2", thread_id),
    ]
    return visible_message_page(
        messages,
        session_id=SESSION_ID,
        thread_id=thread_id,
        checkpoint_id=CHECKPOINT_ID,
        limit=limit,
        cursor=cursor,
    )


def test_cursor_round_trips_with_same_thread_identity() -> None:
    first = _page("thr_main0000000000000000000000001")
    assert first.has_more is True
    assert first.next_cursor is not None

    second = _page(
        "thr_main0000000000000000000000001",
        cursor=first.next_cursor,
    )
    assert [item.message_id for item in second.items] == ["msg_1"]
    assert second.has_more is False


def test_cursor_from_sibling_thread_is_rejected() -> None:
    issued = _page("thr_main0000000000000000000000001")
    assert issued.next_cursor is not None

    # 同一 session 的 sibling thread 不得复用该游标：cursor 必须校验实际 thread。
    with pytest.raises(ValueError, match="消息历史已更新"):
        _page(
            "thr_child000000000000000000000002",
            cursor=issued.next_cursor,
        )


def test_missing_thread_identity_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="缺少权威 thread 身份"):
        visible_message_page(
            [_message("msg_1", "thr_main0000000000000000000000001")],
            session_id=SESSION_ID,
            thread_id="",
            checkpoint_id=CHECKPOINT_ID,
            limit=10,
            cursor=None,
        )
