from __future__ import annotations

import asyncio
import json
import os
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from textwrap import dedent
from urllib.error import HTTPError
from uuid import uuid4

import pytest

from app.abstractions.terminal_resources import TerminalRequestError
from app.agents.tools.terminal import create_exec_command_tool, create_write_stdin_tool
from app.core.mounted_workspaces import MountedWorkspace
from app.core.resource_uri import resource_display_uri
from app.core.workspace_identity import load_or_create_workspace_id
from app.services.infrastructure.terminal_manager_client import TerminalManagerClient
from tests.harness.python.run_context import TestRunContext
from tests.harness.python.terminal_backend import start_terminal_backend
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.workspace import terminate_process
from tests.support.workspace_tool_runtime import (
    WorkspaceToolRuntime,
    workspace_tool_runtime,
)
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_default_test_workspace,
)


@pytest.fixture
def terminal_workspace_root_path(
    integration_workspace_root_path: str,
    integration_config_path: str,
) -> str:
    """每例启动完整 runtime，不能继承另一例手工准入留下的执行状态。"""
    project = Path.cwd()
    root = prepare_default_test_workspace(
        workspace_root=Path(integration_workspace_root_path) / uuid4().hex,
        template_root=project / "tests/fixtures/workspaces/default_test_workspace",
        shared_skill_root=project / "resources/skills",
    )
    install_test_workspace_config(
        workspace_root=root, config_path=Path(integration_config_path),
        schema_path=project / "configs/workspace_schema.jsonc",
    )
    return str(root)


