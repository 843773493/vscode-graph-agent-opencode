"""launch claim 持久化、写侧别名防护、OS 进程身份与恢复决策的定向单元测试。

覆盖 R3b 任务 3.2 / 3.5 的服务层核心：
- claim 在 ``<thread_node>/debug/node/launch-claim.json`` durable 往返并校验 owner；
- 写路径拒绝 ``(P, child)`` 别名（折叠 key 校验）；
- OS 起始身份读取、比对与 PID 复用判定；
- 崩溃恢复决策矩阵（结清 / 核实后停止 / 保持 reconcile_required）；
- 服务层状态机：``stopping`` 可观察且结清时机、运行阻断不提前解除、spawn 前登记、
  握手成功才 ``running``、冷启动按 claim 定点恢复、``reconcile_required`` 的
  进入/阻断/解除，以及旧 generation 回调的写保护。
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
import websockets

from app.core.path_utils import get_session_path_resolver
from app.schemas.internal_v2.node_debug import (
    NodeDebugConfigurationCreateRequest,
    NodeDebugConfigurationDTO,
    NodeDebugConfigurationUpdateRequest,
    NodeDebugLaunchClaimDTO,
    NodeDebugSessionManifestDTO,
)
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.node_debug import process_lifecycle
from app.services.infrastructure.node_debug.launch_claim import (
    claim_marked,
    claim_running,
    claim_with_spawn_identity,
    decide_claim_recovery,
    new_launch_claim,
)
from app.services.infrastructure.node_debug.process_identity import (
    IDENTITY_SOURCE_LINUX_PROC,
    IDENTITY_SOURCE_PSUTIL,
    NodeDebugProcessIdentity,
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
_CHILD_SESSION_ID = "ses_00000000400040008000000000000002"
_CONFIGURATION_ID = "dbgcfg_11111111111111111111111111111111"


def _create_session(
    resolver: object,
    session_id: str,
    *,
    parent_session_id: str | None = None,
) -> Path:
    title = f"测试会话 {session_id}"
    return seed_catalog_session_bundle(
        resolver.sessions_root,
        session_id,
        title=title,
        parent_node_id=parent_session_id,
    ).directory


@pytest.fixture
def session_tree(tmp_path: Path) -> tuple[object, Path, Path]:
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


def _claim(
    *,
    session_id: str = _PARENT_SESSION_ID,
    thread_id: str = "main",
    phase: str = "launch_pending",
    pid: int | None = None,
    source: str | None = None,
    marker: str | None = None,
) -> NodeDebugLaunchClaimDTO:
    now = datetime.now(UTC)
    return NodeDebugLaunchClaimDTO(
        session_id=session_id,
        thread_id=thread_id,
        process_instance_id="node-debug-proc_22222222222222222222222222222222",
        nonce="nonce-abc",
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


def test_launch_claim_round_trip_and_owner_validation(
    session_tree: tuple[object, Path, Path],
) -> None:
    resolver, parent_dir, _child_dir = session_tree
    store = NodeDebugSessionStore(resolver)

    assert store.read_launch_claim(_PARENT_SESSION_ID, "main") is None
    claim = new_launch_claim(
        session_id=_PARENT_SESSION_ID,
        thread_id="main",
        configuration_id=_CONFIGURATION_ID,
        inspector_host="127.0.0.1",
        inspector_port=9229,
    )
    store.write_launch_claim(claim)

    claim_path = parent_dir / "debug" / "node" / "launch-claim.json"
    assert claim_path.is_file()
    loaded = store.read_launch_claim(_PARENT_SESSION_ID, "main")
    assert loaded is not None
    assert loaded.phase == "launch_pending"
    assert loaded.pid is None
    assert loaded.process_instance_id == claim.process_instance_id
    assert loaded.nonce == claim.nonce
    # 每次启动必须是唯一的 process instance + 一次性 nonce，否则崩溃后无法区分代际。
    another = new_launch_claim(
        session_id=_PARENT_SESSION_ID,
        thread_id="main",
        configuration_id=_CONFIGURATION_ID,
        inspector_host="127.0.0.1",
        inspector_port=9229,
    )
    assert loaded.nonce != another.nonce
    assert loaded.process_instance_id != another.process_instance_id

    # 篡改 owner 的 claim 读回必须 fail-loud，不得静默返回别的 thread 数据。
    tampered = json.loads(claim_path.read_text(encoding="utf-8"))
    tampered["thread_id"] = _CHILD_SESSION_ID
    claim_path.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(RuntimeError, match="SessionThread 不匹配"):
        store.read_launch_claim(_PARENT_SESSION_ID, "main")


def test_store_write_paths_reject_alias_owner(
    session_tree: tuple[object, Path, Path],
) -> None:
    resolver, parent_dir, child_dir = session_tree
    store = NodeDebugSessionStore(resolver)
    now = datetime.now(UTC)

    with pytest.raises(ValueError, match="拒绝别名写入"):
        store.write_manifest(
            NodeDebugSessionManifestDTO(
                session_id=_PARENT_SESSION_ID,
                thread_id=_CHILD_SESSION_ID,
                updated_at=now,
            )
        )
    with pytest.raises(ValueError, match="拒绝别名写入"):
        store.write_launch_claim(
            _claim(
                session_id=_PARENT_SESSION_ID,
                thread_id=_CHILD_SESSION_ID,
                phase="launch_pending",
            )
        )
    with pytest.raises(ValueError, match="拒绝别名写入"):
        store.write_configuration(
            _PARENT_SESSION_ID,
            NodeDebugConfigurationDTO(
                configuration_id=_CONFIGURATION_ID,
                name="别名方案",
                created_at=now,
                updated_at=now,
            ),
            _CHILD_SESSION_ID,
        )
    with pytest.raises(ValueError, match="拒绝别名写入"):
        store.delete_configuration(
            _PARENT_SESSION_ID, _CONFIGURATION_ID, _CHILD_SESSION_ID
        )

    assert not (child_dir / "debug" / "node" / "manifest.json").exists()
    assert not (parent_dir / "debug" / "node" / "manifest.json").exists()

    # 折叠后的权威形态可以正常写入子会话节点。
    store.write_manifest(
        NodeDebugSessionManifestDTO(
            session_id=_CHILD_SESSION_ID,
            thread_id="main",
            updated_at=now,
        )
    )
    assert (child_dir / "debug" / "node" / "manifest.json").is_file()


def test_process_identity_reports_real_start_marker_and_detects_pid_reuse() -> None:
    identity = probe_process_identity(os.getpid())
    assert identity is not None
    assert identity.verifiable
    assert identity.source == IDENTITY_SOURCE_LINUX_PROC
    assert (
        identity.compare(
            recorded_source=identity.source,
            recorded_start_marker=identity.start_marker,
        )
        == "match"
    )
    # 同一来源、不同起始标记 ⇒ PID 复用，不是同一实例。
    assert (
        identity.compare(
            recorded_source=identity.source,
            recorded_start_marker=f"{identity.start_marker}-other",
        )
        == "mismatch"
    )
    # 跨来源：标记不同构，属"事实不足"，绝不能被当成 mismatch（＝原实例已终结）。
    assert (
        identity.compare(
            recorded_source="another-source",
            recorded_start_marker=identity.start_marker,
        )
        == "incomparable"
    )
    assert (
        identity.compare(
            recorded_source=identity.source,
            recorded_start_marker=None,
        )
        == "incomparable"
    )

    # 已退出的进程读不到身份，视为不存在。
    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    finished.wait()
    assert probe_process_identity(finished.pid) is None

    with pytest.raises(ValueError, match="正整数"):
        probe_process_identity(0)


def test_recovery_decision_matrix() -> None:
    def probe(identity: NodeDebugProcessIdentity | None):
        return lambda _pid: identity

    # launch_pending 且没有 PID：无法证明进程不存在 ⇒ reconcile_required。
    decision = decide_claim_recovery(_claim(phase="launch_pending"))
    assert decision.outcome == "reconcile_required"
    assert "没有 PID" in decision.reason

    # 登记的实例已不存在 ⇒ 结清。
    decision = decide_claim_recovery(_claim(phase="running", pid=4242), identity_probe=probe(None))
    assert decision.outcome == "settle"
    assert "已不存在" in decision.reason

    # 身份匹配 ⇒ 核实为同一实例，按 owner 策略停止后结清。
    matching = NodeDebugProcessIdentity(
        pid=4242,
        source=IDENTITY_SOURCE_LINUX_PROC,
        start_marker="boot:100",
    )
    decision = decide_claim_recovery(
        _claim(phase="running", pid=4242, source=IDENTITY_SOURCE_LINUX_PROC, marker="boot:100"),
        identity_probe=probe(matching),
    )
    assert decision.outcome == "terminate_then_settle"
    assert decision.identity == matching

    # PID 被复用（起始身份不同）⇒ 不认领也不停止新进程，只结清旧登记。
    reused = NodeDebugProcessIdentity(
        pid=4242,
        source=IDENTITY_SOURCE_LINUX_PROC,
        start_marker="boot:999",
    )
    decision = decide_claim_recovery(
        _claim(phase="running", pid=4242, source=IDENTITY_SOURCE_LINUX_PROC, marker="boot:100"),
        identity_probe=probe(reused),
    )
    assert decision.outcome == "settle"
    assert "复用" in decision.reason

    # 缺少可核实的起始身份 ⇒ 保持阻断。
    unverifiable = NodeDebugProcessIdentity(
        pid=4242,
        source="unavailable",
        start_marker=None,
    )
    decision = decide_claim_recovery(
        _claim(phase="running", pid=4242, source="unavailable", marker=None),
        identity_probe=probe(unverifiable),
    )
    assert decision.outcome == "reconcile_required"
    assert "起始身份" in decision.reason

    # 跨来源（宿主探测能力变化）⇒ 事实不足：绝不能当成"原实例已终结"而结清。
    cross_source = NodeDebugProcessIdentity(
        pid=4242,
        source=IDENTITY_SOURCE_PSUTIL,
        start_marker="boot:100",
    )
    decision = decide_claim_recovery(
        _claim(
            phase="running",
            pid=4242,
            source=IDENTITY_SOURCE_LINUX_PROC,
            marker="boot:100",
        ),
        identity_probe=probe(cross_source),
    )
    assert decision.outcome == "reconcile_required"
    assert "不可比对" in decision.reason

    # 已处于 reconcile_required 的 claim 不会自动降级为结清。
    decision = decide_claim_recovery(
        claim_marked(
            _claim(phase="running", pid=4242, source=IDENTITY_SOURCE_LINUX_PROC, marker="boot:100"),
            phase="reconcile_required",
            reason="人工核实中",
        ),
        identity_probe=probe(matching),
    )
    assert decision.outcome == "reconcile_required"
    assert decision.reason == "人工核实中"

    # 已处于 reconcile_required 的 claim 也不能靠"跨来源"解除。
    decision = decide_claim_recovery(
        claim_marked(
            _claim(
                phase="running",
                pid=4242,
                source=IDENTITY_SOURCE_LINUX_PROC,
                marker="boot:100",
            ),
            phase="reconcile_required",
            reason="人工核实中",
        ),
        identity_probe=probe(cross_source),
    )
    assert decision.outcome == "reconcile_required"
    assert decision.reason == "人工核实中"


def test_claim_phase_transitions_require_expected_phase() -> None:
    pending = _claim(phase="launch_pending")
    identity = NodeDebugProcessIdentity(
        pid=4242,
        source=IDENTITY_SOURCE_LINUX_PROC,
        start_marker="boot:100",
    )
    spawned = claim_with_spawn_identity(pending, pid=4242, identity=identity)
    assert spawned.phase == "spawned"
    assert spawned.pid == 4242

    running = claim_running(spawned, inspector_port=9230)
    assert running.phase == "running"
    assert running.inspector_port == 9230

    with pytest.raises(RuntimeError, match="只有 spawned claim"):
        claim_running(pending, inspector_port=9230)
    with pytest.raises(RuntimeError, match="只有 launch_pending claim"):
        claim_with_spawn_identity(running, pid=4242, identity=identity)
    with pytest.raises(ValueError, match="必须记录无法核实的原因"):
        claim_marked(running, phase="reconcile_required")


# ---------------------------------------------------------------------------
# 服务层状态机：stopping 可观察、claim 登记时序、冷恢复、reconcile_required 阻断与解除
# ---------------------------------------------------------------------------


class _FakeInspectorSocket:
    """按需应答的 Inspector WebSocket 替身，让真实 ``_command``/接收循环可跑通。"""

    def __init__(self) -> None:
        self._responses: asyncio.Queue[str] = asyncio.Queue()
        self.closed = False

    async def send(self, message: str) -> None:
        payload = json.loads(message)
        command_id = payload.get("id")
        if isinstance(command_id, int):
            await self._responses.put(
                json.dumps({"id": command_id, "result": {}})
            )

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

    真实子进程提供可信的 OS 起始身份（PID + starttime），因此身份核对与
    PID 复用判定都走生产代码，不做形式化断言。
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


class _BlockingProcess:
    """可控制"何时真正终结"的进程替身，用于卡住 stopping 窗口。"""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.terminate_called = asyncio.Event()
        self.release_wait = asyncio.Event()

    def terminate(self) -> None:
        self.terminate_called.set()

    def kill(self) -> None:
        self.release_wait.set()

    async def wait(self) -> int:
        await self.release_wait.wait()
        self.returncode = 0
        return self.returncode


def _make_service(
    tmp_path: Path,
    resolver: object,
    *,
    inspector_port: int | None = None,
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
            _DebugConfigStub(inspector_port or 0)
        ),
        session_admission=permissive_node_debug_session_admission(),
        external_resource_leases=ExternalResourceLeaseLedger(),
    )
    return service, store, workspace_root


def _sleeper_child() -> subprocess.Popen[bytes]:
    """真实可定点停止的子进程，充当崩溃后遗留的旧调试实例。"""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])


def _runtime_for(
    workspace_root: Path,
    *,
    configuration_id: str,
    process_instance_id: str,
    process: object | None = None,
    status: str = "running",
) -> NodeDebugRuntime:
    runtime = NodeDebugRuntime(
        session_id=_PARENT_SESSION_ID,
        thread_id="main",
        configuration_id=configuration_id,
        workspace_root=workspace_root,
        script_path=workspace_root / "entry.mjs",
        relative_script_path="entry.mjs",
        working_directory=workspace_root,
        status=status,  # type: ignore[arg-type]
        process=process,  # type: ignore[arg-type]
    )
    runtime.process_instance_id = process_instance_id
    return runtime


@pytest.mark.asyncio
async def test_stopping_state_is_queryable_and_owner_stays_blocked_until_termination(
    tmp_path: Path,
    session_tree: tuple[object, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``stopping`` 期间状态可查询、不报终态，且 durable claim 不得提前结清。"""
    # 把终止/强杀超时放大：stopping 窗口必须由测试显式释放进程才结束，
    # 不能因为真实超时到点而把替身 kill 掉、让窗口在断言中途自己合上。
    monkeypatch.setattr(
        "app.services.infrastructure.node_debug.process_lifecycle._TERMINATE_TIMEOUT_SECONDS",
        60.0,
    )
    monkeypatch.setattr(
        "app.services.infrastructure.node_debug.process_lifecycle._KILL_TIMEOUT_SECONDS",
        60.0,
    )
    resolver, _parent_dir, _child_dir = session_tree
    service, store, workspace_root = _make_service(tmp_path, resolver)
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

    claim = claim_running(
        claim_with_spawn_identity(
            new_launch_claim(
                session_id=_PARENT_SESSION_ID,
                thread_id="main",
                configuration_id=configuration_id,
                inspector_host="127.0.0.1",
                inspector_port=9229,
            ),
            pid=4242,
            identity=NodeDebugProcessIdentity(
                pid=4242,
                source=IDENTITY_SOURCE_LINUX_PROC,
                start_marker="boot:4242",
            ),
        ),
        inspector_port=9229,
    )
    process = _BlockingProcess(4242)
    runtime = _runtime_for(
        workspace_root,
        configuration_id=configuration_id,
        process_instance_id=claim.process_instance_id,
        process=process,
    )
    store.write_launch_claim(claim)
    service._runtimes[(_PARENT_SESSION_ID, "main")] = runtime

    stop_task = asyncio.create_task(
        service.apply_action(
            session_id=_PARENT_SESSION_ID,
            thread_id="main",
            action="stop",
            params={},
        )
    )
    await process.terminate_called.wait()

    stopping = await service.get_state(_PARENT_SESSION_ID, "main")
    assert stopping.status == "stopping"
    assert stopping.pid == 4242
    assert stopping.error_message is None
    persisted = store.read_launch_claim(_PARENT_SESSION_ID, "main")
    assert persisted is not None
    assert persisted.phase == "stopping"
    # 进程尚未核实终结：owner 级运行阻断不得解除。
    with pytest.raises(RuntimeError, match="运行中"):
        await service.update_configuration(
            configuration_id,
            NodeDebugConfigurationUpdateRequest(
                session_id=_PARENT_SESSION_ID,
                thread_id="main",
                name="停止中改名",
                script_path="entry.mjs",
            ),
        )

    process.release_wait.set()
    stopped = await stop_task
    assert stopped.status == "exited"
    persisted_after = store.read_launch_claim(_PARENT_SESSION_ID, "main")
    assert persisted_after is not None
    assert persisted_after.phase == "settled"
    # 结清后阻断解除：同一个方案可以再次改名。
    await service.update_configuration(
        configuration_id,
        NodeDebugConfigurationUpdateRequest(
            session_id=_PARENT_SESSION_ID,
            thread_id="main",
            name="结清后改名",
            script_path="entry.mjs",
        ),
    )


