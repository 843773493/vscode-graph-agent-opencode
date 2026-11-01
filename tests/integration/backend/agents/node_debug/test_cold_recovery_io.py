"""冷调试恢复的原生等待不阻塞事件循环，也不提前归还同 owner 锁。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from contextvars import ContextVar
from pathlib import Path

import pytest

from app.services.infrastructure.node_debug.process.process_identity import (
    probe_process_identity,
)
from app.services.infrastructure.node_debug.session.session_store import (
    NodeDebugSessionStore,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.agents.test_node_debug_receive_errors import (
    DebugReceiveClient,
)
from tests.integration.backend.agents.test_node_debug_receive_errors import (
    debug_receive_client as debug_receive_client,  # noqa: PLC0414
)
from tests.unit.core.session_deletion.test_operation_replay_gc import (
    NativeStep,
    _entered,
    _turns,
)
from tests.unit.core.session_deletion.test_operation_replay_gc import (
    native_step as native_step,  # noqa: PLC0414
)


@pytest.mark.skipif(sys.platform != "linux", reason="原生 /proc 故障点在 Linux 验证")
@pytest.mark.parametrize("stage", ["claim", "identity", "audit"])
@pytest.mark.parametrize("cancel", [False, True], ids=["wait", "cancel"])
async def test_cold_recovery_settles_native_access_before_releasing_owner(
    debug_receive_client: DebugReceiveClient, native_step: NativeStep,
    stage: str, cancel: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store, owner, _root, reopen = debug_receive_client
    configured = await client.post("/api/v1/debug/node/configurations", json={
        **owner, "name": "冷恢复原生等待", "script_path": "entry.mjs",
    })
    assert configured.status_code == 200, configured.text
    configuration_id = configured.json()["data"]["active_configuration_id"]
    started = await client.post("/api/v1/debug/node/start", json={
        **owner, "configuration_id": configuration_id, "path": "entry.mjs",
    })
    assert started.status_code == 200, started.text
    original = store.read_launch_claim(**owner)
    assert original is not None and original.phase == "running" and original.pid is not None
    manifest_before = store.read_manifest_payload(**owner)[0]
    identity_path = Path(f"/proc/{original.pid}/stat")
    await reopen(False)
    assert store.read_launch_claim(**owner) == original
    assert store.read_manifest_payload(**owner)[0] == manifest_before
    assert probe_process_identity(original.pid) is not None

    recovering: ContextVar[bool] = ContextVar("test_cold_debug_recovery", default=False)
    read_bytes, read_text, replace = Path.read_bytes, Path.read_text, os.replace

    def block() -> None:
        if recovering.get() and not native_step.calls:
            native_step.block()

    def observed_bytes(path: Path) -> bytes:
        if stage == "claim" and path.name == NodeDebugSessionStore.LAUNCH_CLAIM_FILE_NAME:
            block()
        return read_bytes(path)

    def observed_text(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
        if stage == "identity" and path == identity_path:
            block()
        return read_text(path, encoding=encoding, errors=errors)

    def observed_replace(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        if stage == "audit" and Path(target).name == NodeDebugSessionStore.MANIFEST_FILE_NAME:
            block()
        replace(source, target)

    async def read_cold():
        token = recovering.set(True)
        try:
            return await client.get("/api/v1/debug/node", params=owner)
        finally:
            recovering.reset(token)

    loop_thread = threading.get_ident()
    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", observed_bytes)
        patch.setattr(Path, "read_text", observed_text)
        patch.setattr(os, "replace", observed_replace)
        reading = asyncio.create_task(read_cold())
        competing = None
        try:
            await _entered(native_step)
            assert len(native_step.calls) == 1 and native_step.calls[0] != loop_thread
            before = store.read_launch_claim(**owner)
            assert before is not None and before.process_instance_id == original.process_instance_id
            if stage == "audit":
                assert before.phase == "settled" and probe_process_identity(original.pid) is None
            else:
                assert before == original and probe_process_identity(original.pid) is not None
            competing = asyncio.create_task(client.get("/api/v1/debug/node", params=owner))
            await _turns()
            assert not reading.done() and not competing.done()
            if cancel:
                reading.cancel("原冷恢复取消")
                await _turns()
                reading.cancel("重复冷恢复取消")
                await _turns()
                assert not reading.done() and not competing.done()
            # 竞争者不带屏障上下文；它只能在原 owner 归还后进入恢复。
            competing.cancel("取消测试竞争者")
            with pytest.raises(asyncio.CancelledError):
                await competing
            native_step.release.set()
            if cancel:
                with pytest.raises(asyncio.CancelledError, match="原冷恢复取消"):
                    await reading
                if stage != "audit":
                    assert store.read_launch_claim(**owner) == original
                    assert probe_process_identity(original.pid) is not None
            else:
                response = await reading
                assert response.status_code == 200, response.text
                assert response.json()["request_id"] == response.headers["X-Request-ID"]
        finally:
            native_step.release.set()
            await asyncio.gather(*(task for task in (reading, competing) if task is not None), return_exceptions=True)

    recovered = await client.get("/api/v1/debug/node", params=owner)
    assert recovered.status_code == 200, recovered.text
    state = recovered.json()["data"]
    assert state["status"] == "idle" and state["pid"] is None
    assert state["active_configuration_id"] == configuration_id
    assert state["actions"][:-1] == started.json()["data"]["actions"]
    assert len([action for action in state["actions"] if action["action"] == "reconcile_settled"]) == 1
    assert probe_process_identity(original.pid) is None
    settled = store.read_launch_claim(**owner)
    assert settled is not None and settled.phase == "settled"
    assert settled.process_instance_id == original.process_instance_id and settled.pid == original.pid
    assert len(native_step.calls) == 1
    await reopen(True)
    cold_again = await client.get("/api/v1/debug/node", params=owner)
    assert cold_again.status_code == 200, cold_again.text
    assert cold_again.json()["data"]["actions"] == state["actions"]
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    (context.artifacts_dir / f"{stage}-{'cancel' if cancel else 'wait'}.json").write_text(json.dumps({
        "started": started.json(), "recovered": recovered.json(), "cold_again": cold_again.json(),
        "claim": original.model_dump(mode="json"), "settled": settled.model_dump(mode="json"),
        "native_threads": native_step.calls, "loop_thread": loop_thread,
        "scope": "真实ASGI、Node/Inspector和冷service；准入为现有替身，不代Session gate、Gateway、硬重启或Windows",
    }, ensure_ascii=False, indent=2) + "\n")
