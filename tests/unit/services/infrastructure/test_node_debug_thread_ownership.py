"""NodeDebugService 精确 (session_id, thread_id) 归属迁移的定向单元测试。

覆盖：
- main/child thread 的断点、活动方案与动作时间线严格隔离；
- child thread 调试数据落在受检 thread 节点目录的 ``debug/node/`` 下；
- thread 节点只按权威目录索引解析（不拼路径、不扫盘、不按 session 猜目标）；
- NodeDebug 只接受 catalog 中 canonical ``thr_*`` thread ID；
- mutation 的 Session 生命周期准入（Session 存在且未删除）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.exceptions import NotFoundError
from app.core.path_utils import get_session_path_resolver
from app.core.session_control_primitives import CONTROL_DATABASE_NAME
from app.core.session_control_store import SessionControlStore
from app.core.session_lifecycle_gate import (
    NavigationTopologyGate,
    SessionDeletionPendingError,
    SessionLifecycleGate,
)
from app.schemas.internal_v2.node_debug import (
    NodeDebugConfigurationCreateRequest,
    NodeDebugSetBreakpointActionRequest,
    NodeDebugSetBreakpointParams,
)
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.node_debug.service import NodeDebugService
from app.services.infrastructure.node_debug.session.session_admission import (
    NodeDebugSessionAdmission,
)
from app.services.infrastructure.node_debug.session.session_store import (
    NodeDebugSessionStore,
)
from app.services.infrastructure.node_debug.session.thread_owner import (
    normalize_node_debug_owner,
    resolve_node_debug_owner,
)
from tests.support.canonical_id_at import thread_id_at
from tests.support.catalog_session_bundle import (
    fixture_main_thread_id,
    seed_catalog_session_bundle,
)
from tests.support.graph_binding import TEST_THREAD_GRAPH_BINDING_JSON
from tests.support.node_debug_dependencies import TEST_THREAD_ID

_PARENT_SESSION_ID = "ses_00000000400070008000000000000001"
_CHILD_SESSION_ID = "ses_00000000400070008000000000000002"
_OTHER_SESSION_ID = "ses_00000000400070008000000000000003"
_GRANDCHILD_SESSION_ID = "ses_00000000400070008000000000000004"
_UNKNOWN_SESSION_ID = "ses_00000000400070008000000000000005"
_MISSING_SESSION_ID = "ses_00000000400070008000000000000006"
_PARENT_MAIN_THREAD_ID = fixture_main_thread_id(_PARENT_SESSION_ID)
_CHILD_MAIN_THREAD_ID = fixture_main_thread_id(_CHILD_SESSION_ID)
_OTHER_MAIN_THREAD_ID = fixture_main_thread_id(_OTHER_SESSION_ID)
_GRANDCHILD_MAIN_THREAD_ID = fixture_main_thread_id(_GRANDCHILD_SESSION_ID)


def _create_session(
    resolver: object,
    session_id: str,
    *,
    parent_session_id: str | None = None,
) -> Path:
    """在权威目录索引中创建最小合法会话节点（child 必须显式声明父会话）。"""
    title = f"测试会话 {session_id}"
    return seed_catalog_session_bundle(
        resolver.sessions_root,
        session_id,
        title=title,
        parent_node_id=parent_session_id,
        main_thread_id=fixture_main_thread_id(session_id),
    ).directory


class _ResolverSessionLifecycle:
    """以权威目录索引和正式 gate 模拟 Session 写准入。"""

    def __init__(self, resolver: object) -> None:
        self._resolver = resolver
        self._topology_gate = NavigationTopologyGate(resolver.sessions_root)
        self._lifecycle_gate = SessionLifecycleGate(resolver.sessions_root)

    @asynccontextmanager
    async def session_write_admission(self, session_id: str) -> AsyncIterator[None]:
        async with AsyncExitStack() as session_gate:
            async with self._topology_gate.shared():
                await session_gate.enter_async_context(
                    self._lifecycle_gate.exclusive(session_id)
                )
                try:
                    node = self._resolver.get_node(session_id)
                except KeyError as error:
                    raise NotFoundError(f"Session {session_id} not found") from error
                if node.kind != "session":
                    raise NotFoundError(f"Session {session_id} not found")
                if node.state != "active":
                    raise SessionDeletionPendingError(
                        "session_deletion_pending: catalog owner 正在删除，"
                        f"拒绝新业务准入: session_id={session_id!r}, state={node.state!r}"
                    )
            yield

    async def get(self, session_id: str) -> object:
        try:
            return self._resolver.resolve_session_node_for_runtime(session_id)
        except KeyError as error:
            raise NotFoundError(f"Session {session_id} not found") from error


class _ThreadsDirectoryResolver:
    """独立 thread 节点形态（``<session>/threads/<thread_id>``）的解析器替身。

    生产 resolver 将 main thread 解析到 Session 下的 canonical thread 节点；该替身
    用于固定“非会话形态 thread 保持 (session_id, thread_id) 原样”
    这一协议分支。
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def get_node(self, node_id: str) -> object:
        raise KeyError(node_id)

    def main_thread_id(self, session_id: str) -> str:
        return TEST_THREAD_ID

    def resolve_session_node(self, session_id: str) -> Path:
        return self._root / session_id

    def resolve_thread_node(self, session_id: str, thread_id: str) -> Path:
        return self._root / session_id / "threads" / thread_id


