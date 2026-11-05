"""Windows 真进程／Job 与原 SDK 命令入口对照；不替换原生返回或协议。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Protocol, cast

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.os.win32.utilities import get_windows_executable_command
from pydantic import ValidationError

from app.core.child_process import OwnedChildProcess
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.mcp.transport.stdio import mcp_stdio_transport
from tests.harness.python.run_context import TestRunContext
from tests.integration.workspace_services.mcp.test_full_schema_invocation import (
    SchemaServerCase,
)
from tests.integration.workspace_services.mcp.test_full_schema_invocation import (
    schema_server_case as schema_server_case,  # noqa: PLC0414 - 原真实 SDK／stdio 工作区装具
)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="需要真实 Windows Process 与 Job Object")


class QueryHandle(Protocol):
    def __int__(self) -> int: ...

    def Close(self) -> None: ...


@pytest.fixture
def windows_context(request: pytest.FixtureRequest):
    return TestRunContext.from_test_file(Path(request.node.path)).prepare()


@pytest.fixture
def native_observations(monkeypatch: pytest.MonkeyPatch, windows_context: TestRunContext):
    """只转发并记录公共 Win32 调用；所有句柄和返回码来自真实内核。"""
    import win32con
    import win32event
    import win32job
    import win32process

    events = []
    create, assign, resume = win32process.CreateProcess, win32job.AssignProcessToJobObject, win32process.ResumeThread
    wait, query, terminate = win32event.WaitForSingleObject, win32job.QueryInformationJobObject, win32job.TerminateJobObject

    def create_observed(*args):
        result = create(*args)
        assert args[5] & win32con.CREATE_SUSPENDED
        events.append({"stage": "create_suspended", "pid": result[2], "process": int(result[0])})
        return result

    def assign_observed(job, process):
        result = assign(job, process)
        assert win32job.IsProcessInJob(process, job)
        events.append({"stage": "assigned", "job": job, "process": process})
        return result

    def resume_observed(thread):
        assert events[-1]["stage"] == "assigned"
        result = resume(thread)
        events.append({"stage": "resumed"})
        return result

    def wait_observed(process, milliseconds):
        result = wait(process, milliseconds)
        if result == win32event.WAIT_OBJECT_0:
            events.append({"stage": "wait_exited", "process": process})
        return result

    def query_observed(job, kind):
        result = query(job, kind)
        if kind == win32job.JobObjectBasicAccountingInformation:
            events.append({"stage": "job_count", "job": job, "active": result["ActiveProcesses"]})
        return result

    def terminate_observed(job, code):
        result = terminate(job, code)
        events.append({"stage": "terminated", "job": job, "code": code})
        return result

    monkeypatch.setattr(win32process, "CreateProcess", create_observed)
    monkeypatch.setattr(win32job, "AssignProcessToJobObject", assign_observed)
    monkeypatch.setattr(win32process, "ResumeThread", resume_observed)
    monkeypatch.setattr(win32event, "WaitForSingleObject", wait_observed)
    monkeypatch.setattr(win32job, "QueryInformationJobObject", query_observed)
    monkeypatch.setattr(win32job, "TerminateJobObject", terminate_observed)
    try:
        yield events
    finally:
        (windows_context.artifacts_dir / "native-events.json").write_text(
            json.dumps(events, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )


async def test_native_stdio_assigns_before_resume_and_waits_original_process(
    schema_server_case: SchemaServerCase, windows_context: TestRunContext, native_observations,
):
    case = schema_server_case
    await case.owner.start()
    runtime = case.owner._runtimes["schema"]
    process = runtime.lifecycle.process.process
    assert process is not None and runtime.process_state is not None
    try:
        assert [event["stage"] for event in native_observations[:3]] == ["create_suspended", "assigned", "resumed"]
        assert native_observations[0]["pid"] == process.pid == runtime.process_state.pid
        assert runtime.process_state.created_at is not None
        result = await case.invoker().ainvoke({"tool_name": "mcp__schema__probe", "arguments": {"text": "Windows 原调用"}})
        assert result and case.received() == [{"name": "probe", "arguments": {"text": "Windows 原调用"}}]
    finally:
        await case.owner.shutdown()
    state = runtime.process_state
    assert state is not None and state.phase == "exited" and state.exit_verified and state.returncode == 0
    assert await process.wait() == 0 and process.returncode == 0
    record = case.owner._leases.get(runtime.connection_ref)
    assert record is not None and record.status == "stopped" and record.process == state
    assert ExternalResourceLeaseLedger(state_path=case.workspace_root / ".boxteam/resources.json").get(runtime.connection_ref) == record
    original_handle = native_observations[0]["process"]
    assert any(event["stage"] == "wait_exited" and event["process"] == original_handle for event in native_observations)
    assert any(event["stage"] == "job_count" and event["active"] == 0 for event in native_observations)
    assert not any(event["stage"] == "terminated" for event in native_observations)
    windows_context.write_result("passed", {"process": asdict(state), "native_events": native_observations})


async def test_native_noncooperative_process_and_child_leave_original_job(
    windows_context: TestRunContext, native_observations,
):
    import win32api
    import win32con
    import win32job

    owner = OwnedChildProcess("windows:noncooperative")
    code = ("import subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)']); "
        "print(child.pid,flush=True); sys.stdin.buffer.read(); time.sleep(120)")
    try:
        await owner.start([sys.executable, "-c", code], env=os.environ, cwd=windows_context.workspace_root)
        process = owner.process
        assert process is not None and process.stdout is not None
        descendant_pid = int((await process.stdout.receive()).decode().strip())
        assert owner.state.created_at is not None
        assert owner._group_empty is not None and not owner._group_empty()
        # Windows venv 启动器会增加原生进程；按收据中的精确 child PID 核实归属。
        job = next(event["job"] for event in native_observations if event["stage"] == "assigned")
        # pywin32 的 OpenProcess stub 标成 int；真实返回值拥有 Close。
        descendant = cast(QueryHandle, cast(object,
            win32api.OpenProcess(win32con.PROCESS_QUERY_INFORMATION, False, descendant_pid)))
        try:
            assert win32job.IsProcessInJob(int(descendant), job)
        finally:
            descendant.Close()
        native_observations.append({"stage": "descendant_in_original_job", "pid": descendant_pid, "job": job})
    finally:
        await owner.close()
    assert owner.state.exit_verified and owner.state.returncode == 1
    assert any(event["stage"] == "terminated" for event in native_observations)
    assert any(event["stage"] == "job_count" and event["active"] == 0 for event in native_observations)
    original_handle = native_observations[0]["process"]
    assert any(event["stage"] == "wait_exited" and event["process"] == original_handle for event in native_observations)
    windows_context.write_result("passed", {"process": asdict(owner.state), "descendant_pid": descendant_pid,
        "native_events": native_observations})


def native_failure_codes(error: BaseException) -> tuple[int, ...]:
    if isinstance(error, BaseExceptionGroup):
        return tuple(code for child in error.exceptions for code in native_failure_codes(child))
    code = getattr(error, "winerror", None)
    if isinstance(code, int):
        return (code,)
    raise error


async def launcher_outcome(parameters: StdioServerParameters, *, owned: bool, version: bool = False):
    process = OwnedChildProcess("windows:launcher-comparison")

    async def started():
        return None

    transport = mcp_stdio_transport(parameters, process, started) if owned else stdio_client(parameters)
    try:
        async with asyncio.timeout(12), transport as (read, write):
            if version:
                message = await read.receive()
                assert isinstance(message, ValidationError), "包管理器版本正文应由原 SDK JSON-RPC 类型拒绝"
                return {"stdout": str(message.errors()[0]["input"])}
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert [tool.name for tool in tools.tools] == ["probe"]
                result = await session.call_tool("probe", {"text": "space & literal"})
                return {"structured_content": result.structuredContent, "content": [block.model_dump() for block in result.content]}
    except Exception as error:  # noqa: BLE001 - 仅比较原生失败码；非 Win32 错误由下层原样重抛。
        return {"native_errors": native_failure_codes(error)}


@pytest.mark.parametrize("kind", ["exe", "explicit-cmd", "bare-npx", "bare-npm"])
async def test_real_launcher_matches_public_sdk_command_contract(
    windows_context: TestRunContext, monkeypatch: pytest.MonkeyPatch, kind: str,
):
    context = windows_context
    # cmd.exe 的路径上限独立于 Python 长路径支持；启动文件放在正式 workspace 的短路径。
    launchers = context.workspace_root / f"{kind} launchers"
    launchers.mkdir(exist_ok=True)
    script = context.project_root / "tests/integration/workspace_services/mcp/schema_server.py"
    definitions = context.artifacts_dir / "definitions.json"
    definitions.write_text(json.dumps({"probe": {"type": "object"}}), encoding="utf-8")
    name = kind.removeprefix("bare-") if kind.startswith("bare-") else "server"
    launcher = launchers / f"{name}.cmd"
    launcher.write_text(f'@echo off\n"{sys.executable}" "{script}" %*\n', encoding="utf-8")
    monkeypatch.setenv("PATH", str(launchers) + os.pathsep + os.environ["PATH"])
    command = sys.executable if kind == "exe" else name if kind.startswith("bare-") else str(launcher)
    outcomes = []
    for owned in (False, True):
        receipts = context.artifacts_dir / ("owned.jsonl" if owned else "sdk.jsonl")
        args = [str(definitions), str(receipts)]
        if kind == "exe":
            args.insert(0, str(script))
        outcomes.append(await launcher_outcome(StdioServerParameters(command=command, args=args,
            cwd=str(context.project_root)), owned=owned))
    assert outcomes[0] == outcomes[1]
    assert "native_errors" not in outcomes[1]
    context.write_result("passed", {"kind": kind, "resolved": get_windows_executable_command(command),
        "sdk": outcomes[0], "owned": outcomes[1]})


@pytest.mark.parametrize("command", ["npx", "npm"])
async def test_installed_package_launcher_version_matches_public_sdk(
    windows_context: TestRunContext, command: str,
):
    if shutil.which(command) is None:
        windows_context.write_result("UNMET_PREREQUISITE", {"missing_command": command})
        pytest.skip(f"Windows 目标未安装 {command}")
    parameters = StdioServerParameters(command=command, args=["--version"])
    original = await launcher_outcome(parameters, owned=False, version=True)
    current = await launcher_outcome(parameters, owned=True, version=True)
    assert original == current
    assert "stdout" in current
    windows_context.write_result("passed", {"command": command, "resolved": get_windows_executable_command(command),
        "sdk": original, "owned": current})
