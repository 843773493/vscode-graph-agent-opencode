"""真实调试 API 启动尚未物化时关闭服务，核实进程与原 claim 结算。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi import FastAPI

from app.api.deps import get_node_debug_service
from app.services.infrastructure.node_debug.process.process_identity import (
    probe_process_identity,
)
from app.services.infrastructure.node_debug.service import NodeDebugService
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.agents.test_node_debug_receive_errors import (
    DebugReceiveClient,
)
from tests.integration.backend.agents.test_node_debug_receive_errors import (
    debug_receive_client as debug_receive_client,  # noqa: PLC0414 - 复用真实调试 API 装具。
)


@pytest.mark.parametrize("cancel_waiter", [False, True])
async def test_close_during_pre_runtime_launch_keeps_original_process_until_verified(
    debug_receive_client: DebugReceiveClient, monkeypatch: pytest.MonkeyPatch, cancel_waiter: bool,
) -> None:
    client, store, owner, _root, reopen = debug_receive_client
    transport = cast(httpx.ASGITransport, client._transport)
    application = cast(FastAPI, transport.app)
    service: NodeDebugService = application.dependency_overrides[get_node_debug_service]()
    entered, release = asyncio.Event(), asyncio.Event()
    recover = service._lifecycle.assert_claim_recoverable

    async def before_runtime(key: tuple[str, str]) -> None:
        await recover(key)
        entered.set()
        await release.wait()

    configured = await client.post("/api/v1/debug/node/configurations", json={
        **owner, "name": "关闭不漏掉已准入启动", "script_path": "entry.mjs",
    })
    assert configured.status_code == 200, configured.text
    payload = {**owner, "configuration_id": configured.json()["data"]["active_configuration_id"],
               "path": "entry.mjs"}
    monkeypatch.setattr(service._lifecycle, "assert_claim_recoverable", before_runtime)
    launch = asyncio.create_task(client.post("/api/v1/debug/node/start", json=payload))
    close = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        key = (owner["session_id"], owner["thread_id"])
        assert key not in service._runtimes and store.read_launch_claim(**owner) is None
        close = asyncio.create_task(service.close())
        await asyncio.sleep(0)
        assert not close.done(), "关闭漏掉尚未物化 runtime 的已准入启动"
        original = service._close_tasks[key]
        if cancel_waiter:
            close.cancel("取消本次关闭观察")
            with pytest.raises(asyncio.CancelledError, match="取消本次关闭观察"):
                await close
            assert not original.done() and not original.cancelling()
            close = asyncio.create_task(service.close())
        denied = await client.post("/api/v1/debug/node/start", json=payload)
        assert denied.status_code == 409 and "正在关闭" in denied.text, denied.text
        assert denied.json()["request_id"] == denied.headers["X-Request-ID"]
        assert service._close_tasks[key] is original
        release.set()
        started = await asyncio.wait_for(launch, 5)
        assert started.status_code == 200, started.text
        assert started.json()["request_id"] == started.headers["X-Request-ID"]
        pid = started.json()["data"]["pid"]
        assert isinstance(pid, int)
        await asyncio.wait_for(close, 5)
        assert probe_process_identity(pid) is None
        claim = store.read_launch_claim(**owner)
        assert claim is not None and claim.pid == pid and claim.phase == "settled"
        assert not service._admitted_operations and original.done()
        leases = service._external_resource_leases
        assert not leases.leases_for_resource(f"node_debug_process:{key[0]}:{key[1]}")
        await reopen(True)
        cold = await client.get("/api/v1/debug/node", params=owner)
        assert cold.status_code == 200 and cold.json()["data"]["status"] == "idle", cold.text
        assert cold.json()["data"]["active_configuration_id"] == payload["configuration_id"]
        restarted = await client.post("/api/v1/debug/node/start", json=payload)
        assert restarted.status_code == 200, restarted.text
        next_claim = store.read_launch_claim(**owner)
        assert next_claim is not None and next_claim.process_instance_id != claim.process_instance_id
        stopped = await client.post("/api/v1/debug/node/action", json={**owner, "action": "stop"})
        assert stopped.status_code == 200 and stopped.json()["data"]["status"] == "exited", stopped.text
        assert probe_process_identity(restarted.json()["data"]["pid"]) is None
        artifacts = TestRunContext.from_test_file(Path(__file__)).prepare().artifacts_dir
        (artifacts / f"close-start-{cancel_waiter}.json").write_text(json.dumps({
            "started": started.json(), "denied": denied.json(), "settled": claim.model_dump(mode="json"),
            "cold": cold.json(), "restarted": restarted.json(), "stopped": stopped.json(),
            "scope": "ASGI调试入口、真实Node/Inspector与原claim；不代Gateway、Backend硬重启或Windows",
        }, ensure_ascii=False, indent=2) + "\n")
    finally:
        release.set()
        await asyncio.gather(launch, *([close] if close is not None else []), return_exceptions=True)
