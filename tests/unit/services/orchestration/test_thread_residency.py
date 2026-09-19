"""ThreadResidency 基础设施与 NodeDebug R3b 残留收口的单元测试。

覆盖 R5b（OpenSpec ``add-context-injection-lifecycle`` 任务 2.8 与
``add-itemized-rollout-context`` 任务 8.8/8.8-A 的 residency 切片 + R3b 复核残留项）：

- fake clock 29:59 仍 resident / 30:00 无 blocker → cold-eligible（触发 unload 回调）；
  产品阈值常量 1800 秒不缩短；
- blocker 活跃期间跨阈值仍 resident（idle 不累计），解除后从解除时刻重新起算；
- ``record_activity`` 重置累计；
- 重启恢复：全新 tracker + 磁盘上的活跃 durable claim → 不 cold-eligible（pull 源）；
- snapshot 字段齐全且 blocker 脱敏（无 PID/端口/路径正文）；
- unload 回调缝：generation 令牌、无 generation 不卸载、失败 fail-loud 且可重试；
- NodeDebug 接线：start 推送 blocker、核实终态解除、reconcile_required 保持阻断；
- R3b 残留收口：spawn 前 closing 守卫、stop/restart 的 per-owner 临界区
  （stop 落在 spawn 窗口不产生"exited + 进程存活"假终态）、并发 start 串行化、
  reconcile_required 阻断方案修改（R3b 建议 3）。

NodeDebug 服务侧的替身风格与 ``test_node_debug_process_lease.py`` 保持一致
（真实子进程提供可信 OS 起始身份，只替身 ``create_subprocess_exec`` 与
``websockets.connect`` 两个无法自然构造的时序点）。
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import websockets

from app.core.session_paths import SessionPathResolver
from app.schemas.internal_v2.node_debug import (
    NodeDebugConfigurationCreateRequest,
    NodeDebugLaunchClaimDTO,
)
from app.services.infrastructure import node_debug_process_identity
from app.services.infrastructure.node_debug_launch_claim import (
    claim_running,
    claim_with_spawn_identity,
    new_launch_claim,
)
from app.services.infrastructure.node_debug_process_identity import (
    _probe_linux_proc_identity,
    probe_process_identity,
)
from app.services.infrastructure.node_debug_service import NodeDebugService
from app.services.infrastructure.node_debug_session_store import NodeDebugSessionStore
from app.services.orchestration.agent_execution_service import AgentExecutionService
from app.services.orchestration.thread_residency import (
    THREAD_IDLE_UNLOAD_SECONDS,
    ResidencyBlocker,
    ThreadResidencyTracker,
    ThreadUnloadRequest,
)

_PARENT_SESSION_ID = "ses_residency_parent"
_THREAD_ID = "main"
_OWNER = (_PARENT_SESSION_ID, _THREAD_ID)
_CONFIGURATION_ID = "dbgcfg_33333333333333333333333333333333"


# ---------------------------------------------------------------------------
# 时钟替身与基础 fixture
# ---------------------------------------------------------------------------


class _FakeMonotonicClock:
    """可推进的单调时钟替身：idle 判定唯一依据，绝不使用墙钟。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FixedWallClock:
    """固定墙钟：只用于快照展示字段的确定性断言。"""

    def __init__(self) -> None:
        self.moment = datetime(2026, 9, 15, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.moment


def _create_session(resolver: SessionPathResolver, session_id: str) -> Path:
    """在权威目录索引中创建最小合法会话节点。"""
    title = f"测试会话 {session_id}"
    session_dir = resolver.allocate_session_dir(
        session_id=session_id,
        title=title,
        parent_node_id=None,
    )
    now = datetime.now(UTC).isoformat()
    (session_dir / "session.json").write_text(
        json.dumps(
            {
                "session_id": session_id,
                "title": title,
                "parent_session_id": None,
                "created_at": now,
                "updated_at": now,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    resolver.register_session(session_id, session_dir)
    return session_dir


@pytest.fixture
def session_tree(tmp_path: Path) -> SessionPathResolver:
    resolver = SessionPathResolver(tmp_path / ".boxteam" / "sessions")
    resolver.initialize()
    _create_session(resolver, _PARENT_SESSION_ID)
    return resolver


# ---------------------------------------------------------------------------
# Part 1：tracker 纯单元（fake clock 29:59 / 30:00 边界、blocker、回调缝）
# ---------------------------------------------------------------------------


def _make_tracker(
    clock: _FakeMonotonicClock,
    *,
    wall_clock: _FixedWallClock | None = None,
    unload_callback=None,
) -> ThreadResidencyTracker:
    return ThreadResidencyTracker(
        clock=clock,
        wall_clock=wall_clock or _FixedWallClock(),
        unload_callback=unload_callback,
    )


@pytest.mark.asyncio
async def test_fake_clock_29_59_resident_and_30_00_cold_eligible_fires_unload() -> None:
    """29:59 仍 resident；30:00 无 blocker → cold-eligible 并触发 unload 回调。"""
    assert THREAD_IDLE_UNLOAD_SECONDS == 1800.0  # 产品阈值不缩短
    clock = _FakeMonotonicClock()
    fired: list[ThreadUnloadRequest] = []

    async def unload(request: ThreadUnloadRequest) -> None:
        fired.append(request)

    tracker = _make_tracker(clock, unload_callback=unload)
    generation = tracker.register_generation(*_OWNER)
    assert generation.generation == 1
    tracker.record_activity(*_OWNER)

    clock.advance(1799)  # 29:59
    snapshot = tracker.snapshot(*_OWNER)
    assert snapshot.residency == "resident"
    assert snapshot.cold_eligible is False
    assert snapshot.idle_seconds == pytest.approx(1799.0)
    assert fired == []

    clock.advance(1)  # 30:00
    snapshot = tracker.snapshot(*_OWNER)
    assert snapshot.cold_eligible is True
    assert snapshot.residency == "resident"  # 尚未执行卸载
    unloaded = await tracker.sweep()
    assert len(unloaded) == 1
    assert len(fired) == 1
    assert (fired[0].session_id, fired[0].thread_id) == _OWNER
    assert fired[0].generation == 1
    assert fired[0].idle_seconds == pytest.approx(1800.0)
    after = tracker.snapshot(*_OWNER)
    assert after.residency == "cold"
    assert after.cold_eligible is False  # 已卸载，不再处于"待卸载"


@pytest.mark.asyncio
async def test_active_blocker_keeps_thread_resident_across_threshold() -> None:
    """blocker 活跃期间跨过 30 分钟仍 resident：idle 不累计、无 deadline。"""
    clock = _FakeMonotonicClock()
    fired: list[ThreadUnloadRequest] = []
    tracker = _make_tracker(clock, unload_callback=fired.append)
    tracker.register_generation(*_OWNER)
    tracker.register_blocker(
        *_OWNER,
        blocker_key="node_debug_claim:proc-1",
        kind="node_debug_process",
        reason="Node 调试进程运行中",
    )
    tracker.record_activity(*_OWNER)

    clock.advance(7200)  # 远超 30 分钟
    snapshot = tracker.snapshot(*_OWNER)
    assert snapshot.residency == "resident"
    assert snapshot.cold_eligible is False
    assert snapshot.idle_seconds is None  # 阻断期间不累计 idle
    assert snapshot.idle_deadline is None
    assert snapshot.execution_state == "node_debug_process"
    assert await tracker.sweep() == ()
    assert fired == []


@pytest.mark.asyncio
async def test_blocker_release_restarts_idle_counting_from_release_moment() -> None:
    """blocker 解除后从解除时刻重新起算：解除前阻断的时长不计入 idle。"""
    clock = _FakeMonotonicClock()
    tracker = _make_tracker(clock)
    tracker.register_generation(*_OWNER)
    tracker.register_blocker(
        *_OWNER,
        blocker_key="node_debug_claim:proc-1",
        kind="node_debug_process",
        reason="Node 调试进程运行中",
    )
    clock.advance(1000)  # 阻断 1000 秒（不累计）
    tracker.release_blocker(*_OWNER, blocker_key="node_debug_claim:proc-1")

    clock.advance(1799)  # 解除后 29:59
    snapshot = tracker.snapshot(*_OWNER)
    assert snapshot.residency == "resident"
    assert snapshot.cold_eligible is False
    assert snapshot.idle_seconds == pytest.approx(1799.0)  # 不是 2799

    clock.advance(1)  # 解除后 30:00
    assert tracker.snapshot(*_OWNER).cold_eligible is True


@pytest.mark.asyncio
async def test_record_activity_resets_idle_accumulation() -> None:
    """record_activity 重置 idle 累计。"""
    clock = _FakeMonotonicClock()
    tracker = _make_tracker(clock)
    tracker.register_generation(*_OWNER)
    tracker.record_activity(*_OWNER)

    clock.advance(1000)
    tracker.record_activity(*_OWNER)  # 重置
    clock.advance(1799)
    snapshot = tracker.snapshot(*_OWNER)
    assert snapshot.residency == "resident"
    assert snapshot.idle_seconds == pytest.approx(1799.0)

    clock.advance(1)
    assert tracker.snapshot(*_OWNER).cold_eligible is True


@pytest.mark.asyncio
async def test_record_activity_during_block_does_not_predate_release_restart() -> None:
    """阻断期间的活动不能让解除后的起算点早于解除时刻（pull 解除路径）。"""
    clock = _FakeMonotonicClock()
    tracker = _make_tracker(clock)
    tracker.register_generation(*_OWNER)

    class _StaticSource:
        """pull 源替身：模拟重启恢复期间磁盘 claim 的 blocker 出现与消失。"""

        def __init__(self) -> None:
            self.blockers: tuple[ResidencyBlocker, ...] = (
                ResidencyBlocker(
                    kind="node_debug_process",
                    reason="Node 调试进程运行中",
                ),
            )

        def residency_blockers(
            self, session_id: str, thread_id: str
        ) -> tuple[ResidencyBlocker, ...]:
            return self.blockers

    source = _StaticSource()
    tracker.add_blocker_source(source)

    blocked = tracker.snapshot(*_OWNER)  # 首次观察即被阻断：锚点保持 None
    assert blocked.blockers
    clock.advance(600)
    tracker.record_activity(*_OWNER)  # 阻断期间的活动：不得预置起算锚点
    clock.advance(600)
    source.blockers = ()  # pull 式解除（owner 结清 claim 后 pull 源不再上报）
    released = tracker.snapshot(*_OWNER)  # 首次"无 blocker"观察：此刻重新起算
    assert released.blockers == ()

    clock.advance(1799)
    snapshot = tracker.snapshot(*_OWNER)
    assert snapshot.residency == "resident"
    assert snapshot.idle_seconds == pytest.approx(1799.0)  # 从解除观察时刻起算


@pytest.mark.asyncio
async def test_sweep_without_registered_generation_never_fires_unload() -> None:
    """generation 0（owner 未登记任何 resident runtime）无从卸载：不触发回调。"""
    clock = _FakeMonotonicClock()
    fired: list[ThreadUnloadRequest] = []
    tracker = _make_tracker(clock, unload_callback=fired.append)
    tracker.record_activity(*_OWNER)
    clock.advance(3600)
    snapshot = tracker.snapshot(*_OWNER)
    assert snapshot.generation == 0
    assert snapshot.cold_eligible is False
    assert await tracker.sweep() == ()
    assert fired == []


@pytest.mark.asyncio
async def test_unload_failure_propagates_and_thread_stays_eligible_for_retry() -> None:
    """unload 回调失败必须显式抛出；thread 保持 cold-eligible 供下次 sweep 重试。"""
    clock = _FakeMonotonicClock()
    calls: list[ThreadUnloadRequest] = []

    async def failing_unload(request: ThreadUnloadRequest) -> None:
        calls.append(request)
        raise RuntimeError("释放该 generation 的 LifetimeScope 失败")

    tracker = _make_tracker(clock, unload_callback=failing_unload)
    tracker.register_generation(*_OWNER)
    tracker.record_activity(*_OWNER)
    clock.advance(1800)

    with pytest.raises(RuntimeError, match="LifetimeScope"):
        await tracker.sweep()
    # 未标记卸载：下次 sweep 重试，绝不虚报已卸载。
    assert tracker.snapshot(*_OWNER).cold_eligible is True

    async def good_unload(request: ThreadUnloadRequest) -> None:
        calls.append(request)

    tracker._unload_callback = good_unload  # 测试缝：修复后的 owner 回调
    fired = await tracker.sweep()
    assert len(fired) == 1
    assert tracker.snapshot(*_OWNER).residency == "cold"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_new_generation_enables_unload_again_and_old_one_stays_unloaded() -> None:
    """同一 generation 不重复卸载；owner 登记新一代后可再次卸载（generation 令牌）。"""
    clock = _FakeMonotonicClock()
    fired: list[ThreadUnloadRequest] = []

    async def unload(request: ThreadUnloadRequest) -> None:
        fired.append(request)

    tracker = _make_tracker(clock, unload_callback=unload)
    tracker.register_generation(*_OWNER)
    tracker.record_activity(*_OWNER)
    clock.advance(1800)
    await tracker.sweep()
    assert [request.generation for request in fired] == [1]

    # 旧 generation 已卸载：继续 idle 不会重复触发。
    clock.advance(1800)
    assert tracker.snapshot(*_OWNER).cold_eligible is False
    assert await tracker.sweep() == ()
    assert len(fired) == 1

    # owner 登记新一代（重建 runtime）：新一代越过阈值可再次卸载。
    second = tracker.register_generation(*_OWNER)
    assert second.generation == 2
    tracker.record_activity(*_OWNER)
    clock.advance(1800)
    assert tracker.snapshot(*_OWNER).cold_eligible is True
    await tracker.sweep()
    assert [request.generation for request in fired] == [1, 2]


def test_tracker_configuration_fails_loud() -> None:
    """非法配置显式失败：阈值必须为正、blocker 源必须有查询方法。"""
    clock = _FakeMonotonicClock()
    with pytest.raises(ValueError, match="idle 阈值"):
        ThreadResidencyTracker(clock=clock, idle_timeout_seconds=0)
    tracker = _make_tracker(clock)
    with pytest.raises(TypeError, match="residency_blockers"):
        tracker.add_blocker_source(object())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Part 2：NodeDebug 接线（P1-B push/pull）与重启恢复
# ---------------------------------------------------------------------------


def _launch_pending_claim() -> NodeDebugLaunchClaimDTO:
    return new_launch_claim(
        session_id=_PARENT_SESSION_ID,
        thread_id=_THREAD_ID,
        configuration_id=_CONFIGURATION_ID,
        inspector_host="127.0.0.1",
        inspector_port=0,
    )


@pytest.mark.asyncio
async def test_restart_recovery_active_durable_claim_is_not_cold_eligible(
    tmp_path: Path,
    session_tree: SessionPathResolver,
) -> None:
    """重启恢复（pull）：全新 tracker + 磁盘活跃 claim → 该 thread 不 cold-eligible。"""
    store = NodeDebugSessionStore(session_tree)
    store.write_launch_claim(_launch_pending_claim())  # spawn 前崩溃留下的活跃登记

    clock = _FakeMonotonicClock()
    fired: list[ThreadUnloadRequest] = []
    tracker = _make_tracker(clock, unload_callback=fired.append)
    service = NodeDebugService(
        workspace_root=tmp_path / "workspace",
        session_store=store,
        residency_tracker=tracker,
    )
    tracker.add_blocker_source(service)  # 生产接线形态（app/container.py）
    # owner 在重启后重建 resident runtime 并登记新一代：此后该 thread 的可卸载性
    # 完全取决于 blocker，而不是"从未登记"的保守默认。
    tracker.register_generation(*_OWNER)
    tracker.record_activity(*_OWNER)

    clock.advance(7200)  # 远超 30 分钟
    snapshot = tracker.snapshot(*_OWNER)
    assert [blocker.kind for blocker in snapshot.blockers] == ["node_debug_process"]
    assert snapshot.blockers[0].reason == "Node 调试进程已登记启动，等待 spawn 与握手核实"
    assert snapshot.cold_eligible is False  # blocker 活跃：idle 不累计、不得虚报可卸载
    assert snapshot.residency == "resident"
    assert await tracker.sweep() == ()
    assert fired == []


@pytest.mark.asyncio
async def test_restart_recovery_settled_claim_restarts_idle_counting(
    tmp_path: Path,
    session_tree: SessionPathResolver,
) -> None:
    """重启恢复结清后 blocker 消失，从结清后的观察时刻重新起算 idle。"""
    child = _sleeper_child()
    try:
        identity = probe_process_identity(child.pid)
        assert identity is not None and identity.verifiable
        store = NodeDebugSessionStore(session_tree)
        claim = claim_running(
            claim_with_spawn_identity(
                _launch_pending_claim(),
                pid=child.pid,
                identity=identity,
            ),
            inspector_port=9229,
        )
        store.write_launch_claim(claim)
        child.kill()
        child.wait(timeout=10)

        clock = _FakeMonotonicClock()
        tracker = _make_tracker(clock)
        service = NodeDebugService(
            workspace_root=tmp_path / "workspace",
            session_store=store,
            residency_tracker=tracker,
        )
        tracker.add_blocker_source(service)

        blocked = tracker.snapshot(*_OWNER)
        assert blocked.blockers  # 结清前：磁盘活跃 claim 阻断
        assert blocked.cold_eligible is False

        # owner 的常规读路径核实"登记实例已不存在"并结清 claim（R5a/R3b 既有语义）。
        state = await service.get_state(_PARENT_SESSION_ID)
        assert state.status == "idle"
        persisted = store.read_launch_claim(_PARENT_SESSION_ID, _THREAD_ID)
        assert persisted is not None
        assert persisted.phase == "settled"

        settled = tracker.snapshot(*_OWNER)
        assert settled.blockers == ()  # 结清后 pull 源不再上报
        assert settled.cold_eligible is False  # generation 0：无在册 runtime 可卸载

        # owner 重新登记 resident runtime 代后，idle 从解除后起算，越阈值才可卸载。
        tracker.register_generation(*_OWNER)
        tracker.record_activity(*_OWNER)
        clock.advance(1800)
        assert tracker.snapshot(*_OWNER).cold_eligible is True
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


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
    """包装真实子进程，暴露 asyncio 子进程句柄需要的属性。"""

    def __init__(self, child: subprocess.Popen[bytes], inspector_url: str) -> None:
        self._child = child
        self.returncode: int | None = None
        self.stderr = asyncio.StreamReader()
        self.stdout = asyncio.StreamReader()
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

    def get_debug_runtime_config(self) -> dict[str, object]:
        return {
            "enabled": True,
            "default_adapter": "node_inspector",
            "command_timeout_seconds": 5.0,
            "node": {
                "inspector_host": "127.0.0.1",
                "inspector_port": 0,
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


def _make_wired_service(
    tmp_path: Path,
    session_tree: SessionPathResolver,
    tracker: ThreadResidencyTracker | None,
) -> NodeDebugService:
    """带 session_store + tracker 的服务；真实 spawn 由测试替身接管。"""
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir(exist_ok=True)
    (workspace_root / "entry.mjs").write_text("console.log(1);\n", encoding="utf-8")
    service = NodeDebugService(
        workspace_root=workspace_root,
        session_store=NodeDebugSessionStore(session_tree),
        config_service=_DebugConfigStub(),
        residency_tracker=tracker,
    )
    service._node_bin = "fake-node"
    return service


def _sleeper_child() -> subprocess.Popen[bytes]:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])


@pytest.mark.asyncio
async def test_start_pushes_blocker_and_verified_stop_releases_it(
    tmp_path: Path,
    session_tree: SessionPathResolver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1-B push：running → blocker 登记；核实终态（settled）→ 解除并重新起算。"""
    clock = _FakeMonotonicClock()
    fired: list[ThreadUnloadRequest] = []
    tracker = _make_tracker(clock, unload_callback=fired.append)
    service = _make_wired_service(tmp_path, session_tree, tracker)
    child = _sleeper_child()

    async def fake_exec(*args: object, **kwargs: object) -> _FakeNodeProcess:
        return _FakeNodeProcess(child, "ws://127.0.0.1:45678/fake-target")

    async def fake_connect(url: object, **kwargs: object) -> _FakeInspectorSocket:
        return _FakeInspectorSocket()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(websockets, "connect", fake_connect)
    try:
        tracker.register_generation(*_OWNER)  # owner 侧 resident runtime 代
        tracker.record_activity(*_OWNER)
        await service.start(
            session_id=_PARENT_SESSION_ID, path="entry.mjs", args=[], breakpoints=[]
        )

        running = tracker.snapshot(*_OWNER)
        assert [blocker.kind for blocker in running.blockers] == ["node_debug_process"]
        assert running.blockers[0].reason == "Node 调试进程运行中"
        clock.advance(7200)  # 调试进程运行中跨过 30 分钟：绝不卸载
        assert tracker.snapshot(*_OWNER).cold_eligible is False
        assert await tracker.sweep() == ()
        assert fired == []

        stopped = await service.apply_action(
            session_id=_PARENT_SESSION_ID, action="stop", params={}
        )
        assert stopped.status == "exited"
        released = tracker.snapshot(*_OWNER)
        assert released.blockers == ()  # 核实终态 + lease 结清 → 解除
        assert released.idle_seconds is not None
        assert released.idle_seconds < 5  # 从解除时刻重新起算，而不是累计 7200+

        clock.advance(1800)
        assert tracker.snapshot(*_OWNER).cold_eligible is True
    finally:
        await service.close()
        if child.poll() is None:
            child.kill()
            child.wait()


@pytest.mark.asyncio
async def test_reconcile_required_claim_keeps_blocker_registered(
    tmp_path: Path,
    session_tree: SessionPathResolver,
) -> None:
    """reconcile_required → blocker 保持登记，绝不进入 cold-eligible。"""
    store = NodeDebugSessionStore(session_tree)
    store.write_launch_claim(_launch_pending_claim())  # launch_pending 无 PID：无法核实
    clock = _FakeMonotonicClock()
    tracker = _make_tracker(clock)
    service = NodeDebugService(
        workspace_root=tmp_path / "workspace",
        session_store=store,
        residency_tracker=tracker,
    )
    tracker.add_blocker_source(service)

    state = await service.get_state(_PARENT_SESSION_ID)
    assert state.status == "reconcile_required"

    snapshot = tracker.snapshot(*_OWNER)
    reasons = {blocker.reason for blocker in snapshot.blockers}
    assert "Node 调试实例无法核实终态，需核实后才能解除占用" in reasons
    clock.advance(7200)
    assert tracker.snapshot(*_OWNER).cold_eligible is False
    assert await tracker.sweep() == ()


@pytest.mark.asyncio
async def test_snapshot_fields_complete_and_blocker_reasons_sanitized(
    tmp_path: Path,
    session_tree: SessionPathResolver,
) -> None:
    """快照字段齐全（2.8 清单）且 blocker 脱敏：无 PID/端口/路径/实例 ID 正文。"""
    store = NodeDebugSessionStore(session_tree)
    claim = _launch_pending_claim()
    store.write_launch_claim(claim)
    tracker = _make_tracker(_FakeMonotonicClock())
    service = NodeDebugService(
        workspace_root=tmp_path / "workspace",
        session_store=store,
        residency_tracker=tracker,
    )
    tracker.add_blocker_source(service)

    snapshot = tracker.snapshot(*_OWNER)
    # 字段齐全：identity / residency / execution / last activity / deadline / blockers
    assert (snapshot.session_id, snapshot.thread_id) == _OWNER
    assert snapshot.residency == "resident"
    assert snapshot.cold_eligible is False
    assert snapshot.execution_state == "node_debug_process"
    assert snapshot.last_activity_at is not None
    assert snapshot.idle_seconds is None  # 阻断期间不累计 idle
    assert snapshot.generation == 0
    assert len(snapshot.blockers) == 1
    # 阻断期间 idle 不累计：没有可展示的 deadline。
    assert snapshot.idle_deadline is None

    # 未阻断 thread 的展示字段：deadline 与 idle 正常展示。
    # pull 源按权威目录索引解析 thread，第二个 thread 用真实会话节点。
    other_session = "ses_residency_other"
    _create_session(session_tree, other_session)
    tracker.record_activity(other_session, "main")
    unblocked = tracker.snapshot(other_session, "main")
    assert unblocked.blockers == ()
    assert unblocked.execution_state == "idle"
    assert unblocked.idle_deadline is not None
    assert unblocked.idle_seconds == pytest.approx(0.0)

    blocker = snapshot.blockers[0]
    assert isinstance(blocker, ResidencyBlocker)
    assert blocker.kind == "node_debug_process"
    assert blocker.reason == "Node 调试进程已登记启动，等待 spawn 与握手核实"
    # 脱敏：固定话术不含数字（PID/端口）、不含路径分隔符、不含实例 ID。
    assert not any(character.isdigit() for character in blocker.reason)
    assert "/" not in blocker.reason and "\\" not in blocker.reason
    assert claim.process_instance_id not in blocker.reason
    assert claim.nonce not in blocker.reason


# ---------------------------------------------------------------------------
# Part 3：R3b 残留收口（closing 守卫、owner 临界区、并发 start、阻断面、身份 TOCTOU）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_aborts_when_stop_already_took_over_runtime(
    tmp_path: Path,
    session_tree: SessionPathResolver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1-C.1 closing 守卫：stop 已接管 runtime（closing）后启动序列绝不 spawn。"""
    store = NodeDebugSessionStore(session_tree)
    service = _make_wired_service(tmp_path, session_tree, tracker=None)
    spawn_calls: list[int] = []

    async def fake_exec(*args: object, **kwargs: object) -> _FakeNodeProcess:
        # 守卫生效时绝不可达：一旦被调用就意味着游离进程已经产生。
        spawn_calls.append(1)
        raise AssertionError("closing 守卫生效时不得执行 spawn")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    original_write = service._write_launch_claim

    def stop_takes_over_before_spawn(claim: NodeDebugLaunchClaimDTO) -> None:
        # 在"claim 已落盘、进程尚未 spawn"的窗口注入并发 stop 的接管结果：
        # `_stop_runtime` 进入后会把 runtime.closing 置位（这是该标志的合同语义），
        # 随后启动序列必须在 spawn 前被守卫拦下。
        original_write(claim)
        if claim.phase == "launch_pending":
            runtime = service._runtimes[(_PARENT_SESSION_ID, _THREAD_ID)]
            runtime.closing = True

    monkeypatch.setattr(service, "_write_launch_claim", stop_takes_over_before_spawn)

    with pytest.raises(RuntimeError, match="取消 spawn"):
        await service.start(
            session_id=_PARENT_SESSION_ID, path="entry.mjs", args=[], breakpoints=[]
        )
    assert spawn_calls == []  # 游离进程没有产生
    persisted = store.read_launch_claim(_PARENT_SESSION_ID, _THREAD_ID)
    assert persisted is not None
    assert persisted.phase == "settled"  # 失败收口：claim 结清，不虚报启动成功


@pytest.mark.asyncio
async def test_stop_during_spawn_window_is_serialized_per_owner(
    tmp_path: Path,
    session_tree: SessionPathResolver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1-C.2 stop 落在 spawn 窗口：被 owner 锁串行化，不产生"exited + 进程存活"假终态。"""
    service = _make_wired_service(tmp_path, session_tree, tracker=None)
    child = _sleeper_child()
    spawn_started = asyncio.Event()
    release_spawn = asyncio.Event()

    async def fake_exec(*args: object, **kwargs: object) -> _FakeNodeProcess:
        # 停在 spawn 窗口内：runtime 已注册、claim 已落盘、进程句柄未返回。
        spawn_started.set()
        await release_spawn.wait()
        return _FakeNodeProcess(child, "ws://127.0.0.1:45678/fake-target")

    async def fake_connect(url: object, **kwargs: object) -> _FakeInspectorSocket:
        return _FakeInspectorSocket()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(websockets, "connect", fake_connect)
    try:
        start_task = asyncio.create_task(
            service.start(
                session_id=_PARENT_SESSION_ID, path="entry.mjs", args=[], breakpoints=[]
            )
        )
        await asyncio.wait_for(spawn_started.wait(), timeout=5)

        stop_task = asyncio.create_task(
            service.apply_action(
                session_id=_PARENT_SESSION_ID, action="stop", params={}
            )
        )
        # 给 stop 多次让出事件循环的机会：它必须一直卡在 owner 锁上。
        for _ in range(20):
            await asyncio.sleep(0.01)
        assert not stop_task.done()  # 串行化：stop 等待启动序列完成
        mid_state = await service.get_state(_PARENT_SESSION_ID)
        assert mid_state.status == "starting"  # 绝不出现 exited 假终态
        assert child.poll() is None  # 进程仍存活，没有被"已停止"的假象掩盖

        release_spawn.set()
        started = await asyncio.wait_for(start_task, timeout=10)
        assert started.status == "running"
        stopped = await asyncio.wait_for(stop_task, timeout=10)
        assert stopped.status == "exited"  # stop 在启动完成后真实收口
        assert child.poll() is not None  # 进程被真实终止，不是假终态
    finally:
        await service.close()
        if child.poll() is None:
            child.kill()
            child.wait()


@pytest.mark.asyncio
async def test_restart_holds_owner_lock_across_stop_and_relaunch(
    tmp_path: Path,
    session_tree: SessionPathResolver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1-C.2 restart：停止旧实例与启动新实例整体处于 owner 临界区，登记不互相覆盖。"""
    store = NodeDebugSessionStore(session_tree)
    service = _make_wired_service(tmp_path, session_tree, tracker=None)
    first_child = _sleeper_child()
    second_child = _sleeper_child()
    children = [first_child, second_child]
    spawn_started = asyncio.Event()
    release_spawn = asyncio.Event()
    spawn_count = 0

    async def fake_exec(*args: object, **kwargs: object) -> _FakeNodeProcess:
        nonlocal spawn_count
        spawn_count += 1
        if spawn_count == 1:
            # 第一次 spawn 停在窗口内：restart 必须在 owner 锁上等待。
            spawn_started.set()
            await release_spawn.wait()
        child = children.pop(0)
        return _FakeNodeProcess(child, "ws://127.0.0.1:45678/fake-target")

    async def fake_connect(url: object, **kwargs: object) -> _FakeInspectorSocket:
        return _FakeInspectorSocket()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(websockets, "connect", fake_connect)
    try:
        start_task = asyncio.create_task(
            service.start(
                session_id=_PARENT_SESSION_ID, path="entry.mjs", args=[], breakpoints=[]
            )
        )
        await asyncio.wait_for(spawn_started.wait(), timeout=5)

        restart_task = asyncio.create_task(service.restart(_PARENT_SESSION_ID))
        for _ in range(20):
            await asyncio.sleep(0.01)
        assert not restart_task.done()  # restart 串行等待启动序列完成
        assert first_child.poll() is None  # 旧实例未被提前停止

        release_spawn.set()
        started = await asyncio.wait_for(start_task, timeout=10)
        assert started.status == "running"
        restarted = await asyncio.wait_for(restart_task, timeout=10)
        assert restarted.status == "running"
        assert restarted.pid == second_child.pid  # 新一代实例
        assert first_child.poll() is not None  # 旧实例被真实停止
        assert second_child.poll() is None

        persisted = store.read_launch_claim(_PARENT_SESSION_ID, _THREAD_ID)
        assert persisted is not None
        assert persisted.phase == "running"
        assert persisted.pid == second_child.pid  # 磁盘登记 = 新一代，未被旧代覆盖
    finally:
        await service.close()
        for child in (first_child, second_child):
            if child.poll() is None:
                child.kill()
                child.wait()


@pytest.mark.asyncio
async def test_concurrent_starts_are_serialized_and_claim_not_overwritten(
    tmp_path: Path,
    session_tree: SessionPathResolver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1-C.3 并发 start 同一 owner：串行执行，磁盘 claim 不互相覆盖、无游离进程。"""
    store = NodeDebugSessionStore(session_tree)
    service = _make_wired_service(tmp_path, session_tree, tracker=None)
    children = [_sleeper_child(), _sleeper_child()]
    spawned: list[int] = []

    async def fake_exec(*args: object, **kwargs: object) -> _FakeNodeProcess:
        # 每次调用都让出事件循环，最大化两个并发 start 的交错机会。
        await asyncio.sleep(0)
        child = children[len(spawned)]
        spawned.append(child.pid)
        return _FakeNodeProcess(child, "ws://127.0.0.1:45678/fake-target")

    async def fake_connect(url: object, **kwargs: object) -> _FakeInspectorSocket:
        return _FakeInspectorSocket()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(websockets, "connect", fake_connect)
    try:
        first, second = await asyncio.gather(
            service.start(
                session_id=_PARENT_SESSION_ID, path="entry.mjs", args=[], breakpoints=[]
            ),
            service.start(
                session_id=_PARENT_SESSION_ID, path="entry.mjs", args=[], breakpoints=[]
            ),
        )
        assert first.status == "running"
        assert second.status == "running"
        assert len(spawned) == 2  # 串行化：两次真实 spawn，没有第三者
        assert spawned[0] != spawned[1]

        persisted = store.read_launch_claim(_PARENT_SESSION_ID, _THREAD_ID)
        assert persisted is not None
        assert persisted.phase == "running"
        assert persisted.pid == spawned[1]  # 磁盘登记 = 第二代，未被第一代回写覆盖
        # 第一代已被第二代启动时的停止路径真实收口，不残留游离进程。
        assert children[0].poll() is not None
        assert children[1].poll() is None
    finally:
        await service.close()
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()


@pytest.mark.asyncio
async def test_configuration_mutation_blocked_by_unsettled_claim(
    tmp_path: Path,
    session_tree: SessionPathResolver,
) -> None:
    """R3b 建议 3：冷场景下未结清 durable claim 阻断方案修改/删除（与运行态阻面对齐）。"""
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    (workspace_root / "entry.mjs").write_text("console.log(1);\n", encoding="utf-8")
    store = NodeDebugSessionStore(session_tree)
    service = NodeDebugService(
        workspace_root=workspace_root,
        session_store=store,
    )
    created = await service.create_configuration(
        NodeDebugConfigurationCreateRequest(
            session_id=_PARENT_SESSION_ID,
            name="入口调试",
            script_path="entry.mjs",
        )
    )
    configuration_id = created.active_configuration_id
    assert configuration_id is not None

    claim = new_launch_claim(
        session_id=_PARENT_SESSION_ID,
        thread_id=_THREAD_ID,
        configuration_id=configuration_id,
        inspector_host="127.0.0.1",
        inspector_port=0,
    )
    store.write_launch_claim(claim)  # launch_pending 无 PID：重启后无法核实
    state = await service.get_state(_PARENT_SESSION_ID)
    assert state.status == "reconcile_required"

    with pytest.raises(RuntimeError, match="修改或删除当前调试方案被未结清"):
        await service.delete_configuration(_PARENT_SESSION_ID, configuration_id)


def test_linux_proc_identity_handles_vanishing_entry_between_probe_steps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2.4：/proc 条目在探测两步之间消失 → 按已核实终结收口（None），其余错误显式抛出。"""

    class _VanishingStatPath:
        def read_text(self, encoding: str | None = None, errors: str | None = None) -> str:
            raise FileNotFoundError(2, "No such file or directory")

    class _VanishingPidDir:
        def __truediv__(self, name: str) -> _VanishingStatPath:
            return _VanishingStatPath()

    class _VanishingProcRoot:
        def __truediv__(self, name: str) -> _VanishingPidDir:
            return _VanishingPidDir()

    class _DeniedStatPath:
        def read_text(self, encoding: str | None = None, errors: str | None = None) -> str:
            raise PermissionError(13, "Permission denied")

    class _DeniedPidDir:
        def __truediv__(self, name: str) -> _DeniedStatPath:
            return _DeniedStatPath()

    class _DeniedProcRoot:
        def __truediv__(self, name: str) -> _DeniedPidDir:
            return _DeniedPidDir()

    monkeypatch.setattr(
        node_debug_process_identity, "_PROC_ROOT", _VanishingProcRoot()
    )
    # 进程在 is_file 与 read_text 之间消失：与"条目不存在"同一事实 ⇒ 已核实终结。
    assert _probe_linux_proc_identity(4242) is None

    monkeypatch.setattr(node_debug_process_identity, "_PROC_ROOT", _DeniedProcRoot())
    # 非消失类读取错误仍然显式抛出，绝不默默吞掉。
    with pytest.raises(PermissionError):
        _probe_linux_proc_identity(4242)


# ---------------------------------------------------------------------------
# Part 4：generation fence 与 runtime owner 接线（OpenSpec 2.8）
# ---------------------------------------------------------------------------


def _make_wired_execution_service(
    tracker: ThreadResidencyTracker | None,
) -> AgentExecutionService:
    """最小依赖构造 AgentExecutionService：只验证 residency 接线面。"""
    return AgentExecutionService(
        config_service=MagicMock(),
        background_task_registry=MagicMock(),
        background_message_bus=MagicMock(),
        job_event_bus=MagicMock(),
        dependency_provider=MagicMock(),
        session_changes_service=MagicMock(),
        tool_selection_store=MagicMock(),
        message_stream_store=MagicMock(),
        workspace_root=Path("."),
        residency_tracker=tracker,
    )


def test_generation_fence_rejects_stale_unknown_and_illegal_generations() -> None:
    """fence：当前代放行；过期代、未知 thread、非法代一律 fail closed。"""
    tracker = _make_tracker(_FakeMonotonicClock())
    first = tracker.register_generation(*_OWNER)
    assert tracker.is_current_generation(*_OWNER, first.generation) is True
    second = tracker.register_generation(*_OWNER)
    assert tracker.is_current_generation(*_OWNER, first.generation) is False
    assert tracker.is_current_generation(*_OWNER, second.generation) is True
    assert tracker.is_current_generation("ses_unknown", "main", 1) is False
    assert tracker.is_current_generation(*_OWNER, 0) is False


@pytest.mark.asyncio
async def test_runtime_owner_unload_evicts_agent_cache_with_generation_fence() -> None:
    """unload 回调：当前代只淘汰该 session 的可重建缓存；过期代 fail closed 不动缓存。"""
    clock = _FakeMonotonicClock()
    tracker = _make_tracker(clock)
    service = _make_wired_execution_service(tracker)
    tracker.set_unload_callback(service.unload_thread_runtime)

    target_key = (_PARENT_SESSION_ID, "agent_a", "rev-1", (), ())
    other_key = ("ses_other", "agent_a", "rev-1", (), ())
    service._agent_cache[target_key] = object()
    service._agent_cache[other_key] = object()

    generation = tracker.register_generation(*_OWNER)
    service._record_thread_residency_activity(_PARENT_SESSION_ID)
    clock.advance(1800)
    unloaded = await tracker.sweep()
    assert len(unloaded) == 1
    assert target_key not in service._agent_cache  # 该 session 缓存被释放
    assert other_key in service._agent_cache  # 其它 session 不受影响

    # 迟到 callback：同 generation 再次到达 → fence 拒绝，重建的缓存不被误释放。
    service._agent_cache[target_key] = object()
    stale_request = ThreadUnloadRequest(
        session_id=_PARENT_SESSION_ID,
        thread_id=_THREAD_ID,
        generation=generation.generation,
        idle_seconds=1800.0,
    )
    await service.unload_thread_runtime(stale_request)
    assert target_key in service._agent_cache


@pytest.mark.asyncio
async def test_runtime_owner_unload_requires_tracker() -> None:
    """未装配 tracker 时调用 unload 回调必须显式失败（fail loud）。"""
    service = _make_wired_execution_service(None)
    request = ThreadUnloadRequest(
        session_id=_PARENT_SESSION_ID,
        thread_id=_THREAD_ID,
        generation=1,
        idle_seconds=1800.0,
    )
    with pytest.raises(RuntimeError, match="residency tracker"):
        await service.unload_thread_runtime(request)


@pytest.mark.asyncio
async def test_run_step_records_activity_and_rehydrates_generation_after_cold() -> None:
    """run_step 调用点：首次活动注册新一代；cold 后再次 step 重建新代（rehydration）。"""
    clock = _FakeMonotonicClock()
    tracker = _make_tracker(clock)
    service = _make_wired_execution_service(tracker)
    tracker.set_unload_callback(service.unload_thread_runtime)

    class _StubStepRunner:
        async def run_step(self, *args: object, **kwargs: object) -> str:
            return "done"

    service._step_runner = _StubStepRunner()
    cache_key = (_PARENT_SESSION_ID, "agent_a", "rev-1", (), ())
    service._agent_cache[cache_key] = object()

    await service.run_step(
        _PARENT_SESSION_ID,
        "你好",
        job_id="job_residency_1",
        message_id="msg_residency_1",
        message_created_at="2026-09-19T00:00:00Z",
    )
    snapshot = tracker.snapshot(*_OWNER)
    assert snapshot.generation == 1  # 重启后首次活动：rehydration 注册新一代
    assert snapshot.residency == "resident"

    clock.advance(1800)
    await tracker.sweep()  # idle 卸载：回调释放可重建缓存
    assert cache_key not in service._agent_cache
    assert tracker.snapshot(*_OWNER).residency == "cold"

    service._agent_cache[cache_key] = object()  # cold 后读取路径重建新 runtime
    await service.run_step(
        _PARENT_SESSION_ID,
        "继续",
        job_id="job_residency_2",
        message_id="msg_residency_2",
        message_created_at="2026-09-19T00:30:00Z",
    )
    snapshot = tracker.snapshot(*_OWNER)
    assert snapshot.generation == 2  # cold 后重建：注册新一代，恢复可卸载性
    assert snapshot.residency == "resident"
