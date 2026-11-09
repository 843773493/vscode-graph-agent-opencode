"""Node Debug 插件运行链的启动拒绝、未知结果和失效实例边界。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Literal
from unittest.mock import AsyncMock

import pytest

from app.schemas.internal_v2.node_debug import NodeDebugDebuggerContributionDTO
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.node_debug.process.claim_runtime import (
    NodeDebugProcessLeaseIdentity,
)
from app.services.infrastructure.node_debug.process.launch_claim import (
    NodeDebugClaimRecoveryDecision,
    claim_marked,
    claim_running,
    claim_with_target_pid,
    new_launch_claim,
)
from app.services.infrastructure.node_debug.process.plugin_runtime import (
    NodeDebugPluginRuntime,
)
from app.services.infrastructure.node_debug.runtime_state import NodeDebugRuntime
from app.services.infrastructure.node_debug.service import NodeDebugService
from app.services.infrastructure.node_debug.session.session_store import (
    NodeDebugSessionStore,
)
from tests.support.node_debug_dependencies import (
    node_debug_binding_snapshot,
    node_debug_file_paths,
    permissive_node_debug_session_admission,
)
from tests.unit.core.catalog_workspace_helper import (
    CatalogWorkspaceContext,
    build_catalog_workspace,
)


@dataclass
class DebugLaunchContext:
    workspace: CatalogWorkspaceContext
    store: NodeDebugSessionStore
    session_id: str
    thread_id: str
    service: NodeDebugService = field(init=False)

    def cold_service(self) -> NodeDebugService:
        root = self.workspace.workspace_root
        return NodeDebugService(
            workspace_root=root, file_paths=node_debug_file_paths(root),
            config_service=ConfigService(workspace_root=root),
            session_store=self.store,
            session_admission=permissive_node_debug_session_admission(),  # type: ignore[arg-type]
            external_resource_leases=ExternalResourceLeaseLedger(),
        )


@pytest.fixture
async def debug_launch_context(tmp_path: Path) -> AsyncIterator[DebugLaunchContext]:
    with build_catalog_workspace(tmp_path / "workspace") as workspace:
        session_id = await workspace.create_session("启动错误边界")
        (workspace.workspace_root / "entry.mjs").write_text("console.log(42);\n")
        store = NodeDebugSessionStore(workspace.resolver)
        fixture = DebugLaunchContext(
            workspace, store, session_id, workspace.resolver.main_thread_id(session_id),
        )
        fixture.service = fixture.cold_service()
        try:
            yield fixture
        finally:
            await fixture.service.close()


@pytest.mark.asyncio
async def test_plugin_launch_rejection_is_not_replayed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = object.__new__(NodeDebugPluginRuntime)
    calls: list[str] = []
    rejection = RuntimeError("Debug 插件拒绝 launch")

    async def request(operation: str, _payload: dict[str, object]) -> dict[str, object]:
        calls.append(operation)
        raise rejection

    monkeypatch.setattr(runtime, "request", request)
    with pytest.raises(RuntimeError, match="拒绝 launch") as caught:
        await runtime.launch({"process_instance_id": "node-debug-proc-test"})
    assert caught.value is rejection
    assert calls == ["launch"]


@pytest.mark.asyncio
async def test_unknown_plugin_launch_result_is_not_replayed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = object.__new__(NodeDebugPluginRuntime)
    runtime.run_id = "run_test"
    calls: list[str] = []

    async def request(operation: str, _payload: dict[str, object]) -> dict[str, object]:
        calls.append(operation)
        if operation == "launch":
            raise TimeoutError("launch response lost")
        return {"run_id": "other_run", "status": "running"}

    monkeypatch.setattr(runtime, "request", request)
    with pytest.raises(RuntimeError, match="结果未知.*没有重放"):
        await runtime.launch({"process_instance_id": "node-debug-proc-test"})
    assert calls == ["launch", "status"]


@pytest.mark.asyncio
async def test_cancelled_wait_for_plugin_request_lock_leaves_no_pending_response() -> None:
    runtime = object.__new__(NodeDebugPluginRuntime)
    runtime._request_lock = asyncio.Lock()
    runtime._responses = {}
    runtime._socket = object()  # type: ignore[assignment]
    runtime.run_id = "run_test"
    runtime._request_id = "request_test"
    await runtime._request_lock.acquire()
    request = asyncio.create_task(runtime.request("status", {}))
    try:
        await asyncio.sleep(0)
        assert not runtime._responses
        request.cancel("等待插件连接锁")
        with pytest.raises(asyncio.CancelledError, match="等待插件连接锁"):
            await request
        assert not runtime._responses
    finally:
        runtime._request_lock.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["status", "inspector"])
async def test_failed_recovery_closes_backend_connection_without_stopping_target(
    debug_launch_context: DebugLaunchContext,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    from app.services.infrastructure.node_debug import (
        service as node_debug_service_module,
    )

    fixture = debug_launch_context
    binding = node_debug_binding_snapshot().model_copy(
        update={"workspace_id": fixture.workspace.workspace_id}
    )
    claim = new_launch_claim(
        session_id=fixture.session_id,
        thread_id=fixture.thread_id,
        run_id="run_recovery_test",
        configuration_id="dbgcfg_" + "1" * 32,
        debugger_binding=binding,
        script_path="entry.mjs",
        args=[],
        node_executable="node",
        working_directory="",
        launch_profile_name=None,
        breakpoints=[],
    )

    class RuntimeConnection:
        closed = 0
        stopped = 0

        async def status(self) -> dict[str, object]:
            if failure_stage == "status":
                raise ConnectionError("状态查询断开")
            return {
                "run_id": claim.run_id,
                "process_instance_id": claim.process_instance_id,
                "status": "running",
                "pid": 43210,
            }

        async def close(self) -> None:
            self.closed += 1

        async def stop(self) -> dict[str, object]:
            self.stopped += 1
            return {"status": "exited"}

    connection = RuntimeConnection()

    async def connect(cls, **_kwargs: object) -> RuntimeConnection:
        return connection

    async def fail_inspector(_runtime: object) -> None:
        raise ConnectionError("Inspector 初始化失败")

    monkeypatch.setattr(
        NodeDebugPluginRuntime, "connect", classmethod(connect),
    )
    monkeypatch.setattr(
        node_debug_service_module, "require_current_request_id", lambda _label: "request_test",
    )
    if failure_stage == "inspector":
        monkeypatch.setattr(fixture.service._inspector, "connect", fail_inspector)
    with pytest.raises(ConnectionError):
        await fixture.service._recover_debug_run(claim)
    assert connection.closed == 1
    assert connection.stopped == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome,claim_phase,lease_status,audit_action",
    [
        ("execution_lost", "execution_lost", "active", "execution_lost"),
        ("settled", "settled", "settled", "execution_settled"),
    ],
)
async def test_plugin_run_recovery_records_audit_and_preserves_owner_lease(
    debug_launch_context: DebugLaunchContext,
    monkeypatch: pytest.MonkeyPatch,
    outcome: Literal["execution_lost", "settled"],
    claim_phase: Literal["execution_lost", "settled"],
    lease_status: Literal["active", "settled"],
    audit_action: Literal["execution_lost", "execution_settled"],
) -> None:
    fixture = debug_launch_context
    binding = node_debug_binding_snapshot().model_copy(
        update={"workspace_id": fixture.workspace.workspace_id}
    )
    claim = new_launch_claim(
        session_id=fixture.session_id,
        thread_id=fixture.thread_id,
        run_id="run_test",
        configuration_id="dbgcfg_" + "1" * 32,
        debugger_binding=binding,
        script_path="entry.mjs",
        args=[],
        node_executable="node",
        working_directory="",
        launch_profile_name=None,
        breakpoints=[],
    )
    claim = claim_running(claim_with_target_pid(claim, pid=43210))
    await fixture.service._claim_runtime.write_launch_claim(claim)
    lease = NodeDebugProcessLeaseIdentity.for_process_instance(
        session_id=fixture.session_id,
        thread_id=fixture.thread_id,
        process_instance_id=claim.process_instance_id,
    )
    leases = fixture.service._external_resource_leases
    leases.register_external(
        resource_id=lease.resource_id, kind="node_debug_process", lifetime_scope="session",
    )
    leases.acquire(
        resource_id=lease.resource_id, turn_stream_id=lease.holder_id,
        lease_id=lease.lease_id, operation_id=lease.operation_id,
    )

    async def missing_original_run(_claim: object) -> NodeDebugClaimRecoveryDecision:
        return NodeDebugClaimRecoveryDecision(outcome, "原 Debug run 已不存在")

    monkeypatch.setattr(fixture.service._lifecycle, "_recover_run", missing_original_run)
    decision = await fixture.service._lifecycle.reconcile_persisted_claim(
        (fixture.session_id, fixture.thread_id)
    )
    assert decision == NodeDebugClaimRecoveryDecision(outcome, "原 Debug run 已不存在")
    persisted = fixture.store.read_launch_claim(fixture.session_id, fixture.thread_id)
    assert persisted is not None and persisted.phase == claim_phase
    assert persisted.process_instance_id == claim.process_instance_id
    lease_record = leases.get_lease(lease.lease_id)
    assert lease_record is not None and lease_record.status == lease_status
    manifest = fixture.store.read_manifest(fixture.session_id, fixture.thread_id)
    assert manifest is not None
    assert any(item.action == audit_action for item in manifest.actions)
    if outcome == "execution_lost":
        with pytest.raises(RuntimeError, match="phase=execution_lost"):
            fixture.service._assert_no_unsettled_claim(
                (fixture.session_id, fixture.thread_id), operation="启动新调试实例",
            )
    else:
        fixture.service._assert_no_unsettled_claim(
            (fixture.session_id, fixture.thread_id), operation="启动新调试实例",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("already_lost", [False, True])
async def test_start_does_not_replace_an_unrecoverable_original_run(
    debug_launch_context: DebugLaunchContext,
    monkeypatch: pytest.MonkeyPatch,
    already_lost: bool,
) -> None:
    fixture = debug_launch_context
    binding = node_debug_binding_snapshot().model_copy(
        update={"workspace_id": fixture.workspace.workspace_id}
    )
    claim = new_launch_claim(
        session_id=fixture.session_id, thread_id=fixture.thread_id,
        run_id="run_unrecoverable", configuration_id="dbgcfg_" + "1" * 32,
        debugger_binding=binding, script_path="entry.mjs", args=[],
        node_executable="node", working_directory="", launch_profile_name=None,
        breakpoints=[],
    )
    reason = "原 Debug 插件状态无法核实"
    if already_lost:
        claim = claim_marked(claim, phase="execution_lost", reason=reason)
    await fixture.service._claim_runtime.write_launch_claim(claim)
    recovery = AsyncMock(return_value=NodeDebugClaimRecoveryDecision("execution_lost", reason))
    connect = AsyncMock()
    monkeypatch.setattr(fixture.service._lifecycle, "_recover_run", recovery)
    monkeypatch.setattr(NodeDebugPluginRuntime, "connect", connect)
    monkeypatch.setattr(fixture.service, "resolve_node_debugger_binding", AsyncMock(
        return_value=NodeDebugDebuggerContributionDTO(
            **binding.model_dump(), label="Node Inspector", configuration_schema={}, capabilities=[],
        ),
    ))

    with pytest.raises(RuntimeError, match="结果未知，结清前拒绝启动新实例"):
        await fixture.service.start(
            session_id=fixture.session_id, thread_id=fixture.thread_id,
            path="entry.mjs", args=[], breakpoints=[],
            gateway_id=binding.gateway_id, request_id="request_unrecoverable",
        )

    persisted = fixture.store.read_launch_claim(fixture.session_id, fixture.thread_id)
    assert persisted is not None and persisted.phase == "execution_lost"
    assert persisted.run_id == claim.run_id
    assert persisted.process_instance_id == claim.process_instance_id
    assert persisted.reconcile_reason == reason
    assert not fixture.service._runtimes
    assert recovery.await_count == (0 if already_lost else 1)
    connect.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("previous_status", ["execution_lost", "exited", "failed"])
async def test_start_cannot_replace_a_disconnected_runtime_with_unknown_target(
    debug_launch_context: DebugLaunchContext,
    monkeypatch: pytest.MonkeyPatch,
    previous_status: Literal["execution_lost", "exited", "failed"],
) -> None:
    fixture = debug_launch_context
    binding = node_debug_binding_snapshot().model_copy(
        update={"workspace_id": fixture.workspace.workspace_id}
    )
    owner = (fixture.session_id, fixture.thread_id)
    runtime = NodeDebugRuntime(
        session_id=owner[0], thread_id=owner[1], configuration_id="dbgcfg_" + "1" * 32,
        debugger_binding=binding, workspace_root=fixture.workspace.workspace_root,
        script_path=fixture.workspace.workspace_root / "entry.mjs", relative_script_path="entry.mjs",
        status=previous_status, target_exited=previous_status != "execution_lost",
    )
    stop = AsyncMock(side_effect=ConnectionError("原控制通道已断开"))
    status = AsyncMock(side_effect=ConnectionError("无法核实原目标"))
    runtime.plugin_runtime = SimpleNamespace(stop=stop, status=status)  # type: ignore[assignment]
    fixture.service._runtimes[owner] = runtime
    connect = AsyncMock()
    monkeypatch.setattr(NodeDebugPluginRuntime, "connect", connect)
    monkeypatch.setattr(fixture.service, "resolve_node_debugger_binding", AsyncMock(
        return_value=NodeDebugDebuggerContributionDTO(
            **binding.model_dump(), label="Node Inspector", configuration_schema={}, capabilities=[],
        ),
    ))
    try:
        with pytest.raises(RuntimeError, match="旧调试实例无法核实终态"):
            await fixture.service.start(
                session_id=owner[0], thread_id=owner[1], path="entry.mjs",
                args=[], breakpoints=[], gateway_id=binding.gateway_id,
                request_id="request_disconnected",
            )
        assert fixture.service._runtimes[owner] is runtime
        assert runtime.status == "reconcile_required"
        assert runtime.target_exited == (previous_status != "execution_lost")
        stop.assert_awaited_once()
        status.assert_awaited_once()
        connect.assert_not_awaited()
        assert fixture.store.read_launch_claim(*owner) is None
    finally:
        fixture.service._runtimes.pop(owner)


@pytest.mark.asyncio
async def test_plugin_stop_rejection_keeps_run_claim_and_lease_until_exit_is_confirmed(
    debug_launch_context: DebugLaunchContext,
) -> None:
    fixture = debug_launch_context
    binding = node_debug_binding_snapshot().model_copy(
        update={"workspace_id": fixture.workspace.workspace_id}
    )
    claim = new_launch_claim(
        session_id=fixture.session_id,
        thread_id=fixture.thread_id,
        run_id="run_stop_rejection",
        configuration_id="dbgcfg_" + "1" * 32,
        debugger_binding=binding,
        script_path="entry.mjs",
        args=[],
        node_executable="node",
        working_directory="",
        launch_profile_name=None,
        breakpoints=[],
    )
    claim = claim_running(claim_with_target_pid(claim, pid=43210))
    fixture.store.write_launch_claim(claim)
    runtime = NodeDebugRuntime(
        session_id=fixture.session_id,
        thread_id=fixture.thread_id,
        configuration_id=claim.configuration_id,
        debugger_binding=binding,
        workspace_root=fixture.workspace.workspace_root,
        script_path=fixture.workspace.workspace_root / "entry.mjs",
        relative_script_path="entry.mjs",
        status="running",
    )
    runtime.run_id = claim.run_id
    runtime.process_instance_id = claim.process_instance_id

    class DebugPluginRun:
        rejection: RuntimeError | None = RuntimeError("原 Debug run 当前不存在")

        async def stop(self) -> dict[str, object]:
            if self.rejection is not None:
                raise self.rejection
            return {"run_id": claim.run_id, "status": "exited", "exit_code": 0}

        async def close(self) -> None:
            return None

    plugin = DebugPluginRun()
    runtime.plugin_runtime = plugin  # type: ignore[assignment]
    owner = (fixture.session_id, fixture.thread_id)
    fixture.service._runtimes[owner] = runtime
    identity = NodeDebugProcessLeaseIdentity.for_process_instance(
        session_id=claim.session_id,
        thread_id=claim.thread_id,
        process_instance_id=claim.process_instance_id,
    )
    leases = fixture.service._external_resource_leases
    leases.register_external(
        resource_id=identity.resource_id,
        kind="node_debug_process",
        lifetime_scope="session",
    )
    leases.acquire(
        resource_id=identity.resource_id,
        turn_stream_id=identity.holder_id,
        lease_id=identity.lease_id,
        operation_id=identity.operation_id,
    )

    with pytest.raises(RuntimeError, match="原 Debug run 当前不存在"):
        await fixture.service._lifecycle.stop_runtime(runtime)
    persisted = fixture.store.read_launch_claim(*owner)
    lease = leases.get_lease(identity.lease_id)
    assert persisted is not None and persisted.phase == "stopping"
    assert lease is not None and lease.status == "active"
    assert runtime.status == "stopping"

    plugin.rejection = None
    assert await fixture.service._lifecycle.stop_runtime(runtime) == "stopped"
    settled = fixture.store.read_launch_claim(*owner)
    lease = leases.get_lease(identity.lease_id)
    assert settled is not None and settled.phase == "settled"
    assert lease is not None and lease.status == "settled"
    runtime.status = "exited"
    fixture.service._runtimes.pop(owner)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claim_phase,same_run,expected,final_claim_phase",
    [
        ("settled", True, "stopped", "settled"),
        ("running", True, "reconcile_required", "stopping"),
        ("settled", False, "reconcile_required", "settled"),
    ],
)
async def test_pluginless_stop_reuses_only_matching_settled_run_claim(
    debug_launch_context: DebugLaunchContext,
    claim_phase: Literal["settled", "running"],
    same_run: bool,
    expected: Literal["stopped", "reconcile_required"],
    final_claim_phase: Literal["settled", "stopping"],
) -> None:
    fixture = debug_launch_context
    binding = node_debug_binding_snapshot().model_copy(
        update={"workspace_id": fixture.workspace.workspace_id}
    )
    claim = claim_running(claim_with_target_pid(new_launch_claim(
        session_id=fixture.session_id,
        thread_id=fixture.thread_id,
        run_id="run_repeat_stop",
        configuration_id="dbgcfg_" + "1" * 32,
        debugger_binding=binding,
        script_path="entry.mjs",
        args=[],
        node_executable="node",
        working_directory="",
        launch_profile_name=None,
        breakpoints=[],
    ), pid=43210))
    if claim_phase == "settled":
        claim = claim_marked(claim, phase="settled")
    fixture.store.write_launch_claim(claim)
    runtime = NodeDebugRuntime(
        session_id=fixture.session_id,
        thread_id=fixture.thread_id,
        configuration_id=claim.configuration_id,
        debugger_binding=binding,
        workspace_root=fixture.workspace.workspace_root,
        script_path=fixture.workspace.workspace_root / "entry.mjs",
        relative_script_path="entry.mjs",
        status="stopping",
    )
    runtime.run_id = claim.run_id
    runtime.process_instance_id = (
        claim.process_instance_id if same_run else "node-debug-proc_other_run"
    )
    owner = (fixture.session_id, fixture.thread_id)
    fixture.service._runtimes[owner] = runtime
    try:
        assert await fixture.service._lifecycle.stop_runtime(runtime) == expected
        persisted = fixture.store.read_launch_claim(*owner)
        assert persisted is not None and persisted.phase == final_claim_phase
        assert persisted.run_id == claim.run_id
        if expected == "reconcile_required":
            assert runtime.status == "reconcile_required"
    finally:
        fixture.service._runtimes.pop(owner)