@pytest.fixture
def session_tree(tmp_path: Path) -> tuple[object, Path, Path]:
    """建立 ``ses_parent`` 与其直接子会话 ``ses_child`` 的权威物理树。"""
    sessions_root = tmp_path / ".boxteam" / "sessions"
    resolver = get_session_path_resolver(sessions_root)
    resolver.initialize()
    parent_dir = _create_session(resolver, _PARENT_SESSION_ID)
    child_dir = _create_session(
        resolver,
        _CHILD_SESSION_ID,
        parent_session_id=_PARENT_SESSION_ID,
    )
    return resolver, parent_dir, child_dir


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "main.mjs").write_text("console.log('main');\n", encoding="utf-8")
    (root / "child.mjs").write_text(
        "console.log('child');\nconsole.log('child-2');\n",
        encoding="utf-8",
    )
    return root


def _service(workspace_root: Path, resolver: object) -> NodeDebugService:
    return NodeDebugService(
        workspace_root=workspace_root,
        config_service=ConfigService(workspace_root=workspace_root),
        session_store=NodeDebugSessionStore(resolver),
        session_admission=NodeDebugSessionAdmission(
            session_service=_ResolverSessionLifecycle(resolver),
            path_resolver=resolver,
        ),
        external_resource_leases=ExternalResourceLeaseLedger(),
    )


def test_explicit_owner_requires_canonical_thread_id() -> None:
    assert normalize_node_debug_owner(_PARENT_SESSION_ID, _PARENT_MAIN_THREAD_ID) == (
        _PARENT_SESSION_ID,
        _PARENT_MAIN_THREAD_ID,
    )

    with pytest.raises(ValueError, match="非空 session_id"):
        normalize_node_debug_owner("", _PARENT_MAIN_THREAD_ID)
    for alias in ("main", _PARENT_SESSION_ID, _CHILD_SESSION_ID, " "):
        with pytest.raises(ValueError, match="thread_id 形态非法"):
            normalize_node_debug_owner(_PARENT_SESSION_ID, alias)


def test_launch_profile_name_resolution_matches_start_semantics(
    workspace_root: Path,
    session_tree: tuple[object, Path, Path],
) -> None:
    """工具面启动前核对的 profile 解析必须复用启动解析规则。"""
    resolver, _parent_dir, _child_dir = session_tree
    service = _service(workspace_root, resolver)

    assert service.resolve_launch_profile_name(None) == "node-default"
    assert service.resolve_launch_profile_name("node-default") == "node-default"
    with pytest.raises(TypeError, match="调试启动配置不存在"):
        service.resolve_launch_profile_name("missing-profile")


