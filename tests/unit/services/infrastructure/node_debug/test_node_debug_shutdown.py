"""服务关闭的启动窗口、owner 隔离与原任务重试。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest

from app.schemas.internal_v2.node_debug import (
    NodeDebugDebuggerContributionDTO,
    NodeDebugStateDTO,
)
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.node_debug.runtime_state import (
    NodeDebugPluginRuntimeProtocol,
    NodeDebugRuntime,
)
from app.services.infrastructure.node_debug.service import NodeDebugService
from app.services.infrastructure.node_debug.session import lifecycle_entry
from app.services.infrastructure.node_debug.session.thread_owner import (
    NodeDebugThreadOwner,
)
from tests.support.node_debug_dependencies import (
    node_debug_binding_snapshot,
    node_debug_file_paths,
    permissive_node_debug_session_admission,
)

_OWNER = ("ses_00000000400070008000000000000001", "thr_00000000400070008000000000000001")
_OTHER = ("ses_00000000400070008000000000000002", "thr_00000000400070008000000000000002")


@pytest.fixture
def service(tmp_path: Path) -> NodeDebugService:
    configuration = tmp_path / "workspace.jsonc"
    configuration.write_text("{}", encoding="utf-8")
    service = NodeDebugService(
        workspace_root=tmp_path, file_paths=node_debug_file_paths(tmp_path),
        config_service=ConfigService(config_dir=Path.cwd() / "configs", config_path=configuration),
        session_admission=permissive_node_debug_session_admission(),  # type: ignore[arg-type]
        external_resource_leases=ExternalResourceLeaseLedger(),
    )
    binding = node_debug_binding_snapshot()
    service.resolve_node_debugger_binding = AsyncMock(  # type: ignore[method-assign]
        return_value=NodeDebugDebuggerContributionDTO(
            **binding.model_dump(),
            label="Node Inspector",
            configuration_schema={},
            capabilities=[],
        )
    )
    return service


@pytest.fixture
def runtimes(tmp_path: Path) -> tuple[NodeDebugRuntime, NodeDebugRuntime]:
    return tuple(NodeDebugRuntime(
        session_id=owner[0], thread_id=owner[1], configuration_id="dbgcfg_" + "4" * 32,
        workspace_root=tmp_path, script_path=tmp_path / "entry.mjs",
        relative_script_path="entry.mjs", status="running",
    ) for owner in (_OWNER, _OTHER))  # type: ignore[return-value]


async def _start(service: NodeDebugService, owner: tuple[str, str] = _OWNER) -> NodeDebugStateDTO:
    return await service.start(session_id=owner[0], thread_id=owner[1], path="entry.mjs",
                               args=[], breakpoints=[], gateway_id="gateway_test",
                               request_id="request_shutdown_test")


@pytest.mark.parametrize("exit_code", [0, None])
async def test_close_reuses_confirmed_exit_without_persistent_claim(
    service: NodeDebugService, runtimes: tuple[NodeDebugRuntime, NodeDebugRuntime],
    exit_code: int | None,
) -> None:
    """实际退出回执可供重复关闭使用，信号退出也不依赖退出码。"""
    runtime = runtimes[0]
    plugin = SimpleNamespace(
        stop=AsyncMock(return_value={
            "run_id": runtime.run_id, "status": "exited", "exit_code": exit_code,
        }),
        close=AsyncMock(),
    )
    runtime.plugin_runtime = cast(NodeDebugPluginRuntimeProtocol, plugin)
    service._runtimes[_OWNER] = runtime
    events: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    runtime.dap_subscribers.add(events)

    assert await service._lifecycle.stop_runtime(runtime) == "stopped"
    assert runtime.target_exited and runtime.target_exit_code == exit_code
    assert runtime.plugin_runtime is None
    await service.close()

    plugin.stop.assert_awaited_once()
    plugin.close.assert_awaited_once()
    assert events.get_nowait() == {"event": "terminated", "body": {}}
    assert events.empty(), "重复关闭不能再次发布终态"


async def test_close_without_exit_observation_still_rejects_unknown_target(
    service: NodeDebugService, runtimes: tuple[NodeDebugRuntime, NodeDebugRuntime],
) -> None:
    runtime = runtimes[0]
    service._runtimes[_OWNER] = runtime
    events: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    runtime.dap_subscribers.add(events)

    with pytest.raises(RuntimeError, match="未能核实进程终态"):
        await service.close()

    assert not runtime.target_exited
    assert runtime.status == "reconcile_required"
    assert events.empty(), "停止结果未知不能伪造终态"


async def test_close_captures_start_before_runtime_exists_and_drains_other_owner(
    service: NodeDebugService, runtimes: tuple[NodeDebugRuntime, NodeDebugRuntime],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release, other_stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
    stopped: list[NodeDebugRuntime] = []

    async def launch(request: object) -> SimpleNamespace:
        entered.set()
        await release.wait()
        service._runtimes[_OWNER] = runtimes[0]
        return SimpleNamespace(owner=_OWNER)

    async def stop(runtime: NodeDebugRuntime) -> str:
        stopped.append(runtime)
        if runtime is runtimes[1]:
            other_stopped.set()
        return "stopped"

    monkeypatch.setattr(service, "_create_launch_orchestrator", lambda: SimpleNamespace(launch=launch))
    monkeypatch.setattr(service, "_get_state_admitted", AsyncMock(return_value=NodeDebugStateDTO(
        session_id=_OWNER[0], thread_id=_OWNER[1], status="running",
    )))
    monkeypatch.setattr(service._lifecycle, "stop_runtime", stop)
    service._runtimes[_OTHER] = runtimes[1]
    start = asyncio.create_task(_start(service))
    await entered.wait()
    close = asyncio.create_task(service.close())
    try:
        await asyncio.wait_for(other_stopped.wait(), 1)
        assert not close.done() and _OWNER not in service._runtimes
        with pytest.raises(RuntimeError, match="正在关闭"):
            await _start(service, _OTHER)
        release.set()
        await start
        await close
        assert stopped == [runtimes[1], runtimes[0]]
        assert not service._admitted_operations
        await service.close()
        assert stopped == [runtimes[1], runtimes[0]]
    finally:
        release.set()
        await asyncio.gather(start, close, return_exceptions=True)


@pytest.mark.parametrize("outcome", ["success", "failure", "unknown"])
async def test_close_keeps_original_owner_task_through_timeout_cancellation_and_retry(
    service: NodeDebugService, runtimes: tuple[NodeDebugRuntime, NodeDebugRuntime],
    monkeypatch: pytest.MonkeyPatch, outcome: str,
) -> None:
    release, entered, healthy_stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls: list[NodeDebugRuntime] = []
    failure = OSError("原停止写盘失败")

    async def stop(runtime: NodeDebugRuntime) -> str:
        calls.append(runtime)
        if runtime is runtimes[1]:
            healthy_stopped.set()
            return "stopped"
        entered.set()
        await release.wait()
        if outcome == "failure":
            raise failure
        return "reconcile_required" if outcome == "unknown" else "stopped"

    service._runtimes.update(zip((_OWNER, _OTHER), runtimes, strict=True))
    monkeypatch.setattr(service._lifecycle, "stop_runtime", stop)
    first = asyncio.create_task(service.close())
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(healthy_stopped.wait(), 1)
        original = service._close_tasks[_OWNER]
        first.cancel("只取消关闭观察")
        with pytest.raises(asyncio.CancelledError, match="只取消关闭观察"):
            await first
        assert not original.done() and not original.cancelling()
        second = asyncio.create_task(service.close())
        await asyncio.sleep(0)
        second.cancel("第二次关闭观察")
        with pytest.raises(asyncio.CancelledError, match="第二次关闭观察"):
            await second
        assert service._close_tasks[_OWNER] is original and calls.count(runtimes[0]) == 1

        wait = asyncio.wait

        async def observe_now(tasks, *, timeout):
            assert timeout == lifecycle_entry.NODE_DEBUG_CLOSE_WAIT_SECONDS == 30.0
            return await wait(tasks, timeout=0)

        with monkeypatch.context() as patch:
            patch.setattr(lifecycle_entry.asyncio, "wait", observe_now)
            with pytest.raises(TimeoutError, match=_OWNER[0]):
                await service.close()
        assert service._close_tasks[_OWNER] is original and not original.cancelling()
        release.set()
        # 先等原任务结算，下一调用才能显式重试失败；等待者不修改原结果。
        result = await asyncio.gather(original, return_exceptions=True)
        if outcome == "failure":
            assert result == [failure]
        elif outcome == "unknown":
            assert isinstance(result[0], RuntimeError) and _OWNER[0] in str(result[0])
        else:
            assert result == [None]
        assert service._runtimes[_OWNER] is runtimes[0]
        monkeypatch.setattr(service._lifecycle, "stop_runtime", AsyncMock(return_value="stopped"))
        await service.close()
        assert (service._close_tasks[_OWNER] is original) == (outcome == "success")
        assert calls.count(runtimes[1]) == 1
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second else []),
                             *service._close_tasks.values(), return_exceptions=True)


async def test_close_reports_finished_failure_and_pending_owner_together(
    service: NodeDebugService, runtimes: tuple[NodeDebugRuntime, NodeDebugRuntime],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release, entered = asyncio.Event(), asyncio.Event()
    failure = OSError("原 owner 失败")

    async def stop(runtime: NodeDebugRuntime) -> str:
        if runtime is runtimes[0]:
            raise failure
        entered.set()
        await release.wait()
        return "stopped"

    service._runtimes.update(zip((_OWNER, _OTHER), runtimes, strict=True))
    monkeypatch.setattr(service._lifecycle, "stop_runtime", stop)
    first = asyncio.create_task(service.close())
    try:
        await entered.wait()
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        wait = asyncio.wait

        async def observe_now(tasks, *, timeout):
            # 失败任务的显式重试仍拒绝，另一 owner 保持同一个等待。
            await asyncio.sleep(0)
            return await wait(tasks, timeout=0)

        with monkeypatch.context() as patch:
            patch.setattr(lifecycle_entry.asyncio, "wait", observe_now)
            with pytest.raises(ExceptionGroup) as caught:
                await service.close()
        assert caught.value.exceptions[0] is failure
        assert isinstance(caught.value.exceptions[1], TimeoutError)
        assert _OTHER[0] in str(caught.value.exceptions[1])
    finally:
        release.set()
        await asyncio.gather(*service._close_tasks.values(), return_exceptions=True)


async def test_waiting_session_admission_cannot_start_after_close(
    service: NodeDebugService, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class Admission:
        @asynccontextmanager
        async def admit(self, session_id: str, thread_id: str) -> AsyncIterator[NodeDebugThreadOwner]:
            entered.set()
            await release.wait()
            yield NodeDebugThreadOwner(session_id=session_id, thread_id=thread_id, thread_node=tmp_path)

    launch = AsyncMock()
    monkeypatch.setattr(service, "_session_admission", Admission())
    monkeypatch.setattr(service, "_create_launch_orchestrator", lambda: SimpleNamespace(launch=launch))
    start = asyncio.create_task(_start(service))
    try:
        await entered.wait()
        await service.close()
        release.set()
        with pytest.raises(RuntimeError, match="正在关闭"):
            await start
        launch.assert_not_called()
        assert not service._runtimes and not service._admitted_operations
    finally:
        release.set()
        await asyncio.gather(start, return_exceptions=True)


async def test_overlapping_close_retry_cannot_replace_first_waiters_original_failure(
    service: NodeDebugService, runtimes: tuple[NodeDebugRuntime, NodeDebugRuntime],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    failure = OSError("第一个等待者应收到的原失败")
    attempts = 0

    async def stop(runtime: NodeDebugRuntime) -> str:
        nonlocal attempts
        if runtime is runtimes[0]:
            attempts += 1
            if attempts == 1:
                raise failure
        else:
            entered.set()
            await release.wait()
        return "stopped"

    service._runtimes.update(zip((_OWNER, _OTHER), runtimes, strict=True))
    monkeypatch.setattr(service._lifecycle, "stop_runtime", stop)
    first = asyncio.create_task(service.close())
    second = None
    try:
        await entered.wait()
        original = service._close_tasks[_OWNER]
        assert original.done() and original.exception() is failure
        second = asyncio.create_task(service.close())
        await asyncio.sleep(0)
        assert service._close_tasks[_OWNER] is not original
        release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert results == [failure, None]
        assert attempts == 2
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
