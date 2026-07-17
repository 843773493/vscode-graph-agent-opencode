from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from app.core.exceptions import NotFoundError
from app.core.identifier import create_prefixed_id
from app.core.path_utils import get_session_file, ensure_session_dir, get_session_path, get_sessions_dir
from app.schemas.public_v2.session import (
    DeleteSessionResultDTO,
    SessionControlResultDTO,
    SessionCreateRequest,
    SessionDelegationDTO,
    SessionDTO,
    SessionListResultDTO,
    SessionKind,
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
                    with open(session_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    sessions.append(SessionDTO.model_validate(data))

        sessions.sort(key=lambda s: s.created_at, reverse=True)
        paginated = sessions[skip:skip+limit]

        return SessionListResultDTO(items=paginated, total=len(sessions), cursor=None)

    async def create(self, session: SessionCreateRequest) -> SessionDTO:
        return await self._create(
            title=session.title,
            title_source=session.title_source,
            agent_id=session.agent_id,
        )

    async def create_context_fork(
        self,
        *,
        title: str,
        agent_id: str,
        parent_session_id: str,
    ) -> SessionDTO:
        return await self._create(
            title=title,
            title_source="auto",
            agent_id=agent_id,
            parent_session_id=parent_session_id,
            kind="context_fork",
        )

    async def create_delegated(
        self,
        *,
        title: str,
        agent_id: str,
        parent_session_id: str,
        parent_job_id: str,
        parent_tool_call_id: str,
        subagent_type: str,
    ) -> SessionDTO:
        return await self._create(
            title=title,
            title_source="auto",
            agent_id=agent_id,
            parent_session_id=parent_session_id,
            kind="delegated",
            delegation=SessionDelegationDTO(
                parent_session_id=parent_session_id,
                parent_job_id=parent_job_id,
                parent_tool_call_id=parent_tool_call_id,
                subagent_type=subagent_type,
            ),
        )

    async def _create(
        self,
        *,
        title: str | None,
        title_source: TitleSource | None,
        agent_id: str | None,
        parent_session_id: str | None = None,
        kind: SessionKind = "normal",
        delegation: SessionDelegationDTO | None = None,
    ) -> SessionDTO:
        session_id = create_prefixed_id("ses")
        now = datetime.now(timezone.utc)
        if self._config_service is None:
            raise RuntimeError("SessionService 未绑定 ConfigService")
        config_service = self._config_service
        resolved_agent_id = config_service.validate_agent_id(agent_id)
        await self._validate_parent_session(
            session_id=session_id,
            workspace_id="ws_local",
            parent_session_id=parent_session_id,
        )

        session_data = SessionDTO(
            session_id=session_id,
            workspace_id="ws_local",
            title=title or "新会话",
            title_source=self._infer_created_title_source(
                title,
                title_source,
            ),
            current_agent_id=resolved_agent_id,
            parent_session_id=parent_session_id,
            kind=kind,
            delegation=delegation,
            created_at=now,
            updated_at=now
        )

        ensure_session_dir(session_id)
        session_file = get_session_file(session_id)

        with open(session_file, "w", encoding="utf-8") as f:
            json.dump(session_data.model_dump(), f, ensure_ascii=False, indent=2, default=str)

        return session_data

    async def set_delegation_start_result(
        self,
        session_id: str,
        *,
        status: str,
        error: str | None = None,
    ) -> SessionDTO:
        if status not in {"running", "failed"}:
            raise ValueError(f"不支持的委派启动状态: {status}")
        existing = await self.get(session_id)
        if existing.delegation is None:
            raise ValueError(f"会话不是委派子会话: {session_id}")
        existing.delegation.start_status = status
        existing.delegation.start_error = error
        existing.updated_at = datetime.now(timezone.utc)
        session_file = get_session_file(session_id)
        with open(session_file, "w", encoding="utf-8") as f:
            json.dump(existing.model_dump(), f, ensure_ascii=False, indent=2, default=str)
        return existing

    async def update(self, session_id: str, session: SessionUpdateRequest) -> SessionDTO:
        existing = await self.get(session_id)

        if session.agent_id is not None:
            if self._config_service is None:
                raise RuntimeError("SessionService 未绑定 ConfigService")
            self._config_service.validate_agent_id(session.agent_id)

        update_data = session.model_dump(exclude_unset=True)

        if "parent_session_id" in update_data:
            requested_parent_id = update_data["parent_session_id"]
            if (
                existing.kind != "normal"
                and requested_parent_id is not None
                and requested_parent_id != existing.parent_session_id
            ):
                raise ValueError(
                    f"{existing.kind} 会话不能改绑到另一个父会话；请新建会话"
                )
            await self._validate_parent_session(
                session_id=session_id,
                workspace_id=existing.workspace_id,
                parent_session_id=requested_parent_id,
            )
            if requested_parent_id is None and existing.kind == "context_fork":
                update_data["kind"] = "normal"

        for key, value in update_data.items():
            if key == "agent_id":
                existing.current_agent_id = value
            elif key == "title_source":
                existing.title_source = value
            else:
                setattr(existing, key, value)

        if "title" in update_data and "title_source" not in update_data:
            existing.title_source = "user"

        existing.updated_at = datetime.now(timezone.utc)

        session_file = get_session_file(session_id)
        with open(session_file, "w", encoding="utf-8") as f:
            json.dump(existing.model_dump(), f, ensure_ascii=False, indent=2, default=str)

        return existing

    async def _validate_parent_session(
        self,
        *,
        session_id: str,
        workspace_id: str,
        parent_session_id: str | None,
    ) -> None:
        if parent_session_id is None:
            return
        if parent_session_id == session_id:
            raise ValueError("会话不能绑定到自身")

        ancestor_id: str | None = parent_session_id
        visited: set[str] = set()
        while ancestor_id is not None:
            if ancestor_id == session_id:
                raise ValueError("会话绑定会形成循环父子关系")
            if ancestor_id in visited:
                raise RuntimeError(f"现有会话树包含循环关系: session_id={ancestor_id}")
            visited.add(ancestor_id)
            try:
                ancestor = await self.get(ancestor_id)
            except NotFoundError as exc:
                raise ValueError(f"父会话不存在: {ancestor_id}") from exc
            if ancestor.workspace_id != workspace_id:
                raise ValueError("父子会话必须属于同一个工作区")
            ancestor_id = ancestor.parent_session_id

    async def delete(self, session_id: str) -> DeleteSessionResultDTO:
        session_dir = get_session_path(session_id)

        if not session_dir.exists():
            raise NotFoundError(f"Session {session_id} not found")

        sessions = await self.list(limit=100_000)
        direct_children = [
            session
            for session in sessions.items
            if session.parent_session_id == session_id
        ]
        for child in direct_children:
            await self.update(
                child.session_id,
                SessionUpdateRequest(parent_session_id=None),
            )

        import shutil
        shutil.rmtree(session_dir)
        return DeleteSessionResultDTO(session_id=session_id, status="deleted")

    async def control(self, session_id: str, action: str, payload: dict = None) -> SessionControlResultDTO:
        await self.get(session_id)
        return SessionControlResultDTO(session_id=session_id, action=action, status="executed")

    async def list_trace_events(
        self,
        session_id: str,
        after_event_id: str | None = None,
    ) -> list[TraceEventDTO]:
        await self.get(session_id)
        events = self._trace_event_store.read_events(session_id, after_event_id)
        mapper = TraceEventMapper()
        return mapper.map_many([event.model_dump() for event in events], session_id=session_id)

    async def ensure_trace_cursor(self, session_id: str, after_event_id: str | None) -> None:
        await self.get(session_id)
        self._trace_event_store.ensure_cursor(session_id, after_event_id)

    async def stream_trace_events(self, session_id: str, after_event_id: str | None = None):
        await self.get(session_id)
        mapper = TraceEventMapper()
        async for event in self._trace_event_store.stream_events(session_id, after_event_id):
            dto = mapper.map_one(event.model_dump(), session_id=session_id)
            if dto is not None:
                yield dto