@pytest.mark.asyncio
async def test_cold_launch_pending_claim_surfaces_reconcile_required_and_blocks_owner(
    tmp_path: Path,
    session_tree: tuple[object, Path, Path],
) -> None:
    """登记后、spawn 前崩溃：读接口如实报告无法核实，并阻断启动/重启/动作。"""
    resolver, _parent_dir, _child_dir = session_tree
    service, store, _workspace_root = _make_service(tmp_path, resolver)
    store.write_launch_claim(
        new_launch_claim(
            session_id=_PARENT_SESSION_ID,
            thread_id="main",
            configuration_id=_CONFIGURATION_ID,
            inspector_host="127.0.0.1",
            inspector_port=9229,
        )
    )

    state = await service.get_state(_PARENT_SESSION_ID, "main")
    assert state.status == "reconcile_required"
    assert state.error_message is not None
    assert "launch_pending" in state.error_message
    persisted = store.read_launch_claim(_PARENT_SESSION_ID, "main")
    assert persisted is not None
    assert persisted.phase == "reconcile_required"
    assert persisted.pid is None

    # 轮询读接口不得每次追加动作或反复重写登记。
    reconcile_actions = [
        action
        for action in (await service.get_state(_PARENT_SESSION_ID, "main")).actions
        if action.action == "reconcile_required"
    ]
    assert len(reconcile_actions) == 1

    with pytest.raises(RuntimeError, match="拒绝启动新实例"):
        await service.start(
            session_id=_PARENT_SESSION_ID,
            thread_id="main",
            path="entry.mjs",
            args=[],
            breakpoints=[],
        )
    with pytest.raises(RuntimeError, match="未结清"):
        await service.restart(_PARENT_SESSION_ID, thread_id="main")
    with pytest.raises(RuntimeError, match="未结清"):
        await service.apply_action(
            session_id=_PARENT_SESSION_ID,
            thread_id="main",
            action="continue",
            params={},
        )


