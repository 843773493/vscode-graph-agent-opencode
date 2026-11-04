"""丢失启动停止证明后，由新Backend沿原身份核实并结清原目录占用。"""

from __future__ import annotations

import json
import os
import signal
import sys
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

import pytest

from app.core.atomic_fs import native_io_path
from app.core.resource_uri import file_uri_child, parse_resource_uri
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.attachments.process_lifecycle import (
    directory_snapshot,
)
from tests.integration.backend.sessions.attachments.test_process_retention_restart import (
    directory_lease,
    resource_leases,
)
from tests.integration.backend.sessions.test_attachment_directory_entry import upload
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    CommunicationCase,
    _completed,
)
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    communication_case as communication_case,  # noqa: PLC0414
)
from tests.integration.workspace_services.terminal.test_terminal_startup_cleanup import (
    is_executing,
    wait_observed,
    workload,
)
from tests.support.api_helpers import create_session

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="真实Linux PTY与Backend硬退出；不代替Windows")


@pytest.fixture(autouse=True)
def startup_fault_environment(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    run = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    control = run.artifacts_dir / "startup-control"
    control.mkdir()
    shell = control / "gated-terminal-startup-cleanup.py"
    shell.write_text(f"#!{sys.executable}\n" + workload(control, ignore_sigterm=False))
    shell.chmod(0o700)
    for marker in ("late-native-identity", "drop-stop-proof"):
        (control / marker).touch()
    (control / "mode").write_text("late-native-identity")
    monkeypatch.setenv("SHELL", str(shell))
    monkeypatch.setenv("NODE_OPTIONS", f"--import={(Path.cwd() / 'tests/integration/workspace_services/terminal/terminal_startup_failure.mjs').as_uri()}")
    monkeypatch.setenv("BOXTEAM_TEST_PROJECT_ROOT", str(Path.cwd()))
    monkeypatch.setenv("BOXTEAM_TEST_TERMINAL_CONTROL", str(control))
    try:
        yield control
    finally:
        # 只在失败收尾停止本例独立观察到的原实例，不改写产品账本或恢复记录。
        if (control / "observed.json").exists():
            for identity in json.loads((control / "observed.json").read_text()).values():
                if is_executing(identity):
                    os.kill(int(identity["pid"]), signal.SIGKILL)


@pytest.mark.parametrize("communication_case", [{"resources": True}], indirect=True, ids=["pty"])
async def test_startup_unknown_survives_backend_loss_until_management_proof(
    communication_case: CommunicationCase, startup_fault_environment: Path,
) -> None:
    case, control = communication_case, startup_fault_environment
    session = await create_session(case.client, "启动未知占用跨Backend恢复")
    await case.plan(session, [{"content": "原件已接受"}])
    response = await case.client.post(f"/api/v1/sessions/{session}/messages", json={
        "message": {"content": "保存原件", "attachments": [upload(b"startup-recovery-original").model_dump(mode="json")]},
        "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert response.status_code == 200, response.text
    await _completed(case.client, response.json()["data"]["job_id"])
    response = await case.client.post(f"/api/v1/sessions/{session}/history", json={"direction": "head", "turns": 10})
    assert response.status_code == 200, response.text
    original, = response.json()["data"]["items"][0]["user_messages"][0]["attachments"]
    attachment_id = parse_resource_uri(original["directory_uri"]).tail_segments[0]
    initial = directory_snapshot(case)
    directory = native_io_path((case.workspace / ".boxteam/attachments").joinpath(*PurePosixPath(str(initial["relative_locator"])).parts))
    canonical = case.main_thread_node(session) / "rollout/rollout.jsonl"
    history = canonical.read_bytes()

    response = await case.client.post("/api/v1/resources/terminals", json={
        "session_id": session, "retention_scope": "workspace",
        "cwd_relative": file_uri_child(original["directory_uri"], "derived"),
    })
    assert response.status_code == 500, response.text
    observed = await wait_observed(control)
    assert all(not is_executing(identity) for identity in observed.values())
    state_file = case.workspace / ".boxteam/terminal-manager/terminals.json"
    native, = json.loads(state_file.read_bytes())["terminals"]
    rid = native["terminal_id"]
    assert native["status"] == "created" and native["reconciliation_status"] == "unknown"
    for field, system_field in (("os_pid", "pid"), ("process_group_id", "processGroupId"),
                                ("process_session_id", "processSessionId"), ("process_start_time", "processStartTime")):
        assert native[field] == observed["root"][system_field]
    operation, = resource_leases(case, rid)
    assert operation["status"] == "reconcile_required" and operation["holder_session_id"] == session
    directory_use = directory_lease(case, attachment_id)
    assert directory_use["status"] == "active" and directory_use["operation_id"] == directory_use["turn_stream_id"] == rid
    workload_bytes = (control / "workload.json").read_bytes()
    receipts = (control / "stop-receipts.jsonl").read_bytes()

    # 不发detach，不补写unknown/身份；新Backend从原持久事实接管。
    await case.hard_restart()
    response = await case.client.get(f"/api/v1/resources/{rid}")
    assert response.status_code == 200, response.text
    unknown = response.json()["data"]
    assert unknown["status"] == "created" and unknown["reconciliation_status"] == "unknown"
    assert unknown["source_thread"]["session_id"] == session
    assert unknown["owner_revision"] == native["transport_owner_revision"] + 1
    assert resource_leases(case, rid) == [operation]
    assert directory_lease(case, attachment_id) == directory_use
    released = directory_snapshot(case)
    assert released["association_state"] == "released" and released["binding_token"] == initial["binding_token"]

    # 已退出的进程不能代替正式停止证明；普通查询不能自动结清。
    await case.hard_restart()
    response = await case.client.get(f"/api/v1/resources/{rid}")
    assert response.status_code == 200, response.text
    unknown = response.json()["data"]
    assert unknown["status"] == "created" and unknown["reconciliation_status"] == "unknown"
    assert unknown["owner_revision"] == native["transport_owner_revision"] + 2
    assert resource_leases(case, rid) == [operation] and directory_lease(case, attachment_id) == directory_use
    assert directory_snapshot(case) == released and directory.is_dir()
    assert (directory / "original/content").read_bytes() == b"startup-recovery-original"

    response = await case.client.post(f"/api/v1/resources/{rid}/stop", json={"expected_revision": unknown["owner_revision"]})
    assert response.status_code == 200, response.text
    stopped = response.json()["data"]
    assert stopped["resource_id"] == rid and stopped["source_thread"] == unknown["source_thread"]
    assert stopped["status"] == "terminated" and stopped["reconciliation_status"] == "verified"
    assert stopped["owner_revision"] == unknown["owner_revision"] + 1
    settled_operation, = (item for item in resource_leases(case, rid) if item["lease_id"] == operation["lease_id"])
    assert settled_operation == {**operation, "status": "settled", "updated_at": settled_operation["updated_at"]}
    settled_directory = directory_lease(case, attachment_id)
    assert settled_directory == {**directory_use, "status": "settled", "updated_at": settled_directory["updated_at"]}
    final, = json.loads(state_file.read_bytes())["terminals"]
    for field in ("os_pid", "process_group_id", "process_session_id", "process_start_time", "created_at"):
        assert final[field] == native[field]
    assert final["transport_owner_revision"] == stopped["owner_revision"]
    assert final["status"] == "terminated" and final["reconciliation_status"] == "verified"

    await case.hard_restart()
    assert directory_snapshot(case) == {**released, "availability": "released", "release_pending": 0, "release_reason": None}
    assert not directory.exists() and directory_lease(case, attachment_id) == settled_directory
    assert canonical.read_bytes() == history
    assert (control / "workload.json").read_bytes() == workload_bytes
    assert (control / "stop-receipts.jsonl").read_bytes() == receipts
    assert all(not is_executing(identity) for identity in observed.values())
    case.record("startup-operation-restart", {
        "native": native, "observed": observed, "unknown": unknown, "stopped": stopped,
        "operation": operation, "settled_operation": settled_operation,
        "directory_use": directory_use, "settled_directory": settled_directory,
        "scope": "真实Gateway/Backend/HTTP/PTY、丢停止证明、Backend硬重启和原管理停止；不代表无原生身份接管、Windows或Web",
    })
