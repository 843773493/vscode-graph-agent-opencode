"""typed ``node_debug_process`` lease 的登记、保持与结清单元测试。

覆盖 R5a（OpenSpec 任务 3.5 的 lease 部分，design.md 第 60 行账本要求）：

- 握手成功、claim 进入 ``running`` 时登记 typed 资源与跨 Turn 占用 lease；
- owner 核实终态、claim 结清（settle）时结清同一 lease；
- ``reconcile_required`` 期间占用保持 active，作为重启后 owner 的恢复引用；
- backend 重启冷恢复不重复 acquire（幂等），只在再次核实终态后结清；
- lease 不驱动服务行为：状态判断始终以 durable claim + OS 起始身份核实为准；
- 账本操作失败 fail-closed，不吞成“看起来已登记”。

本文件只验证账本接入点，claim 状态机本身由
``test_node_debug_launch_claim.py`` 覆盖；为保持证据文件自包含，这里自带
与既有测试风格一致的替身（真实子进程提供可信 OS 起始身份）。
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
import websockets

from app.core.path_utils import get_session_path_resolver
from app.schemas.internal_v2.node_debug import (
    NodeDebugConfigurationCreateRequest,
    NodeDebugControlActionRequest,
    NodeDebugLaunchClaimDTO,
)
from app.services.infrastructure.events.channel_events import (
    ResourceStateEventPublisher,
)
from app.services.infrastructure.events.event_channel_service import (
    EventChannelService,
)
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.node_debug.claim_runtime import (
    NodeDebugProcessLeaseIdentity,
)
from app.services.infrastructure.node_debug.launch_claim import (
    claim_running,
    claim_with_spawn_identity,
    new_launch_claim,
)
from app.services.infrastructure.node_debug.process_identity import (
    IDENTITY_SOURCE_PSUTIL,
    probe_process_identity,
)
from app.services.infrastructure.node_debug.runtime_state import NodeDebugRuntime
from app.services.infrastructure.node_debug.service import NodeDebugService
from app.services.infrastructure.node_debug.session_store import NodeDebugSessionStore
from tests.support.catalog_session_bundle import seed_catalog_session_bundle
from tests.support.node_debug_dependencies import (
    permissive_node_debug_session_admission,
)

_PARENT_SESSION_ID = "ses_00000000400040008000000000000001"
_CONFIGURATION_ID = "dbgcfg_22222222222222222222222222222222"


def _create_session(resolver: object, session_id: str) -> Path:
    title = f"测试会话 {session_id}"
    return seed_catalog_session_bundle(
        resolver.sessions_root,
        session_id,
        title=title,
    ).directory


@pytest.fixture
def session_tree(tmp_path: Path) -> object:
    sessions_root = tmp_path / ".boxteam" / "sessions"
    resolver = get_session_path_resolver(sessions_root)
    resolver.initialize()
    _create_session(resolver, _PARENT_SESSION_ID)
    return resolver


class _FakeInspectorSocket:
    """按需应答的 Inspector WebSocket 替身，让真实 ``_command``/接收循环可跑通。"""

    def __init__(self) -> None:
        self._responses: asyncio.Queue[str] = asyncio.Queue()
        self.closed = False

    async def send(self, message: str) -> None:
        payload = json.loads(message)
        command_id = payload.get("id")
        if isinstance(command_id, int):
            await self._responses.put(json.dumps({"id": command_id, "result": {}}))

    def __aiter__(self) -> _FakeInspectorSocket:
        return self

    async def __anext__(self) -> str:
        if self.closed:
            raise StopAsyncIteration
        return await self._responses.get()

    async def close(self) -> None:
        self.closed = True


class _FakeNodeProcess:
    """包装真实子进程，暴露 asyncio 子进程句柄需要的属性。

    真实子进程提供可信的 OS 起始身份（PID + starttime），身份核对走生产代码。
    """

    def __init__(self, child: subprocess.Popen[bytes], inspector_url: str | None) -> None:
        self._child = child
        self.returncode: int | None = None
        self.stderr = asyncio.StreamReader()
        self.stdout = asyncio.StreamReader()
        if inspector_url is not None:
            self.stderr.feed_data(f"Debugger listening on {inspector_url}\n".encode())
        self.stderr.feed_eof()
        self.stdout.feed_eof()

    @property
    def pid(self) -> int:
        return self._child.pid

    def terminate(self) -> None:
        self._child.terminate()

    def kill(self) -> None:
        self._child.kill()

    async def wait(self) -> int:
        while self._child.poll() is None:
            await asyncio.sleep(0.01)
        self.returncode = self._child.returncode
        assert self.returncode is not None
        return self.returncode


class _UnstoppableProcess:
    """终止信号无效的进程替身；真实 pid 用于走生产代码的身份核实。"""

    def __init__(self, child: subprocess.Popen[bytes]) -> None:
        self._child = child
        self.returncode: int | None = None

    @property
    def pid(self) -> int:
        return self._child.pid

    def terminate(self) -> None:
        return None

    def kill(self) -> None:
        return None

    async def wait(self) -> int:
        await asyncio.Event().wait()
        raise AssertionError("不可达：wait() 必须超时")


class _DebugConfigStub:
    """只提供 ``get_debug_runtime_config``，把 Inspector 端口固定成可断言值。"""

    def __init__(self, inspector_port: int) -> None:
        self._inspector_port = inspector_port

    def get_debug_runtime_config(self) -> dict[str, object]:
        return {
            "enabled": True,
            "default_adapter": "node_inspector",
            "command_timeout_seconds": 5.0,
            "node": {
                "inspector_host": "127.0.0.1",
                "inspector_port": self._inspector_port,
                "executable": "",
            },
            "python": {
                "adapter": "debugpy",
                "debugpy_host": "127.0.0.1",
                "debugpy_port": 0,
            },
            "launch_profiles": {
                "node-default": {
                    "adapter": "node_inspector",
                    "runtime": "node",
                    "program": "",
                    "working_directory": "",
                    "args": [],
                }
            },
        }


def _make_service(
    tmp_path: Path,
    resolver: object,
    external_resource_leases: ExternalResourceLeaseLedger,
    *,
    inspector_port: int | None = None,
    state_events: ResourceStateEventPublisher | None = None,
) -> tuple[NodeDebugService, NodeDebugSessionStore, Path]:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir(exist_ok=True)
    (workspace_root / "entry.mjs").write_text(
        "const answer = 42;\nconsole.log(answer);\n",
        encoding="utf-8",
    )
    store = NodeDebugSessionStore(resolver)
    service = NodeDebugService(
        workspace_root=workspace_root,
        session_store=store,
        config_service=(
            None if inspector_port is None else _DebugConfigStub(inspector_port)
        ),
        external_resource_leases=external_resource_leases,
        session_admission=permissive_node_debug_session_admission(),
        state_events=state_events,
    )
    # 真实 spawn 由测试替身接管，二进制名只用于通过“必须能找到 Node”的前置检查，
    # 让本文件的 lease 证据不依赖宿主是否安装 Node。
    service._node_bin = "fake-node"
    return service, store, workspace_root


@pytest.mark.asyncio
async def test_release_failure_publishes_release_failed_state_event(
    tmp_path: Path,
    session_tree: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """owner 停止失败进入 reconcile_required 时发布 release_failed 事件。"""
    monkeypatch.setattr(
        "app.services.infrastructure.node_debug.process_lifecycle._TERMINATE_TIMEOUT_SECONDS",
        0.05,
    )
    monkeypatch.setattr(
        "app.services.infrastructure.node_debug.process_lifecycle._KILL_TIMEOUT_SECONDS",
        0.05,
    )
    manager = ExternalResourceLeaseLedger(state_path=tmp_path / "resources.json")
    event_service = EventChannelService()
    service, store, workspace_root = _make_service(
        tmp_path,
        session_tree,
        manager,
        state_events=ResourceStateEventPublisher(
            event_service=event_service,
            owner_domain="node_debug",
        ),
    )
    subscription = event_service.channel("resource.state/node_debug").subscribe(
        label="test"
    )
    created = await service.create_configuration(
        NodeDebugConfigurationCreateRequest(
            session_id=_PARENT_SESSION_ID,
            thread_id="main",
            name="入口调试",
            script_path="entry.mjs",
        )
    )
    configuration_id = created.active_configuration_id
    assert configuration_id is not None

    child = _sleeper_child()
    try:
        real_identity = probe_process_identity(child.pid)
        assert real_identity is not None and real_identity.verifiable
        claim = claim_running(
            claim_with_spawn_identity(
                new_launch_claim(
                    session_id=_PARENT_SESSION_ID,
                    thread_id="main",
                    configuration_id=configuration_id,
                    inspector_host="127.0.0.1",
                    inspector_port=9229,
                ),
                pid=child.pid,
                identity=real_identity,
            ),
            inspector_port=9229,
        )
        store.write_launch_claim(claim)
        identity = _seed_active_lease(manager, claim.process_instance_id)
        runtime = _runtime_for(
            workspace_root,
            configuration_id=configuration_id,
            process_instance_id=claim.process_instance_id,
            process=_UnstoppableProcess(child),
        )
        runtime.process_identity_source = real_identity.source
        runtime.process_start_marker = real_identity.start_marker
        service._runtimes[(_PARENT_SESSION_ID, "main")] = runtime

        blocked = await service.apply_action(
            command=NodeDebugControlActionRequest(
                session_id=_PARENT_SESSION_ID,
                thread_id="main",
                action="stop",
            ),
        )
        assert blocked.status == "reconcile_required"
        # 通知只携带轻量身份字段，不夹带 reason 正文或进程细节。
        deliveries = subscription.pending()
        assert [delivery.event.state for delivery in deliveries] == [
            "release_failed"
        ]
        assert [delivery.event.resource_id for delivery in deliveries] == [
            identity.resource_id
        ]
        assert [delivery.event.owner_domain for delivery in deliveries] == [
            "node_debug"
        ]
        # 账本未接 publisher：成功终态的 released 通知由账本层负责，这里不重复。
        child.terminate()
        child.wait(timeout=10)
        await service.apply_action(
            command=NodeDebugControlActionRequest(
                session_id=_PARENT_SESSION_ID,
                thread_id="main",
                action="stop",
            ),
        )
        assert subscription.pending() == ()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def _sleeper_child() -> subprocess.Popen[bytes]:
    """真实可定点停止的子进程，充当调试实例。"""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])


def _lease_identity(process_instance_id: str) -> NodeDebugProcessLeaseIdentity:
    return NodeDebugProcessLeaseIdentity.for_process_instance(
        session_id=_PARENT_SESSION_ID,
        thread_id="main",
        process_instance_id=process_instance_id,
    )


def _seed_active_lease(
    manager: ExternalResourceLeaseLedger,
    process_instance_id: str,
) -> NodeDebugProcessLeaseIdentity:
    identity = _lease_identity(process_instance_id)
    manager.register_external(
        resource_id=identity.resource_id,
        kind="node_debug_process",
        lifetime_scope="session",
    )
    manager.acquire(
        resource_id=identity.resource_id,
        turn_stream_id=identity.holder_id,
        lease_id=identity.lease_id,
        operation_id=identity.operation_id,
    )
    return identity


def _runtime_for(
    workspace_root: Path,
    *,
    configuration_id: str,
    process_instance_id: str,
    process: object | None = None,
) -> NodeDebugRuntime:
    runtime = NodeDebugRuntime(
        session_id=_PARENT_SESSION_ID,
        thread_id="main",
        configuration_id=configuration_id,
        workspace_root=workspace_root,
        script_path=workspace_root / "entry.mjs",
        relative_script_path="entry.mjs",
        working_directory=workspace_root,
        status="running",
        process=process,  # type: ignore[arg-type]
    )
    runtime.process_instance_id = process_instance_id
    return runtime


def _claim(
    *,
    process_instance_id: str,
    phase: str = "running",
    pid: int | None = None,
    source: str | None = None,
    marker: str | None = None,
) -> NodeDebugLaunchClaimDTO:
    now = datetime.now(UTC)
    return NodeDebugLaunchClaimDTO(
        session_id=_PARENT_SESSION_ID,
        thread_id="main",
        process_instance_id=process_instance_id,
        nonce="nonce-lease",
        phase=phase,  # type: ignore[arg-type]
        configuration_id=_CONFIGURATION_ID,
        pid=pid,
        process_identity_source=source,
        process_start_marker=marker,
        inspector_host="127.0.0.1",
        inspector_port=9229,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_handshake_registers_typed_lease_and_verified_stop_settles_it(
    tmp_path: Path,
    session_tree: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """running→active lease；核实终态→同一 lease 结清，并 durable 落盘。"""
    manager = ExternalResourceLeaseLedger(state_path=tmp_path / "resources.json")
    service, store, _workspace_root = _make_service(
        tmp_path,
        session_tree,
        manager,
        inspector_port=0,
    )
    child = _sleeper_child()

    async def fake_exec(*args: object, **kwargs: object) -> _FakeNodeProcess:
        return _FakeNodeProcess(child, "ws://127.0.0.1:45678/fake-target")

    async def fake_connect(url: object, **kwargs: object) -> _FakeInspectorSocket:
        return _FakeInspectorSocket()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(websockets, "connect", fake_connect)
    try:
        state = await service.start(
            session_id=_PARENT_SESSION_ID,
            thread_id="main",
            path="entry.mjs",
            args=[],
            breakpoints=[],
        )
        assert state.status == "running"
        claim = store.read_launch_claim(_PARENT_SESSION_ID, "main")
        assert claim is not None
        assert claim.phase == "running"

        identity = _lease_identity(claim.process_instance_id)
        record = manager.get(identity.resource_id)
        assert record is not None
        assert record.kind == "node_debug_process"
        assert record.lifetime_scope == "session"

        leases = manager.leases_for_turn(identity.holder_id)
        assert [lease.lease_id for lease in leases] == [identity.lease_id]
        assert leases[0].status == "active"
        assert leases[0].resource_id == identity.resource_id
        # 占用 holder 是 debug owner 的 process_instance_id，不是本次 tool/Web 调用。
        assert leases[0].operation_id == claim.process_instance_id

        stopped = await service.apply_action(
            command=NodeDebugControlActionRequest(
                session_id=_PARENT_SESSION_ID,
                thread_id="main",
                action="stop",
            ),
        )
        assert stopped.status == "exited"
        persisted = store.read_launch_claim(_PARENT_SESSION_ID, "main")
        assert persisted is not None
        assert persisted.phase == "settled"
        settled = manager.get_lease(identity.lease_id)
        assert settled is not None
        assert settled.status == "settled"

        # 落盘证据：backend 重启后从同一 state path 仍读到结清结果。
        reloaded = ExternalResourceLeaseLedger(state_path=tmp_path / "resources.json")
        reloaded_lease = reloaded.get_lease(identity.lease_id)
        assert reloaded_lease is not None
        assert reloaded_lease.status == "settled"
    finally:
        await service.close()
        if child.poll() is None:
            child.kill()
            child.wait()


@pytest.mark.asyncio
async def test_reconcile_required_keeps_lease_active_until_termination_verified(
    tmp_path: Path,
    session_tree: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无法核实终态时占用保持 active；再次核实终结后才结清同一 lease。"""
    monkeypatch.setattr(
        "app.services.infrastructure.node_debug.process_lifecycle._TERMINATE_TIMEOUT_SECONDS",
        0.05,
    )
    monkeypatch.setattr(
        "app.services.infrastructure.node_debug.process_lifecycle._KILL_TIMEOUT_SECONDS",
        0.05,
    )
    manager = ExternalResourceLeaseLedger(state_path=tmp_path / "resources.json")
    service, store, workspace_root = _make_service(tmp_path, session_tree, manager)
    created = await service.create_configuration(
        NodeDebugConfigurationCreateRequest(
            session_id=_PARENT_SESSION_ID,
            thread_id="main",
            name="入口调试",
            script_path="entry.mjs",
        )
    )
    configuration_id = created.active_configuration_id
    assert configuration_id is not None

    child = _sleeper_child()
    try:
        real_identity = probe_process_identity(child.pid)
        assert real_identity is not None and real_identity.verifiable
        claim = claim_running(
            claim_with_spawn_identity(
                new_launch_claim(
                    session_id=_PARENT_SESSION_ID,
                    thread_id="main",
                    configuration_id=configuration_id,
                    inspector_host="127.0.0.1",
                    inspector_port=9229,
                ),
                pid=child.pid,
                identity=real_identity,
            ),
            inspector_port=9229,
        )
        store.write_launch_claim(claim)
        identity = _seed_active_lease(manager, claim.process_instance_id)
        runtime = _runtime_for(
            workspace_root,
            configuration_id=configuration_id,
            process_instance_id=claim.process_instance_id,
            process=_UnstoppableProcess(child),
        )
        runtime.process_identity_source = real_identity.source
        runtime.process_start_marker = real_identity.start_marker
        service._runtimes[(_PARENT_SESSION_ID, "main")] = runtime

        blocked = await service.apply_action(
            command=NodeDebugControlActionRequest(
                session_id=_PARENT_SESSION_ID,
                thread_id="main",
                action="stop",
            ),
        )
        assert blocked.status == "reconcile_required"
        persisted = store.read_launch_claim(_PARENT_SESSION_ID, "main")
        assert persisted is not None
        assert persisted.phase == "reconcile_required"
        kept = manager.get_lease(identity.lease_id)
        assert kept is not None
        assert kept.status == "active"
        assert child.poll() is None

        child.terminate()
        child.wait(timeout=10)
        released = await service.apply_action(
            command=NodeDebugControlActionRequest(
                session_id=_PARENT_SESSION_ID,
                thread_id="main",
                action="stop",
            ),
        )
        assert released.status == "exited"
        persisted_after = store.read_launch_claim(_PARENT_SESSION_ID, "main")
        assert persisted_after is not None
        assert persisted_after.phase == "settled"
        assert manager.get_lease(identity.lease_id).status == "settled"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