def test_thread_node_resolution_follows_catalog_ownership(
    session_tree: tuple[object, Path, Path],
) -> None:
    resolver, parent_dir, _child_dir = session_tree
    parent_main_thread_id = resolver.main_thread_id(_PARENT_SESSION_ID)
    parent_main_dir = resolver.resolve_thread_node(
        _PARENT_SESSION_ID, parent_main_thread_id
    )
    assert parent_main_dir == parent_dir

    main_owner = resolve_node_debug_owner(
        resolver,
        session_id=_PARENT_SESSION_ID,
        thread_id=parent_main_thread_id,
    )
    assert main_owner.thread_id == parent_main_thread_id
    assert main_owner.thread_node == parent_main_dir
    assert main_owner.key == (_PARENT_SESSION_ID, parent_main_thread_id)

    child_main_thread_id = resolver.main_thread_id(_CHILD_SESSION_ID)
    child_main_node = resolver.resolve_thread_node(
        _CHILD_SESSION_ID, child_main_thread_id
    )
    child_owner = resolve_node_debug_owner(
        resolver,
        session_id=_CHILD_SESSION_ID,
        thread_id=child_main_thread_id,
    )
    assert child_owner.thread_node == child_main_node
    assert child_owner.key == (_CHILD_SESSION_ID, child_main_thread_id)

    with pytest.raises(ValueError, match="thread_id 形态非法"):
        resolve_node_debug_owner(
            resolver,
            session_id=_PARENT_SESSION_ID,
            thread_id=_UNKNOWN_SESSION_ID,
        )
    with pytest.raises(KeyError):
        resolve_node_debug_owner(
            resolver,
            session_id=_PARENT_SESSION_ID,
            thread_id=TEST_THREAD_ID,
        )

    _create_session(resolver, _OTHER_SESSION_ID)
    with pytest.raises(KeyError):
        resolve_node_debug_owner(
            resolver,
            session_id=_PARENT_SESSION_ID,
            thread_id=_OTHER_MAIN_THREAD_ID,
        )

    _create_session(
        resolver,
        _GRANDCHILD_SESSION_ID,
        parent_session_id=_CHILD_SESSION_ID,
    )

    with pytest.raises(KeyError):
        resolve_node_debug_owner(
            resolver,
            session_id=_PARENT_SESSION_ID,
            thread_id=_GRANDCHILD_MAIN_THREAD_ID,
        )


def test_independent_thread_node_keeps_thread_owner_form(tmp_path: Path) -> None:
    """独立 thread 节点形态不折叠，保持 (session_id, thread_id) owner。"""
    resolver = _ThreadsDirectoryResolver(tmp_path / "sessions")
    owner = resolve_node_debug_owner(
        resolver,
        session_id="ses_parent",
        thread_id=TEST_THREAD_ID,
    )

    assert owner.key == ("ses_parent", TEST_THREAD_ID)
    assert owner.thread_node == (
        tmp_path / "sessions" / "ses_parent" / "threads" / TEST_THREAD_ID
    )


@pytest.mark.asyncio
async def test_main_and_child_thread_state_is_isolated(
    session_tree: tuple[object, Path, Path],
    workspace_root: Path,
) -> None:
    resolver, _parent_dir, _child_dir = session_tree
    parent_thread_id = resolver.main_thread_id(_PARENT_SESSION_ID)
    child_thread_id = resolver.main_thread_id(_CHILD_SESSION_ID)
    service = _service(workspace_root, resolver)

    main_state = await service.create_configuration(
        NodeDebugConfigurationCreateRequest(
            session_id=_PARENT_SESSION_ID,
            thread_id=parent_thread_id,
            name="主线程方案",
            script_path="main.mjs",
        )
    )
    child_state = await service.create_configuration(
        NodeDebugConfigurationCreateRequest(
            session_id=_CHILD_SESSION_ID,
            thread_id=child_thread_id,
            name="子线程方案",
            script_path="child.mjs",
        )
    )

    assert main_state.thread_id == parent_thread_id
    assert main_state.session_id == _PARENT_SESSION_ID
    assert child_state.session_id == _CHILD_SESSION_ID
    assert child_state.thread_id == child_thread_id
    assert [item.name for item in main_state.configurations] == ["主线程方案"]
    assert [item.name for item in child_state.configurations] == ["子线程方案"]
    assert main_state.script_path == "main.mjs"
    assert child_state.script_path == "child.mjs"
    assert main_state.active_configuration_id != child_state.active_configuration_id

    main_state = await service.apply_action(
        command=NodeDebugSetBreakpointActionRequest(
            session_id=_PARENT_SESSION_ID,
            thread_id=parent_thread_id,
            action="set_breakpoint",
            params=NodeDebugSetBreakpointParams(
                path="main.mjs",
                line=1,
                condition="true",
            ),
        ),
    )
    child_state = await service.apply_action(
        command=NodeDebugSetBreakpointActionRequest(
            session_id=_CHILD_SESSION_ID,
            thread_id=child_thread_id,
            action="set_breakpoint",
            params=NodeDebugSetBreakpointParams(path="child.mjs", line=1),
        ),
    )

    assert [(item.path, item.line) for item in main_state.breakpoints] == [
        ("main.mjs", 1)
    ]
    assert [(item.path, item.line) for item in child_state.breakpoints] == [
        ("child.mjs", 1)
    ]

    main_actions = " ".join(item.message for item in main_state.actions)
    child_actions = " ".join(item.message for item in child_state.actions)
    assert "主线程方案" in main_actions and "子线程方案" not in main_actions
    assert "子线程方案" in child_actions and "主线程方案" not in child_actions
    assert "child.mjs" not in main_actions
    assert "main.mjs" not in child_actions
    assert all(item.thread_id == parent_thread_id for item in main_state.actions)
    assert all(item.thread_id == child_thread_id for item in child_state.actions)
    assert {item.action_id for item in main_state.actions}.isdisjoint(
        {item.action_id for item in child_state.actions}
    )

    stored_main = await service.get_state(_PARENT_SESSION_ID, parent_thread_id)
    stored_child = await service.get_state(_CHILD_SESSION_ID, child_thread_id)
    assert [item.path for item in stored_main.breakpoints] == ["main.mjs"]
    assert [item.path for item in stored_child.breakpoints] == ["child.mjs"]
    assert [item.name for item in stored_main.configurations] == ["主线程方案"]
    assert [item.name for item in stored_child.configurations] == ["子线程方案"]

    with pytest.raises(FileNotFoundError, match="调试方案不存在"):
        service.get_configuration(
            _PARENT_SESSION_ID,
            child_state.active_configuration_id or "",
            parent_thread_id,
        )


