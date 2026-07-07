from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from app.core.exceptions import NotFoundError
from app.core.path_utils import get_session_file, ensure_session_dir, get_session_path, get_sessions_dir
from app.schemas.event import Event
from app.schemas.public_v2.session import (
    DeleteSessionResultDTO,
    SessionControlResultDTO,
    SessionCreateRequest,
    SessionDTO,
    SessionListResultDTO,
    SessionUpdateRequest,
    TitleSource,
)
from app.schemas.public_v2.trace import TraceEventDTO
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.trace_event_store import TraceEventStore
from app.services.mapping.trace_event_mapper import TraceEventMapper


class SessionService:
    DEFAULT_SESSION_TITLES = {"", "新会话", "未命名"}

    def __init__(self, *, config_service: ConfigService, trace_event_store: TraceEventStore):
        self._config_service = config_service
        self._trace_event_store = trace_event_store

    @classmethod
    def _infer_created_title_source(
        cls,
        title: str | None,
        explicit_source: TitleSource | None,
    ) -> TitleSource:
        if explicit_source is not None:
            return explicit_source
        if (title or "").strip() in cls.DEFAULT_SESSION_TITLES:
            return "default"
        return "user"

    async def get(self, session_id: str) -> SessionDTO:
        session_file = get_session_file(session_id)

        if not session_file.exists():
            raise NotFoundError(f"Session {session_id} not found")

        with open(session_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        return SessionDTO.model_validate(data)

    async def list(self, workspace_id: Optional[str] = None, skip: int = 0, limit: int = 100, cursor: Optional[str] = None) -> SessionListResultDTO:
        sessions_dir = get_sessions_dir()
        sessions_dir.mkdir(exist_ok=True)

        sessions = []
        for session_dir in sessions_dir.iterdir():
            if session_dir.is_dir():
                session_file = session_dir / "session.json"
                if session_file.exists():
                    try:
                        with open(session_file, "r", encoding="utf-8") as f:
                            data = json.load(f)
                            sessions.append(SessionDTO.model_validate(data))
                    except Exception:
                        continue

        sessions.sort(key=lambda s: s.created_at, reverse=True)
        paginated = sessions[skip:skip+limit]

        return SessionListResultDTO(items=paginated, total=len(sessions), cursor=None)

    async def create(self, session: SessionCreateRequest) -> SessionDTO:
        session_id = f"ses_{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc)
        if self._config_service is None:
            raise RuntimeError("SessionService 未绑定 ConfigService")
        config_service = self._config_service
        resolved_agent_id = config_service.validate_agent_id(session.agent_id)

        session_data = SessionDTO(
            session_id=session_id,
            workspace_id="ws_local",
            title=session.title,
            title_source=self._infer_created_title_source(
                session.title,
                session.title_source,
            ),
            current_agent_id=resolved_agent_id,
            created_at=now,
            updated_at=now
        )

        ensure_session_dir(session_id)
        session_file = get_session_file(session_id)

        with open(session_file, "w", encoding="utf-8") as f:
            json.dump(session_data.model_dump(), f, ensure_ascii=False, indent=2, default=str)

        return session_data

    async def update(self, session_id: str, session: SessionUpdateRequest) -> SessionDTO:
        existing = await self.get(session_id)

        if session.agent_id is not None:
            if self._config_service is None:
                raise RuntimeError("SessionService 未绑定 ConfigService")
            self._config_service.validate_agent_id(session.agent_id)

        update_data = session.model_dump(exclude_unset=True)

        old_agent_id = existing.current_agent_id
        new_agent_id = update_data.get("agent_id")
        agent_id_changed = new_agent_id is not None and new_agent_id != old_agent_id

        for key, value in update_data.items():
            if key == "agent_id":
                existing.current_agent_id = value
            elif key == "title_source":
                existing.title_source = value
            else:
                setattr(existing, key, value)

        if "title" in update_data and "title_source" not in update_data:
            existing.title_source = "user"

        if agent_id_changed:
            try:
                pass
            except Exception:
                pass

        import time
        time.sleep(0.001)
        existing.updated_at = datetime.now(timezone.utc)

        session_file = get_session_file(session_id)
        with open(session_file, "w", encoding="utf-8") as f:
            json.dump(existing.model_dump(), f, ensure_ascii=False, indent=2, default=str)

        return existing

    async def delete(self, session_id: str) -> DeleteSessionResultDTO:
        session_dir = get_session_path(session_id)

        if not session_dir.exists():
            raise NotFoundError(f"Session {session_id} not found")

        import shutil
        shutil.rmtree(session_dir)
        return DeleteSessionResultDTO(session_id=session_id, status="deleted")

    async def control(self, session_id: str, action: str, payload: dict = None) -> SessionControlResultDTO:
        await self.get(session_id)
        return SessionControlResultDTO(session_id=session_id, action=action, status="executed")

    async def list_trace_events(self, session_id: str) -> list[TraceEventDTO]:
        await self.get(session_id)
        events = self._trace_event_store.read_events(session_id)
        mapper = TraceEventMapper()
        return mapper.map_many([event.model_dump() for event in events], session_id=session_id)

    async def stream_trace_events(self, session_id: str):
        await self.get(session_id)
        mapper = TraceEventMapper()
        async for event in self._trace_event_store.stream_events(session_id):
            dto = mapper.map_one(event.model_dump(), session_id=session_id)
            if dto is not None:
                yield dto
