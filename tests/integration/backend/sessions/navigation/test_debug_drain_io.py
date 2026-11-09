"""删除排空在 Debug 插件 run 的 claim、manifest 与核验读取结算前不得归还 gate。"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import threading
from contextlib import closing
from contextvars import ContextVar
from pathlib import Path
from typing import Literal

import httpx
import pytest

from app.container import AppContainer
from app.core.identifier import create_prefixed_id
from app.core.session_control_store import SessionControlStore
from app.schemas.internal_v2.node_debug import NodeDebugConfigurationCreateRequest
from app.schemas.internal_v2.session import SessionCreateRequest
from app.services.infrastructure.node_debug.process.launch_claim import (
    claim_with_target_pid,
    new_launch_claim,
)
from app.services.infrastructure.node_debug.runtime_state import NodeDebugRuntime
from app.services.infrastructure.node_debug.session.session_store import (
    NodeDebugSessionStore,
)
from tests.integration.backend.sessions.navigation.test_delete_settlement_errors import (
    _receipt,
    _settlement,
)
from tests.integration.backend.sessions.navigation.test_public_errors import (
    navigation_api as navigation_api,  # noqa: PLC0414
)
from tests.integration.backend.sessions.navigation.test_public_errors import (
    startup_container as startup_container,  # noqa: PLC0414
)
from tests.support.node_debug_dependencies import node_debug_binding_snapshot
from tests.unit.core.session_deletion.test_operation_replay_gc import (
    NativeStep,
    _entered,
    _turns,
)
from tests.unit.core.session_deletion.test_operation_replay_gc import (
    native_step as native_step,  # noqa: PLC0414
)


@pytest.mark.parametrize("stage", ["stopping", "manifest", "final"])
@pytest.mark.parametrize("outcome", ["wait", "cancel", "failure"])
async def test_debug_delete_wait_preserves_owner_until_original_io_settles(
    navigation_api: tuple[AppContainer, httpx.AsyncClient, str], native_step: NativeStep,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    stage: str, outcome: str,
) -> None:
    container, client, _ = navigation_api
    resolver = container.session_catalog_service.path_resolver
    operations = container.session_catalog_service.operations
    service = container.node_debug_service
    session = await container.session_service.create(SessionCreateRequest(title="调试排空原生等待"))
    peer = await container.session_service.create(SessionCreateRequest(title="排空不阻塞旁支"))
    owner = (session.session_id, resolver.main_thread_id(session.session_id))
    source = resolver.resolve_session_node(session.session_id)
    original_manifest = (source / "session.json").read_bytes()
    script = container.workspace_root / "debug-drain.mjs"
    script.write_text("setInterval(() => {}, 1000);\n", encoding="utf-8")
    configured = await service.create_configuration(NodeDebugConfigurationCreateRequest(
        session_id=owner[0], thread_id=owner[1], name="删除前实际运行", script_path=script.name,
    ))
    configuration_id = configured.active_configuration_id
    assert configuration_id is not None
    store = service._session_store
    assert store is not None
    binding = node_debug_binding_snapshot().model_copy(
        update={"workspace_id": service._workspace_id}
    )
    claim = new_launch_claim(
        session_id=owner[0], thread_id=owner[1], run_id=create_prefixed_id("op"),
        configuration_id=configuration_id, debugger_binding=binding,
        script_path=script.name, args=[], node_executable="node", working_directory="",
        launch_profile_name=None, breakpoints=[],
    )

    class ExitedPluginRun:
        async def stop(self) -> dict[str, object]:
            return {"run_id": claim.run_id, "status": "exited", "exit_code": 0}

        async def status(self) -> dict[str, object]:
            return {"run_id": claim.run_id, "status": "exited", "exit_code": 0}

        async def close(self) -> None:
            return None

    runtime = NodeDebugRuntime(
        session_id=owner[0], thread_id=owner[1], configuration_id=configuration_id,
        debugger_binding=binding, workspace_root=container.workspace_root,
        script_path=script, relative_script_path=script.name, status="running",
    )
    runtime.run_id = claim.run_id
    runtime.process_instance_id = claim.process_instance_id
    runtime.plugin_runtime = ExitedPluginRun()  # type: ignore[assignment]
    service._runtimes[owner] = runtime
    service._session_state.persist_runtime_state(*owner, runtime)
    await service._claim_runtime.write_launch_claim(claim)
    claim = claim_with_target_pid(claim, pid=4242)
    await service._claim_runtime.write_launch_claim(claim)
    await service._claim_runtime.mark_claim_running(runtime)
    claim = store.read_launch_claim(*owner)
    assert claim is not None and claim.phase == "running" and claim.run_id == runtime.run_id
    manifest = store.read_manifest(*owner)
    assert manifest is not None
    operation_id = create_prefixed_id("op")
    target = resolver.sessions_root / ".deleting" / operation_id / owner[0]
    draining: ContextVar[bool] = ContextVar("test_debug_drain_io", default=False)
    phase: ContextVar[str] = ContextVar("test_debug_drain_phase", default="")
    drain, mark = service.drain_session, service._lifecycle._mark_claim_phase
    active_claim = service._claim_runtime.active_claim
    read_bytes, replace = Path.read_bytes, os.replace
    failure = PermissionError(errno.EACCES, "调试原生访问被拒绝", str(source / "debug/node"))

    async def observed_drain(session_id: str) -> None:
        token = draining.set(True)
        try:
            await drain(session_id)
        finally:
            draining.reset(token)

    async def observed_mark(
        runtime: NodeDebugRuntime,
        value: Literal["stopping", "execution_lost", "settled"], reason: str | None = None,
    ) -> None:
        token = phase.set(value)
        try:
            await mark(runtime, value, reason)
        finally:
            phase.reset(token)

    def observed_active(session_id: str, thread_id: str):
        if phase.get():
            # 原驻留回调也读取claim；本故障点只针对排空入口末尾的独立核验。
            return active_claim(session_id, thread_id)
        token = phase.set("final")
        try:
            return active_claim(session_id, thread_id)
        finally:
            phase.reset(token)

    def block() -> None:
        if draining.get() and not native_step.calls:
            native_step.block()
            if outcome == "failure":
                raise failure

    def observed_read(path: Path) -> bytes:
        if path.name == NodeDebugSessionStore.LAUNCH_CLAIM_FILE_NAME and phase.get() == stage:
            block()
        return read_bytes(path)

    def observed_replace(pending: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
        if stage == "manifest" and Path(destination).name == NodeDebugSessionStore.MANIFEST_FILE_NAME:
            block()
        replace(pending, destination)

    monkeypatch.setattr(service, "drain_session", observed_drain)
    monkeypatch.setattr(service._lifecycle, "_mark_claim_phase", observed_mark)
    monkeypatch.setattr(service._claim_runtime, "active_claim", observed_active)
    monkeypatch.setattr(Path, "read_bytes", observed_read)
    monkeypatch.setattr(os, "replace", observed_replace)
    caplog.set_level(logging.ERROR)
    stopping = None
    worker = operations._worker_task
    assert worker is not None
    try:
        accepted = await client.post("/api/v1/session-catalog/operations:enqueue", json={
            "intents": [{"client_operation_id": operation_id, "client_sequence": 1, "kind": "delete_session",
                         "target_node_id": owner[0], "base_catalog_revision": resolver.revision}],
        })
        assert accepted.status_code == 202, accepted.text
        await _entered(native_step)
        assert len(native_step.calls) == 1 and native_step.calls[0] != threading.get_ident()
        pending = await _receipt(client, operation_id)
        record = resolver.catalog_store.get_subtree_delete_record(operation_id)
        assert pending.state == "committed" and pending.settlement == "pending"
        assert record.state == "deleting" and record.drained_session_ids == ()
        assert service._owner_lock(owner).locked()
        assert resolver._delete_service._key_lock(operation_id).locked()
        assert (source / "session.json").read_bytes() == original_manifest and not target.exists()
        with closing(SessionControlStore(source / "session-control.sqlite", read_only=True)) as control:
            assert control.get_fence() == ("active", 1)
        current = store.read_launch_claim(*owner)
        assert current is not None and current.process_instance_id == claim.process_instance_id
        assert current.phase == ("running" if stage == "stopping" else "settled")
        assert current.run_id == claim.run_id
        if outcome == "cancel":
            stopping = asyncio.create_task(operations.stop())
            await _turns()
            assert not stopping.done() and not worker.done()
        async with resolver._delete_service._session_gate.exclusive(peer.session_id):
            assert resolver.catalog_store.get_node(peer.session_id).state == "active"
        response = await client.get("/api/v1/session-catalog/roots", headers={"X-Request-ID": "debug-drain-probe"})
        assert response.status_code == 200, response.text
        assert response.json()["request_id"] == response.headers["X-Request-ID"] == "debug-drain-probe"
        native_step.release.set()
        if stopping is not None:
            await stopping
            assert worker.cancelled() and operations.worker_state == "stopped"
            assert resolver.catalog_store.get_subtree_delete_record(operation_id) == record
            assert source.is_dir() and not target.exists()
            with closing(SessionControlStore(source / "session-control.sqlite", read_only=True)) as control:
                assert control.get_fence() == ("active", 1)
            await operations.start()
        elif outcome == "failure":
            failed = await _settlement(client, operation_id, "repair_required")
            assert failed.state == "committed" and "PermissionError" in str(failed.error_detail)
            assert source.is_dir() and not target.exists()
            assert resolver.catalog_store.get_subtree_delete_record(operation_id).drained_session_ids == ()
            assert any(item.exc_info is not None and item.exc_info[1] is not None
                       and item.exc_info[1].__cause__ is failure for item in caplog.records)
            await operations.stop()
            await operations.start()
        settled = await _settlement(client, operation_id, "settled")
        assert settled.state == "committed" and settled.receipt_revision > pending.receipt_revision
        assert not source.exists() and (target / "session.json").read_bytes() == original_manifest
        completed = resolver.catalog_store.get_subtree_delete_record(operation_id)
        assert completed.state == "completed" and completed.drained_session_ids == (owner[0],)
        assert completed.frozen_session_locators == record.frozen_session_locators
        assert len(native_step.calls) == 1
        # 从实际隔离位置核原实例结算和方案，不通过已删除catalog重新定位。
        claim_path, = target.rglob(NodeDebugSessionStore.LAUNCH_CLAIM_FILE_NAME)
        final_claim = type(claim).model_validate_json(claim_path.read_bytes())
        assert final_claim.phase == "settled" and final_claim.run_id == claim.run_id
        manifest_path, = target.rglob(NodeDebugSessionStore.MANIFEST_FILE_NAME)
        final_manifest = type(manifest).model_validate_json(manifest_path.read_bytes())
        assert final_manifest.active_configuration_id == manifest.active_configuration_id
        assert final_manifest.actions == manifest.actions
        await service.close()
    finally:
        native_step.release.set()
        if stopping is not None:
            await asyncio.gather(stopping, return_exceptions=True)
        await operations.stop()
        await service.close()
