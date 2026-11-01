"""真实 Node/Inspector 启动各阶段的 claim 持久化与原 owner 结算。"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import signal
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, get_ident
from typing import Literal

import pytest

from app.schemas.internal_v2.node_debug import NodeDebugLaunchClaimDTO
from app.services.infrastructure.node_debug.process.observation import (
    NodeDebugRuntimeObserver,
)
from app.services.infrastructure.node_debug.process.process_identity import (
    probe_process_identity,
)
from app.services.infrastructure.node_debug.runtime_state import NodeDebugRuntime
from app.services.infrastructure.node_debug.session.session_store import (
    NodeDebugSessionStore,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.agents.test_node_debug_receive_errors import (
    DebugReceiveClient,
)
from tests.integration.backend.agents.test_node_debug_receive_errors import (
    debug_receive_client as debug_receive_client,  # noqa: PLC0414 - 沿原真实调试 API 装具
)


@dataclass
class ClaimWriteGate:
    phase: Literal["launch_pending", "spawned", "running"] = "launch_pending"
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: Event = field(default_factory=Event)
    failure: OSError | None = None
    candidate: NodeDebugLaunchClaimDTO | None = None
    writers: list[int] = field(default_factory=list)


@pytest.fixture
async def claim_write_gate(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[ClaimWriteGate]:
    gate = ClaimWriteGate()
    loop, replace = asyncio.get_running_loop(), os.replace

    def publish(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        if Path(target).name == NodeDebugSessionStore.LAUNCH_CLAIM_FILE_NAME:
            claim = NodeDebugLaunchClaimDTO.model_validate_json(Path(source).read_bytes())
            gate.writers.append(get_ident())
            if claim.phase == gate.phase and gate.candidate is None:
                gate.candidate = claim
                loop.call_soon_threadsafe(gate.entered.set)
                assert gate.release.wait(4), "测试未放行原 claim 写盘"
                if gate.failure is not None:
                    raise gate.failure
        replace(source, target)

    monkeypatch.setattr(os, "replace", publish)
    try:
        yield gate
    finally:
        gate.release.set()


@pytest.mark.parametrize("phase", ["launch_pending", "spawned", "running"])
@pytest.mark.parametrize("outcome", ["success", "failure", "cancel", "cancel-failure"])
async def test_launch_claim_wait_keeps_loop_and_owner_until_original_commit_settles(
    claim_write_gate: ClaimWriteGate, debug_receive_client: DebugReceiveClient,
    phase: Literal["launch_pending", "spawned", "running"], outcome: str,
) -> None:
    client, store, owner, root, reopen = debug_receive_client
    gate = claim_write_gate
    gate.phase = phase
    if "failure" in outcome:
        gate.failure = OSError(errno.ENOSPC, "claim 发布失败", str(root / "claim-secret"))
    configured = await client.post("/api/v1/debug/node/configurations", json={
        **owner, "name": "登记等待后原方案继续", "script_path": "entry.mjs",
    })
    assert configured.status_code == 200, configured.text
    payload = {**owner, "configuration_id": configured.json()["data"]["active_configuration_id"], "path": "entry.mjs"}
    launch = asyncio.create_task(client.post("/api/v1/debug/node/start", json=payload))
    stop = None
    try:
        await asyncio.wait_for(gate.entered.wait(), 6)
        assert gate.writers[-1] != get_ident(), "原 claim 提交仍阻塞事件循环"
        claim = gate.candidate
        assert claim is not None and claim.phase == phase
        before = store.read_launch_claim(**owner)
        assert (before.phase if before is not None else None) == {
            "launch_pending": None, "spawned": "launch_pending", "running": "spawned",
        }[phase]
        if claim.pid is not None:
            assert probe_process_identity(claim.pid) is not None
        probe = await asyncio.wait_for(client.get("/api/v1/debug/node/capabilities"), 1)
        assert probe.status_code == 200, probe.text
        assert probe.json()["request_id"] == probe.headers["X-Request-ID"]
        if outcome.startswith("cancel"):
            launch.cancel("原 claim 启动取消")
            await asyncio.sleep(0)
            launch.cancel("重复取消不得遗弃原writer")
            await asyncio.sleep(0)
            assert not launch.done()
        stop = asyncio.create_task(client.post("/api/v1/debug/node/action", json={**owner, "action": "stop"}))
        await asyncio.sleep(0.02)
        assert not stop.done() and store.read_launch_claim(**owner) == before
        gate.release.set()
        if outcome.startswith("cancel"):
            with pytest.raises(asyncio.CancelledError, match="原 claim 启动取消") as caught:
                await launch
            if gate.failure is not None:
                assert isinstance(caught.value.__cause__, OSError)
                assert caught.value.__cause__.__cause__ is gate.failure
        else:
            started = await launch
            assert started.status_code == (409 if gate.failure else 200), started.text
            assert started.json()["request_id"] == started.headers["X-Request-ID"]
            assert str(root / "claim-secret") not in started.text
        stopped = await asyncio.wait_for(stop, 5)
        assert stopped.status_code == 200 and stopped.json()["data"]["status"] == "exited", stopped.text
        if claim.pid is not None:
            assert probe_process_identity(claim.pid) is None
        settled = store.read_launch_claim(**owner)
        if phase == "launch_pending" and gate.failure is not None:
            assert settled is None
        else:
            assert settled is not None and settled.phase == "settled"
            assert settled.process_instance_id == claim.process_instance_id
        await reopen(True)
        cold = await client.get("/api/v1/debug/node", params=owner)
        assert cold.status_code == 200 and cold.json()["data"]["status"] == "idle", cold.text
        retried = await client.post("/api/v1/debug/node/start", json=payload)
        assert retried.status_code == 200 and retried.json()["data"]["status"] == "running", retried.text
        next_claim = store.read_launch_claim(**owner)
        assert next_claim is not None and next_claim.process_instance_id != claim.process_instance_id
        final = await client.post("/api/v1/debug/node/action", json={**owner, "action": "stop"})
        assert final.status_code == 200 and final.json()["data"]["status"] == "exited", final.text
        assert next_claim.pid is not None and probe_process_identity(next_claim.pid) is None
        assert all(writer != get_ident() for writer in gate.writers)
        context = TestRunContext.from_test_file(Path(__file__)).prepare()
        (context.artifacts_dir / f"{phase}-{outcome}.json").write_text(json.dumps({
            "candidate": claim.model_dump(mode="json"), "stopped": stopped.json(),
            "cold": cold.json(), "retried": retried.json(), "final": final.json(),
            "writer_threads": gate.writers, "event_loop_thread": get_ident(),
            "scope": "ASGI API、真实 Node/Inspector、原claim存储与冷service；不代Gateway、独立进程重启或Windows",
        }, ensure_ascii=False, indent=2) + "\n")
    finally:
        gate.release.set()
        await asyncio.gather(launch, *([stop] if stop is not None else []), return_exceptions=True)


@pytest.mark.parametrize("phase", ["spawned", "running"])
async def test_process_exit_cannot_overtake_original_claim_publication(
    claim_write_gate: ClaimWriteGate, debug_receive_client: DebugReceiveClient,
    monkeypatch: pytest.MonkeyPatch, phase: Literal["spawned", "running"],
) -> None:
    client, store, owner, _root, _reopen = debug_receive_client
    gate = claim_write_gate
    gate.phase = phase
    exited = asyncio.Event()
    monitor = NodeDebugRuntimeObserver.monitor_process

    async def observe_exit(observer: NodeDebugRuntimeObserver, runtime: NodeDebugRuntime) -> None:
        process = runtime.process
        assert process is not None
        await process.wait()
        exited.set()
        await monitor(observer, runtime)

    monkeypatch.setattr(NodeDebugRuntimeObserver, "monitor_process", observe_exit)
    configured = await client.post("/api/v1/debug/node/configurations", json={
        **owner, "name": "登记等待期间真实进程退出", "script_path": "entry.mjs",
    })
    assert configured.status_code == 200, configured.text
    launch = asyncio.create_task(client.post("/api/v1/debug/node/start", json={
        **owner, "configuration_id": configured.json()["data"]["active_configuration_id"], "path": "entry.mjs",
    }))
    try:
        await asyncio.wait_for(gate.entered.wait(), 6)
        claim = gate.candidate
        assert claim is not None and claim.pid is not None
        assert gate.writers[-1] != get_ident() and probe_process_identity(claim.pid) is not None
        before = store.read_launch_claim(**owner)
        assert before is not None and before.phase == ("launch_pending" if phase == "spawned" else "spawned")
        launch.cancel("进程退出前原 claim 启动取消")
        # 只给此用例经产品创建并核过身份的进程发信号，终态仍由原 monitor 核实。
        os.kill(claim.pid, signal.SIGTERM)
        await asyncio.wait_for(exited.wait(), 2)
        assert store.read_launch_claim(**owner) == before
        assert not launch.done()
        gate.release.set()
        with pytest.raises(asyncio.CancelledError, match="进程退出前原 claim 启动取消"):
            await launch
        async with asyncio.timeout(5):
            while (settled := store.read_launch_claim(**owner)) is None or settled.phase != "settled":
                await asyncio.sleep(0.01)
        assert settled.process_instance_id == claim.process_instance_id and settled.pid == claim.pid
        assert probe_process_identity(claim.pid) is None
        state = await client.get("/api/v1/debug/node", params=owner)
        assert state.status_code == 200 and state.json()["data"]["status"] == "failed", state.text
        context = TestRunContext.from_test_file(Path(__file__)).prepare()
        (context.artifacts_dir / f"{phase}-process-exit.json").write_text(json.dumps({
            "candidate": claim.model_dump(mode="json"), "settled": settled.model_dump(mode="json"),
            "state": state.json(), "writer_threads": gate.writers, "event_loop_thread": get_ident(),
        }, ensure_ascii=False, indent=2) + "\n")
    finally:
        gate.release.set()
        await asyncio.gather(launch, return_exceptions=True)
