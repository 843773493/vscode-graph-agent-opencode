"""真实PTY的错误回执保留原操作和目录占用，工作区完成查询核实真实终态。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import subprocess
import sys
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.core.resource_uri import file_uri_child, parse_resource_uri
from app.schemas.internal_v2.message import MessageCreateRequest
from app.services.business.workspace_resources.owner import ResourceConflict
from app.services.infrastructure.events.channel_events import ResourceStateEvent
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.test_attachment_directory_entry import upload
from tests.integration.backend.sessions.test_attachment_directory_shell import (
    process_running,
)
from tests.integration.backend.sessions.test_attachment_directory_shell import (
    shell_model_configuration as shell_model_configuration,  # noqa: PLC0414
)
from tests.integration.test_terminal_owner_tool_invocation import (
    terminal_runtime as terminal_runtime,  # noqa: PLC0414
)
from tests.integration.test_terminal_owner_tool_invocation import (
    terminal_workspace_root_path as terminal_workspace_root_path,  # noqa: PLC0414
)
from tests.support.workspace_tool_runtime import WorkspaceToolRuntime


@pytest.fixture
async def managed_terminal(terminal_runtime: WorkspaceToolRuntime):
    container, sid, context = terminal_runtime
    store, owner, leases = container.attachment_blob_store, container.terminal_resource_owner, container.external_resource_leases
    message = await container.message_service.create(sid, MessageCreateRequest(
        content="回执验证目录", attachments=[upload(b"keep-original-receipt")],
    ))
    ref, = message.attachments
    directory_uri = ref.directory_uri
    assert isinstance(directory_uri, str)
    binding = context.thread_binding
    assert binding is not None
    attachment_id = parse_resource_uri(directory_uri).tail_segments[0]
    directory = store.resolve_directory_uri(parse_resource_uri(directory_uri))
    created = await owner.create_terminal(
        session_id=sid, thread_id=binding.thread_id, retention_scope="workspace",
        cwd=file_uri_child(directory_uri, "derived"), holder_id="request:receipt-create", operation_id="receipt-create",
    )
    rid = created["resource_id"]
    assert isinstance(rid, str)
    current = await owner.command(rid, action="promote", expected_revision=1,
                                  holder_id="request:receipt-version", operation_id="receipt-version")
    assert current["owner_revision"] == 2
    directory_lease, = leases.leases_for_resource(attachment_id)
    try:
        yield container, sid, message.thread_id, rid, attachment_id, directory, directory_lease
    finally:
        current = await owner.get_resource(rid)
        if current["status"] not in {"terminated", "exited", "completed", "deleted"}:
            revision = current["owner_revision"]
            assert type(revision) is int
            await owner.command(rid, action="stop", expected_revision=revision,
                                holder_id="request:receipt-cleanup", operation_id="receipt-cleanup")


@pytest.mark.parametrize("entry", ["read", "write", "complete"])
@pytest.mark.parametrize("fault", ["stale", "unknown"])
async def test_real_operation_fault_keeps_exact_leases_until_verified_management_stop(
    managed_terminal, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, entry: str, fault: str,
) -> None:
    container, sid, tid, rid, attachment_id, directory, directory_lease = managed_terminal
    owner, store, leases = container.terminal_resource_owner, container.attachment_blob_store, container.external_resource_leases
    external = await owner._transport.get_resource(rid)
    pid = int(external["os_pid"])
    assert pid == external["os_pid"] and pid > 0
    assert process_running(pid)
    method = {"read": "read_terminal", "write": "write_terminal", "complete": "get_resource"}[entry]
    original = getattr(owner._transport, method)
    received = []

    async def returned(*args, **kwargs):
        result = await original(*args, **kwargs)
        runtime = result.get("terminal", result)
        received.append(runtime)
        assert runtime["transport_owner_revision"] == 2
        # 故障只更改真实HTTP回执，不伪造PTY/持久账本或原进程终态。
        altered = {**runtime, **({"transport_owner_revision": 1} if fault == "stale" else {"reconciliation_status": "unknown"})}
        return {**result, "terminal": altered} if "terminal" in result else altered

    probe = directory / "derived/receipt_probe.py"
    marker = directory / "derived/written.txt"
    probe.write_text("from pathlib import Path\nPath('written.txt').write_text('side-effect', encoding='utf-8')\n", encoding="utf-8")
    args = [sys.executable, probe.name]
    # 原生PTY用回车提交，Windows控制台中的换行本身不会执行命令。
    command = (subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args)) + "\r"
    store.release_thread_references(sid, tid)
    monkeypatch.setattr(store, "_clock", lambda: datetime.now(UTC) + timedelta(days=31))
    async with owner.operation(session_id=sid, thread_id=tid, holder_id="request:real-receipt", operation_id="real-receipt") as operation:
        await operation.read_terminal(rid)
        active, = leases.leases_for_resource(rid)
        assert active.status == "active"
        assert (active.resource_id, active.turn_stream_id, active.operation_id,
                active.holder_session_id, active.holder_thread_id) == (
            rid, "request:real-receipt", "real-receipt", sid, tid,
        )
        with monkeypatch.context() as patch:
            patch.setattr(owner._transport, method, returned)
            with pytest.raises(ValueError if fault == "stale" else ResourceConflict):
                if entry == "read":
                    await operation.read_terminal(rid)
                elif entry == "write":
                    await operation.write_terminal(terminal_id=rid, data=command)
                else:
                    await operation.complete_terminal(rid)
        assert leases.get_lease(active.lease_id).status == "reconcile_required"
    assert len(received) == 1
    cold = ExternalResourceLeaseLedger(state_path=leases._state_path)
    pending = leases.get_lease(active.lease_id)
    assert pending.status == "reconcile_required" and cold.get_lease(active.lease_id) == pending
    assert pending == replace(active, status="reconcile_required", updated_at=pending.updated_at)
    assert cold.get_lease(directory_lease.lease_id) == directory_lease
    assert leases.get_lease(directory_lease.lease_id).status == "active"
    record = owner._store.get(rid)
    assert record.reconciliation_status == "unknown" and record.owner_revision == 2
    if entry == "write":
        async with asyncio.timeout(10):
            while not marker.exists():
                await asyncio.sleep(0.05)
        assert marker.read_text(encoding="utf-8") == "side-effect"
    current = await owner.get_resource(rid)
    assert current["reconciliation_status"] == "unknown"
    assert current["owner_revision"] == 2
    actual = await owner._transport.get_resource(rid)
    assert actual["os_pid"] == pid and actual["transport_owner_revision"] == 2
    assert process_running(pid) and await store.collect_garbage(leases=leases) == ()
    assert (directory / "original/content").read_bytes() == b"keep-original-receipt"
    stopped = await owner.command(rid, action="stop", expected_revision=2,
                                  holder_id="request:receipt-proof", operation_id="receipt-proof")
    assert stopped["status"] == "terminated" and stopped["owner_revision"] == 3
    assert not process_running(pid)
    settled = leases.get_lease(active.lease_id)
    assert settled == replace(active, status="settled", updated_at=settled.updated_at)
    assert leases.get_lease(directory_lease.lease_id).status == "settled"
    cold = ExternalResourceLeaseLedger(state_path=leases._state_path)
    assert cold.get_lease(active.lease_id) == settled
    assert cold.get_lease(directory_lease.lease_id) == leases.get_lease(directory_lease.lease_id)
    assert await store.collect_garbage(leases=leases) == (attachment_id,)
    assert not directory.exists() and len(received) == 1
    run = TestRunContext.from_test_file(Path(request.path)).prepare()
    (run.artifacts_dir / f"receipt-{entry}-{fault}.json").write_text(json.dumps({
        "entry": entry, "fault": fault, "os_pid": pid, "original_operation_lease": active.lease_id,
        "original_operation": asdict(active), "pending_operation": asdict(pending), "settled_operation": asdict(settled),
        "original_directory_lease": directory_lease.lease_id, "stop": stopped,
        "scope": "正式Session/上传/Terminal创建、原工具operation及真实Node/PTY/HTTP；仅回执为故障注入，不代表Gateway/Web或独立Backend重启",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def _assert_workspace_completion(
    managed_terminal, monkeypatch: pytest.MonkeyPatch,
    notification_failure: str, caplog: pytest.LogCaptureFixture, request: pytest.FixtureRequest,
) -> None:
    container, sid, tid, rid, attachment_id, directory, directory_lease = managed_terminal
    owner, store, leases = container.terminal_resource_owner, container.attachment_blob_store, container.external_resource_leases
    external = await owner._transport.get_resource(rid)
    pid = int(external["os_pid"])
    assert pid == external["os_pid"] and pid > 0
    # 暂停独立轮询，确保本次完成查询是唯一提交终态的观察者。
    await container.terminal_steering_service.shutdown()
    store.release_thread_references(sid, tid)
    monkeypatch.setattr(store, "_clock", lambda: datetime.now(UTC) + timedelta(days=31))
    # 真实外部owner已停止；Python业务记录只能由本次完成查询核实并提交。
    stopped = await owner._transport.stop_resource(rid)
    assert stopped["terminal"]["status"] == "terminated" and not process_running(pid)
    assert owner._store.get(rid).status == "running"
    assert leases.get_lease(directory_lease.lease_id).status == "active"
    assert await store.collect_garbage(leases=leases) == ()
    failure = RuntimeError(f"事件发布失败 {directory / 'lease-event-secret'}；" + "完整诊断" * 80)
    sink_error = OSError("日志输出失败 /private/ledger-log-secret")
    channel = container.job_event_bus.event_channel_service.channel("resource.state/external_resource_leases")
    publish, call_handlers = channel.publish, logging.Logger.callHandlers
    notifications: list[ResourceStateEvent] = []

    def fail_notification(event: ResourceStateEvent) -> object:
        if event.resource_id == attachment_id:
            notifications.append(event)
            raise failure
        return publish(event)

    def fail_log(logger: logging.Logger, record: logging.LogRecord) -> None:
        call_handlers(logger, record)
        if record.getMessage().startswith("resource.state 事件发布失败:"):
            raise sink_error

    with monkeypatch.context() as fault, caplog.at_level(logging.ERROR):
        if notification_failure != "none":
            fault.setattr(channel, "publish", fail_notification)
        if notification_failure == "publish-and-log":
            fault.setattr(logging.Logger, "callHandlers", fail_log)
        async with owner.operation(session_id=sid, thread_id=tid, holder_id="request:complete-proof", operation_id="complete-proof") as operation:
            result = await operation.complete_terminal(rid)
            assert result["status"] == "terminated" and result["owner_revision"] == 2
            assert result["retention_scope"] == "workspace" and result["thread_associations"]
            assert owner._store.get(rid).status == "terminated"
            assert leases.get_lease(directory_lease.lease_id).status == "settled"
            original, = leases.leases_for_resource(rid)
            assert original.status == "active"
    assert leases.get_lease(original.lease_id).status == "released"
    cold = ExternalResourceLeaseLedger(state_path=leases._state_path)
    assert cold.get_lease(original.lease_id) == leases.get_lease(original.lease_id)
    assert cold.get_lease(directory_lease.lease_id) == leases.get_lease(directory_lease.lease_id)
    if notification_failure == "none":
        assert not notifications and not leases.notification_errors
    else:
        assert len(notifications) == len(leases.notification_errors) == 1
        assert notifications[0].state == "released"
        detail, = leases.notification_errors
        assert f"resource_id={attachment_id}, state=released" in detail
        assert "stage=resource-state-notify, error=RuntimeError" in detail
        assert any(record.exc_info and record.exc_info[1] is failure for record in caplog.records)
        assert str(failure) in caplog.text
        if notification_failure == "publish-and-log":
            assert "diagnostic_sink_error=OSError" in detail
            assert any(str(sink_error) in note for note in failure.__notes__)
        else:
            assert "diagnostic_sink_error" not in detail
    for text in (*leases.notification_errors, json.dumps(result), leases._state_path.read_text()):
        assert "lease-event-secret" not in text and "ledger-log-secret" not in text
    assert await store.collect_garbage(leases=leases) == (attachment_id,)
    assert not directory.exists()
    run = TestRunContext.from_test_file(Path(request.path)).prepare()
    (run.artifacts_dir / f"completion-{notification_failure}.json").write_text(json.dumps({
        "resource_id": rid, "os_pid": pid, "completed": result,
        "directory_lease": directory_lease.lease_id, "notification_errors": leases.notification_errors,
        "directory_removed": not directory.exists(),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def test_workspace_completion_commits_real_stopped_pty_and_releases_due_directory(
    managed_terminal, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, request: pytest.FixtureRequest,
) -> None:
    await _assert_workspace_completion(managed_terminal, monkeypatch, "none", caplog, request)


@pytest.mark.parametrize("notification_failure", ["publish", "publish-and-log"])
async def test_completion_notification_failure_keeps_committed_leases_and_directory_gc(
    managed_terminal, monkeypatch: pytest.MonkeyPatch,
    notification_failure: str, caplog: pytest.LogCaptureFixture, request: pytest.FixtureRequest,
) -> None:
    await _assert_workspace_completion(managed_terminal, monkeypatch, notification_failure, caplog, request)