@pytest.mark.asyncio
async def test_cold_recovery_terminates_only_the_verified_orphan(
    tmp_path: Path,
    session_tree: tuple[object, Path, Path],
) -> None:
    """崩溃后按 claim 恢复：身份匹配才定点停止，结清后才允许 owner 级操作。"""
    resolver, _parent_dir, _child_dir = session_tree
    service, store, _workspace_root = _make_service(tmp_path, resolver)
    orphan = _sleeper_child()
    try:
        identity = probe_process_identity(orphan.pid)
        assert identity is not None and identity.verifiable
        store.write_launch_claim(
            _claim(
                session_id=_PARENT_SESSION_ID,
                thread_id="main",
                phase="running",
                pid=orphan.pid,
                source=identity.source,
                marker=identity.start_marker,
            )
        )

        state = await service.get_state(_PARENT_SESSION_ID, "main")
        assert state.status == "idle"
        # SIGTERM 后子进程需要一点时间被回收，断言给有界等待而不是瞬时假设。
        for _ in range(200):
            if orphan.poll() is not None:
                break
            await asyncio.sleep(0.05)
        assert orphan.poll() is not None
        persisted = store.read_launch_claim(_PARENT_SESSION_ID, "main")
        assert persisted is not None
        assert persisted.phase == "settled"
        assert state.actions[-1].action == "reconcile_settled"
        await service.create_configuration(
            NodeDebugConfigurationCreateRequest(
                session_id=_PARENT_SESSION_ID,
                thread_id="main",
                name="恢复后新建方案",
                script_path="entry.mjs",
            )
        )
    finally:
        if orphan.poll() is None:
            orphan.kill()
            orphan.wait()


