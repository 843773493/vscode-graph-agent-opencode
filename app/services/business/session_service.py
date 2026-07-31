from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from app.abstractions.job_service import JobServiceProtocol
from app.core.exceptions import NotFoundError
from app.core.identifier import create_prefixed_id
from app.core.path_utils import get_session_path_resolver
from app.core.session_paths import SessionPathResolver, SessionPhysicalNode
from app.schemas.public_v2.common import CursorPage
from app.schemas.public_v2.session import (
    DeleteSessionResultDTO,
    SessionControlResultDTO,
    SessionCreateRequest,
    SessionDelegationDTO,
    SessionDTO,
    SessionGenerationOriginDTO,
    SessionKind,
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

    def __init__(
        self,
        *,
        config_service: ConfigService,
        trace_event_store: TraceEventStore,
        path_resolver: SessionPathResolver | None = None,
    ):
        self._config_service = config_service
        self._trace_event_store = trace_event_store
        self._path_resolver = path_resolver or get_session_path_resolver()
        self._path_resolver.initialize()
        self._job_service: JobServiceProtocol | None = None
        self._change_listeners: list[Callable[[str, str], None]] = []

    @property
    def path_resolver(self) -> SessionPathResolver:
        return self._path_resolver

    def register_change_listener(self, listener: Callable[[str, str], None]) -> None:
        self._change_listeners.append(listener)

    def bind_job_service(self, job_service: JobServiceProtocol) -> None:
        self._job_service = job_service

    def _notify_changed(self, action: str, session_id: str) -> None:
        for listener in tuple(self._change_listeners):
            listener(action, session_id)

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
        try:
            session_file = (
                self._path_resolver.resolve_session_node(session_id) / "session.json"
            )
        except KeyError as error:
            raise NotFoundError(f"Session {session_id} not found") from error
        if not session_file.is_file():
            raise NotFoundError(f"Session {session_id} not found")

        with open(session_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        session = SessionDTO.model_validate(data)
        if session.current_provider_id is None:
            session.current_provider_id = (
                self._config_service.resolve_agent_provider_id(session.current_agent_id)
            )
        return session

    async def list(
        self,
        workspace_id: str | None = None,
        skip: int = 0,
        limit: int = 100,
        cursor: str | None = None,
    ) -> SessionListResultDTO:
        sessions = []
        for node in self._path_resolver.list_nodes():
            if node.kind != "session":
                continue
            session_file = node.path / "session.json"
            with open(session_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            session = SessionDTO.model_validate(data)
            if session.current_provider_id is None:
                session.current_provider_id = (
                    self._config_service.resolve_agent_provider_id(
                        session.current_agent_id
                    )
                )
            sessions.append(session)

        sessions.sort(key=lambda s: s.created_at, reverse=True)
        paginated = sessions[skip : skip + limit]

        return SessionListResultDTO(items=paginated, total=len(sessions), cursor=None)

    async def create(self, session: SessionCreateRequest) -> SessionDTO:
        return await self._create(
            title=session.title,
            title_source=session.title_source,
            agent_id=session.agent_id,
            parent_node_id=session.folder_id,
        )

    async def create_context_fork(
        self,
        *,
        title: str,
        agent_id: str,
        parent_session_id: str | None,
        context_source_session_id: str,
        generation_origin: SessionGenerationOriginDTO | None = None,
        parent_node_id: str | None = None,
    ) -> SessionDTO:
        return await self._create(
            title=title,
            title_source="auto",
            agent_id=agent_id,
            parent_session_id=parent_session_id,
            context_source_session_id=context_source_session_id,
            kind="context_fork",
            generation_origin=generation_origin,
            parent_node_id=parent_node_id,
        )

    async def create_generated(
        self,
        *,
        title: str,
        agent_id: str | None,
        parent_session_id: str | None,
        generation_origin: SessionGenerationOriginDTO,
        parent_node_id: str | None = None,
    ) -> SessionDTO:
        return await self._create(
            title=title,
            title_source="auto",
            agent_id=agent_id,
            parent_session_id=parent_session_id,
            generation_origin=generation_origin,
            parent_node_id=parent_node_id,
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
        context_source_session_id: str | None = None,
        kind: SessionKind = "normal",
        delegation: SessionDelegationDTO | None = None,
        generation_origin: SessionGenerationOriginDTO | None = None,
        parent_node_id: str | None = None,
    ) -> SessionDTO:
        session_id = create_prefixed_id("ses")
        now = datetime.now(UTC)
        if self._config_service is None:
            raise RuntimeError("SessionService 未绑定 ConfigService")
        config_service = self._config_service
        resolved_agent_id = config_service.resolve_new_session_agent_id(agent_id)
        resolved_provider_id = config_service.resolve_new_session_provider_id(
            resolved_agent_id
        )
        await self._validate_parent_session(
            session_id=session_id,
            workspace_id="ws_local",
            parent_session_id=parent_session_id,
        )

        resolved_parent_node_id = parent_node_id
        physical_parent_session_id = self._path_resolver.nearest_session_ancestor(
            parent_node_id
        )
        if parent_session_id is not None:
            if parent_node_id is None:
                resolved_parent_node_id = parent_session_id
                physical_parent_session_id = parent_session_id
            elif physical_parent_session_id != parent_session_id:
                raise ValueError(
                    "会话父节点与目标物理目录不一致: "
                    f"parent_session_id={parent_session_id}, "
                    f"physical_parent_session_id={physical_parent_session_id}"
                )
        else:
            parent_session_id = physical_parent_session_id

        session_data = SessionDTO(
            session_id=session_id,
            workspace_id="ws_local",
            title=title or "新会话",
            title_source=self._infer_created_title_source(
                title,
                title_source,
            ),
            current_agent_id=resolved_agent_id,
            current_provider_id=resolved_provider_id,
            parent_session_id=parent_session_id,
            context_source_session_id=context_source_session_id,
            kind=kind,
            delegation=delegation,
            generation_origin=generation_origin,
            created_at=now,
            updated_at=now,
        )

        session_dir = self._path_resolver.allocate_session_dir(
            session_id=session_id,
            title=session_data.title,
            parent_node_id=resolved_parent_node_id,
        )
        session_file = session_dir / "session.json"
        try:
            self._write_session_file(session_file, session_data)
            self._path_resolver.register_session(session_id, session_dir)
        except Exception:
            self._path_resolver.abandon_session_allocation(session_dir)
            raise

        self._notify_changed("create", session_id)
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
        existing.updated_at = datetime.now(UTC)
        session_file = (
            self._path_resolver.resolve_session_node(session_id) / "session.json"
        )
        self._write_session_file(session_file, existing)
        self._notify_changed("update", session_id)
        return existing

    async def update(
        self, session_id: str, session: SessionUpdateRequest
    ) -> SessionDTO:
        existing = await self.get(session_id)

        if session.agent_id is not None:
            if self._config_service is None:
                raise RuntimeError("SessionService 未绑定 ConfigService")
            self._config_service.validate_agent_id(session.agent_id)

        update_data = session.model_dump(exclude_unset=True)
        target_agent_id = update_data.get("agent_id", existing.current_agent_id)
        requested_provider_id = update_data.get("provider_id")
        if "agent_id" in update_data and "provider_id" not in update_data:
            requested_provider_id = None
        if "agent_id" in update_data or "provider_id" in update_data:
            update_data["current_provider_id"] = (
                self._config_service.resolve_agent_provider_id(
                    target_agent_id,
                    requested_provider_id,
                )
            )
        update_data.pop("provider_id", None)

        for key, value in update_data.items():
            if key == "agent_id":
                existing.current_agent_id = value
            elif key == "title_source":
                existing.title_source = value
            else:
                setattr(existing, key, value)

        if "title" in update_data and "title_source" not in update_data:
            existing.title_source = "user"

        existing.updated_at = datetime.now(UTC)

        session_dir = self._path_resolver.resolve_session_node(session_id)
        self._write_session_file(session_dir / "session.json", existing)
        self._path_resolver.update_node_name(session_id, existing.title)
        self._notify_changed("update", session_id)
        return existing

    async def move_session(
        self,
        session_id: str,
        parent_node_id: str | None,
    ) -> SessionDTO:
        """显式移动会话物理子树，并同步最近物理父会话。"""
        existing = await self.get(session_id)
        if parent_node_id is not None:
            self._path_resolver.get_node(parent_node_id)
        target_parent_session_id = self._path_resolver.nearest_session_ancestor(
            parent_node_id
        )
        await self._validate_parent_session(
            session_id=session_id,
            workspace_id=existing.workspace_id,
            parent_session_id=target_parent_session_id,
        )
        if (
            existing.kind != "normal"
            and target_parent_session_id is not None
            and target_parent_session_id != existing.parent_session_id
        ):
            raise ValueError(f"{existing.kind} 会话不能移动到另一个父会话的目录下")
        if existing.kind == "context_fork" and target_parent_session_id is None:
            existing.kind = "normal"
        existing.parent_session_id = target_parent_session_id
        existing.updated_at = datetime.now(UTC)

        async def move() -> None:
            self._path_resolver.relocate_session(
                session_id=session_id,
                parent_node_id=parent_node_id,
                manifest=existing.model_dump(mode="json"),
            )

        affected_session_ids = self._path_resolver.descendant_session_ids(
            session_id,
            include_self=True,
        )
        if self._job_service is None:
            await move()
        else:
            await self._job_service.run_sessions_idle_operation(
                affected_session_ids,
                move,
            )
        self._notify_changed("update", session_id)
        return existing

    async def move_to_folder(
        self,
        session_id: str,
        folder_id: str | None,
    ) -> SessionDTO:
        if folder_id is not None:
            folder = self._path_resolver.get_node(folder_id)
            if folder.kind != "folder":
                raise ValueError(f"目标节点不是会话文件夹: {folder_id}")
        return await self.move_session(session_id, folder_id)

    async def relocate_folder_tree(
        self,
        *,
        folder_id: str,
        parent_node_id: str | None,
        name: str,
    ) -> SessionPhysicalNode:
        """准备文件夹子树中的会话父关系，再交给 resolver 原子移动。"""
        expected_parents = (
            self._path_resolver.expected_session_parents_after_folder_move(
                folder_id=folder_id,
                parent_node_id=parent_node_id,
            )
        )
        manifests: dict[str, dict[str, object]] = {}
        changed_session_ids: list[str] = []
        for session_id, expected_parent_id in expected_parents.items():
            existing = await self.get(session_id)
            await self._validate_parent_session(
                session_id=session_id,
                workspace_id=existing.workspace_id,
                parent_session_id=expected_parent_id,
            )
            if (
                existing.kind != "normal"
                and expected_parent_id is not None
                and expected_parent_id != existing.parent_session_id
            ):
                raise ValueError(
                    f"{existing.kind} 会话不能随文件夹改绑到另一个父会话: "
                    f"session_id={session_id}"
                )
            if existing.parent_session_id != expected_parent_id:
                existing.parent_session_id = expected_parent_id
                existing.updated_at = datetime.now(UTC)
                changed_session_ids.append(session_id)
            if existing.kind == "context_fork" and expected_parent_id is None:
                existing.kind = "normal"
                if session_id not in changed_session_ids:
                    existing.updated_at = datetime.now(UTC)
                    changed_session_ids.append(session_id)
            manifests[session_id] = existing.model_dump(mode="json")

        moved = self._path_resolver.relocate_folder_tree(
            folder_id=folder_id,
            parent_node_id=parent_node_id,
            name=name,
            session_manifests=manifests,
        )
        for session_id in changed_session_ids:
            self._notify_changed("update", session_id)
        return moved

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

    async def delete(
        self,
        session_id: str,
        *,
        cascade: bool = False,
    ) -> DeleteSessionResultDTO:
        try:
            session_dir = self._path_resolver.resolve_session_node(session_id)
        except KeyError as error:
            raise NotFoundError(f"Session {session_id} not found") from error

        physical_children = self._path_resolver.child_nodes(session_id)
        descendant_session_ids = self._path_resolver.descendant_session_ids(session_id)
        if physical_children and not cascade:
            child_ids = ",".join(sorted(child.node_id for child in physical_children))
            raise RuntimeError(
                "会话包含物理子树，必须显式确认级联删除: "
                f"session_id={session_id}, children={child_ids}"
            )
        if not session_dir.exists():
            raise NotFoundError(f"Session {session_id} not found")

        deleted_descendant_ids = self._path_resolver.delete_session_subtree(session_id)
        if deleted_descendant_ids != descendant_session_ids:
            raise RuntimeError(
                "删除会话子树结果与权威索引预检不一致: "
                f"expected={descendant_session_ids}, actual={deleted_descendant_ids}"
            )
        for descendant_session_id in descendant_session_ids:
            self._notify_changed("delete", descendant_session_id)
        self._notify_changed("delete", session_id)
        return DeleteSessionResultDTO(session_id=session_id, status="deleted")

    @staticmethod
    def _write_session_file(path: Path, session: SessionDTO) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            dir=path.parent,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump(
                    session.model_dump(),
                    file,
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, path)
        finally:
            temporary_path.unlink(missing_ok=True)

    async def control(
        self, session_id: str, action: str, payload: dict = None
    ) -> SessionControlResultDTO:
        await self.get(session_id)
        return SessionControlResultDTO(
            session_id=session_id, action=action, status="executed"
        )

    async def list_trace_events(
        self,
        session_id: str,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> CursorPage[TraceEventDTO]:
        await self.get(session_id)
        page = self._trace_event_store.read_trace_page(
            session_id,
            cursor=cursor,
            limit=limit,
        )
        mapper = TraceEventMapper()
        return CursorPage(
            items=mapper.map_many(
                [event.model_dump() for event in page.events],
                session_id=session_id,
            ),
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )

    async def ensure_trace_cursor(
        self, session_id: str, after_event_id: str | None
    ) -> None:
        await self.get(session_id)
        self._trace_event_store.ensure_cursor(session_id, after_event_id)

    async def stream_trace_events(
        self, session_id: str, after_event_id: str | None = None
    ):
        await self.get(session_id)
        mapper = TraceEventMapper()
        async for record in self._trace_event_store.stream_events(
            session_id,
            after_event_id,
        ):
            dto = mapper.map_one(record.event.model_dump(), session_id=session_id)
            if dto is not None:
                yield dto, record.cursor