@pytest.mark.asyncio
async def test_restart_recovery_keeps_active_lease_without_duplicate_acquire(
    tmp_path: Path,
    session_tree: object,
) -> None:
    """重启恢复：账本已有 active 占用时保持，不重复 acquire，也不误杀进程。"""
    child = _sleeper_child()
    try:
        real_identity = probe_process_identity(child.pid)
        assert real_identity is not None and real_identity.verifiable
        store = NodeDebugSessionStore(session_tree)
        # 登记的来源与当前探测来源不同构：事实不足 ⇒ reconcile_required，不认领不停止。
        claim = _claim(
            process_instance_id="node-debug-proc_restart_keep",
            pid=child.pid,
            source=IDENTITY_SOURCE_PSUTIL,
            marker=real_identity.start_marker,
        )
        store.write_launch_claim(claim)

        manager_before = ExternalResourceLeaseLedger(state_path=tmp_path / "resources.json")
        identity = _seed_active_lease(manager_before, claim.process_instance_id)

        # 模拟 backend 重启：新的 ExternalResourceLeaseLedger 从同一 durable state path 恢复账本。
        manager_after = ExternalResourceLeaseLedger(state_path=tmp_path / "resources.json")
        service, _store, _workspace_root = _make_service(
            tmp_path,
            session_tree,
            manager_after,
        )

        first = await service.get_state(_PARENT_SESSION_ID, "main")
        assert first.status == "reconcile_required"
        assert child.poll() is None
        kept = manager_after.get_lease(identity.lease_id)
        assert kept is not None
        assert kept.status == "active"

        # 轮询读接口幂等：不重复 acquire、不新增占用行、也不虚报终态。
        second = await service.get_state(_PARENT_SESSION_ID, "main")
        assert second.status == "reconcile_required"
        leases = manager_after.leases_for_turn(identity.holder_id)
        assert [lease.lease_id for lease in leases] == [identity.lease_id]
        assert leases[0].status == "active"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


