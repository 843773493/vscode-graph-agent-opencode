from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar, Protocol

from app.abstractions.job_service import JobServiceProtocol
from app.core.exceptions import NotFoundError
from app.core.identifier import create_prefixed_id
from app.core.path_utils import get_session_path_resolver
from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.core.session_control_store import SessionControlStore
from app.core.session_paths import SessionPathResolver, SessionPhysicalNode
from app.core.session_tree.support import SESSION_ALLOCATION_MARKER_NAME
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
    def pinned_fork_children(self, source_thread_id: str) -> tuple[str, ...]: ...

    def release_fork_retentions(self, child_session_id: str) -> None: ...


# 新模型口径（R12/R13 剥离对齐）：catalog 权威模式下 session.json 不再承载
# 可变导航字段，唯一权威 = resolver index/catalog；仅在开关切到新 catalog
# resolver 时于写入侧剥离。TODO(切换收口后删除)：旧 resolver 分支随开关移除。
_MANIFEST_NAVIGATION_KEYS = ("title", "title_source", "parent_session_id")


class SessionService:
    DEFAULT_SESSION_TITLES: ClassVar[set[str]] = {"", "新会话", "未命名"}

    def __init__(
        self,
        *,
        config_service: ConfigService,
        trace_event_store: TraceEventStore,
        workspace_id: str,
        path_resolver: SessionPathResolver | None = None,
        fork_relationship_checker: ForkRelationshipChecker | None = None,
    ):
        self._workspace_id = validate_workspace_id(workspace_id)
        self._config_service = config_service
        self._trace_event_store = trace_event_store
        self._path_resolver = path_resolver or get_session_path_resolver()
        self._fork_relationship_checker = fork_relationship_checker
        # TODO(切换收口后删除): 8.2-切片3b-1 换源模式判定。新 catalog
        # resolver 的 manifest 不承载可变导航字段（title/title_source/
        # parent_session_id，register 剥离口径）：写入时剥离三键、移动走
        # 逻辑移动签名；旧 resolver 的物理校验强制 manifest 携带这些字段，
        # 写入口径保持现状不变。
        self._catalog_mode = isinstance(self._path_resolver, SessionCatalogPathResolver)
        self._path_resolver.initialize()
        self._migrate_legacy_workspace_ids()
        self._job_service: JobServiceProtocol | None = None
        self._change_listeners: list[Callable[[str, str], None]] = []

    @property
    def path_resolver(self) -> SessionPathResolver:
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

        for node in self._path_resolver.list_authoritative_nodes():
            if node.kind != "session":
                continue
            session_file = node.path / "session.json"
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

    def _authoritative_navigation_projection(
        self,
        session_id: str,
    ) -> tuple[SessionPhysicalNode, dict[str, SessionPhysicalNode]]:
        """返回会话节点在权威索引上的投影与全量节点表（模式无关读源）。

        换源依据（OpenSpec 8.2-切片3b-1）：title/parent_session_id 不再读
        manifest——title 取节点显示名（旧 resolver=索引 name，新 resolver=
        catalog display_name），parent_session_id 由父链派生。这里使用
        ``list_authoritative_nodes``（只投影权威索引，不做物理树严格校验）
        而非 ``get_node``/``nearest_session_ancestor``：后两者在旧 resolver
        下会因无关物理漂移抛 RuntimeError，会破坏
        ``resolve_session_node_for_runtime`` 建立的「运行时解析容忍无关
        物理漂移」契约；健康树下两者结果完全一致。
        """
        nodes = self._path_resolver.list_authoritative_nodes()
        nodes_by_id = {node.node_id: node for node in nodes}
        node = nodes_by_id.get(session_id)
        if node is None:
            raise KeyError(f"权威会话目录索引不存在: session_id={session_id}")
        return node, nodes_by_id

    @staticmethod
    def _nearest_session_ancestor_in_projection(
        parent_node_id: str | None,
        nodes_by_id: dict[str, SessionPhysicalNode],
    ) -> str | None:
        """在权威索引投影上派生最近 session 祖先（语义含传入节点本身）。

        与旧 resolver ``nearest_session_ancestor`` 同语义：传入 session
        直接返回它，folder 沿父链向上找第一个 session，None 返回 None。
        旧 resolver 的物理校验保证 manifest parent_session_id 恒等于该
        派生值，新 resolver 由 catalog 父链直接派生，因此两种模式下
        parent_session_id 读源一致。
        """
        current_id = parent_node_id
        visited: set[str] = set()
        while current_id is not None:
            if current_id in visited:
                raise RuntimeError(f"权威会话目录索引包含循环: {current_id}")
            visited.add(current_id)
            node = nodes_by_id.get(current_id)
            if node is None:
                raise RuntimeError(f"物理会话节点父节点不存在: {current_id}")
            if node.kind == "session":
                return node.node_id
            current_id = node.parent_node_id
        return None

    async def get(self, session_id: str) -> SessionDTO:
        try:
            session_file = (
                self._path_resolver.resolve_session_node_for_runtime(session_id)
                / "session.json"
            )
        except KeyError as error:
            raise NotFoundError(f"Session {session_id} not found") from error
        if not session_file.is_file():
            raise NotFoundError(f"Session {session_id} not found")

        node, nodes_by_id = self._authoritative_navigation_projection(session_id)
        data = json.loads(
            await asyncio.to_thread(session_file.read_text, encoding="utf-8")
        )
        # 换源：title/parent_session_id 以 resolver 权威投影为准回填，
        # manifest 仍提供其余字段（kind/delegation/created_at 等）。
        data["title"] = node.name
        data["parent_session_id"] = self._nearest_session_ancestor_in_projection(
            node.parent_node_id,
            nodes_by_id,
        )

        session = SessionDTO.model_validate(data)
        self._assert_workspace_binding(session)
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
        try:
            nodes = self._path_resolver.list_nodes()
        except RuntimeError:
            # 业务读只投影权威索引，保留物理树错误供目录刷新入口报告；
            # 不扫描或吸收未登记的物理目录。
            nodes = self._path_resolver.list_authoritative_nodes()
        nodes_by_id = {node.node_id: node for node in nodes}
        for node in nodes:
            if node.kind != "session":
                continue
            session_file = node.path / "session.json"
            data = json.loads(
                await asyncio.to_thread(session_file.read_text, encoding="utf-8")
            )
            # 换源：与 get() 同口径，title 取权威索引节点显示名，
            # parent_session_id 由父链派生，不读 manifest 中这两键。
            data["title"] = node.name
            data["parent_session_id"] = self._nearest_session_ancestor_in_projection(
                node.parent_node_id,
                nodes_by_id,
            )
            session = SessionDTO.model_validate(data)
            self._assert_workspace_binding(session)
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

        session_data = SessionDTO(
            session_id=session_id,
            workspace_id=self._workspace_id,
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
            generation_origin=generation_origin,
            created_at=now,
            updated_at=now,
        )

        session_dir = self._path_resolver.allocate_session_dir(
            session_id=session_data.session_id,
            title=session_data.title,
            parent_node_id=resolved_parent_node_id,
        )
        # 模式无关回读分配 marker：旧 resolver 的 marker 记录传入的
        # session_id（恒等于上方生成值，此处为幂等校正）；新 catalog
        # resolver 自 R17 起 honor 传入 canonical ID（非 canonical 传入
        # 直接拒绝），上方传入的恒为软件生成 canonical ID，此处为幂等
        # 校正（切换期兼容契约，TODO(切换轮) 随 journal 原生创建流移除）。
        allocated_session_id = str(
            json.loads(
                (session_dir / SESSION_ALLOCATION_MARKER_NAME).read_text(
                    encoding="utf-8"
                )
            )["session_id"]
        )
        if allocated_session_id != session_data.session_id:
            session_data = session_data.model_copy(
                update={"session_id": allocated_session_id}
            )

        session_file = session_dir / "session.json"
        try:
            self._write_session_file(session_file, session_data)
            self._path_resolver.register_session(allocated_session_id, session_dir)
        except Exception:
            self._path_resolver.abandon_session_allocation(session_dir)
            raise

        # register 是创建可见性提交点：新 resolver 会以创建流冻结的 UTC
        # 时间规范化 manifest created_at；旧 resolver 从 manifest 读回同值。
        # 这里把返回 DTO 的 created_at 对齐到 resolver 权威投影（旧模式恒等）。
        session_data.created_at = self._path_resolver.get_node(
            allocated_session_id
        ).created_at
        # 新模式下 created_at 被冻结值改写后，DTO 的 updated_at（构造时的
        # 本地时刻）会早于 created_at，违背「创建时 updated==created」且与
        # 落盘 manifest 不一致；随 created_at 一并对齐到创建时刻（旧模式
        # 两者本就同值，恒等）。
        session_data.updated_at = session_data.created_at
        # TODO(切换收口后删除)：catalog 模式 register 只规范化 created_at
        # （updated_at 走 setdefault 保留调用方本地时刻），落盘 manifest 的
        # updated_at 停留在对齐前的值——这里以对齐后的 DTO 重写一次，使
        # 磁盘 manifest 与权威投影一致；旧模式对齐恒等、不重写，行为
        # 逐字节保持。
        if self._catalog_mode:
            self._write_session_file(session_dir / "session.json", session_data)

        self._notify_changed("create", allocated_session_id)
        return session_data

    async def update(
        self, session_id: str, session: SessionUpdateRequest
    ) -> SessionDTO:
        """更新会话；title/parent_session_id 读源自 resolver（8.2-切片3b-1 换源）。

        换源后重命名的持久化路径为：manifest 写入（catalog 模式下剥离
        title/title_source）+ ``resolver.update_node_name`` 同步权威显示名。
        旧 resolver 下 index name 与 manifest title 由该流程保持同步；
        R18 默认切新 resolver 后旧分支随开关一并移除。
        """
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
        kind_demoted_to_normal = (
            existing.kind == "context_fork" and target_parent_session_id is None
        )
        if kind_demoted_to_normal:
            existing.kind = "normal"
        existing.parent_session_id = target_parent_session_id
        existing.updated_at = datetime.now(UTC)

        async def move() -> None:
            if self._catalog_mode:
                # 新 resolver：逻辑移动只改 catalog 父关系，不搬磁盘也不改
                # 写 manifest（design.md §9）；context_fork 降级 normal 属于
                # 业务规则，由服务层在此补写剥离版 manifest。
                self._path_resolver.relocate_session(
                    session_id=session_id,
                    parent_node_id=parent_node_id,
                )
                if kind_demoted_to_normal:
                    session_file = (
                        self._path_resolver.resolve_session_node(session_id)
                        / "session.json"
                    )
                    self._write_session_file(session_file, existing)
                return
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
        if self._catalog_mode:
            return await self._relocate_folder_tree_catalog(
                folder_id=folder_id,
                parent_node_id=parent_node_id,
                name=name,
            )
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

    async def _relocate_folder_tree_catalog(
        self,
        *,
        folder_id: str,
        parent_node_id: str | None,
        name: str,
    ) -> SessionPhysicalNode:
        """新模型文件夹子树移动：逻辑移动不搬磁盘（TODO(切换收口后删除)）。

        与旧路径的业务规则对齐：

        - 仍按 ``expected_session_parents_after_folder_move`` 预检每个
          session 的父子绑定与 kind 改绑约束（catalog 父关系移动后立即
          派生生效）；
        - 新 resolver 的 ``relocate_folder_tree`` 不再接受 ``name``——显示
          名变更走 ``update_node_name``（非原子：重命名成功后移动失败会
          保留新显示名，属切换期已接受的语义拆分）；
        - context_fork 移出父会话降级 normal 由服务层补写剥离版 manifest。
        """
        expected_parents = (
            self._path_resolver.expected_session_parents_after_folder_move(
                folder_id=folder_id,
                parent_node_id=parent_node_id,
            )
        )
        changed_session_ids: list[str] = []
        demoted_sessions: list[SessionDTO] = []
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
            parent_changed = existing.parent_session_id != expected_parent_id
            if parent_changed:
                existing.parent_session_id = expected_parent_id
                existing.updated_at = datetime.now(UTC)
                changed_session_ids.append(session_id)
            if existing.kind == "context_fork" and expected_parent_id is None:
                existing.kind = "normal"
                demoted_sessions.append(existing)
                if not parent_changed:
                    existing.updated_at = datetime.now(UTC)
                    changed_session_ids.append(session_id)

        folder_node = self._path_resolver.get_node(folder_id)
        if folder_node.name != name:
            self._path_resolver.update_node_name(folder_id, name)
        self._path_resolver.relocate_folder_tree(
            folder_id=folder_id,
            parent_node_id=parent_node_id,
        )
        for demoted in demoted_sessions:
            session_file = (
                self._path_resolver.resolve_session_node(demoted.session_id)
                / "session.json"
            )
            self._write_session_file(session_file, demoted)
        for session_id in changed_session_ids:
            self._notify_changed("update", session_id)
        return self._path_resolver.get_node(folder_id)

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

        if self._fork_relationship_checker is not None:
            pinned_children = self._fork_relationship_checker.pinned_fork_children(
                session_id
            )
            if pinned_children:
                raise RuntimeError(
                    "会话存在 pinned fork，不能删除源会话: "
                    f"session_id={session_id}, children={','.join(pinned_children)}"
                )

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
        payload = session.model_dump()
        if self._catalog_mode:
            # 新模型口径：catalog 是 title/title_source/parent_session_id 的
            # 唯一权威，manifest 剥离可变导航字段（与新 resolver register
            # 的剥离重写一致）。旧 resolver 依赖 manifest 携带这些字段
            # （物理校验强制 parent_session_id），写入保持现状。
            for key in _MANIFEST_NAVIGATION_KEYS:
                payload.pop(key, None)
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
