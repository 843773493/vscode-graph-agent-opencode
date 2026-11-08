"""真实上传、Shell 工具和 PTY 子进程验证目录占用跨工具返回后继续保活。"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import shlex
import subprocess
import sys
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from textwrap import dedent

import psutil
import pytest

from app.abstractions.terminal.resources import TerminalRequestError
from app.agents.tools.terminal import create_exec_command_tool
from app.core.resource_uri import file_uri_child, parse_resource_uri
from app.core.trace_middleware import reset_current_gateway_id, set_current_gateway_id
from app.schemas.internal_v2.message import MessageCreateRequest
from app.services.business.workspace_resources.records import ThreadRef
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.terminal_manager_client import TerminalManagerClient
from configs.runtime import read_jsonc_object
from tests.harness.python.run_context import TestRunContext
from tests.harness.python.terminal_plugin import terminal_plugin_gateway_for_workspace
from tests.integration.backend.sessions.test_attachment_directory_entry import upload
from tests.integration.test_terminal_owner_tool_invocation import (
    terminal_runtime as terminal_runtime,  # noqa: PLC0414
)
from tests.integration.test_terminal_owner_tool_invocation import (
    terminal_workspace_root_path as terminal_workspace_root_path,  # noqa: PLC0414
)
from tests.support.ports import integration_port_block_for_file
from tests.support.workspace_tool_runtime import (
    WorkspaceToolRuntime,
    workspace_tool_runtime,
)


@pytest.fixture(autouse=True)
def shell_model_configuration(terminal_workspace_root_path: str) -> None:
    """Shell 验收不调用模型，配置不得依赖个人凭据或外部 Provider。"""
    config_path = Path(terminal_workspace_root_path) / ".boxteam/workspace.jsonc"
    config = read_jsonc_object(config_path)
    for provider in config["llm"]["providers"]:
        provider["api_key"] = "shell-contract-unused-key"
        provider["endpoint"] = "http://127.0.0.1:1/v1"
        provider.pop("auth", None)
    config_path.write_text(json.dumps(config, ensure_ascii=False) + "\n", encoding="utf-8")


@pytest.fixture(params=[None, "gateway_6c8b2f9317a5a2c6c4569958b1e23d84"], ids=["local", "gateway"])
def shell_gateway(request: pytest.FixtureRequest) -> Iterator[str | None]:
    token = set_current_gateway_id(request.param)
    try:
        yield request.param
    finally:
        reset_current_gateway_id(token)


def process_running(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() not in {psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD}
    except psutil.NoSuchProcess:
        return False


@pytest.mark.parametrize("terminal_runtime", ["missing"], indirect=True)
@pytest.mark.parametrize("ledger_failure", [False, True], ids=["release-success", "release-ledger-retry"])
async def test_verified_startup_failure_releases_the_real_attachment_workdir(
    terminal_runtime: WorkspaceToolRuntime, monkeypatch: pytest.MonkeyPatch, ledger_failure: bool,
) -> None:
    container, session_id, context = terminal_runtime
    store, owner, leases = container.attachment_blob_store, container.terminal_resource_owner, container.external_resource_leases
    message = await container.message_service.create(
        session_id, MessageCreateRequest(content="启动失败目录占用", attachments=[upload(b"original-for-startup-failure")]),
    )
    ref, = message.attachments
    directory = store.resolve_directory_uri(parse_resource_uri(ref.directory_uri))
    with pytest.raises(TerminalRequestError) as rejected:
        await owner.create_terminal(
            session_id=session_id, thread_id=context.thread_binding.thread_id,
            holder_id="request:attachment-startup", operation_id="attachment-startup",
            cwd=file_uri_child(ref.directory_uri, "derived"),
        )
    receipt = rejected.value.creation_receipt
    assert receipt is not None and receipt["outcome"] == "failed"
    terminal_id = receipt["resource_id"]
    snapshot = await owner.get_resource(terminal_id)
    assert snapshot["status"] == "terminated" and snapshot["reconciliation_status"] == "verified"
    attachment_id = parse_resource_uri(ref.directory_uri).tail_segments[0]
    assert owner._store.get(terminal_id).working_directory_uri == ref.directory_uri
    assert leases.leases_for_resource(attachment_id) == []
    assert leases.leases_for_resource(terminal_id) == []
    assert (directory / "original/content").read_bytes() == b"original-for-startup-failure"
    # 暂停独立轮询，让账本故障只由本次显式处理入口触发。
    await container.terminal_steering_service.shutdown()
    store.release_thread_references(session_id, message.thread_id)
    if ledger_failure:
        ledger_path = leases._state_path
        assert ledger_path is not None
        temporary = ledger_path.with_suffix(".tmp")
        temporary.mkdir()
        original = store._catalog.require_available_directory(attachment_id)
        try:
            failure_type = PermissionError if os.name == "nt" else IsADirectoryError
            with pytest.raises(failure_type, match="artifact=external_resource_leases: stage=write") as failure:
                await store.process_pending_directories()
            assert failure.value.errno == (errno.EACCES if os.name == "nt" else errno.EISDIR)
            cause = failure.value.__cause__
            assert isinstance(cause, OSError) and cause.filename == str(temporary)
            pending = store._catalog.get_directory(attachment_id)
            assert pending is not None and pending.availability == "releasing"
            assert pending.relative_locator == original.relative_locator
            assert not directory.exists()
            cold = ExternalResourceLeaseLedger(state_path=ledger_path)
            registered = cold.get(attachment_id)
            assert registered is not None and registered.status == "running"
            assert cold.leases_for_resource(attachment_id) == []
        finally:
            temporary.rmdir()
    assert await store.process_pending_directories() == (attachment_id,)
    assert not directory.exists()
    registered = leases.get(attachment_id)
    assert registered is not None and registered.status == "stopped"
    final = store._catalog.get_directory(attachment_id)
    assert final is not None and final.availability == "released"


@pytest.mark.parametrize("input_form", ["uri", "absolute", "relative"])
async def test_shell_process_holds_directory_after_tool_returns_until_verified_stop(
    terminal_runtime: WorkspaceToolRuntime, monkeypatch: pytest.MonkeyPatch, input_form: str,
    shell_gateway: str | None,
) -> None:
    container, session_id, context = terminal_runtime
    store, owner, leases = container.attachment_blob_store, container.terminal_resource_owner, container.external_resource_leases
    message = await container.message_service.create(session_id, MessageCreateRequest(content="Shell 处理原件", attachments=[upload(b"original-for-shell")]))
    ref, = message.attachments
    local_directory_uri = parse_resource_uri(ref.directory_uri, local_gateway_id=shell_gateway).display_uri
    directory = store.resolve_directory_uri(parse_resource_uri(local_directory_uri))
    workdir = directory / "derived"
    requested = {"uri": file_uri_child(ref.directory_uri, "derived"), "absolute": str(workdir),
                 "relative": workdir.relative_to(container.workspace_root).as_posix()}[input_form]
    execute = create_exec_command_tool(session_id, terminal_owner=owner, invocation_context=context)
    # 同一目标端程序经真实原生 Shell 启动，保留各平台路径和进程树语义。
    probe = workdir / "attachment_shell_probe.py"
    probe.write_text(dedent("""\
        import subprocess
        import sys
        from pathlib import Path

        Path("output.txt").write_text("derived", encoding="utf-8")
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        Path("child.pid").write_text(str(child.pid), encoding="ascii")
        print(Path("../original/content").read_text(encoding="utf-8"), flush=True)
        child.wait()
    """), encoding="utf-8")
    command = [sys.executable, "-u", probe.name]
    result = await execute.ainvoke({
        "cmd": subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command),
        "workdir": requested, "yield_time_ms": 1000,
    })
    terminal_id = result["terminal_id"]
    try:
        assert result["session_id"] == terminal_id and "exit_code" not in result
        assert "original-for-shell" in result["output"]
        assert (workdir / "output.txt").read_text() == "derived"
        child_pid = int((workdir / "child.pid").read_text())
        assert process_running(child_pid)
        attachment_id = parse_resource_uri(ref.directory_uri).tail_segments[0]
        active, = leases.leases_for_resource(attachment_id)
        assert active.turn_stream_id == active.operation_id == terminal_id
        assert (active.holder_session_id, active.holder_thread_id) == (session_id, message.thread_id)
        assert not leases.leases_for_resource(terminal_id)
        assert owner._store.get(terminal_id).working_directory_uri == local_directory_uri
        # 直接调用真实 detach owner 边界，不伪造进程或 lease。
        store.release_thread_references(session_id, message.thread_id)
        assert await store.process_pending_directories() == ()
        assert directory.is_dir() and process_running(child_pid)
        snapshot = await owner.get_resource(terminal_id)
        stopped = await owner.command(terminal_id, action="stop", expected_revision=snapshot["owner_revision"],
            holder_id="request:attachment-shell-stop", operation_id="attachment-shell-stop")
        assert stopped["status"] == "terminated" and stopped["reconciliation_status"] == "verified"
        assert not process_running(child_pid)
        assert not leases.leases_for_resource(attachment_id)
        assert leases.get_lease(active.lease_id).status == "settled"
        assert await store.process_pending_directories() == ()
        assert (workdir / "output.txt").read_text() == "derived"
        assert int((workdir / "child.pid").read_text()) == child_pid
        assert probe.is_file() and directory.is_dir()
    finally:
        snapshot = await owner.get_resource(terminal_id)
        if snapshot["status"] not in {"terminated", "completed", "exited", "deleted"}:
            await owner.command(terminal_id, action="stop", expected_revision=snapshot["owner_revision"],
                holder_id="request:attachment-shell-cleanup", operation_id="attachment-shell-cleanup")


async def test_shell_directory_admission_and_process_lease_exclude_working_copy_release(
    terminal_runtime: WorkspaceToolRuntime, monkeypatch: pytest.MonkeyPatch,
) -> None:
    container, session_id, _context = terminal_runtime
    store, owner, leases = container.attachment_blob_store, container.terminal_resource_owner, container.external_resource_leases
    message = await container.message_service.create(session_id, MessageCreateRequest(
        content="Shell 准入与回收互斥", attachments=[upload(b"keep-during-admission")],
    ))
    ref, = message.attachments
    attachment_id = parse_resource_uri(ref.directory_uri).tail_segments[0]
    directory = store.resolve_directory_uri(parse_resource_uri(ref.directory_uri))
    store.release_thread_references(session_id, message.thread_id)
    admitted, resume = asyncio.Event(), asyncio.Event()
    original = store.admit_working_directory

    @asynccontextmanager
    async def hold_admission(target: Path):
        async with original(target) as uri:
            admitted.set()
            await resume.wait()
            yield uri

    monkeypatch.setattr(store, "admit_working_directory", hold_admission)
    creation = asyncio.create_task(owner.create_terminal(
        session_id=session_id, thread_id=message.thread_id, retention_scope="workspace",
        cwd=file_uri_child(ref.directory_uri, "derived"), holder_id="request:directory-admission",
        operation_id="directory-admission",
    ))
    try:
        await asyncio.wait_for(admitted.wait(), 5)
        # 真实目录 gate 仍被准入持有；回收不能等待它，也不能在 lease 发布前删除。
        assert await asyncio.wait_for(store.process_pending_directories(), 5) == ()
        assert not creation.done() and directory.is_dir()
    finally:
        resume.set()
        resource = await creation
    terminal_id = resource["resource_id"]
    try:
        active, = leases.leases_for_resource(attachment_id)
        assert active.turn_stream_id == terminal_id
        assert await store.process_pending_directories() == ()
        assert directory.is_dir()
    finally:
        stopped = await owner.command(terminal_id, action="stop", expected_revision=resource["owner_revision"],
            holder_id="request:directory-admission-stop", operation_id="directory-admission-stop")
        assert stopped["status"] == "terminated"
    assert await store.process_pending_directories() == ()
    assert not directory.exists()


async def test_pending_stop_receipt_keeps_its_directory_but_does_not_block_another_pty(
    terminal_runtime: WorkspaceToolRuntime, monkeypatch: pytest.MonkeyPatch,
) -> None:
    container, session_id, context = terminal_runtime
    store, owner, leases = container.attachment_blob_store, container.terminal_resource_owner, container.external_resource_leases
    execute = create_exec_command_tool(session_id, terminal_owner=owner, invocation_context=context)
    terminals: list[str] = []
    directories: list[Path] = []
    attachments: list[str] = []
    pids: list[int] = []
    entered, release = asyncio.Event(), asyncio.Event()
    stopping = None
    try:
        for index in range(2):
            message = await container.message_service.create(session_id, MessageCreateRequest(
                content=f"独立处理目录 {index}", attachments=[upload(f"original-{index}".encode())],
            ))
            ref, = message.attachments
            directory = store.resolve_directory_uri(parse_resource_uri(ref.directory_uri))
            directories.append(directory)
            attachments.append(parse_resource_uri(ref.directory_uri).tail_segments[0])
            probe = directory / "derived/independent.py"
            probe.write_text(dedent("""\
                import os
                import time
                from pathlib import Path

                Path("shell.pid").write_text(str(os.getpid()), encoding="ascii")
                Path("output.txt").write_text("independent", encoding="utf-8")
                print("started", flush=True)
                time.sleep(600)
            """), encoding="utf-8")
            command = [sys.executable, "-u", probe.name]
            result = await execute.ainvoke({
                "cmd": subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command),
                "workdir": file_uri_child(ref.directory_uri, "derived"), "yield_time_ms": 1000,
            })
            terminals.append(result["terminal_id"])
            assert "started" in result["output"] and "exit_code" not in result
            pids.append(int((directory / "derived/shell.pid").read_text()))
            assert process_running(pids[-1])

        original_stop = owner._transport.stop_resource
        calls: list[str] = []

        async def hold_receipt(terminal_id: str, *, reason: str | None = None):
            calls.append(terminal_id)
            receipt = await original_stop(terminal_id, reason=reason)
            if terminal_id == terminals[0]:
                # HTTP/PTY 已真正停止，故障点只挂起返回 owner 的最终回执。
                entered.set()
                await release.wait()
            return receipt

        monkeypatch.setattr(owner._transport, "stop_resource", hold_receipt)
        first = await owner.get_resource(terminals[0])
        stopping = asyncio.create_task(owner.command(
            terminals[0], action="stop", expected_revision=first["owner_revision"],
            holder_id="request:pending-receipt", operation_id="pending-receipt",
        ))
        await asyncio.wait_for(entered.wait(), 5)
        assert not process_running(pids[0]) and process_running(pids[1])
        store.release_thread_references(session_id, message.thread_id)
        assert await store.process_pending_directories() == ()
        second = await asyncio.wait_for(owner.get_resource(terminals[1]), 5)
        stopped = await asyncio.wait_for(owner.command(
            terminals[1], action="stop", expected_revision=second["owner_revision"],
            holder_id="request:independent-stop", operation_id="independent-stop",
        ), 5)
        assert stopped["status"] == "terminated" and stopped["reconciliation_status"] == "verified"
        assert not process_running(pids[1])
        assert (directories[1] / "derived/output.txt").read_text() == "independent"
        assert not stopping.done() and leases.leases_for_resource(attachments[0])
        assert not leases.leases_for_resource(attachments[1])
        assert await store.process_pending_directories() == ()
        assert all(directory.is_dir() for directory in directories)
        assert calls == terminals
        release.set()
        first_stopped = await stopping
        assert first_stopped["status"] == "terminated" and first_stopped["reconciliation_status"] == "verified"
        assert not leases.leases_for_resource(attachments[0])
        assert await store.process_pending_directories() == ()
        assert (directories[0] / "derived/output.txt").read_text() == "independent"
        assert all(directory.is_dir() for directory in directories) and calls == terminals
    finally:
        release.set()
        if stopping is not None:
            await stopping
        for terminal_id in terminals:
            resource = await owner.get_resource(terminal_id)
            if resource["status"] not in {"terminated", "completed", "exited", "deleted"}:
                await owner.command(terminal_id, action="stop", expected_revision=resource["owner_revision"],
                    holder_id="request:independent-cleanup", operation_id="independent-cleanup")


async def test_failed_directory_lease_settlement_blocks_release_until_original_stop_is_reconciled(
    terminal_runtime: WorkspaceToolRuntime, monkeypatch: pytest.MonkeyPatch,
) -> None:
    container, session_id, context = terminal_runtime
    store, owner, leases = container.attachment_blob_store, container.terminal_resource_owner, container.external_resource_leases
    message = await container.message_service.create(session_id, MessageCreateRequest(
        content="停止回执持久失败", attachments=[upload(b"keep-until-durable-settlement")],
    ))
    ref, = message.attachments
    attachment_id = parse_resource_uri(ref.directory_uri).tail_segments[0]
    directory = store.resolve_directory_uri(parse_resource_uri(ref.directory_uri))
    probe = directory / "derived/settlement.py"
    probe.write_text("import os, time\nfrom pathlib import Path\nPath('shell.pid').write_text(str(os.getpid()))\nprint('started', flush=True)\ntime.sleep(600)\n")
    command = [sys.executable, "-u", probe.name]
    execute = create_exec_command_tool(session_id, terminal_owner=owner, invocation_context=context)
    result = await execute.ainvoke({
        "cmd": subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command),
        "workdir": file_uri_child(ref.directory_uri, "derived"), "yield_time_ms": 1000,
    })
    terminal_id = result["terminal_id"]
    try:
        assert "started" in result["output"] and "exit_code" not in result
        pid = int((directory / "derived/shell.pid").read_text())
        assert process_running(pid)
        original, = leases.leases_for_resource(attachment_id)
        store.release_thread_references(session_id, message.thread_id)
        path = container.workspace_root / ".boxteam/resources.json"
        failure = OSError(5, "目录占用结清写盘失败")
        replace, stop = Path.replace, owner._transport.stop_resource
        calls: list[str] = []

        async def record_stop(resource_id: str, *, reason: str | None = None):
            calls.append(resource_id)
            return await stop(resource_id, reason=reason)

        monkeypatch.setattr(owner._transport, "stop_resource", record_stop)

        def fail_settlement(source: Path, target: Path) -> Path:
            if target == path and any(lease["lease_id"] == original.lease_id and lease["status"] == "settled"
                                      for lease in json.loads(source.read_text())["leases"]):
                raise failure
            return replace(source, target)

        snapshot = await owner.get_resource(terminal_id)
        with monkeypatch.context() as io_fault:
            io_fault.setattr(Path, "replace", fail_settlement)
            with pytest.raises(OSError) as rejected:
                await owner.command(terminal_id, action="stop", expected_revision=snapshot["owner_revision"],
                                    holder_id="request:durable-stop", operation_id="durable-stop")
            assert type(rejected.value) is type(failure) and rejected.value.errno == failure.errno
            assert rejected.value.__cause__ is failure
            assert "stage=write" in str(rejected.value) and str(path) not in str(rejected.value)
        assert calls == [terminal_id] and not process_running(pid)
        assert leases.get_lease(original.lease_id) == original
        assert ExternalResourceLeaseLedger(state_path=path).get_lease(original.lease_id) == original
        assert await store.process_pending_directories() == ()
        assert (directory / "original/content").read_bytes() == b"keep-until-durable-settlement"
        # 普通查询不自动重放未知停止；用户以新管理操作核实同一原资源。
        unknown = await owner.get_resource(terminal_id)
        assert unknown["reconciliation_status"] == "unknown" and calls == [terminal_id]
        reconciled = await owner.command(
            terminal_id, action="stop", expected_revision=unknown["owner_revision"],
            holder_id="request:verify-durable-stop", operation_id="verify-durable-stop",
        )
        assert reconciled["status"] == "terminated" and reconciled["reconciliation_status"] == "verified"
        assert calls == [terminal_id] and not process_running(pid)
        assert leases.get_lease(original.lease_id).status == "settled"
        assert ExternalResourceLeaseLedger(state_path=path).get_lease(original.lease_id).status == "settled"
        assert await store.process_pending_directories() == ()
        assert probe.is_file() and int((directory / "derived/shell.pid").read_text()) == pid
    finally:
        snapshot = await owner.get_resource(terminal_id)
        if snapshot["status"] not in {"terminated", "completed", "exited", "deleted"}:
            await owner.command(terminal_id, action="stop", expected_revision=snapshot["owner_revision"],
                                holder_id="request:settlement-cleanup", operation_id="settlement-cleanup")


async def test_pending_cold_revision_keeps_original_pty_and_allows_other_pty_and_results(
    terminal_workspace_root_path: str, request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(terminal_workspace_root_path)
    port = integration_port_block_for_file(Path(request.node.path)).port(40)
    run = TestRunContext.from_test_file(Path(request.node.path))
    home = run.boxteam_home_for_node(request.node.nodeid)
    monkeypatch.setenv("BOXTEAM_HOME", str(home))
    terminals: list[str] = []
    directories: list[Path] = []
    attachments: list[str] = []
    pids: list[int] = []
    async with terminal_plugin_gateway_for_workspace(
        workspace_root=root, artifacts_dir=run.artifacts_dir, gateway_port=port, boxteam_home=home,
    ) as plugin:
        monkeypatch.setenv("BOXTEAM_GATEWAY_URL", plugin.gateway_url)
        # 第一代由真实上传/Shell创建目录和进程，提升后由原Session清理解除关联。
        async with workspace_tool_runtime(root) as (container, session_id, context):
            owner, store = container.terminal_resource_owner, container.attachment_blob_store
            execute = create_exec_command_tool(session_id, terminal_owner=owner, invocation_context=context)
            for index in range(2):
                message = await container.message_service.create(session_id, MessageCreateRequest(
                    content=f"冷恢复目录 {index}", attachments=[upload(f"cold-original-{index}".encode())],
                ))
                ref, = message.attachments
                directory = store.resolve_directory_uri(parse_resource_uri(ref.directory_uri))
                directories.append(directory)
                attachments.append(parse_resource_uri(ref.directory_uri).tail_segments[0])
                probe = directory / "derived/cold.py"
                probe.write_text("import os, time\nfrom pathlib import Path\nPath('shell.pid').write_text(str(os.getpid()))\nprint('started', flush=True)\ntime.sleep(600)\n")
                command = [sys.executable, "-u", probe.name]
                result = await execute.ainvoke({
                    "cmd": subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command),
                    "workdir": file_uri_child(ref.directory_uri, "derived"), "yield_time_ms": 1000,
                })
                assert "started" in result["output"] and "exit_code" not in result
                terminals.append(result["terminal_id"])
                pids.append(int((directory / "derived/shell.pid").read_text()))
                current = await owner.get_resource(terminals[-1])
                await owner.command(
                    terminals[-1], action="promote", expected_revision=current["owner_revision"],
                    holder_id=f"request:cold-promote-{index}", operation_id=f"cold-promote-{index}",
                )
            store.release_thread_references(session_id, message.thread_id)
        assert all(process_running(pid) for pid in pids)

        # 第二代启动的真实steering会主动恢复；故障必须在装配前接到原HTTP端口。
        entered, release = asyncio.Event(), asyncio.Event()
        original = TerminalManagerClient.sync_owner_revision
        calls: list[tuple[str, int]] = []

        async def pending_revision(transport: TerminalManagerClient, resource_id: str, revision: int, *, for_stop: bool) -> None:
            calls.append((resource_id, revision))
            if resource_id == terminals[0]:
                entered.set()
                await release.wait()
            await original(transport, resource_id, revision, for_stop=for_stop)

        monkeypatch.setattr(TerminalManagerClient, "sync_owner_revision", pending_revision)
        # 服务图重新从原catalog/owner/lease文件恢复，不复制内存状态。
        async with workspace_tool_runtime(root) as (container, session_id, context):
            owner, store, leases = container.terminal_resource_owner, container.attachment_blob_store, container.external_resource_leases
            recovering = asyncio.create_task(owner.recover())
            try:
                await asyncio.wait_for(entered.wait(), 5)
                healthy = await asyncio.wait_for(owner.get_resource(terminals[1]), 5)
                frozen_revision = owner._store.get(terminals[0]).owner_revision
                assert healthy["reconciliation_status"] == "verified"
                thread = ThreadRef(session_id, container.session_catalog.resolver.main_thread_id(session_id))
                await asyncio.wait_for(owner.command(
                    terminals[1], action="attach", expected_revision=healthy["owner_revision"], thread=thread,
                    holder_id="request:cold-attach", operation_id="cold-attach",
                ), 5)
                async with owner.operation(
                    session_id=session_id, thread_id=thread.thread_id,
                    holder_id="request:cold-read", operation_id="cold-read",
                ) as operation:
                    read = await asyncio.wait_for(operation.read_terminal(terminals[1]), 5)
                # 模型增量游标由第一次Shell读取消费；完整运行快照仍保留原输出。
                assert read["output"] == "" and "started" in read["terminal"]["buffer"]
                assert read["terminal"]["resource_id"] == terminals[1]
                assert await store.process_pending_directories() == ()
                healthy = await owner.get_resource(terminals[1])
                stopped = await asyncio.wait_for(owner.command(
                    terminals[1], action="stop", expected_revision=healthy["owner_revision"],
                    holder_id="request:cold-stop", operation_id="cold-stop",
                ), 5)
                assert stopped["reconciliation_status"] == "verified" and not process_running(pids[1])
                assert process_running(pids[0]) and not recovering.done()
                assert leases.leases_for_resource(attachments[0]) and not leases.leases_for_resource(attachments[1])
                assert await store.process_pending_directories() == ()
                assert (directories[0] / "original/content").read_bytes() == b"cold-original-0"
                assert int((directories[1] / "derived/shell.pid").read_text()) == pids[1]
                assert calls.count((terminals[0], frozen_revision)) == 1
            finally:
                release.set()
                await recovering
                for terminal_id in terminals:
                    resource = await owner.get_resource(terminal_id)
                    if resource["status"] not in {"terminated", "completed", "exited", "deleted"}:
                        await owner.command(
                            terminal_id, action="stop", expected_revision=resource["owner_revision"],
                            holder_id="request:cold-cleanup", operation_id="cold-cleanup",
                        )
            assert not any(process_running(pid) for pid in pids)
            assert await store.process_pending_directories() == ()
            for directory, pid in zip(directories, pids, strict=True):
                assert (directory / "derived/cold.py").is_file()
                assert int((directory / "derived/shell.pid").read_text()) == pid



async def test_owner_terminal_commit_failure_keeps_real_workdir_until_durable_reconciliation(
    terminal_runtime: WorkspaceToolRuntime, monkeypatch: pytest.MonkeyPatch,
) -> None:
    container, session_id, context = terminal_runtime
    store, owner, leases = container.attachment_blob_store, container.terminal_resource_owner, container.external_resource_leases
    message = await container.message_service.create(session_id, MessageCreateRequest(
        content="终态持久化失败仍保留目录", attachments=[upload(b"preserve-until-owner-commit")],
    ))
    ref, = message.attachments
    attachment_id = parse_resource_uri(ref.directory_uri).tail_segments[0]
    directory = store.resolve_directory_uri(parse_resource_uri(ref.directory_uri))
    workdir = directory / "derived"
    command = [sys.executable, "-u", "-c", (
        "import os,time; from pathlib import Path; "
        "Path('owner-commit.pid').write_text(str(os.getpid()), encoding='ascii'); "
        "print('owner-commit-ready', flush=True); time.sleep(600)"
    )]
    execute = create_exec_command_tool(session_id, terminal_owner=owner, invocation_context=context)
    result = await execute.ainvoke({
        "cmd": subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command),
        "workdir": file_uri_child(ref.directory_uri, "derived"), "yield_time_ms": 1000,
    })
    rid = result["terminal_id"]
    assert "owner-commit-ready" in result["output"]
    pid = int((workdir / "owner-commit.pid").read_text(encoding="ascii"))
    original_replace = Path.replace
    failures: list[OSError] = []

    def fail_terminal_commit(path: Path, target: Path):
        if target == owner._store.path and not failures:
            record, = json.loads(path.read_bytes())["records"]
            if record["status"] == "terminated" and record["reconciliation_status"] == "verified":
                error = OSError("受控的 owner 终态发布失败")
                failures.append(error)
                raise error
        return original_replace(path, target)

    try:
        assert process_running(pid) and workdir.is_dir()
        directory_lease, = leases.leases_for_resource(attachment_id)
        store.release_thread_references(session_id, message.thread_id)
        revision = (await owner.get_resource(rid))["owner_revision"]
        with monkeypatch.context() as patch:
            patch.setattr(Path, "replace", fail_terminal_commit)
            with pytest.raises(OSError) as caught:
                await owner.command(rid, action="stop", expected_revision=revision,
                                    holder_id="request:owner-commit-fail", operation_id="owner-commit-fail")
        assert type(caught.value) is type(failures[0]) and caught.value.errno == failures[0].errno
        assert caught.value.__cause__ is failures[0] and not process_running(pid)
        assert "stage=write" in str(caught.value) and str(owner._store.path) not in str(caught.value)
        record = owner._store.get(rid)
        persisted, = json.loads(owner._store.path.read_bytes())["records"]
        assert record.status == persisted["status"] == "stopping"
        assert record.reconciliation_status == persisted["reconciliation_status"] == "unknown"
        assert leases.get_lease(directory_lease.lease_id).status == "active"
        original_operation, = leases.leases_for_resource(rid)
        assert original_operation.status == "reconcile_required"
        assert await store.process_pending_directories() == ()
        assert (directory / "original/content").read_bytes() == b"preserve-until-owner-commit"
        stopped = await owner.command(rid, action="stop", expected_revision=record.owner_revision,
                                      holder_id="request:owner-commit-reconcile", operation_id="owner-commit-reconcile")
        assert stopped["status"] == "terminated" and stopped["reconciliation_status"] == "verified"
        assert leases.get_lease(original_operation.lease_id).status == "settled"
        assert leases.get_lease(directory_lease.lease_id).status == "settled"
        assert await store.process_pending_directories() == ()
        assert int((workdir / "owner-commit.pid").read_text(encoding="ascii")) == pid
    finally:
        snapshot = await owner.get_resource(rid)
        if snapshot["status"] not in {"terminated", "completed", "exited", "deleted"} or snapshot["reconciliation_status"] != "verified":
            await owner.command(rid, action="stop", expected_revision=snapshot["owner_revision"],
                                holder_id="request:owner-commit-cleanup", operation_id="owner-commit-cleanup")