@pytest.mark.asyncio
async def test_child_thread_debug_data_lands_in_thread_node_directory(
    session_tree: tuple[object, Path, Path],
    workspace_root: Path,
) -> None:
    resolver, _parent_dir, child_dir = session_tree
    parent_thread_id = resolver.main_thread_id(_PARENT_SESSION_ID)
    child_thread_id = resolver.main_thread_id(_CHILD_SESSION_ID)
    service = _service(workspace_root, resolver)
    store = NodeDebugSessionStore(resolver)

    main_state = await service.create_configuration(
        NodeDebugConfigurationCreateRequest(
            session_id=_PARENT_SESSION_ID,
            thread_id=parent_thread_id,
            name="主线程方案",
            script_path="main.mjs",
        )
    )
    child_state = await service.create_configuration(
        NodeDebugConfigurationCreateRequest(
            session_id=_CHILD_SESSION_ID,
            thread_id=child_thread_id,
            name="子线程方案",
            script_path="child.mjs",
        )
    )

    main_thread_node = resolver.resolve_thread_node(
        _PARENT_SESSION_ID, resolver.main_thread_id(_PARENT_SESSION_ID)
    )
    child_thread_node = resolver.resolve_thread_node(
        _CHILD_SESSION_ID, resolver.main_thread_id(_CHILD_SESSION_ID)
    )
    main_debug_dir = main_thread_node / "debug" / "node"
    child_debug_dir = child_thread_node / "debug" / "node"
    assert child_dir == resolver.resolve_session_node(_CHILD_SESSION_ID)
    assert (main_debug_dir / "manifest.json").is_file()
    assert (child_debug_dir / "manifest.json").is_file()

    main_manifest = json.loads(
        (main_debug_dir / "manifest.json").read_text(encoding="utf-8")
    )
    child_manifest = json.loads(
        (child_debug_dir / "manifest.json").read_text(encoding="utf-8")
    )
    assert main_manifest["session_id"] == _PARENT_SESSION_ID
    assert main_manifest["thread_id"] == parent_thread_id
    assert main_manifest["active_configuration_id"] == (
        main_state.active_configuration_id
    )
    assert child_manifest["session_id"] == _CHILD_SESSION_ID
    assert child_manifest["thread_id"] == child_thread_id
    assert child_manifest["active_configuration_id"] == (
        child_state.active_configuration_id
    )

    main_ids = {
        path.stem for path in (main_debug_dir / "configurations").glob("*.json")
    }
    child_ids = {
        path.stem for path in (child_debug_dir / "configurations").glob("*.json")
    }
    assert main_ids == {main_state.active_configuration_id}
    assert child_ids == {child_state.active_configuration_id}
    assert main_ids.isdisjoint(child_ids)

    assert store.read_manifest(_PARENT_SESSION_ID, parent_thread_id) is not None
    assert (
        store.read_manifest(
            _PARENT_SESSION_ID, parent_thread_id
        ).active_configuration_id
        == main_state.active_configuration_id
    )
    assert (
        store.read_manifest(_CHILD_SESSION_ID, child_thread_id).active_configuration_id
        == child_state.active_configuration_id
    )
    # Session ID 是节点标识，不是 thread ID；NodeDebug 拒绝它作为别名。
    with pytest.raises(ValueError, match="thread_id 形态非法"):
        store.read_manifest(_PARENT_SESSION_ID, _CHILD_SESSION_ID)


