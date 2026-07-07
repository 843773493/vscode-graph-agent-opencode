from __future__ import annotations

import re

from app.abstractions.job_event_bus import JobEventBusProtocol
from app.core.job_event_bus import EventType
from app.schemas.public_v2.session import SessionUpdateRequest
from app.services.business.session_service import SessionService

DEFAULT_SESSION_TITLES = {"", "新会话", "未命名"}
TITLE_MAX_WORDS = 8
TITLE_MAX_CHARS = 80
TITLE_TOKEN_RE = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]|[A-Za-z0-9]+(?:[._/-][A-Za-z0-9]+)*",
    re.UNICODE,
)


def build_session_title_from_first_message(user_message: str) -> str:
    normalized = re.sub(r"\s+", " ", user_message).strip()
    if not normalized:
        raise RuntimeError("首条用户消息为空，无法自动命名会话")

    tokens = list(TITLE_TOKEN_RE.finditer(normalized))
    if len(tokens) > TITLE_MAX_WORDS:
        normalized = normalized[: tokens[TITLE_MAX_WORDS - 1].end()].strip()

    return normalize_session_title(normalized)


def normalize_session_title(raw_title: str) -> str:
    title = raw_title.splitlines()[0].strip() if raw_title else ""
    title = title.strip(" \t\r\n\"'`“”‘’《》「」[]()（）")
    title = re.sub(r"\s+", " ", title).strip()
    title = title.rstrip("。.!！?？；;，,")
    if not title:
        raise RuntimeError("首条用户消息无法生成有效会话标题")
    if len(title) > TITLE_MAX_CHARS:
        title = title[:TITLE_MAX_CHARS].rstrip()
    return title


class SessionTitleService:
    def __init__(
        self,
        *,
        session_service: SessionService,
        job_event_bus: JobEventBusProtocol,
    ) -> None:
        self._session_service = session_service
        self._bus = job_event_bus

    async def maybe_auto_title_before_first_message(
        self,
        *,
        session_id: str,
        job_id: str,
        user_message: str,
    ) -> str | None:
        session = await self._session_service.get(session_id)
        if session.title_source != "default":
            return None
        if session.title.strip() not in DEFAULT_SESSION_TITLES:
            return None

        title = build_session_title_from_first_message(user_message)
        updated = await self._session_service.update(
            session_id,
            SessionUpdateRequest(title=title, title_source="auto"),
        )
        await self._bus.publish(
            job_id=job_id,
            event_type=EventType.STATUS_CHANGE,
            payload={
                "session_id": session_id,
                "status": f"已自动命名会话: {updated.title}",
                "reason": "session_auto_title_updated",
                "title": updated.title,
            },
            agent_id="session_title_service",
        )
        return updated.title
