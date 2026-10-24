"""真实上传、Shell 工具和 PTY 子进程验证目录占用跨工具返回后继续保活。"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import sys
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from textwrap import dedent

import psutil
import pytest

from app.agents.tools.terminal import create_exec_command_tool
from app.core.resource_uri import file_uri_child, parse_resource_uri
from app.core.trace_middleware import reset_current_gateway_id, set_current_gateway_id
from app.schemas.internal_v2.message import MessageCreateRequest
from configs.runtime import read_jsonc_object
from tests.integration.backend.sessions.test_attachment_directory_entry import upload
from tests.integration.test_terminal_owner_tool_invocation import (
    terminal_runtime as terminal_runtime,  # noqa: PLC0414
)
from tests.integration.test_terminal_owner_tool_invocation import (
    terminal_workspace_root_path as terminal_workspace_root_path,  # noqa: PLC0414
)
from tests.support.workspace_tool_runtime import WorkspaceToolRuntime


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
        assert owner._store.records[terminal_id].working_directory_uri == local_directory_uri
        # 直接调用真实 detach owner 边界，外置时钟只改变回收时刻，不伪造进程或 lease。
        store.release_thread_references(session_id, message.thread_id)
        monkeypatch.setattr(store, "_clock", lambda: datetime.now(UTC) + timedelta(days=31))
        assert await store.collect_garbage(leases=leases) == ()
        assert directory.is_dir() and process_running(child_pid)
        snapshot = await owner.get_resource(terminal_id)
        stopped = await owner.command(terminal_id, action="stop", expected_revision=snapshot["owner_revision"],
            holder_id="request:attachment-shell-stop", operation_id="attachment-shell-stop")
        assert stopped["status"] == "terminated" and stopped["reconciliation_status"] == "verified"
        assert not process_running(child_pid)
        assert not leases.leases_for_resource(attachment_id)
        assert leases.get_lease(active.lease_id).status == "settled"
        assert await store.collect_garbage(leases=leases) == (attachment_id,)
        assert not directory.exists()
    finally:
        snapshot = await owner.get_resource(terminal_id)
        if snapshot["status"] not in {"terminated", "completed", "exited", "deleted"}:
            await owner.command(terminal_id, action="stop", expected_revision=snapshot["owner_revision"],
                holder_id="request:attachment-shell-cleanup", operation_id="attachment-shell-cleanup")


async def test_shell_directory_admission_and_process_lease_exclude_due_gc(
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
    monkeypatch.setattr(store, "_clock", lambda: datetime.now(UTC) + timedelta(days=31))
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
        assert await asyncio.wait_for(store.collect_garbage(leases=leases), 5) == ()
        assert not creation.done() and directory.is_dir()
    finally:
        resume.set()
        resource = await creation
    terminal_id = resource["resource_id"]
    try:
        active, = leases.leases_for_resource(attachment_id)
        assert active.turn_stream_id == terminal_id
        assert await store.collect_garbage(leases=leases) == ()
        assert directory.is_dir()
    finally:
        stopped = await owner.command(terminal_id, action="stop", expected_revision=resource["owner_revision"],
            holder_id="request:directory-admission-stop", operation_id="directory-admission-stop")
        assert stopped["status"] == "terminated"
    assert await store.collect_garbage(leases=leases) == (attachment_id,)
    assert not directory.exists()