@pytest.mark.asyncio
async def test_child_session_main_owner_is_catalog_canonical(
    session_tree: tuple[object, Path, Path],
    workspace_root: Path,
) -> None:
    """子会话的 canonical main thread 始终定位到自己的独立 NodeDebug owner。"""
    resolver, _parent_dir, _child_dir = session_tree
    child_thread_id = resolver.main_thread_id(_CHILD_SESSION_ID)
    service = _service(workspace_root, resolver)
    store = NodeDebugSessionStore(resolver)

    created = await service.create_configuration(
        NodeDebugConfigurationCreateRequest(
            session_id=_CHILD_SESSION_ID,
            thread_id=child_thread_id,
            name="子线程方案",
            script_path="child.mjs",
        )
    )
    configuration_id = created.active_configuration_id
    assert configuration_id is not None

    from_child_session = await service.get_state(_CHILD_SESSION_ID, child_thread_id)
    assert created == from_child_session
    assert from_child_session.session_id == _CHILD_SESSION_ID
    assert from_child_session.thread_id == child_thread_id
    assert from_child_session.active_configuration_id == configuration_id
    assert [
        item.configuration_id
        for item in service.list_configurations(_CHILD_SESSION_ID, child_thread_id)
    ] == [configuration_id]

    # 所有 NodeDebug 读写都用同一个 catalog owner。
    await service.apply_action(
        command=NodeDebugSetBreakpointActionRequest(
            session_id=_CHILD_SESSION_ID,
            thread_id=child_thread_id,
            action="set_breakpoint",
            params=NodeDebugSetBreakpointParams(path="child.mjs", line=1),
        ),
    )
    after_first = await service.get_state(_CHILD_SESSION_ID, child_thread_id)
    assert [(item.path, item.line) for item in after_first.breakpoints] == [
        ("child.mjs", 1)
    ]

    child_main_node = resolver.resolve_thread_node(_CHILD_SESSION_ID, child_thread_id)
    manifests = sorted(child_main_node.glob("debug/node/manifest.json"))
    assert manifests == [child_main_node / "debug" / "node" / "manifest.json"]
    manifest = store.read_manifest(_CHILD_SESSION_ID, child_thread_id)
    assert manifest is not None
    assert manifest.session_id == _CHILD_SESSION_ID
    assert manifest.thread_id == child_thread_id
    assert manifest.active_configuration_id == configuration_id
    assert all(action.thread_id == child_thread_id for action in manifest.actions)
    with pytest.raises(ValueError, match="thread_id 形态非法"):
        await service.get_state(_PARENT_SESSION_ID, _CHILD_SESSION_ID)