@pytest.mark.asyncio
async def test_restart_recovery_settles_lease_only_after_instance_verified_gone(
    tmp_path: Path,
    session_tree: object,
) -> None:
    """重启恢复：只有在核实登记实例确已不存在之后才结清 claim 与 lease。"""
    gone = _sleeper_child()
    real_identity = probe_process_identity(gone.pid)
    assert real_identity is not None and real_identity.verifiable
    gone.kill()
    gone.wait(timeout=10)

    store = NodeDebugSessionStore(session_tree)
    claim = _claim(
        process_instance_id="node-debug-proc_restart_gone",
        pid=gone.pid,
        source=real_identity.source,
        marker=real_identity.start_marker,
    )
    store.write_launch_claim(claim)
    manager_before = ExternalResourceLeaseLedger(state_path=tmp_path / "resources.json")
    identity = _seed_active_lease(manager_before, claim.process_instance_id)

    manager_after = ExternalResourceLeaseLedger(state_path=tmp_path / "resources.json")
    service, _store, _workspace_root = _make_service(
        tmp_path,
        session_tree,
        manager_after,
    )

    state = await service.get_state(_PARENT_SESSION_ID, "main")
    assert state.status == "idle"
    persisted = store.read_launch_claim(_PARENT_SESSION_ID, "main")
    assert persisted is not None
    assert persisted.phase == "settled"
    settled = manager_after.get_lease(identity.lease_id)
    assert settled is not None
    assert settled.status == "settled"
    # 恢复过程只结清既有占用，不重复 acquire 出第二行。
    assert [lease.lease_id for lease in manager_after.leases_for_turn(identity.holder_id)] == [
        identity.lease_id
    ]


