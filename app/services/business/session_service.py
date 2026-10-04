from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import os
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar, Protocol
from uuid import uuid4

from app.abstractions.job_service import JobServiceProtocol
from app.core.exceptions import NotFoundError
from app.core.session_catalog_resolver import (
    SessionCatalogNodeProjection,
    SessionCatalogPathResolver,
)
from app.core.session_control_store import SessionControlStore
from app.core.session_creation import SessionCreationService
from app.core.session_lifecycle_gate import SessionDeletionPendingError
from app.core.workspace_identity import (
    LEGACY_BACKEND_WORKSPACE_IDS,
    validate_workspace_id,
)
from app.schemas.internal_v2.common import CursorPage
from app.schemas.internal_v2.session import (
    ChildThreadListDTO,
    ChildThreadSummaryDTO,
    DeleteSessionResultDTO,
    SessionControlResultDTO,
    SessionCreateRequest,
    SessionDTO,
    SessionGenerationOriginDTO,
    SessionKind,
    SessionListResultDTO,
    SessionUpdateRequest,
    TitleSource,
)
from app.schemas.internal_v2.trace import TraceEventDTO
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.trace_event_store import TraceEventStore
from app.services.mapping.trace_event_mapper import TraceEventMapper


class ForkRelationshipChecker(Protocol):
    def release_fork_retentions(self, child_session_id: str) -> None: ...


# SQLite catalog 是导航字段的唯一权威，session manifest 只保存会话业务字段。
_MANIFEST_NAVIGATION_KEYS = ("title", "title_source", "parent_session_id")
# 由 catalog 冻结指针派生的投影字段同样不能写回 manifest，避免出现第二权威。
_MANIFEST_DERIVED_KEYS = (*_MANIFEST_NAVIGATION_KEYS, "thread_id")