@pytest.mark.asyncio
async def test_mutation_admission_rejects_missing_and_deleted_session(
    session_tree: tuple[object, Path, Path],
    workspace_root: Path,
) -> None:
    resolver, _parent_dir, _child_dir = session_tree
    parent_thread_id = resolver.main_thread_id(_PARENT_SESSION_ID)
    service = NodeDebugService(
        workspace_root=workspace_root,
        config_service=ConfigService(workspace_root=workspace_root),
        session_store=NodeDebugSessionStore(resolver),
        session_admission=NodeDebugSessionAdmission(
            session_service=_ResolverSessionLifecycle(resolver),
            path_resolver=resolver,
        ),
        external_resource_leases=ExternalResourceLeaseLedger(),
    )

    admitted = await service.create_configuration(
        NodeDebugConfigurationCreateRequest(
            session_id=_PARENT_SESSION_ID,
            thread_id=parent_thread_id,
            name="准入方案",
            script_path="main.mjs",
        )
    )
    assert admitted.active_configuration_name == "准入方案"

    with pytest.raises(FileNotFoundError, match="不存在或已删除"):
        await service.create_configuration(
            NodeDebugConfigurationCreateRequest(
                session_id=_MISSING_SESSION_ID,
                thread_id=parent_thread_id,
                name="缺失方案",
                script_path="main.mjs",
            )
        )

    await resolver.delete_subtree(idempotency_key="node-debug-owner-delete", root_node_id=_PARENT_SESSION_ID)
    with pytest.raises(FileNotFoundError, match="不存在或已删除"):
        await service.activate_configuration(
            _PARENT_SESSION_ID,
            admitted.active_configuration_id or "",
            thread_id=parent_thread_id,
        )
    with pytest.raises(FileNotFoundError, match="不存在或已删除"):
        await service.apply_action(
            command=NodeDebugSetBreakpointActionRequest(
                session_id=_PARENT_SESSION_ID,
                thread_id=parent_thread_id,
                action="set_breakpoint",
                params=NodeDebugSetBreakpointParams(path="main.mjs", line=1),
            ),
        )


@pytest.mark.asyncio
async def test_mutation_admission_rejects_foreign_thread(
    session_tree: tuple[object, Path, Path],
    workspace_root: Path,
) -> None:
    resolver, _parent_dir, _child_dir = session_tree
    _create_session(resolver, _OTHER_SESSION_ID)
    service = NodeDebugService(
        workspace_root=workspace_root,
        config_service=ConfigService(workspace_root=workspace_root),
        session_store=NodeDebugSessionStore(resolver),
        session_admission=NodeDebugSessionAdmission(
            session_service=_ResolverSessionLifecycle(resolver),
            path_resolver=resolver,
        ),
        external_resource_leases=ExternalResourceLeaseLedger(),
    )

    foreign_thread_id = resolver.main_thread_id(_OTHER_SESSION_ID)
    with pytest.raises(FileNotFoundError, match="调试目标 thread 不存在"):
        await service.create_configuration(
            NodeDebugConfigurationCreateRequest(
                session_id=_PARENT_SESSION_ID,
                thread_id=foreign_thread_id,
                name="越界方案",
                script_path="main.mjs",
            )
        )
    unknown_thread_id = fixture_main_thread_id(_UNKNOWN_SESSION_ID)
    with pytest.raises(FileNotFoundError, match="调试目标 thread 不存在"):
        await service.create_configuration(
            NodeDebugConfigurationCreateRequest(
                session_id=_PARENT_SESSION_ID,
                thread_id=unknown_thread_id,
                name="未知线程方案",
                script_path="main.mjs",
            )
        )


def _publish_catalog_child_thread(session_node: Path) -> str:
    """用控制库发布 child row，并按冻结 locator 创建其物理节点。"""
    created_at = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
    thread_id = thread_id_at(created_at)
    control = SessionControlStore(session_node / CONTROL_DATABASE_NAME)
    key = f"node-debug-drain-{thread_id}"
    try:
        control.create_or_get_thread_creation_record(
            idempotency_key=key,
            initial_state="idle",
            preimage_hash="a" * 64,
            graph_binding=TEST_THREAD_GRAPH_BINDING_JSON,
            capability_profile="{}",
            created_at=created_at,
            thread_id=thread_id,
        )
        manifest = "{}"
        control.freeze_thread_creation_artifact_manifest(
            key,
            artifact_manifest=manifest,
            artifact_manifest_hash=hashlib.sha256(manifest.encode("utf-8")).hexdigest(),
        )
        relative_locator = control.publish_thread_creation_record(
            key, session_id=session_node.name
        ).final_relative_locator
    finally:
        control.close()
    (session_node / relative_locator).mkdir(parents=True)
    return thread_id