@pytest.mark.asyncio
async def test_active_lease_without_claim_or_process_reports_idle(
    tmp_path: Path,
    session_tree: object,
) -> None:
    """账本里的 active 占用不是进程状态来源：没有 claim/进程时必须报 idle。"""
    manager = ExternalResourceLeaseLedger(state_path=tmp_path / "resources.json")
    service, _store, _workspace_root = _make_service(tmp_path, session_tree, manager)
    ghost = _seed_active_lease(manager, "node-debug-proc_ghost")

    state = await service.get_state(_PARENT_SESSION_ID, "main")
    assert state.status == "idle"
    # 也不会阻断 owner 级操作：lease 不驱动任何服务行为。
    created = await service.create_configuration(
        NodeDebugConfigurationCreateRequest(
            session_id=_PARENT_SESSION_ID,
            thread_id="main",
            name="账本孤儿占用不阻断",
            script_path="entry.mjs",
        )
    )
    assert created.active_configuration_name == "账本孤儿占用不阻断"
    assert manager.get_lease(ghost.lease_id).status == "active"


@pytest.mark.asyncio
async def test_ledger_settlement_does_not_change_verified_running_report(
    tmp_path: Path,
    session_tree: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """反向证据：账本占用被外部结清时，服务仍按 claim + OS 事实报告 running 并可停止。"""
    manager = ExternalResourceLeaseLedger(state_path=tmp_path / "resources.json")
    service, store, _workspace_root = _make_service(
        tmp_path,
        session_tree,
        manager,
        inspector_port=0,
    )
    child = _sleeper_child()

    async def fake_exec(*args: object, **kwargs: object) -> _FakeNodeProcess:
        return _FakeNodeProcess(child, "ws://127.0.0.1:45678/fake-target")

    async def fake_connect(url: object, **kwargs: object) -> _FakeInspectorSocket:
        return _FakeInspectorSocket()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(websockets, "connect", fake_connect)
    try:
        await service.start(
            session_id=_PARENT_SESSION_ID,
            thread_id="main",
            path="entry.mjs",
            args=[],
            breakpoints=[],
        )
        claim = store.read_launch_claim(_PARENT_SESSION_ID, "main")
        assert claim is not None
        identity = _lease_identity(claim.process_instance_id)
        # 外部把账本占用错误结清：进程与 claim 事实不变。
        manager.settle(identity.lease_id)
        assert manager.get_lease(identity.lease_id).status == "settled"

        state = await service.get_state(_PARENT_SESSION_ID, "main")
        assert state.status == "running"
        assert state.pid == child.pid

        stopped = await service.apply_action(
            command=NodeDebugControlActionRequest(
                session_id=_PARENT_SESSION_ID,
                thread_id="main",
                action="stop",
            ),
        )
        assert stopped.status == "exited"
        assert child.poll() is not None
    finally:
        await service.close()
        if child.poll() is None:
            child.kill()
            child.wait()


@pytest.mark.asyncio
async def test_ledger_conflict_fails_start_closed_without_fake_registration(
    tmp_path: Path,
    session_tree: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """账本操作失败必须显式抛出，且不得留下“看起来已登记”的占用。"""
    manager = ExternalResourceLeaseLedger(state_path=tmp_path / "resources.json")
    service, store, _workspace_root = _make_service(
        tmp_path,
        session_tree,
        manager,
        inspector_port=0,
    )
    conflicting_id = f"node_debug_process:{_PARENT_SESSION_ID}:main"
    manager.register(
        resource_id=conflicting_id,
        kind="terminal",
        lifetime_scope="session",
    )
    child = _sleeper_child()

    async def fake_exec(*args: object, **kwargs: object) -> _FakeNodeProcess:
        return _FakeNodeProcess(child, "ws://127.0.0.1:45678/fake-target")

    async def fake_connect(url: object, **kwargs: object) -> _FakeInspectorSocket:
        return _FakeInspectorSocket()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(websockets, "connect", fake_connect)
    try:
        with pytest.raises(RuntimeError, match="资源类型发生冲突"):
            await service.start(
                session_id=_PARENT_SESSION_ID,
                thread_id="main",
                path="entry.mjs",
                args=[],
                breakpoints=[],
            )

        # 失败收口：进程被真实停止、claim 结清，账本里没有伪造出的占用行。
        assert child.poll() is not None
        persisted = store.read_launch_claim(_PARENT_SESSION_ID, "main")
        assert persisted is not None
        assert persisted.phase == "settled"
        assert manager.leases_for_turn(f"node-debug-owner:{_PARENT_SESSION_ID}:main") == []
    finally:
        await service.close()
        if child.poll() is None:
            child.kill()
            child.wait()
