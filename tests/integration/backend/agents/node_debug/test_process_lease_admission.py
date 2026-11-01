"""真实调试启动的账本写入不阻塞循环，取消仍等待原写入结算。"""

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

from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.node_debug.process.claim_runtime import (
    NodeDebugClaimRuntime,
    NodeDebugProcessLeaseIdentity,
)
from app.services.infrastructure.node_debug.process.process_identity import (
    probe_process_identity,
)
from app.services.infrastructure.node_debug.runtime_state import NodeDebugRuntime
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.agents import (
    test_node_debug_receive_errors as debug_fixture,
)
from tests.integration.backend.agents.test_node_debug_receive_errors import (
    DebugReceiveClient,
)
from tests.integration.backend.agents.test_node_debug_receive_errors import (
    debug_receive_client as debug_receive_client,  # noqa: PLC0414 - 复用真实 Node/Inspector 与 API 装具
)


@dataclass
class LeaseWriteGate:
    path: Path
    publication: int = 1
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    settling: asyncio.Event = field(default_factory=asyncio.Event)
    release: Event = field(default_factory=Event)
    failure: OSError | None = None
    writers: list[int] = field(default_factory=list)
    ledgers: list[ExternalResourceLeaseLedger] = field(default_factory=list)


@pytest.fixture
async def lease_write_gate(
    integration_workspace_root_path: str, monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[LeaseWriteGate]:
    """只替换原生发布边界；原账本锁、序列化、fsync 和状态发布均保留。"""
    gate = LeaseWriteGate(Path(integration_workspace_root_path) / ".boxteam" / "resources.json")
    loop, replace = asyncio.get_running_loop(), Path.replace
    mark_phase = NodeDebugClaimRuntime.mark_claim_phase

    async def observe_terminal(
        owner: NodeDebugClaimRuntime, runtime: NodeDebugRuntime,
        phase: Literal["stopping", "reconcile_required", "settled"], reason: str | None = None,
    ) -> None:
        if phase == "settled":
            gate.settling.set()
        await mark_phase(owner, runtime, phase, reason)

    def ledger() -> ExternalResourceLeaseLedger:
        instance = ExternalResourceLeaseLedger(state_path=gate.path)
        gate.ledgers.append(instance)
        return instance

    def publish(source: Path, target: Path) -> Path:
        if target == gate.path:
            gate.writers.append(get_ident())
            if len(gate.writers) == gate.publication:
                loop.call_soon_threadsafe(gate.entered.set)
                assert gate.release.wait(4), "测试未放行调试进程的原账本发布"
                if gate.failure is not None:
                    raise gate.failure
        return replace(source, target)

    monkeypatch.setattr(debug_fixture, "ExternalResourceLeaseLedger", ledger)
    monkeypatch.setattr(Path, "replace", publish)
    monkeypatch.setattr(NodeDebugClaimRuntime, "mark_claim_phase", observe_terminal)
    try:
        yield gate
    finally:
        gate.release.set()


@pytest.fixture
async def lease_client(
    lease_write_gate: LeaseWriteGate, debug_receive_client: DebugReceiveClient,
) -> AsyncIterator[DebugReceiveClient]:
    # 先注入持久账本，再让原装具创建服务；冷服务也从同一文件构造新的账本。
    yield debug_receive_client


@pytest.mark.parametrize("publication", [1, 2], ids=["register", "acquire"])
@pytest.mark.parametrize("outcome", ["success", "failure", "cancel", "cancel-failure"])
async def test_launch_lease_write_keeps_owner_until_original_publication_settles(
    lease_client: DebugReceiveClient, lease_write_gate: LeaseWriteGate,
    publication: int, outcome: str,
) -> None:
    client, store, owner, root, reopen = lease_client
    gate = lease_write_gate
    gate.publication = publication
    if "failure" in outcome:
        gate.failure = OSError(errno.ENOSPC, "调试占用发布失败", str(root / "ledger-secret"))
    configured = await client.post("/api/v1/debug/node/configurations", json={
        **owner, "name": "账本等待后原方案继续", "script_path": "entry.mjs",
    })
    assert configured.status_code == 200, configured.text
    configuration_id = configured.json()["data"]["active_configuration_id"]
    payload = {**owner, "configuration_id": configuration_id, "path": "entry.mjs"}
    launch = asyncio.create_task(client.post("/api/v1/debug/node/start", json=payload))
    stop = None
    try:
        await asyncio.wait_for(gate.entered.wait(), 6)
        assert gate.writers[-1] != get_ident(), "持久账本仍在事件循环中等待锁和发布"
        assert not launch.done() and not gate.release.is_set()
        claim = store.read_launch_claim(**owner)
        assert claim is not None and claim.phase == "running" and claim.pid is not None
        assert probe_process_identity(claim.pid) is not None
        identity = NodeDebugProcessLeaseIdentity.for_process_instance(**owner, process_instance_id=claim.process_instance_id)
        before = gate.path.read_bytes() if gate.path.exists() else None
        assert gate.ledgers[0].get_lease(identity.lease_id) is None
        probe = await asyncio.wait_for(client.get("/api/v1/debug/node/capabilities"), 1)
        assert probe.status_code == 200, probe.text
        assert probe.json()["request_id"] == probe.headers["X-Request-ID"]
        if outcome.startswith("cancel"):
            launch.cancel("原调试启动取消")
            await asyncio.sleep(0)
            launch.cancel("重复取消不得遗弃原writer")
            await asyncio.sleep(0)
            assert not launch.done()
        # 原 owner 锁必须覆盖迟到写入，否则停止会先结清、随后又发布活跃占用。
        stop = asyncio.create_task(client.post("/api/v1/debug/node/action", json={**owner, "action": "stop"}))
        await asyncio.sleep(0.02)
        assert not stop.done() and probe_process_identity(claim.pid) is not None
        assert (gate.path.read_bytes() if gate.path.exists() else None) == before
        gate.release.set()
        if outcome.startswith("cancel"):
            with pytest.raises(asyncio.CancelledError, match="原调试启动取消") as caught:
                await launch
            if gate.failure is not None:
                assert isinstance(caught.value.__cause__, OSError)
                assert caught.value.__cause__.__cause__ is gate.failure
        else:
            started = await launch
            assert started.status_code == (409 if gate.failure else 200), started.text
            assert started.json()["request_id"] == started.headers["X-Request-ID"]
            assert str(root / "ledger-secret") not in started.text
        stopped = await asyncio.wait_for(stop, 5)
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["data"]["status"] == "exited"
        assert probe_process_identity(claim.pid) is None
        settled = store.read_launch_claim(**owner)
        assert settled is not None and settled.phase == "settled" and settled.process_instance_id == claim.process_instance_id
        old_lease = ExternalResourceLeaseLedger(state_path=gate.path).get_lease(identity.lease_id)
        if gate.failure:
            assert old_lease is None
        else:
            assert old_lease is not None and old_lease.status == "settled"
        await reopen(True)
        cold = await client.get("/api/v1/debug/node", params=owner)
        assert cold.status_code == 200 and cold.json()["data"]["status"] == "idle", cold.text
        retried = await client.post("/api/v1/debug/node/start", json=payload)
        assert retried.status_code == 200 and retried.json()["data"]["status"] == "running", retried.text
        next_claim = store.read_launch_claim(**owner)
        assert next_claim is not None and next_claim.process_instance_id != claim.process_instance_id and next_claim.pid is not None
        final = await client.post("/api/v1/debug/node/action", json={**owner, "action": "stop"})
        assert final.status_code == 200 and final.json()["data"]["status"] == "exited", final.text
        assert probe_process_identity(next_claim.pid) is None
        assert ExternalResourceLeaseLedger(state_path=gate.path).get_lease(identity.lease_id) == old_lease
        context = TestRunContext.from_test_file(Path(__file__)).prepare()
        (context.artifacts_dir / f"{publication}-{outcome}.json").write_text(json.dumps({
            "claim": claim.model_dump(mode="json"), "stopped": stopped.json(), "cold": cold.json(),
            "retried": retried.json(), "final": final.json(), "writer_threads": gate.writers,
            "event_loop_thread": get_ident(),
            "scope": "ASGI API、真实 Node/Inspector、原账本文件与冷 service；不代 Gateway、进程重启或 Windows",
        }, ensure_ascii=False, indent=2) + "\n")
    finally:
        gate.release.set()
        await asyncio.gather(launch, *([stop] if stop is not None else []), return_exceptions=True)


@pytest.mark.parametrize("publication", [1, 2], ids=["register", "acquire"])
async def test_process_exit_waits_for_its_pending_lease_publication(
    lease_client: DebugReceiveClient, lease_write_gate: LeaseWriteGate, publication: int,
) -> None:
    client, store, owner, _root, _reopen = lease_client
    gate = lease_write_gate
    gate.publication = publication
    configured = await client.post("/api/v1/debug/node/configurations", json={
        **owner, "name": "发布期间进程自行结束", "script_path": "entry.mjs",
    })
    assert configured.status_code == 200, configured.text
    launch = asyncio.create_task(client.post("/api/v1/debug/node/start", json={
        **owner, "configuration_id": configured.json()["data"]["active_configuration_id"], "path": "entry.mjs",
    }))
    try:
        await asyncio.wait_for(gate.entered.wait(), 6)
        claim = store.read_launch_claim(**owner)
        assert claim is not None and claim.phase == "running" and claim.pid is not None
        identity = NodeDebugProcessLeaseIdentity.for_process_instance(**owner, process_instance_id=claim.process_instance_id)
        assert probe_process_identity(claim.pid) is not None
        launch.cancel("进程退出前原启动取消")
        # 只终止此用例经产品入口创建并核对过身份的进程，终态仍由原monitor取得。
        os.kill(claim.pid, signal.SIGTERM)
        await asyncio.wait_for(gate.settling.wait(), 2)
        assert store.read_launch_claim(**owner) == claim, "原lease尚未发布便结清了claim，迟到占用将无人结清"
        assert not launch.done()
        gate.release.set()
        with pytest.raises(asyncio.CancelledError, match="进程退出前原启动取消"):
            await launch
        async with asyncio.timeout(5):
            while (settled := store.read_launch_claim(**owner)) is None or settled.phase != "settled":
                await asyncio.sleep(0.01)
        assert settled.process_instance_id == claim.process_instance_id
        assert probe_process_identity(claim.pid) is None
        lease = ExternalResourceLeaseLedger(state_path=gate.path).get_lease(identity.lease_id)
        assert lease is not None and lease.status == "settled"
        state = await client.get("/api/v1/debug/node", params=owner)
        assert state.status_code == 200 and state.json()["data"]["status"] == "failed", state.text
    finally:
        gate.release.set()
        await asyncio.gather(launch, return_exceptions=True)