class SessionService:
    DEFAULT_SESSION_TITLES: ClassVar[set[str]] = {"", "新会话", "未命名"}

    def __init__(
        self,
        *,
        config_service: ConfigService,
        trace_event_store: TraceEventStore,
        workspace_id: str,
        path_resolver: SessionCatalogPathResolver,
        creation_service: SessionCreationService,
        fork_relationship_checker: ForkRelationshipChecker | None = None,
    ):
        self._workspace_id = validate_workspace_id(workspace_id)
        self._config_service = config_service
        self._trace_event_store = trace_event_store
        self._path_resolver = path_resolver
        self._creation_service = creation_service
        self._fork_relationship_checker = fork_relationship_checker
        self._path_resolver.initialize()
        self._migrate_legacy_workspace_ids()
        self._job_service: JobServiceProtocol | None = None
        self._change_listeners: list[Callable[[str, str], None]] = []

    @property
    def path_resolver(self) -> SessionCatalogPathResolver:
        return self._path_resolver

    @property
    def workspace_id(self) -> str:
        return self._workspace_id

    def _migrate_legacy_workspace_ids(self) -> None:
        """把已知固定后端 ID 的历史 manifest 迁移到当前工作区 UUID。

        换源后 manifest 可能是剥离形态（不含 title/title_source/
        parent_session_id，见 _MANIFEST_NAVIGATION_KEYS），因此先用原始
        JSON 的 workspace_id 判定是否命中旧 ID，仅对需要迁移的旧形态完整
        manifest 做 SessionDTO 校验与重写，剥离形态在此处不参与校验。
        """

        for node in self._path_resolver.list_nodes():
            if node.kind != "session":
                continue
            session_file = (
                self._path_resolver.resolve_session_node(node.node_id)
                / "session.json"
            )
            data = json.loads(session_file.read_text(encoding="utf-8"))
            if data.get("workspace_id") not in LEGACY_BACKEND_WORKSPACE_IDS:
                continue
            if all(key in data for key in _MANIFEST_NAVIGATION_KEYS):
                # 旧形态完整 manifest：按完整模型校验并重写。
                session = SessionDTO.model_validate(data)
                session.workspace_id = self._workspace_id
                self._write_session_file(session_file, session)
                continue
            # 剥离形态：导航键由 catalog 权威承载，SessionDTO 必填的
            # title 不在 manifest 上；只改写 workspace_id，不整体校验。
            data["workspace_id"] = self._workspace_id
            self._write_stripped_session_file(session_file, data)

    def _write_stripped_session_file(self, path: Path, data: dict[str, object]) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            dir=path.parent,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump(data, file, ensure_ascii=False, indent=2, default=str)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, path)
        finally:
            temporary_path.unlink(missing_ok=True)

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

    def _assert_workspace_binding(self, session: SessionDTO) -> None:
        if session.workspace_id != self._workspace_id:
            raise RuntimeError(
                "会话与当前工作区后端标识不一致: "
                f"session_id={session.session_id}, "
                f"session_workspace_id={session.workspace_id}, "
                f"backend_workspace_id={self._workspace_id}"
            )

    def assert_session_active(self, session_id: str) -> None:
        """拒绝删除中的 owner 进入新业务准入，目录与清理读取仍可见。"""

        node = self._path_resolver.get_node(session_id)
        if node.kind != "session":
            raise RuntimeError(
                "Session 准入要求 catalog session 节点: "
                f"session_id={session_id!r}, kind={node.kind!r}"
            )
        if node.state != "active":
            raise SessionDeletionPendingError(
                "session_deletion_pending: catalog owner 正在删除，"
                f"拒绝新业务准入: session_id={session_id!r}, state={node.state!r}"
            )

    async def get(self, session_id: str) -> SessionDTO:
        # 按 ID 的单节点查询（不触发全 catalog BFS）：单会话读取的工作量
        # 与目录规模无关。未登记会话的 KeyError 与其余缺失同样归为 NotFound。
        try:
            node = self._path_resolver.get_node(session_id)
        except KeyError as error:
            raise NotFoundError(f"Session {session_id} not found") from error
        return await self._read_session_dto(node)

    async def _read_session_dto(
        self,
        node: SessionCatalogNodeProjection,
    ) -> SessionDTO:
        """读取单个会话的 session.json 并按权威索引回填导航字段。

        唯一从 manifest 取得、无法由目录索引提供的字段是 ``title_source``
        与 ``updated_at``；其余字段（title/parent_session_id/thread_id）以
        resolver 权威投影为准回填，manifest 仍提供 kind/delegation/
        created_at 等。该读路径只服务**当前页**的会话，不得对全工作区逐会话
        调用（否则读取次数会随会话总数线性增长）。
        """
        session_id = node.node_id
        try:
            session_file = (
                self._path_resolver.resolve_session_node_for_runtime(session_id)
                / "session.json"
            )
        except KeyError as error:
            raise NotFoundError(f"Session {session_id} not found") from error
        if not session_file.is_file():
            raise NotFoundError(f"Session {session_id} not found")

        data = json.loads(
            await asyncio.to_thread(session_file.read_text, encoding="utf-8")
        )
        data["title"] = node.name
        # parent_session_id 沿 catalog 父链派生最近 session 祖先（不含 folder）。
        data["parent_session_id"] = self._path_resolver.nearest_session_ancestor(
            node.parent_node_id
        )
        # 普通 Session 入口只定位 main thread；身份取自 catalog 冻结指针，
        # 不接受 session_id 冒充 thread_id。
        data["thread_id"] = self._path_resolver.main_thread_id(session_id)

        session = SessionDTO.model_validate(data)
        self._assert_workspace_binding(session)
        if session.current_provider_id is None:
            session.current_provider_id = (
                self._config_service.resolve_agent_provider_id(session.current_agent_id)
            )
        return session

    async def resolve_main_thread(self, session_id: str) -> str:
        """解析 Session 的权威 main thread id（普通入口的唯一事实来源）。

        身份取自 catalog 冻结 ``main_thread_id``，缺指针或节点非 session
        时 fail closed；不得用 session_id 冒充 thread_id。
        """
        try:
            return self._path_resolver.main_thread_id(session_id)
        except KeyError as error:
            raise NotFoundError(f"Session {session_id} not found") from error

    async def list(
        self,
        workspace_id: str | None = None,
        skip: int = 0,
        limit: int = 100,
        cursor: str | None = None,
    ) -> SessionListResultDTO:
        """返回会话列表；读取工作量与返回页大小相关，不随会话总数增长。

        目录索引是成员与顺序的唯一来源：先用索引节点（``kind`` 与权威
        ``created_at``）排序并**只对当前页**调用 ``resolve_session_node`` /
        逐个读取 session.json。读取次数因此与页大小相关，而非全工作区会话
        总数；``total`` 与全量顺序仍由索引给出。
        """
        nodes = self._path_resolver.list_nodes()
        session_nodes = [node for node in nodes if node.kind == "session"]
        session_nodes.sort(key=lambda node: node.created_at, reverse=True)
        total = len(session_nodes)
        if limit < 1:
            # 非正 limit 会返回空页却仍带 ``has_more=True`` 与相同 offset 的 cursor，
            # 调用方按契约继续翻页时永远拿到空页且 next_cursor 不前进（死循环）。
            # 公开查询参数必须 fail-closed，不能静默返回不可收敛的页。
            raise ValueError(f"会话列表 limit 必须大于 0: limit={limit}")
        revision = self._session_list_revision(session_nodes)
        offset = (
            _decode_session_list_cursor(cursor, revision=revision)
            if cursor is not None
            else skip
        )
        if offset < 0 or offset > total:
            raise ValueError(
                "会话列表 cursor offset 越界: "
                f"offset={offset}, total={total}"
            )
        page_nodes = session_nodes[offset : offset + limit]

        sessions = [await self._read_session_dto(node) for node in page_nodes]
        next_offset = offset + len(page_nodes)

        return SessionListResultDTO(
            items=sessions,
            total=total,
            next_cursor=(
                _encode_session_list_cursor(next_offset, revision=revision)
                if next_offset < total
                else None
            ),
            has_more=next_offset < total,
        )

    @staticmethod
    def _session_list_revision(
        session_nodes: list[SessionCatalogNodeProjection],
    ) -> str:
        """会话列表分页 revision：会话成员与顺序（创建时间倒序）的指纹。

        cursor 绑定该值，使会话集合或顺序变化后旧 cursor 显式失效，
        而不是静默跳页或重排。
        """
        payload = json.dumps(
            [(node.node_id, str(node.created_at)) for node in session_nodes],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    async def child_session_summary(
        self,
        session_id: str,
        *,
        limit: int,
    ) -> tuple[int, list[str], bool]:
        """读取有界的逻辑子会话摘要，不为诊断快照加载全部会话详情。"""
        await self.get(session_id)
        return self._path_resolver.child_session_summary(
            session_id,
            limit=limit,
        )

    async def list_child_threads(self, session_id: str) -> ChildThreadListDTO:
        """列出 owner Session 的 durable child thread（8.5-B 权威投影）。

        数据来自 owner ``session-control.sqlite```：thread_catalog 的
        child row 是唯一可见性提交点产物，collaboration ledger/member 提供
        delegation 语义，initial execution intent 提供 admission 状态。
        旧 delegated child Session 链路已下线，不再读 manifest。
        """
        await self.get(session_id)
        control_database = (
            self._path_resolver.resolve_session_node(session_id)
            / "session-control.sqlite"
        )
        if not control_database.is_file():
            # 无控制库 = 该 Session 尚无 child thread（不建库、零副作用）。
            return ChildThreadListDTO(parent_session_id=session_id, items=[], total=0)
        control = SessionControlStore(control_database)
        try:
            threads = control.list_child_thread_rows()
            members = {
                member.child_thread_id: member
                for member in control.list_collaboration_members(
                    coordinator_session_id=session_id
                )
            }
            intents = {
                intent.thread_id: intent
                for intent in control.list_initial_execution_intents()
            }
        finally:
            control.close()
        items: list[ChildThreadSummaryDTO] = []
        for thread in threads:
            member = members.get(thread.thread_id)
            intent = intents.get(thread.thread_id)
            items.append(
                ChildThreadSummaryDTO(
                    thread_id=thread.thread_id,
                    created_at=thread.created_at,
                    delegation_id=(
                        member.delegation_id if member is not None else None
                    ),
                    role=member.role if member is not None else None,
                    subagent_type=(
                        member.subagent_type if member is not None else None
                    ),
                    title=member.title if member is not None else None,
                    collaboration_state=(member.state if member is not None else None),
                    admission_state=(intent.state if intent is not None else None),
                    status=(
                        "running"
                        if intent is not None and intent.state == "bound"
                        else "failed"
                        if member is not None and member.state == "cancelled"
                        else "pending"
                    ),
                )
            )
        items.sort(key=lambda item: (item.created_at, item.thread_id), reverse=True)
        return ChildThreadListDTO(
            parent_session_id=session_id,
            items=items,
            total=len(items),
        )

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

    async def _create(
        self,
        *,
        title: str | None,
        title_source: TitleSource | None,
        agent_id: str | None,
        parent_session_id: str | None = None,
        context_source_session_id: str | None = None,
        kind: SessionKind = "normal",
        generation_origin: SessionGenerationOriginDTO | None = None,
        parent_node_id: str | None = None,
    ) -> SessionDTO:
        if self._config_service is None:
            raise RuntimeError("SessionService 未绑定 ConfigService")
        config_service = self._config_service
        resolved_agent_id = config_service.resolve_new_session_agent_id(agent_id)
        resolved_provider_id = config_service.resolve_new_session_provider_id(
            resolved_agent_id
        )
        idempotency_key = f"session-{uuid4().hex}"
        await self._validate_parent_session(
            session_id=idempotency_key,
            workspace_id=self._workspace_id,
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

        resolved_title = title or "新会话"
        result = await self._creation_service.create(
            idempotency_key=idempotency_key,
            title=resolved_title,
            parent_node_id=resolved_parent_node_id,
            session_metadata={
                "kind": kind,
                "delegation": None,
                "generation_origin": (
                    generation_origin.model_dump(mode="json")
                    if generation_origin is not None
                    else None
                ),
                "current_agent_id": resolved_agent_id,
                "current_provider_id": resolved_provider_id,
                "context_source_session_id": context_source_session_id,
            },
        )
        created_at = datetime.fromisoformat(result.node.created_at or "")
        session_data = SessionDTO(
            session_id=result.session_id,
            workspace_id=self._workspace_id,
            title=resolved_title,
            title_source=self._infer_created_title_source(
                title,
                title_source,
            ),
            current_agent_id=resolved_agent_id,
            current_provider_id=resolved_provider_id,
            parent_session_id=parent_session_id,
            context_source_session_id=context_source_session_id,
            kind=kind,
            generation_origin=generation_origin,
            created_at=created_at,
            updated_at=created_at,
            thread_id=result.main_thread_id,
        )

        self._notify_changed("create", result.session_id)
        return session_data

    async def update(
        self, session_id: str, session: SessionUpdateRequest
    ) -> SessionDTO:
        """更新会话并同步 catalog 显示名。"""
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

        if self._fork_relationship_checker is not None:
            for child_session_id in (*descendant_session_ids, session_id):
                self._fork_relationship_checker.release_fork_retentions(
                    child_session_id
                )

        deleted_descendant_ids = await self._path_resolver.delete_session_subtree(
            session_id
        )
        if deleted_descendant_ids != descendant_session_ids:
            raise RuntimeError(
                "删除会话子树结果与权威索引预检不一致: "
                f"expected={descendant_session_ids}, actual={deleted_descendant_ids}"
            )
        for descendant_session_id in descendant_session_ids:
            self._notify_changed("delete", descendant_session_id)
        self._notify_changed("delete", session_id)
        return DeleteSessionResultDTO(session_id=session_id, status="deleted")

    def _write_session_file(self, path: Path, session: SessionDTO) -> None:
        payload = session.model_dump(exclude=set(_MANIFEST_DERIVED_KEYS))
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            dir=path.parent,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump(
                    payload,
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
        self, session_id: str, action: str, payload: dict[str, object] | None = None
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


def _encode_session_list_cursor(offset: int, *, revision: str) -> str:
    payload = json.dumps(
        {"offset": offset, "revision": revision},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_session_list_cursor(cursor: str, *, revision: str) -> int:
    """解析会话列表分页 cursor；revision 变化时显式报错，不静默降级。"""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        binascii.Error,
    ) as error:
        raise ValueError("会话列表 cursor 格式无效") from error
    if not isinstance(payload, dict):
        raise TypeError("会话列表 cursor 格式无效")
    if payload.get("revision") != revision:
        raise ValueError("会话列表已更新，请从第一页重新加载")
    offset = payload.get("offset")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("会话列表 cursor offset 无效")
    return offset
