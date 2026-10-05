from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from textwrap import dedent

import pytest

from app.agents.tools.terminal import create_exec_command_tool, create_write_stdin_tool
from tests.e2e.system.workspace_services.terminal.terminal_process_helpers import (
    start_terminal_backend,
)
from tests.support.ports import integration_port_block_for_file
from tests.support.processes import terminate_process
from tests.support.workspace_tool_runtime import (
    WorkspaceToolRuntime,
    workspace_tool_runtime,
)


@pytest.fixture
async def terminal_runtime(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[WorkspaceToolRuntime]:
    root = Path(integration_workspace_root_path)
    port = integration_port_block_for_file(Path(request.node.path)).port(40)
    monkeypatch.setenv("BOXTEAM_TERMINAL_BACKEND_URL", f"http://127.0.0.1:{port}")
    process = start_terminal_backend(backend_port=port, workspace_root=str(root))
    try:
        async with workspace_tool_runtime(root) as runtime:
            yield runtime
    finally:
        terminate_process(process)


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code", [0, 9])
async def test_real_tool_acceptance_owner_lease_pty_and_final_receipt(
    terminal_runtime: WorkspaceToolRuntime, exit_code: int
) -> None:
    container, session_id, context = terminal_runtime
    owner = container.terminal_resource_owner
    execute = create_exec_command_tool(
        session_id, terminal_owner=owner, invocation_context=context
    )
    write = create_write_stdin_tool(
        session_id, terminal_owner=owner, invocation_context=context
    )
    result = await execute.ainvoke(
        {
            "cmd": f"printf 'BOXTEAM_TERMINAL_OK\\n'; read value; printf 'BOXTEAM_STDIN:%s\\n' \"$value\"; exit {exit_code}",
            "yield_time_ms": 1000,
        }
    )
    assert "exit_code" not in result
    assert result["session_id"] == result["terminal_id"]
    assert (
        "BOXTEAM_TERMINAL_OK" in result["output"]
        and "__BOXTEAM_CMD_" not in result["output"]
    )
    resource = await owner.get_resource(result["terminal_id"])
    assert resource["source_thread"] == {
        "session_id": session_id,
        "thread_id": context.thread_binding.thread_id,
    }
    assert resource["thread_associations"] == [resource["source_thread"]]
    assert resource["retention_scope"] == "thread"
    assert resource["last_command_exit_code"] is None
    completed = await write.ainvoke(
        {
            "session_id": result["terminal_id"],
            "chars": "boxteam-input\r",
            "yield_time_ms": 1000,
        }
    )
    assert "BOXTEAM_STDIN:boxteam-input" in completed["output"]
    assert completed["exit_code"] == exit_code
    assert isinstance(completed["exit_code"], int)
    assert completed["chunk_id"] != result["chunk_id"]
    assert (await owner.get_resource(result["terminal_id"]))["status"] == "deleted"
    assert owner._leases.leases_for_resource(result["terminal_id"]) == []


@pytest.mark.asyncio
async def test_actual_python_crash_stopped_pty_settles_persisted_lease_and_allows_cleanup(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.container import WorkspaceContainerRegistry
    from app.core.mounted_workspaces import MountedWorkspace, MountedWorkspaceRegistry
    from app.core.path_utils import catalog_provider_scope
    from app.core.workspace_identity import load_or_create_workspace_id

    root = Path(integration_workspace_root_path)
    port = integration_port_block_for_file(Path(request.node.path)).port(40)
    monkeypatch.setenv("BOXTEAM_TERMINAL_BACKEND_URL", f"http://127.0.0.1:{port}")
    process = start_terminal_backend(backend_port=port, workspace_root=str(root))
    child = dedent(r"""
        import asyncio, json, os, sys
        from pathlib import Path
        from app.core.turn_execution_scope import get_current_turn_execution_scope
        from tests.support.workspace_tool_runtime import workspace_tool_runtime

        async def crash():
            async with workspace_tool_runtime(Path(sys.argv[1])) as (container, session_id, context):
                owner = container.terminal_resource_owner
                scope = get_current_turn_execution_scope()
                async with owner.operation(
                    session_id=session_id, thread_id=context.thread_binding.thread_id,
                    holder_id=scope.turn_stream_id, operation_id=context.require_tool_call_id(),
                ) as operation:
                    created = await operation.create_terminal(retention_scope="thread", title="真实崩溃租约")
                    rid = created["resource_id"]
                    stopped = await operation.kill_terminal(rid)
                    assert stopped["terminal"]["status"] == "terminated"
                    leases = owner._leases.leases_for_resource(rid)
                    assert len(leases) == 1 and leases[0].status == "active"
                    print(json.dumps({"rid":rid, "session_id":session_id, "lease_id":leases[0].lease_id}), flush=True)
                    # 真正退出 Python，跳过 operation/Turn/lifespan 的所有 finally。
                    os._exit(71)
        asyncio.run(crash())
    """)
    try:
        crashed = await asyncio.to_thread(
            subprocess.run, ["uv", "run", "python", "-c", child, str(root)],
            capture_output=True, text=True, timeout=45, check=False,
        )
        assert crashed.returncode == 71, crashed.stderr
        identity = json.loads(crashed.stdout.splitlines()[-1])
        persisted = json.loads((root / ".boxteam" / "resources.json").read_text())
        assert next(lease for lease in persisted["leases"] if lease["lease_id"] == identity["lease_id"])["status"] == "active"
        workspace_id = load_or_create_workspace_id(root)
        with (
            WorkspaceContainerRegistry(
                mounted_workspaces=MountedWorkspaceRegistry([MountedWorkspace(workspace_id, root)]),
                project_root=Path.cwd(),
            ) as registry,
            catalog_provider_scope(registry),
        ):
            container = registry.get(workspace_id)
            owner = container.terminal_resource_owner
            try:
                recovered = await owner.get_resource(identity["rid"])
                assert recovered["status"] == "terminated"
                assert recovered["reconciliation_status"] == "verified"
                assert owner._leases.get_lease(identity["lease_id"]).status == "settled"
                await owner.cleanup_session(identity["session_id"])
                assert (await owner.get_resource(identity["rid"]))["status"] == "deleted"
                assert owner._leases.leases_for_resource(identity["rid"]) == []
            finally:
                container.workspace_activity_service.store.close()
                container.session_catalog.close()
    finally:
        terminate_process(process)


@pytest.mark.asyncio
@pytest.mark.parametrize("delete_happened", [True, False])
async def test_real_tracker_same_generation_retry_requires_confirmed_pty_termination(
    terminal_runtime: WorkspaceToolRuntime, monkeypatch: pytest.MonkeyPatch,
    delete_happened: bool,
) -> None:
    from app.core.execution_admission_context import require_current_execution_admission
    from app.domain.itemized.mutation_intents import MutationIntentOwner
    from app.services.business.workspace_resources.owner import ResourceConflict

    container, session_id, context = terminal_runtime
    owner = container.terminal_resource_owner
    tracker = owner._thread_release_fence.__self__
    moment = [0.0]
    monkeypatch.setattr(tracker, "_clock", lambda: moment[0])
    thread_id = context.thread_binding.thread_id
    # graph 值与恢复无关；真实 committed admission、tracker generation 和 scope 保留。
    lease = await tracker.acquire(
        MutationIntentOwner(session_id, thread_id), require_current_execution_admission(),
        "recovery-transport-proof", lambda _session, _thread, _scope: object(),
    )
    generation = lease.generation
    lease.release()
    resource = await owner.create_terminal(
        session_id=session_id, thread_id=thread_id,
        holder_id="request:recovery-proof", operation_id="recovery-create",
    )
    rid = resource["resource_id"]
    original = owner._transport.delete_resource
    calls = []
    async def lost_response(resource_id: str) -> dict[str, object]:
        calls.append(resource_id)
        if delete_happened:
            await original(resource_id)
        raise ConnectionError("真实 Node DELETE 回执断开")
    monkeypatch.setattr(owner._transport, "delete_resource", lost_response)
    moment[0] = 1800.0
    with pytest.raises(ConnectionError):
        await tracker.sweep()
    state = tracker._states[(session_id, thread_id)]
    assert state.generation == generation and state.runtime.closing is False
    assert state.runtime.lifetime_scope.state == "open"
    discovered = await owner.list_resources()
    assert len(discovered) == 1 and discovered[0]["reconciliation_status"] == "unknown"
    if not delete_happened:
        with pytest.raises(ResourceConflict):
            await tracker.sweep()
        assert (await owner._transport.get_resource(rid))["status"] == "running"
        assert tracker._states[(session_id, thread_id)].generation == generation
        # 取得真实终态证据后再重试；不重放那次结果未知的 DELETE。
        await original(rid)
    completed = await tracker.sweep()
    assert len(completed) == 1
    assert calls == [rid]
    assert (session_id, thread_id) not in tracker._states
    assert await owner.list_resources() == []
