"""可见消息历史的分页与游标业务规则，不持有正文或存储连接。"""

from __future__ import annotations

import base64
import json
from collections.abc import Sequence

from app.schemas.internal_v2.common import CursorPage, MessageRole
from app.schemas.internal_v2.message import MessageDTO


def _encode_cursor(session_id: str, checkpoint_id: str, before: int) -> str:
    payload = json.dumps(
        {"session_id": session_id, "checkpoint_id": checkpoint_id, "before": before},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str, session_id: str, checkpoint_id: str) -> int:
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = json.loads(
            base64.b64decode(cursor + padding, altchars=b"-_", validate=True)
        )
    except ValueError as error:
        raise ValueError("消息历史游标格式无效") from error
    if not isinstance(payload, dict):
        raise TypeError("消息历史游标内容无效")
    if (
        payload.get("session_id") != session_id
        or payload.get("checkpoint_id") != checkpoint_id
    ):
        raise ValueError("消息历史已更新，请重新加载最新消息")
    before = payload.get("before")
    if isinstance(before, bool) or not isinstance(before, int):
        raise TypeError("消息历史游标缺少 before")
    return before


def visible_message_page(
    messages: Sequence[MessageDTO],
    *,
    session_id: str,
    checkpoint_id: str,
    limit: int,
    cursor: str | None,
) -> CursorPage[MessageDTO]:
    """在同一 checkpoint 的可见投影中分页，游标不能跨会话或版本复用。"""
    if not checkpoint_id:
        raise RuntimeError(f"checkpoint 缺少有效 id: session_id={session_id}")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("消息分页 limit 必须为正整数")
    end = (
        len(messages)
        if cursor is None
        else _decode_cursor(cursor, session_id, checkpoint_id)
    )
    if end < 0 or end > len(messages):
        raise ValueError("消息历史游标位置无效")
    start = max(0, end - limit)
    # 输入已经经过可见性投影，内部提醒不在此列表中；避免留下孤立回复。
    while start > 0 and messages[start].role != MessageRole.user:
        start -= 1
    return CursorPage(
        items=list(messages[start:end]),
        next_cursor=_encode_cursor(session_id, checkpoint_id, start) if start > 0 else None,
        has_more=start > 0,
    )