@pytest.mark.asyncio
async def test_cold_recovery_never_kills_a_process_on_a_reused_pid(
    tmp_path: Path,
    session_tree: tuple[object, Path, Path],
) -> None:
    """PID 已被回收复用：不能认领，更绝不能将新进程当旧实例停止。"""
    resolver, _parent_dir, _child_dir = session_tree
    service, store, _workspace_root = _make_service(tmp_path, resolver)
    squatter = _sleeper_child()
    try:
        store.write_launch_claim(
            _claim(
                session_id=_PARENT_SESSION_ID,
                thread_id="main",
                phase="running",
                pid=squatter.pid,
                source=IDENTITY_SOURCE_LINUX_PROC,
                marker="boot:1-not-the-recorded-instance",
            )
        )

        state = await service.get_state(_PARENT_SESSION_ID, "main")
        assert state.status == "idle"
        assert squatter.poll() is None
        persisted = store.read_launch_claim(_PARENT_SESSION_ID, "main")
        assert persisted is not None
        assert persisted.phase == "settled"
        assert "复用" in state.actions[-1].message
    finally:
        squatter.kill()
        squatter.wait()


@pytest.mark.asyncio
async def test_reconcile_required_releases_only_after_termination_is_verified(
    tmp_path: Path,
    session_tree: tuple[object, Path, Path],
) -> None:
    """reconcile_required 既不自动接管也不自动终止；核实终结后才解除阻断。"""
    resolver, _parent_dir, _child_dir = session_tree
    service, store, _workspace_root = _make_service(tmp_path, resolver)
    live = _sleeper_child()
    try:
        identity = probe_process_identity(live.pid)
        assert identity is not None and identity.verifiable
        store.write_launch_claim(
            claim_marked(
                _claim(
                    session_id=_PARENT_SESSION_ID,
                    thread_id="main",
                    phase="running",
                    pid=live.pid,
                    source=identity.source,
                    marker=identity.start_marker,
                ),
                phase="reconcile_required",
                reason="停止时无法核实终态",
            )
        )

        blocked = await service.get_state(_PARENT_SESSION_ID, "main")
        assert blocked.status == "reconcile_required"
        assert live.poll() is None
        with pytest.raises(RuntimeError, match="拒绝启动新实例"):
            await service.start(
                session_id=_PARENT_SESSION_ID,
                thread_id="main",
                path="entry.mjs",
                args=[],
                breakpoints=[],
            )

        # 人工核实并让旧实例真正终结后，下一次读取核实到进程不存在即结清。
        live.terminate()
        live.wait(timeout=10)
        released = await service.get_state(_PARENT_SESSION_ID, "main")
        assert released.status == "idle"
        persisted = store.read_launch_claim(_PARENT_SESSION_ID, "main")
        assert persisted is not None
        assert persisted.phase == "settled"
        assert "不再存在" in released.actions[-1].message
    finally:
        if live.poll() is None:
            live.kill()
            live.wait()


