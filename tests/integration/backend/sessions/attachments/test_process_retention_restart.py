"""真实 Shell 进程跨 Backend 硬退出保活，核实终态后才回收到期目录。"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import PurePosixPath

import psutil
import pytest

from app.core.atomic_fs import native_io_path
from app.core.resource_uri import file_uri_child, parse_resource_uri
from tests.integration.backend.sessions.attachments.test_retention_restart import (
    DAY_SECONDS,
    directory_snapshot,
)
from tests.integration.backend.sessions.test_attachment_directory_entry import upload
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    CommunicationCase,
    _completed,
    _tool,
)
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    communication_case as communication_case,  # noqa: PLC0414
)
from tests.support.api_helpers import create_session
from tests.support.e2e_control.harness import control_request


def resource_leases(case: CommunicationCase, resource_id: str) -> list[dict[str, object]]:
    """只读唯一持久账本，不通过构造 owner 帮产品结算。"""
    ledger = json.loads((case.workspace / ".boxteam/resources.json").read_bytes())
    return [item for item in ledger["leases"] if item["resource_id"] == resource_id]


def directory_lease(case: CommunicationCase, attachment_id: str) -> dict[str, object]:
    lease, = resource_leases(case, attachment_id)
    return lease


@pytest.mark.parametrize("communication_case", [{"resources": True}], indirect=True, ids=["pty"])
@pytest.mark.parametrize("stop_boundary", [None, "before_dispatch", "after_receipt"], ids=["live", "before-stop", "after-stop"])
async def test_process_retention_across_hard_restarts(
    communication_case: CommunicationCase, stop_boundary: str | None,
) -> None:
    case = communication_case
    session = await create_session(case.client, "处理进程跨崩溃保活")
    await case.plan(session, [{"content": "附件已接受"}])
    response = await case.client.post(f"/api/v1/sessions/{session}/messages", json={
        "message": {"content": "保存待处理原件", "attachments": [upload(b"process-retention-original").model_dump(mode="json")]},
        "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert response.status_code == 200, response.text
    await _completed(case.client, response.json()["data"]["job_id"])
    history = await case.client.post(f"/api/v1/sessions/{session}/history", json={"direction": "head", "turns": 10})
    assert history.status_code == 200, history.text
    original, = history.json()["data"]["items"][0]["user_messages"][0]["attachments"]
    attachment_id = parse_resource_uri(original["directory_uri"]).tail_segments[0]
    initial = directory_snapshot(case)
    directory = native_io_path((case.workspace / ".boxteam/attachments").joinpath(
        *PurePosixPath(str(initial["relative_locator"])).parts,
    ))
    # 正式模型工具调用启动父/子进程，工具返回后两者仍在附件 derived 中运行。
    program = (
        "import json,os,subprocess,sys,time; from pathlib import Path; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(600)']); "
        "Path('processes.json').write_text(json.dumps([os.getpid(),child.pid]),encoding='ascii'); "
        "print('attachment-process-ready',flush=True); child.wait()"
    )
    arguments = [sys.executable, "-u", "-c", program]
    calls = await case.plan(session, [
        _tool("exec_command", {
            "cmd": subprocess.list2cmdline(arguments) if os.name == "nt" else shlex.join(arguments),
            "workdir": file_uri_child(original["directory_uri"], "derived"), "yield_time_ms": 1000,
        }, "hold-original-directory"),
        {"content": "处理程序继续运行"},
    ])
    response = await case.client.post(f"/api/v1/sessions/{session}/messages", json={
        "message": {"content": "在附件目录中启动后台处理程序"},
        "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert response.status_code == 200, response.text
    await _completed(case.client, response.json()["data"]["job_id"])
    tool = case.tool_result(session, calls[0] + ":tool-call:hold-original-directory")
    assert tool["tool_outcome"] == "success" and tool["execution_confirmed"] is True, tool
    assert isinstance(tool["content"], str), tool
    result = json.loads(tool["content"])
    assert "exit_code" not in result and "attachment-process-ready" in result["output"], result
    rid = result["terminal_id"]
    processes = [psutil.Process(pid) for pid in json.loads((directory / "derived/processes.json").read_bytes())]
    identities = [{"pid": process.pid, "created_at": process.create_time()} for process in processes]
    assert len(processes) == 2 and all(process.is_running() for process in processes)
    lease = directory_lease(case, attachment_id)
    assert lease["status"] == "active" and lease["operation_id"] == lease["turn_stream_id"] == rid
    assert lease["holder_session_id"] == session
    canonical = native_io_path(case.main_thread_node(session) / "rollout/rollout.jsonl")
    frozen_history = canonical.read_bytes()

    # 硬退出不发 detach；新 Backend 释放旧 runtime 关联，但不能释放活 PTY 的原占用。
    await case.hard_restart()
    released = directory_snapshot(case)
    assert released["association_state"] == "released" and released["binding_token"] == initial["binding_token"]
    assert datetime.fromisoformat(str(released["gc_after"])) == datetime.fromisoformat(str(released["unattached_since"])) + timedelta(days=30)
    assert directory_lease(case, attachment_id) == lease
    assert all(process.is_running() for process in processes)
    response = await case.client.get(f"/api/v1/resources/{rid}")
    assert response.status_code == 200, response.text
    resource = response.json()["data"]
    assert resource["resource_id"] == rid and resource["status"] == "running"
    assert resource["reconciliation_status"] == "verified"
    assert resource["source_thread"]["session_id"] == session

    case.replay.control.advance_wait(30 * DAY_SECONDS)
    await case.hard_restart()
    assert directory_snapshot(case) == released
    assert directory_lease(case, attachment_id) == lease
    assert all(process.is_running() for process in processes)
    assert (directory / "original/content").read_bytes() == b"process-retention-original"
    assert canonical.read_bytes() == frozen_history
    available = await case.client.get("/api/v1/workspace/files/content", params={"path": original["file_id"]})
    assert available.status_code == 200 and available.json()["data"]["content"] == "process-retention-original", available.text

    # 使用同一资源的公开管理入口核实真实终态，不由测试杀 PID 或手填 settled。
    response = await case.client.get(f"/api/v1/resources/{rid}")
    assert response.status_code == 200, response.text
    resource = response.json()["data"]
    recovery = None
    if stop_boundary is not None:
        control = case.replay.control
        phase = "resource.stop." + stop_boundary
        barrier = "attachment-stop-receipt"
        control.arm_match(barrier, phase, {"resource_id": rid, "kind": "terminal", "delete": False})
        pending = asyncio.create_task(case.client.post(
            f"/api/v1/resources/{rid}/stop", json={"expected_revision": resource["owner_revision"]},
        ))
        try:
            reached = await asyncio.to_thread(control.acknowledged_match, barrier)
            operation, = (item for item in resource_leases(case, rid) if item["lease_id"] == reached["operation_id"])
            assert operation["status"] == "active"
            assert directory_lease(case, attachment_id) == lease
            if stop_boundary == "before_dispatch":
                assert all(process.is_running() for process in processes)
            else:
                assert all(not process.is_running() or process.status() in {psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD} for process in processes)
            await case.hard_restart()
            interrupted = await pending
            assert interrupted.status_code == 502, interrupted.text
        finally:
            control_request(json.loads(control.environment["BOXTEAM_E2E_TEST_CONTROL"]), {
                "action": "release_match", "barrier_id": barrier,
            })
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        response = await case.client.get(f"/api/v1/resources/{rid}")
        assert response.status_code == 200, response.text
        resource = response.json()["data"]
        recovered_operation, = (item for item in resource_leases(case, rid) if item["lease_id"] == operation["lease_id"])
        expected = "reconcile_required" if stop_boundary == "before_dispatch" else "settled"
        assert recovered_operation == {**operation, "status": expected, "updated_at": recovered_operation["updated_at"]}
        events = [event for event in control.events if isinstance(details := event.get("details"), dict)
                  and details.get("resource_id") == rid
                  and event["phase"] in ("resource.stop.before_dispatch", "resource.stop.after_receipt")]
        assert [event["phase"] for event in events] == ["resource.stop.before_dispatch"] + (
            ["resource.stop.after_receipt"] if stop_boundary == "after_receipt" else []
        )
        if stop_boundary == "before_dispatch":
            assert resource["status"] == "running" and resource["reconciliation_status"] == "reconcile_required"
            assert directory_lease(case, attachment_id) == lease
            assert directory_snapshot(case) == released and directory.is_dir()
            assert all(process.is_running() for process in processes)
        else:
            assert resource["status"] == "terminated" and resource["reconciliation_status"] == "verified"
            assert directory_lease(case, attachment_id)["status"] == "settled"
        recovery = {"boundary": stop_boundary, "operation_before": operation,
                    "operation_after": recovered_operation, "events": events, "resource": resource}

    if stop_boundary != "after_receipt":
        response = await case.client.post(f"/api/v1/resources/{rid}/stop", json={"expected_revision": resource["owner_revision"]})
        assert response.status_code == 200, response.text
        stopped = response.json()["data"]
    else:
        stopped = resource
    assert stopped["status"] == "terminated" and stopped["reconciliation_status"] == "verified"
    assert stopped["resource_id"] == rid and stopped["source_thread"] == resource["source_thread"]
    assert all(not process.is_running() or process.status() in {psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD} for process in processes)
    settled = directory_lease(case, attachment_id)
    assert settled == {**lease, "status": "settled", "updated_at": settled["updated_at"]}
    await case.hard_restart()
    expired = directory_snapshot(case)
    assert expired == {**released, "availability": "tombstoned"}
    assert not directory.exists() and directory_lease(case, attachment_id) == settled
    assert canonical.read_bytes() == frozen_history
    unavailable = await case.client.get("/api/v1/workspace/files/content", params={"path": original["file_id"]})
    assert unavailable.status_code == 404, unavailable.text
    await case.hard_restart()
    assert directory_snapshot(case) == expired and not directory.exists()
    assert directory_lease(case, attachment_id) == settled and canonical.read_bytes() == frozen_history
    case.record("process-retention-restart", {
        "resource_id": rid, "processes": identities, "initial": initial, "released": released,
        "expired": expired, "lease": lease, "settled": settled, "stopped": stopped, "recovery": recovery,
        "scope": "真实Gateway/Backend/model_stream/Shell/PTY和硬退出，精确到期保活，停止派发前或真实回执返回后硬退出，由新Backend核实原终态或新管理操作终止后回收；不证明不合作执行可强制终止",
    })
