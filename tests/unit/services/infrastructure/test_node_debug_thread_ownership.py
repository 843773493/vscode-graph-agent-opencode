"""NodeDebugService 精确 (session_id, thread_id) 归属迁移的定向单元测试。

覆盖：
- main/child thread 的断点、活动方案与动作时间线严格隔离；
- child thread 调试数据落在受检 thread 节点目录的 ``debug/node/`` 下；
- thread 节点只按权威目录索引解析（不拼路径、不扫盘、不按 session 猜目标）；
- 显式 ``thread_id="main"`` 的 main owner 归一语义；
- mutation 的 Session 生命周期准入（Session 存在且未删除）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.exceptions import NotFoundError
from app.core.path_utils import get_session_path_resolver
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
from app.services.infrastructure.node_debug.session_admission import (
    NodeDebugSessionAdmission,
)
from app.services.infrastructure.node_debug.session_store import NodeDebugSessionStore
from app.services.infrastructure.node_debug.thread_owner import (
    MAIN_THREAD_ID,
    normalize_node_debug_owner,
    resolve_node_debug_owner,
)
from tests.support.catalog_session_bundle import seed_catalog_session_bundle

_PARENT_SESSION_ID = "ses_00000000400040008000000000000001"
_CHILD_SESSION_ID = "ses_00000000400040008000000000000002"
_OTHER_SESSION_ID = "ses_00000000400040008000000000000003"
_GRANDCHILD_SESSION_ID = "ses_00000000400040008000000000000004"
_UNKNOWN_SESSION_ID = "ses_00000000400040008000000000000005"
_MISSING_SESSION_ID = "ses_00000000400040008000000000000006"


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
    ).directory


class _ResolverSessionLifecycle:
    """以权威目录索引模拟 SessionService.get 的“存在且未删除”语义。"""

    def __init__(self, resolver: object) -> None:
        self._resolver = resolver

    async def get(self, session_id: str) -> object:
        try:
            return self._resolver.resolve_session_node_for_runtime(session_id)
        except KeyError as error:
            raise NotFoundError(f"Session {session_id} not found") from error


class _ThreadsDirectoryResolver:
    """独立 thread 节点形态（``<session>/threads/<thread_id>``）的解析器替身。

    生产 ``object`` 把 child thread 解析为子会话自身节点（因此会折叠
    owner）；该替身用于固定“非会话形态 thread 保持 (session_id, thread_id) 原样”
    这一协议分支。
    """

    def __init__(self, root: Path) -> None:
        self._root = root

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


def test_explicit_session_thread_entry_normalizes_to_main_thread() -> None:
    assert normalize_node_debug_owner(_PARENT_SESSION_ID, "main") == (
        _PARENT_SESSION_ID,
        MAIN_THREAD_ID,
    )
    assert normalize_node_debug_owner(
        _PARENT_SESSION_ID, _PARENT_SESSION_ID
    ) == (_PARENT_SESSION_ID, MAIN_THREAD_ID)
    assert normalize_node_debug_owner(
        _PARENT_SESSION_ID, _CHILD_SESSION_ID
    ) == (_PARENT_SESSION_ID, _CHILD_SESSION_ID)

    with pytest.raises(ValueError, match="非空 session_id"):
        normalize_node_debug_owner("", MAIN_THREAD_ID)
    with pytest.raises(ValueError, match="thread_id 不能为空"):
        normalize_node_debug_owner(_PARENT_SESSION_ID, "   ")


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
    resolver, parent_dir, child_dir = session_tree

    main_owner = resolve_node_debug_owner(
        resolver,
        session_id=_PARENT_SESSION_ID,
        thread_id=MAIN_THREAD_ID,
    )
    assert main_owner.thread_id == MAIN_THREAD_ID
    assert main_owner.thread_node == parent_dir
    assert main_owner.key == (_PARENT_SESSION_ID, MAIN_THREAD_ID)

    child_owner = resolve_node_debug_owner(
        resolver,
        session_id=_PARENT_SESSION_ID,
        thread_id=_CHILD_SESSION_ID,
    )
    assert child_owner.thread_node == child_dir
    assert child_owner.thread_node == resolver.resolve_thread_node(
        _PARENT_SESSION_ID, _CHILD_SESSION_ID
    )
    # 单一归属决策：child thread 节点即子会话自身节点，别名地址折叠为子会话的 main 形态。
    assert child_owner.session_id == _CHILD_SESSION_ID
    assert child_owner.thread_id == MAIN_THREAD_ID
    assert child_owner.key == resolve_node_debug_owner(
        resolver,
        session_id=_CHILD_SESSION_ID,
        thread_id=MAIN_THREAD_ID,
    ).key
    assert child_owner.key == (_CHILD_SESSION_ID, MAIN_THREAD_ID)

    with pytest.raises(KeyError):
        resolve_node_debug_owner(
            resolver,
            session_id=_PARENT_SESSION_ID,
            thread_id=_UNKNOWN_SESSION_ID,
        )

    _create_session(resolver, _OTHER_SESSION_ID)
    with pytest.raises(RuntimeError, match="thread 不属于目标 session"):
        resolve_node_debug_owner(
            resolver,
            session_id=_PARENT_SESSION_ID,
            thread_id=_OTHER_SESSION_ID,
        )

    _create_session(
        resolver,
        _GRANDCHILD_SESSION_ID,
        parent_session_id=_CHILD_SESSION_ID,
    )
    with pytest.raises(RuntimeError, match="thread 不属于目标 session"):
        resolve_node_debug_owner(
            resolver,
            session_id=_PARENT_SESSION_ID,
            thread_id=_GRANDCHILD_SESSION_ID,
        )


def test_independent_thread_node_keeps_thread_owner_form(tmp_path: Path) -> None:
    """独立 thread 节点形态不折叠，保持 (session_id, thread_id) owner。"""
    resolver = _ThreadsDirectoryResolver(tmp_path / "sessions")
    owner = resolve_node_debug_owner(
        resolver,
        session_id="ses_parent",
        thread_id="child-thread",
    )

    assert owner.key == ("ses_parent", "child-thread")
    assert owner.thread_node == (
        tmp_path / "sessions" / "ses_parent" / "threads" / "child-thread"
    )


@pytest.mark.asyncio
async def test_main_and_child_thread_state_is_isolated(
    session_tree: tuple[object, Path, Path],
    workspace_root: Path,
) -> None:
    resolver, _parent_dir, _child_dir = session_tree
    service = _service(workspace_root, resolver)

    main_state = await service.create_configuration(
        NodeDebugConfigurationCreateRequest(
            session_id=_PARENT_SESSION_ID,
            thread_id=MAIN_THREAD_ID,
            name="主线程方案",
            script_path="main.mjs",
        )
    )
    child_state = await service.create_configuration(
        NodeDebugConfigurationCreateRequest(
            session_id=_PARENT_SESSION_ID,
            thread_id=_CHILD_SESSION_ID,
            name="子线程方案",
            script_path="child.mjs",
        )
    )

    assert main_state.thread_id == MAIN_THREAD_ID
    assert main_state.session_id == _PARENT_SESSION_ID
    # child thread 的权威 owner 形态是子会话自身的 main：session_id=child, thread_id=main。
    assert child_state.session_id == _CHILD_SESSION_ID
    assert child_state.thread_id == MAIN_THREAD_ID
    assert [item.name for item in main_state.configurations] == ["主线程方案"]
    assert [item.name for item in child_state.configurations] == ["子线程方案"]
    assert main_state.script_path == "main.mjs"
    assert child_state.script_path == "child.mjs"
    assert main_state.active_configuration_id != child_state.active_configuration_id

    main_state = await service.apply_action(
        command=NodeDebugSetBreakpointActionRequest(
            session_id=_PARENT_SESSION_ID,
            thread_id=MAIN_THREAD_ID,
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
            session_id=_PARENT_SESSION_ID,
            thread_id=_CHILD_SESSION_ID,
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
    assert all(item.thread_id == MAIN_THREAD_ID for item in main_state.actions)
    assert all(item.thread_id == MAIN_THREAD_ID for item in child_state.actions)
    assert {item.action_id for item in main_state.actions}.isdisjoint(
        {item.action_id for item in child_state.actions}
    )

    stored_main = await service.get_state(_PARENT_SESSION_ID, MAIN_THREAD_ID)
    stored_child = await service.get_state(_PARENT_SESSION_ID, _CHILD_SESSION_ID)
    # 子会话自身裸入口命中同一 owner，返回完全一致的状态。
    stored_child_bare = await service.get_state(_CHILD_SESSION_ID, MAIN_THREAD_ID)
    assert stored_child == stored_child_bare
    assert [item.path for item in stored_main.breakpoints] == ["main.mjs"]
    assert [item.path for item in stored_child.breakpoints] == ["child.mjs"]
    assert [item.name for item in stored_main.configurations] == ["主线程方案"]
    assert [item.name for item in stored_child.configurations] == ["子线程方案"]

    with pytest.raises(FileNotFoundError, match="调试方案不存在"):
        service.get_configuration(
            _PARENT_SESSION_ID,
            child_state.active_configuration_id or "",
            MAIN_THREAD_ID,
        )


@pytest.mark.asyncio
async def test_child_thread_debug_data_lands_in_thread_node_directory(
    session_tree: tuple[object, Path, Path],
    workspace_root: Path,
) -> None:
    resolver, parent_dir, child_dir = session_tree
    service = _service(workspace_root, resolver)
    store = NodeDebugSessionStore(resolver)

    main_state = await service.create_configuration(
        NodeDebugConfigurationCreateRequest(
            session_id=_PARENT_SESSION_ID,
            thread_id=MAIN_THREAD_ID,
            name="主线程方案",
            script_path="main.mjs",
        )
    )
    child_state = await service.create_configuration(
        NodeDebugConfigurationCreateRequest(
            session_id=_PARENT_SESSION_ID,
            thread_id=_CHILD_SESSION_ID,
            name="子线程方案",
            script_path="child.mjs",
        )
    )

    main_debug_dir = parent_dir / "debug" / "node"
    child_debug_dir = child_dir / "debug" / "node"
    assert child_dir == resolver.resolve_thread_node(
        _PARENT_SESSION_ID, _CHILD_SESSION_ID
    )
    assert (main_debug_dir / "manifest.json").is_file()
    assert (child_debug_dir / "manifest.json").is_file()

    main_manifest = json.loads(
        (main_debug_dir / "manifest.json").read_text(encoding="utf-8")
    )
    child_manifest = json.loads(
        (child_debug_dir / "manifest.json").read_text(encoding="utf-8")
    )
    assert main_manifest["session_id"] == _PARENT_SESSION_ID
    assert main_manifest["thread_id"] == MAIN_THREAD_ID
    assert main_manifest["active_configuration_id"] == (
        main_state.active_configuration_id
    )
    # 子 thread 的持久化 owner 是子会话自身的 main 形态。
    assert child_manifest["session_id"] == _CHILD_SESSION_ID
    assert child_manifest["thread_id"] == MAIN_THREAD_ID
    assert child_manifest["active_configuration_id"] == (
        child_state.active_configuration_id
    )

    main_ids = {path.stem for path in (main_debug_dir / "configurations").glob("*.json")}
    child_ids = {
        path.stem for path in (child_debug_dir / "configurations").glob("*.json")
    }
    assert main_ids == {main_state.active_configuration_id}
    assert child_ids == {child_state.active_configuration_id}
    assert main_ids.isdisjoint(child_ids)

    assert store.read_manifest(_PARENT_SESSION_ID, MAIN_THREAD_ID) is not None
    assert store.read_manifest(
        _PARENT_SESSION_ID, MAIN_THREAD_ID
    ).active_configuration_id == main_state.active_configuration_id
    assert store.read_manifest(
        _CHILD_SESSION_ID, MAIN_THREAD_ID
    ).active_configuration_id == child_state.active_configuration_id
    # 旧的别名形态不再是 owner：直接按 (parent, child) 读会 fail-loud，绝不静默返回
    # 另一份数据（服务入口已折叠，因此正常路径不会走到这里）。
    with pytest.raises(RuntimeError, match="SessionThread 不匹配"):
        store.read_manifest(_PARENT_SESSION_ID, _CHILD_SESSION_ID)


@pytest.mark.asyncio
async def test_child_thread_and_child_session_main_address_share_one_owner(
    session_tree: tuple[object, Path, Path],
    workspace_root: Path,
) -> None:
    """两个地址命中同一 owner：状态一致、断点/方案同源、无覆盖写。"""
    resolver, _parent_dir, child_dir = session_tree
    service = _service(workspace_root, resolver)
    store = NodeDebugSessionStore(resolver)

    created = await service.create_configuration(
        NodeDebugConfigurationCreateRequest(
            session_id=_PARENT_SESSION_ID,
            thread_id=_CHILD_SESSION_ID,
            name="子线程方案",
            script_path="child.mjs",
        )
    )
    configuration_id = created.active_configuration_id
    assert configuration_id is not None

    from_child_thread = await service.get_state(
        _PARENT_SESSION_ID, _CHILD_SESSION_ID
    )
    from_child_session = await service.get_state(_CHILD_SESSION_ID, MAIN_THREAD_ID)
    from_child_session_explicit_main = await service.get_state(
        _CHILD_SESSION_ID, MAIN_THREAD_ID
    )
    assert from_child_thread == from_child_session
    assert from_child_thread == from_child_session_explicit_main
    assert from_child_thread.session_id == _CHILD_SESSION_ID
    assert from_child_thread.thread_id == MAIN_THREAD_ID
    assert from_child_thread.active_configuration_id == configuration_id
    assert [
        item.configuration_id
        for item in service.list_configurations(_PARENT_SESSION_ID, _CHILD_SESSION_ID)
    ] == [configuration_id]
    assert [
        item.configuration_id
        for item in service.list_configurations(_CHILD_SESSION_ID, MAIN_THREAD_ID)
    ] == [configuration_id]

    # 经 (parent, child) 加断点，经 (child, main) 必须看到同一条时间线。
    await service.apply_action(
        command=NodeDebugSetBreakpointActionRequest(
            session_id=_PARENT_SESSION_ID,
            thread_id=_CHILD_SESSION_ID,
            action="set_breakpoint",
            params=NodeDebugSetBreakpointParams(path="child.mjs", line=1),
        ),
    )
    after_first = await service.get_state(_CHILD_SESSION_ID, MAIN_THREAD_ID)
    assert [(item.path, item.line) for item in after_first.breakpoints] == [
        ("child.mjs", 1)
    ]

    # 经 (child, main) 再加一个断点，反向地址同样看到两个断点，且无覆盖写。
    await service.apply_action(
        command=NodeDebugSetBreakpointActionRequest(
            session_id=_CHILD_SESSION_ID,
            thread_id=MAIN_THREAD_ID,
            action="set_breakpoint",
            params=NodeDebugSetBreakpointParams(path="child.mjs", line=2),
        ),
    )
    after_second = await service.get_state(
        _PARENT_SESSION_ID, _CHILD_SESSION_ID
    )
    assert [(item.path, item.line) for item in after_second.breakpoints] == [
        ("child.mjs", 1),
        ("child.mjs", 2),
    ]
    # 两个地址写的是同一条时间线：前一段动作记录没有被覆盖。
    assert (
        after_second.actions[: len(after_first.actions)] == after_first.actions
    )
    assert len(after_second.actions) > len(after_first.actions)

    # 只有子会话节点下一个 manifest，owner 恒为 (child, main)。
    manifests = sorted(child_dir.glob("debug/node/manifest.json"))
    assert manifests == [child_dir / "debug" / "node" / "manifest.json"]
    manifest = store.read_manifest(_CHILD_SESSION_ID, MAIN_THREAD_ID)
    assert manifest is not None
    assert manifest.session_id == _CHILD_SESSION_ID
    assert manifest.thread_id == MAIN_THREAD_ID
    assert manifest.active_configuration_id == configuration_id
    assert all(action.thread_id == MAIN_THREAD_ID for action in manifest.actions)
    manifest_messages = " ".join(action.message for action in manifest.actions)
    assert "child.mjs:1" in manifest_messages
    assert "child.mjs:2" in manifest_messages


@pytest.mark.asyncio
async def test_mutation_admission_rejects_missing_and_deleted_session(
    session_tree: tuple[object, Path, Path],
    workspace_root: Path,
) -> None:
    resolver, _parent_dir, _child_dir = session_tree
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
            thread_id=MAIN_THREAD_ID,
            name="准入方案",
            script_path="main.mjs",
        )
    )
    assert admitted.active_configuration_name == "准入方案"

    with pytest.raises(FileNotFoundError, match="不存在或已删除"):
        await service.create_configuration(
            NodeDebugConfigurationCreateRequest(
                session_id=_MISSING_SESSION_ID,
                thread_id=MAIN_THREAD_ID,
                name="缺失方案",
                script_path="main.mjs",
            )
        )

    await resolver.delete_session_subtree(_PARENT_SESSION_ID)
    with pytest.raises(FileNotFoundError, match="不存在或已删除"):
        await service.activate_configuration(
            _PARENT_SESSION_ID,
            admitted.active_configuration_id or "",
            thread_id=MAIN_THREAD_ID,
        )
    with pytest.raises(FileNotFoundError, match="不存在或已删除"):
        await service.apply_action(
            command=NodeDebugSetBreakpointActionRequest(
                session_id=_PARENT_SESSION_ID,
                thread_id=MAIN_THREAD_ID,
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

    with pytest.raises(RuntimeError, match="thread 不属于目标 session"):
        await service.create_configuration(
            NodeDebugConfigurationCreateRequest(
                session_id=_PARENT_SESSION_ID,
                thread_id=_OTHER_SESSION_ID,
                name="越界方案",
                script_path="main.mjs",
            )
        )
    with pytest.raises(FileNotFoundError, match="调试目标 thread 不存在"):
        await service.create_configuration(
            NodeDebugConfigurationCreateRequest(
                session_id=_PARENT_SESSION_ID,
                thread_id=_UNKNOWN_SESSION_ID,
                name="未知线程方案",
                script_path="main.mjs",
            )
        )


@pytest.mark.asyncio
async def test_drain_session_stops_exact_main_runtime_before_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """删除 drain 只停止目标 Session 的 main owner，并核实 claim 已收敛。"""
    resolver = get_session_path_resolver(tmp_path / "sessions")
    resolver.initialize()
    service = NodeDebugService(
        workspace_root=tmp_path,
        config_service=ConfigService(workspace_root=tmp_path),
        session_admission=NodeDebugSessionAdmission(
            session_service=_ResolverSessionLifecycle(resolver),
            path_resolver=resolver,
        ),
        external_resource_leases=ExternalResourceLeaseLedger(),
    )
    runtime = SimpleNamespace(
        session_id=_PARENT_SESSION_ID,
        thread_id=MAIN_THREAD_ID,
        status="running",
        error_message="running",
        state_lock=asyncio.Lock(),
    )
    owner = (_PARENT_SESSION_ID, MAIN_THREAD_ID)
    service._runtimes[owner] = runtime  # type: ignore[attr-defined]
    stopped: list[tuple[str, str]] = []
    reconciled: list[tuple[str, str]] = []

    async def stop_runtime(
        candidate: object,
        *,
        clear_error: bool = True,
    ) -> str:
        del clear_error
        assert candidate is runtime
        stopped.append((runtime.session_id, runtime.thread_id))
        return "stopped"

    async def reconcile(candidate_owner: tuple[str, str]) -> None:
        reconciled.append(candidate_owner)

    monkeypatch.setattr(service._lifecycle, "stop_runtime", stop_runtime)
    monkeypatch.setattr(service, "_persist_session_state", lambda *args: None)
    monkeypatch.setattr(service._lifecycle, "reconcile_persisted_claim", reconcile)
    monkeypatch.setattr(service._claim_runtime, "active_claim", lambda *_args: None)

    await service.drain_session(_PARENT_SESSION_ID)

    assert stopped == [owner]
    assert reconciled == [owner]
    assert runtime.status == "exited"
    assert runtime.error_message is None