@pytest.mark.asyncio
async def test_start_registers_claim_before_spawn_and_running_only_after_handshake(
    tmp_path: Path,
    session_tree: tuple[object, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """spawn 前 durable 登记 launch_pending；握手成功才登记权威 PID/端口。"""
    resolver, _parent_dir, _child_dir = session_tree
    # 生产模板使用动态端口（0）：真实端口只有 Node 上报的握手 URL 才可信。
    service, store, _workspace_root = _make_service(
        tmp_path,
        resolver,
        inspector_port=0,
    )
    observed: dict[str, NodeDebugLaunchClaimDTO | None] = {}
    child = _sleeper_child()
    original_read = store.read_launch_claim

    async def fake_exec(*args: object, **kwargs: object) -> _FakeNodeProcess:
        # 交接给 OS 之前的瞬间：登记必须已经落盘，且尚无 PID/运行属性。
        observed["at_spawn"] = original_read(_PARENT_SESSION_ID, "main")
        return _FakeNodeProcess(child, "ws://127.0.0.1:45678/fake-target")

    async def fake_connect(url: object, **kwargs: object) -> _FakeInspectorSocket:
        # 握手发生的瞬间：仍是 spawned，PID/端口还没成为权威运行属性。
        observed["at_handshake"] = original_read(_PARENT_SESSION_ID, "main")
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

        at_spawn = observed["at_spawn"]
        assert at_spawn is not None
        assert at_spawn.phase == "launch_pending"
        assert at_spawn.pid is None
        assert at_spawn.process_start_marker is None
        assert at_spawn.nonce and at_spawn.process_instance_id
        # 启动模板是动态端口：登记里的端口只是意图值，不能当作运行事实。
        assert at_spawn.inspector_port == 0

        at_handshake = observed["at_handshake"]
        assert at_handshake is not None
        assert at_handshake.phase == "spawned"
        assert at_handshake.pid == child.pid
        assert at_handshake.process_start_marker is not None
        # 握手发生的那一刻仍未 running：PID/端口尚未成为权威运行属性。
        assert at_handshake.inspector_port == 0

        assert state.status == "running"
        assert state.pid == child.pid
        persisted = original_read(_PARENT_SESSION_ID, "main")
        assert persisted is not None
        assert persisted.phase == "running"
        assert persisted.pid == child.pid
        # 权威端口来自握手 URL 实际报告的端口，而不是模板的 0。
        assert persisted.inspector_port == 45678
        assert persisted.process_instance_id == at_spawn.process_instance_id
    finally:
        await service.close()
        if child.poll() is None:
            child.kill()
            child.wait()


@pytest.mark.asyncio
async def test_stale_generation_callback_cannot_write_new_instance_claim(
    tmp_path: Path,
    session_tree: tuple[object, Path, Path],
) -> None:
    """旧 generation 的回调只能认到自己的登记，绝不能改写新实例。"""
    resolver, _parent_dir, _child_dir = session_tree
    service, store, workspace_root = _make_service(tmp_path, resolver)
    current = new_launch_claim(
        session_id=_PARENT_SESSION_ID,
        thread_id="main",
        configuration_id=_CONFIGURATION_ID,
        inspector_host="127.0.0.1",
        inspector_port=9229,
    )
    store.write_launch_claim(current)
    stale = _runtime_for(
        workspace_root,
        configuration_id=_CONFIGURATION_ID,
        process_instance_id="node-debug-proc_stale_generation",
    )

    service._mark_claim_phase(stale, "settled", "旧代际回调")
    service._mark_claim_running(stale)

    persisted = store.read_launch_claim(_PARENT_SESSION_ID, "main")
    assert persisted is not None
    assert persisted.phase == "launch_pending"
    assert persisted.process_instance_id == current.process_instance_id

    current_runtime = _runtime_for(
        workspace_root,
        configuration_id=_CONFIGURATION_ID,
        process_instance_id=current.process_instance_id,
    )
    service._mark_claim_phase(current_runtime, "settled", "本代际结清")
    persisted_after = store.read_launch_claim(_PARENT_SESSION_ID, "main")
    assert persisted_after is not None
    assert persisted_after.phase == "settled"


@pytest.mark.asyncio
async def test_spawn_failure_settles_prewrite_and_leaves_no_blocker(
    tmp_path: Path,
    session_tree: tuple[object, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """spawn 调用本身失败：可证明没有产生实例，登记必须当场结清而不是永久阻断。"""
    resolver, _parent_dir, _child_dir = session_tree
    service, store, _workspace_root = _make_service(tmp_path, resolver)
    service._node_bin = "/nonexistent/node"
    observed: list[NodeDebugLaunchClaimDTO | None] = []

    async def failing_exec(*args: object, **kwargs: object) -> _FakeNodeProcess:
        observed.append(store.read_launch_claim(_PARENT_SESSION_ID, "main"))
        raise FileNotFoundError(2, "No such file or directory", "/nonexistent/node")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", failing_exec)

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

    with pytest.raises(RuntimeError, match="启动 Node Inspector 失败"):
        await service.start(
            session_id=_PARENT_SESSION_ID,
            thread_id="main",
            path="entry.mjs",
            args=[],
            breakpoints=[],
        )

    # spawn 前的 durable 登记：已有唯一实例 ID 与 nonce，但还没有 PID/运行属性。
    assert len(observed) == 1
    assert observed[0] is not None
    assert observed[0].phase == "launch_pending"
    assert observed[0].pid is None
    assert observed[0].process_start_marker is None
    assert observed[0].nonce

    persisted = store.read_launch_claim(_PARENT_SESSION_ID, "main")
    assert persisted is not None
    assert persisted.phase == "settled"
    assert persisted.process_instance_id == observed[0].process_instance_id

    failed = await service.get_state(_PARENT_SESSION_ID, "main")
    assert failed.status == "failed"
    assert failed.actions[-1].action == "start_failed"

    # 结清后 owner 级操作不再被登记阻断。
    second = await service.create_configuration(
        NodeDebugConfigurationCreateRequest(
            session_id=_PARENT_SESSION_ID,
            thread_id="main",
            name="第二个方案",
            script_path="entry.mjs",
            activate=True,
        )
    )
    assert second.active_configuration_name == "第二个方案"
    assert (
        store.read_launch_claim(_PARENT_SESSION_ID, "main") is not None
    )  # 未被新启动覆写时仍保留上一次的结清登记


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


@pytest.mark.asyncio
async def test_stop_failure_enters_reconcile_required_and_releases_after_verification(
    tmp_path: Path,
    session_tree: tuple[object, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """停止失败且进程仍存活：保持 reconcile_required 不虚报终态，核实终结后才解除。"""
    resolver, _parent_dir, _child_dir = session_tree
    service, store, workspace_root = _make_service(tmp_path, resolver)
    monkeypatch.setattr(
        "app.services.infrastructure.node_debug.process_lifecycle._TERMINATE_TIMEOUT_SECONDS",
        0.05,
    )
    monkeypatch.setattr(
        "app.services.infrastructure.node_debug.process_lifecycle._KILL_TIMEOUT_SECONDS",
        0.05,
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
        identity = probe_process_identity(child.pid)
        assert identity is not None and identity.verifiable
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
                identity=identity,
            ),
            inspector_port=9229,
        )
        store.write_launch_claim(claim)
        runtime = _runtime_for(
            workspace_root,
            configuration_id=configuration_id,
            process_instance_id=claim.process_instance_id,
            process=_UnstoppableProcess(child),
        )
        runtime.process_identity_source = identity.source
        runtime.process_start_marker = identity.start_marker
        service._runtimes[(_PARENT_SESSION_ID, "main")] = runtime

        blocked = await service.apply_action(
            session_id=_PARENT_SESSION_ID,
            thread_id="main",
            action="stop",
            params={},
        )
        assert blocked.status == "reconcile_required"
        assert blocked.error_message is not None
        assert "无法核实终态" in blocked.error_message
        assert blocked.actions[-1].action == "stop_reconcile_required"
        persisted = store.read_launch_claim(_PARENT_SESSION_ID, "main")
        assert persisted is not None
        assert persisted.phase == "reconcile_required"
        # 未被核实终结前，owner 级操作仍被阻断，且绝不自动去动这个进程。
        assert child.poll() is None
        with pytest.raises(RuntimeError, match="拒绝重启"):
            await service.restart(_PARENT_SESSION_ID, thread_id="main")
        with pytest.raises(RuntimeError, match="运行中"):
            await service.update_configuration(
                configuration_id,
                NodeDebugConfigurationUpdateRequest(
                    session_id=_PARENT_SESSION_ID,
                    thread_id="main",
                    name="阻断期间改名",
                    script_path="entry.mjs",
                ),
            )

        # 外部核实并终止旧实例后，重试停止即可核实终结并结清登记。
        child.terminate()
        child.wait(timeout=10)
        released = await service.apply_action(
            session_id=_PARENT_SESSION_ID,
            thread_id="main",
            action="stop",
            params={},
        )
        assert released.status == "exited"
        persisted_after = store.read_launch_claim(_PARENT_SESSION_ID, "main")
        assert persisted_after is not None
        assert persisted_after.phase == "settled"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


@pytest.mark.asyncio
async def test_cross_source_identity_cannot_report_termination(
    tmp_path: Path,
    session_tree: tuple[object, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """登记来源与当前探测来源不同构：事实不足，不得判为已终结（否则会虚报 exited）。"""
    resolver, _parent_dir, _child_dir = session_tree
    service, store, workspace_root = _make_service(tmp_path, resolver)
    monkeypatch.setattr(
        "app.services.infrastructure.node_debug.process_lifecycle._TERMINATE_TIMEOUT_SECONDS",
        0.05,
    )
    monkeypatch.setattr(
        "app.services.infrastructure.node_debug.process_lifecycle._KILL_TIMEOUT_SECONDS",
        0.05,
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
        runtime = _runtime_for(
            workspace_root,
            configuration_id=configuration_id,
            process_instance_id=claim.process_instance_id,
            process=_UnstoppableProcess(child),
        )
        # 登记时宿主用的是另一套来源（例如当时 psutil 可用，现在只剩 /proc）。
        runtime.process_identity_source = IDENTITY_SOURCE_PSUTIL
        runtime.process_start_marker = real_identity.start_marker
        service._runtimes[(_PARENT_SESSION_ID, "main")] = runtime

        stopped = await service.apply_action(
            session_id=_PARENT_SESSION_ID,
            thread_id="main",
            action="stop",
            params={},
        )
        # 进程句柄没报告终态、身份又不可比对 ⇒ 只能保持 reconcile_required。
        assert stopped.status == "reconcile_required"
        assert stopped.error_message is not None
        assert "不可比对" in stopped.error_message
        assert child.poll() is None
        persisted = store.read_launch_claim(_PARENT_SESSION_ID, "main")
        assert persisted is not None
        assert persisted.phase == "reconcile_required"
    finally:
        child.kill()
        child.wait()


@pytest.mark.asyncio
async def test_reconcile_terminate_rechecks_identity_before_sigkilling(
    tmp_path: Path,
    session_tree: tuple[object, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """等待窗口内 PID 被回收复用：只接受复用/消失作为证据，绝不升级 SIGKILL。"""
    resolver, _parent_dir, _child_dir = session_tree
    service, _store, _workspace_root = _make_service(tmp_path, resolver)
    monkeypatch.setattr(
        "app.services.infrastructure.node_debug.process_lifecycle._RECONCILE_TERMINATE_TIMEOUT_SECONDS",
        0.05,
    )
    recorded_marker = "boot:100"
    same_instance = NodeDebugProcessIdentity(
        pid=4321,
        source=IDENTITY_SOURCE_LINUX_PROC,
        start_marker=recorded_marker,
    )
    reused_instance = NodeDebugProcessIdentity(
        pid=4321,
        source=IDENTITY_SOURCE_LINUX_PROC,
        start_marker="boot:777777",
    )
    sent_signals: list[int] = []

    class _KillRecorder:
        """替换服务模块内的 ``os`` 名字：只记录信号，绝不真打到某个 PID 上。"""

        def kill(self, pid: int, signal_number: int) -> None:
            sent_signals.append(signal_number)

        def __getattr__(self, name: str) -> object:
            return getattr(os, name)

    class _ProbeScript:
        def __init__(self, entries: list[NodeDebugProcessIdentity | None]) -> None:
            self._entries = entries
            self.calls = 0

        def __call__(self, _pid: int) -> NodeDebugProcessIdentity | None:
            self.calls += 1
            if self.calls <= len(self._entries):
                return self._entries[self.calls - 1]
            return self._entries[-1]

    monkeypatch.setattr(process_lifecycle, "os", _KillRecorder())
    # 第一次核实＝同一实例 → 允许 SIGTERM；SIGTERM 后该 PID 被回收复用 → 不得再打 SIGKILL。
    monkeypatch.setattr(
        process_lifecycle,
        "probe_process_identity",
        _ProbeScript([same_instance, reused_instance]),
    )
    terminated = await service._lifecycle.terminate_verified_instance(
        pid=4321,
        recorded_source=IDENTITY_SOURCE_LINUX_PROC,
        recorded_start_marker=recorded_marker,
    )
    assert terminated is True
    assert sent_signals == [signal.SIGTERM]

    # 反向对照：整个等待窗口内都核实为同一实例，才允许升级到 SIGKILL；
    # 这里模拟"只有收到 SIGKILL 进程才消失"，核实结果一直是同一实例。
    sent_signals.clear()

    def kill_aware_probe(_pid: int) -> NodeDebugProcessIdentity | None:
        if signal.SIGKILL in sent_signals:
            return None
        return same_instance

    monkeypatch.setattr(process_lifecycle, "probe_process_identity", kill_aware_probe)
    terminated = await service._lifecycle.terminate_verified_instance(
        pid=4321,
        recorded_source=IDENTITY_SOURCE_LINUX_PROC,
        recorded_start_marker=recorded_marker,
    )
    assert terminated is True
    assert sent_signals == [signal.SIGTERM, signal.SIGKILL]

    # 事实不足（跨来源）同样不允许升级强制终止，也不能判为已终结。
    sent_signals.clear()
    cross_source = NodeDebugProcessIdentity(
        pid=4321,
        source=IDENTITY_SOURCE_PSUTIL,
        start_marker=recorded_marker,
    )
    monkeypatch.setattr(
        process_lifecycle,
        "probe_process_identity",
        _ProbeScript([same_instance, cross_source]),
    )
    terminated = await service._lifecycle.terminate_verified_instance(
        pid=4321,
        recorded_source=IDENTITY_SOURCE_LINUX_PROC,
        recorded_start_marker=recorded_marker,
    )
    assert terminated is False
    assert sent_signals == [signal.SIGTERM]


@pytest.mark.asyncio
async def test_concurrent_starts_are_serialized_per_owner(
    tmp_path: Path,
    session_tree: tuple[object, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一 owner 并发 start：登记与 spawn 必须串行，不得留下无登记的游离进程。"""
    resolver, _parent_dir, _child_dir = session_tree
    service, store, _workspace_root = _make_service(
        tmp_path,
        resolver,
        inspector_port=0,
    )
    children: list[subprocess.Popen[bytes]] = []
    written: list[tuple[str, str]] = []
    original_write = service._write_launch_claim

    def spy_write(claim: NodeDebugLaunchClaimDTO) -> None:
        original_write(claim)
        written.append((claim.phase, claim.process_instance_id))

    async def fake_exec(*args: object, **kwargs: object) -> _FakeNodeProcess:
        child = _sleeper_child()
        children.append(child)
        return _FakeNodeProcess(child, "ws://127.0.0.1:45700/fake-target")

    async def fake_connect(url: object, **kwargs: object) -> _FakeInspectorSocket:
        return _FakeInspectorSocket()

    async def fake_command(
        runtime: NodeDebugRuntime,
        method: str,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return {}

    async def fake_background(runtime: NodeDebugRuntime) -> None:
        return None

    monkeypatch.setattr(service, "_write_launch_claim", spy_write)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(websockets, "connect", fake_connect)
    monkeypatch.setattr(service._inspector, "command", fake_command)
    monkeypatch.setattr(service._inspector, "receive_messages", fake_background)
    monkeypatch.setattr(service._inspector, "wait_for_execution_state", fake_background)
    monkeypatch.setattr(service._inspector, "wait_for_frame_variables", fake_background)
    try:
        await asyncio.gather(
            service.start(
                session_id=_PARENT_SESSION_ID,
                thread_id="main",
                path="entry.mjs",
                args=[],
                breakpoints=[],
            ),
            service.start(
                session_id=_PARENT_SESSION_ID,
                thread_id="main",
                path="entry.mjs",
                args=[],
                breakpoints=[],
            ),
        )

        phases = [phase for phase, _ in written]
        instances = [instance for phase, instance in written if phase == "launch_pending"]
        assert len(children) == 2
        assert len(instances) == 2
        assert instances[0] != instances[1]
        # 串行证据：第二个 launch_pending 之前，第一个实例必须已走完 running 并结清。
        second = phases.index("launch_pending", 1)
        first_segment = phases[:second]
        assert "running" in first_segment
        assert first_segment[-1] == "settled"
        # 没有代际错配：先启动的实例被后一次启动核实并停止。
        assert probe_process_identity(children[0].pid) is None
        assert probe_process_identity(children[1].pid) is not None
        persisted = store.read_launch_claim(_PARENT_SESSION_ID, "main")
        assert persisted is not None
        assert persisted.process_instance_id == instances[1]
        assert persisted.pid == children[1].pid
        assert persisted.phase == "running"
    finally:
        await service.close()
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait()