@pytest.mark.asyncio
async def test_drain_session_stops_all_catalog_thread_runtimes_before_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """删除 drain 排空 main 与已发布 child owner，不触碰其它 Session。"""
    resolver = get_session_path_resolver(tmp_path / "sessions")
    resolver.initialize()
    session_node = _create_session(resolver, _PARENT_SESSION_ID)
    parent_thread_id = resolver.main_thread_id(_PARENT_SESSION_ID)
    child_thread_id = _publish_catalog_child_thread(session_node)
    service = NodeDebugService(
        workspace_root=tmp_path,
        config_service=ConfigService(workspace_root=tmp_path),
        session_store=NodeDebugSessionStore(resolver),
        session_admission=NodeDebugSessionAdmission(
            session_service=_ResolverSessionLifecycle(resolver),
            path_resolver=resolver,
        ),
        external_resource_leases=ExternalResourceLeaseLedger(),
    )
    owners = (
        (_PARENT_SESSION_ID, parent_thread_id),
        (_PARENT_SESSION_ID, child_thread_id),
    )
    runtimes = {
        owner: SimpleNamespace(
            session_id=owner[0],
            thread_id=owner[1],
            status="running",
            error_message="running",
            state_lock=asyncio.Lock(),
        )
        for owner in owners
    }
    foreign_owner = (_OTHER_SESSION_ID, _OTHER_MAIN_THREAD_ID)
    foreign_runtime = SimpleNamespace(
        session_id=foreign_owner[0],
        thread_id=foreign_owner[1],
        status="running",
        error_message="running",
        state_lock=asyncio.Lock(),
    )
    service._runtimes.update(runtimes)  # type: ignore[attr-defined]
    service._runtimes[foreign_owner] = foreign_runtime  # type: ignore[attr-defined]
    stopped: list[tuple[str, str]] = []
    reconciled: list[tuple[str, str]] = []

    async def stop_runtime(
        candidate: object,
        *,
        clear_error: bool = True,
    ) -> str:
        del clear_error
        owner = (candidate.session_id, candidate.thread_id)
        assert runtimes[owner] is candidate
        stopped.append(owner)
        return "stopped"

    async def reconcile(candidate_owner: tuple[str, str]) -> None:
        reconciled.append(candidate_owner)

    monkeypatch.setattr(service._lifecycle, "stop_runtime", stop_runtime)
    monkeypatch.setattr(
        service._session_state,
        "persist_runtime_state",
        lambda *args: None,
    )
    monkeypatch.setattr(service._lifecycle, "reconcile_persisted_claim", reconcile)
    monkeypatch.setattr(service._claim_runtime, "active_claim", lambda *_args: None)

    await service.drain_session(_PARENT_SESSION_ID)

    assert stopped == list(owners)
    assert reconciled == list(owners)
    assert all(runtime.status == "exited" for runtime in runtimes.values())
    assert all(runtime.error_message is None for runtime in runtimes.values())
    assert foreign_runtime.status == "running"


@pytest.mark.asyncio
async def test_drain_session_blocks_when_child_claim_cannot_be_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = get_session_path_resolver(tmp_path / "sessions")
    resolver.initialize()
    session_node = _create_session(resolver, _PARENT_SESSION_ID)
    child_thread_id = _publish_catalog_child_thread(session_node)
    service = _service(tmp_path, resolver)
    reconciled: list[tuple[str, str]] = []

    async def reconcile(owner: tuple[str, str]) -> object | None:
        reconciled.append(owner)
        if owner[1] == child_thread_id:
            return SimpleNamespace(
                outcome="reconcile_required",
                reason="claim identity is unknown",
            )
        return None

    monkeypatch.setattr(service._lifecycle, "reconcile_persisted_claim", reconcile)
    monkeypatch.setattr(service._claim_runtime, "active_claim", lambda *_args: None)

    with pytest.raises(RuntimeError, match="保持 reconcile_required 并阻断删除"):
        await service.drain_session(_PARENT_SESSION_ID)

    assert reconciled == [
        (_PARENT_SESSION_ID, resolver.main_thread_id(_PARENT_SESSION_ID)),
        (_PARENT_SESSION_ID, child_thread_id),
    ]


@pytest.mark.asyncio
async def test_drain_session_fails_closed_when_control_catalog_is_missing(
    tmp_path: Path,
) -> None:
    resolver = get_session_path_resolver(tmp_path / "sessions")
    resolver.initialize()
    session_node = _create_session(resolver, _PARENT_SESSION_ID)
    control_path = session_node / CONTROL_DATABASE_NAME
    control_path.unlink()
    service = _service(tmp_path, resolver)

    with pytest.raises(RuntimeError, match="无法核实全部 thread owners"):
        await service.drain_session(_PARENT_SESSION_ID)

    assert not control_path.exists()