@pytest.fixture
async def terminal_runtime(
    request: pytest.FixtureRequest,
    terminal_workspace_root_path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[WorkspaceToolRuntime]:
    root = Path(terminal_workspace_root_path)
    port = integration_port_block_for_file(Path(request.node.path)).port(40)
    process = start_terminal_backend(backend_port=port, workspace_root=str(root), artifacts_dir=TestRunContext.from_test_file(Path(__file__)).artifacts_dir)
    try:
        async with workspace_tool_runtime(root, startup_mount=MountedWorkspace(
            load_or_create_workspace_id(root), root, terminal_backend_url=f"http://127.0.0.1:{port}",
        )) as runtime:
            yield runtime
    finally:
        terminate_process(process)


@pytest.mark.asyncio
async def test_real_terminal_http_failures_keep_native_details_in_server_diagnostics(
    request: pytest.FixtureRequest,
    terminal_runtime: WorkspaceToolRuntime,
) -> None:
    container, _session_id, _context = terminal_runtime
    owner = container.terminal_resource_owner
    root = owner.workspace_root
    port = integration_port_block_for_file(Path(request.node.path)).port(40)
    client = TerminalManagerClient(
        backend_url=f"http://127.0.0.1:{port}", workspace_id=owner.workspace_id,
    )
    not_directory = root / "not-a-terminal-directory.txt"
    not_directory.write_text("原件不能被终端创建改写", encoding="utf-8")
    missing_command = root / "missing-native-shell"
    cases = [
        ("missing-relative-cwd", None, "终端工作目录不存在或不是目录"),
        (str(root / "missing-absolute-cwd"), None, "终端工作目录不存在或不是目录"),
        (str(not_directory), None, "终端工作目录不存在或不是目录"),
        ("bad\0directory", None, "终端工作目录检查失败"),
        (str(root), str(missing_command), "终端 PTY 启动失败"),
    ]
    for mode in ("sync", "async"):
        for cwd, command, reason in cases:
            terminal_id = f"term_{uuid4().hex}"
            payload = {
                "workspace_id": owner.workspace_id,
                "terminal_id": terminal_id,
                "owner_revision": 1,
                "cwd": cwd,
            }
            if command is not None:
                payload["command"] = command
                payload["args"] = []
            with pytest.raises(TerminalRequestError) as rejected:
                if mode == "sync":
                    await asyncio.to_thread(
                        client._json_request_sync, "POST", "/api/terminals", payload,
                    )
                else:
                    await client._json_request("POST", "/api/terminals", payload)
            error = rejected.value
            assert isinstance(error.__cause__, HTTPError)
            assert error.__cause__.code == 500
            assert (error.method, error.path, error.status) == ("POST", "/api/terminals", 500)
            detail = json.loads(error.response_body)
            expected = f"{reason}: terminal_id={terminal_id}" if command else reason
            assert detail == {"error": expected}
            assert str(root) not in str(error)
            assert str(missing_command) not in str(error)
            assert await client.list_resources() == []
    assert not_directory.read_text(encoding="utf-8") == "原件不能被终端创建改写"
    state = json.loads((root / ".boxteam/terminal-manager/terminals.json").read_text())
    assert state["terminals"] == []
    assert await owner.list_resources() == []
    logs = TestRunContext.from_test_file(Path(__file__)).artifacts_dir
    diagnostic = "\n".join(path.read_text() for path in logs.glob("terminal-backend-*.stderr.log"))
    assert str(not_directory) in diagnostic
    assert str(missing_command) in diagnostic
    assert "ENOENT" in diagnostic


@pytest.mark.asyncio
async def test_real_terminal_tool_preserves_native_external_workdir(
    terminal_runtime: WorkspaceToolRuntime,
) -> None:
    container, session_id, context = terminal_runtime
    owner = container.terminal_resource_owner
    # 兄弟目录仍在本文件的正式隔离工作区内，不依赖宿主机的固定目录。
    root = owner.workspace_root
    external = root.with_name(f"external-{uuid4().hex}")
    external.mkdir()
    probe = external / "terminal-cwd-probe.txt"
    probe.write_text("BOXTEAM_NATIVE_EXTERNAL_CWD", encoding="utf-8")
    execute = create_exec_command_tool(
        session_id, terminal_owner=owner, invocation_context=context,
    )
    try:
        result = await execute.ainvoke({
            "cmd": "cat terminal-cwd-probe.txt", "workdir": str(external),
            "yield_time_ms": 1000,
        })
        assert "BOXTEAM_NATIVE_EXTERNAL_CWD" in result["output"]
        assert result["exit_code"] == 0
        assert str(root) not in json.dumps(result)
        assert str(external) not in json.dumps(result)
        assert probe.read_text(encoding="utf-8") == "BOXTEAM_NATIVE_EXTERNAL_CWD"
    finally:
        probe.unlink()
        external.rmdir()


@pytest.mark.asyncio
async def test_real_terminal_tool_uses_one_cwd_for_native_paths_and_directory_uri(
    terminal_runtime: WorkspaceToolRuntime, caplog: pytest.LogCaptureFixture,
) -> None:
    container, session_id, context = terminal_runtime
    owner = container.terminal_resource_owner
    directory = owner.workspace_root / "附件处理 with spaces"
    directory.mkdir()
    original = directory / "probe.txt"
    original.write_text("BOXTEAM_SHARED_DIRECTORY_INPUT", encoding="utf-8")
    uri = resource_display_uri(
        scope="workspace", scope_id=owner.workspace_id, kind="files",
        tail_segments=("root", directory.name),
    )
    execute = create_exec_command_tool(
        session_id, terminal_owner=owner, invocation_context=context,
    )
    receipts: list[dict[str, object]] = []
    for workdir in (directory.name, str(directory), uri):
        result = await execute.ainvoke({
            "cmd": "cat probe.txt; printf boxteam://literal-command-text; printf 'BOXTEAM_DERIVED_OUTPUT' > output.txt",
            "workdir": workdir, "yield_time_ms": 1000,
        })
        assert result["exit_code"] == 0
        assert "BOXTEAM_SHARED_DIRECTORY_INPUT" in result["output"]
        assert "boxteam://literal-command-text" in result["output"]
        assert result["cwd_relative"] == directory.name
        assert str(owner.workspace_root) not in json.dumps(result)
        assert (directory / "output.txt").read_text() == "BOXTEAM_DERIVED_OUTPUT"
        (directory / "output.txt").unlink()
        record = await owner.get_resource(result["terminal_id"])
        assert record["status"] == "deleted"
        assert record["reconciliation_status"] == "verified"
        assert owner._leases.leases_for_resource(result["terminal_id"]) == []
        receipts.append(result)
    assert len({item["terminal_id"] for item in receipts}) == 3
    assert original.read_text(encoding="utf-8") == "BOXTEAM_SHARED_DIRECTORY_INPUT"
    if os.name != "nt":
        native_directory = owner.workspace_root / "build:" / "debug"
        native_directory.mkdir(parents=True)
        (native_directory / "probe.txt").write_text("BOXTEAM_NATIVE_COLON_DIRECTORY", encoding="utf-8")
        for workdir in ("build://debug", str(owner.workspace_root) + "/build://debug"):
            result = await execute.ainvoke({"cmd": "cat probe.txt", "workdir": workdir, "yield_time_ms": 1000})
            assert result["exit_code"] == 0
            assert "BOXTEAM_NATIVE_COLON_DIRECTORY" in result["output"]
            assert result["cwd_relative"] == "build:/debug"
            assert str(owner.workspace_root) not in json.dumps(result)
            assert (await owner.get_resource(result["terminal_id"]))["status"] == "deleted"
            assert owner._leases.leases_for_resource(result["terminal_id"]) == []
            receipts.append(result)
    state_files = [
        owner._store.path, owner.workspace_root / ".boxteam/resources.json",
        owner.workspace_root / ".boxteam/terminal-manager/terminals.json",
    ]
    before = {path: path.read_bytes() for path in state_files}
    rejected_uris = [
        resource_display_uri(
            scope="workspace", scope_id=str(uuid4()), kind="files", tail_segments=("root",),
        ),
        resource_display_uri(
            scope="workspace", scope_id=owner.workspace_id, kind="files", tail_segments=("root",),
            gateway_authority="gateway_abcdef0123456789abcdef0123456789",
        ),
        uri + "/%2Fetc",
    ]
    for rejected_uri in rejected_uris:
        with pytest.raises(ValueError):
            await execute.ainvoke({"cmd": "printf SHOULD_NOT_RUN > unexpected.txt", "workdir": rejected_uri})
        assert {path: path.read_bytes() for path in state_files} == before
    if os.name != "nt":
        # 真实符号链接环经 Path.resolve 抛出含本机路径的 RuntimeError。
        loop = directory / "loop"
        loop.symlink_to("loop")
        try:
            with pytest.raises(RuntimeError) as rejected:
                await execute.ainvoke({"cmd": "printf SHOULD_NOT_RUN", "workdir": uri + "/loop"})
            assert "Terminal 目录 URI 解析失败" in str(rejected.value)
            assert str(owner.workspace_root) not in str(rejected.value)
            assert isinstance(rejected.value.__cause__, RuntimeError)
            assert str(loop) in str(rejected.value.__cause__)
            diagnostic = next(record for record in caplog.records if record.msg == "Terminal 目录 URI 解析失败: uri=%s")
            assert diagnostic.exc_info is not None
            assert diagnostic.exc_info[1] is rejected.value.__cause__
            assert {path: path.read_bytes() for path in state_files} == before
        finally:
            loop.unlink()
    assert set(owner._store.records) == {item["terminal_id"] for item in receipts}
    assert not (directory / "unexpected.txt").exists()
    artifacts = TestRunContext.from_test_file(Path(__file__)).artifacts_dir
    (artifacts / f"directory-uri-cwd-{uuid4().hex}.json").write_text(
        json.dumps({"uri": uri, "receipts": receipts, "rejected_uris": rejected_uris}, ensure_ascii=False, indent=2), encoding="utf-8",
    )


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
    terminal_workspace_root_path: str,
) -> None:
    from app.container import WorkspaceContainerRegistry
    from app.core.mounted_workspaces import MountedWorkspace, MountedWorkspaceRegistry
    from app.core.path_utils import catalog_provider_scope
    from app.core.workspace_identity import load_or_create_workspace_id

    root = Path(terminal_workspace_root_path)
    port = integration_port_block_for_file(Path(request.node.path)).port(40)
    process = start_terminal_backend(backend_port=port, workspace_root=str(root), artifacts_dir=TestRunContext.from_test_file(Path(__file__)).artifacts_dir)
    child = dedent(r"""
        import asyncio, json, os, sys
        from pathlib import Path
        from app.core.turn_execution_scope import get_current_turn_execution_scope
        from tests.support.workspace_tool_runtime import workspace_tool_runtime
        from app.core.mounted_workspaces import MountedWorkspace
        from app.core.workspace_identity import load_or_create_workspace_id

        async def crash():
            root = Path(sys.argv[1])
            async with workspace_tool_runtime(root, startup_mount=MountedWorkspace(
                load_or_create_workspace_id(root), root, terminal_backend_url=sys.argv[2],
            )) as (container, session_id, context):
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
            subprocess.run, ["uv", "run", "python", "-c", child, str(root), f"http://127.0.0.1:{port}"],
            capture_output=True, text=True, timeout=45, check=False,
        )
        assert crashed.returncode == 71, crashed.stderr
        identity = json.loads(crashed.stdout.splitlines()[-1])
        persisted = json.loads((root / ".boxteam" / "resources.json").read_text())
        assert next(lease for lease in persisted["leases"] if lease["lease_id"] == identity["lease_id"])["status"] == "active"
        workspace_id = load_or_create_workspace_id(root)
        with (
            WorkspaceContainerRegistry(
                mounted_workspaces=MountedWorkspaceRegistry([MountedWorkspace(workspace_id, root, terminal_backend_url=f"http://127.0.0.1:{port}")]),
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
