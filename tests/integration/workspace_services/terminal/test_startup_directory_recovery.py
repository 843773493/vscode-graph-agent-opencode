"""丢失原生停止证明后，Python 管理入口核实 Terminal 并结清原目录占用。"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import signal
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from app.abstractions.terminal.resources import TerminalRequestError
from app.core.resource_uri import file_uri_child, parse_resource_uri
from app.schemas.internal_v2.message import MessageCreateRequest
from app.services.business.workspace_resources.records import ThreadRef
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from tests.harness.python.run_context import TestRunContext
from tests.harness.python.terminal_plugin import terminal_plugin_gateway_for_workspace
from tests.integration.backend.sessions.test_attachment_directory_entry import upload
from tests.integration.workspace_services.terminal.test_terminal_startup_cleanup import (
    is_executing,
    process_identity,
    wait_observed,
    workload,
)
from tests.support.workspace_tool_runtime import workspace_tool_runtime
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_default_test_workspace,
)

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="真实 Linux PTY 和原生身份；不代 Windows")


@pytest.fixture(autouse=True)
def startup_shell(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Node 启动前设置真实故障注入 preload 和原生 shell。
    control = TestRunContext.from_test_file(Path(request.node.path)).artifacts_dir / "control"
    shell = tmp_path / "gated-terminal-startup-cleanup"
    shell.write_text("#!/bin/sh\nexec " + shlex.join([
        sys.executable, "-c", workload(control, ignore_sigterm=False),
    ]) + "\n")
    shell.chmod(0o700)
    monkeypatch.setenv("SHELL", str(shell))
    preload = Path.cwd() / "tests/integration/workspace_services/terminal/terminal_startup_failure.mjs"
    monkeypatch.setenv("NODE_OPTIONS", f"{os.environ.get('NODE_OPTIONS', '')} --import={preload.as_uri()}".strip())
    monkeypatch.setenv("BOXTEAM_TEST_PROJECT_ROOT", str(Path.cwd()))
    monkeypatch.setenv("BOXTEAM_TEST_TERMINAL_CONTROL", str(control))


@pytest.mark.parametrize("cold", [False, True], ids=["same-node", "restarted-node"])
async def test_unverified_startup_keeps_directory_until_original_management_stop(
    request: pytest.FixtureRequest, integration_backend_port: int, integration_config_path: str,
    monkeypatch: pytest.MonkeyPatch, cold: bool,
    prepared_developer_tools_package: Path,
) -> None:
    run = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    root = prepare_default_test_workspace(
        workspace_root=run.workspace_root / uuid4().hex,
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
    )
    install_test_workspace_config(
        workspace_root=root, config_path=Path(integration_config_path),
        schema_path=Path.cwd() / "configs/workspace_schema.jsonc",
    )
    control = run.artifacts_dir / "control"
    control.mkdir(parents=True, exist_ok=True)
    for marker in ["late-native-identity", "drop-stop-proof"]:
        (control / marker).touch()
    (control / "mode").write_text("late-native-identity")
    home = run.boxteam_home_for_node(request.node.nodeid)
    monkeypatch.setenv("BOXTEAM_HOME", str(home))
    async with (
        terminal_plugin_gateway_for_workspace(
            workspace_root=root, artifacts_dir=run.artifacts_dir,
            gateway_port=integration_backend_port + 68, boxteam_home=home,
            prepared_package_root=prepared_developer_tools_package,
        ) as plugin,
        workspace_tool_runtime(root) as (container, session_id, context),
    ):
        # 由本例管理请求独占核实，不让周期 steering 提前消费原生终态。
        await container.terminal_steering_service.shutdown()
        owner, store, leases = container.terminal_resource_owner, container.attachment_blob_store, container.external_resource_leases
        message = await container.message_service.create(session_id, MessageCreateRequest(
            content="启动待核实目录", attachments=[upload(b"original-before-unverified-startup")],
        ))
        ref, = message.attachments
        assert ref.directory_uri is not None and context.thread_binding is not None
        directory = store.resolve_directory_uri(parse_resource_uri(ref.directory_uri))
        attachment_id = parse_resource_uri(ref.directory_uri).tail_segments[0]
        with pytest.raises(TerminalRequestError) as rejected:
            await owner.create_terminal(
                session_id=session_id, thread_id=context.thread_binding.thread_id,
                holder_id="request:directory-startup", operation_id="directory-startup",
                cwd=file_uri_child(ref.directory_uri, "derived"),
            )
        assert rejected.value.status == 500 and rejected.value.creation_receipt is None
        record, = owner._store.all()
        rid = record.resource_id
        assert record.working_directory_uri == ref.directory_uri
        assert record.reconciliation_status == "unknown"
        operation_lease, = leases.leases_for_resource(rid)
        directory_lease, = leases.leases_for_resource(attachment_id)
        assert operation_lease.status == "reconcile_required" and directory_lease.status == "active"
        assert directory_lease.turn_stream_id == directory_lease.operation_id == rid
        assert (directory_lease.holder_session_id, directory_lease.holder_thread_id) == (session_id, message.thread_id)
        observed = await wait_observed(control)
        assert all(not is_executing(identity) for identity in observed.values())
        original_workload = (control / "workload.json").read_bytes()
        state_file = root / ".boxteam/terminal-manager/terminals.json"
        native, = json.loads(state_file.read_text())["terminals"]
        assert native["status"] == "created" and native["reconciliation_status"] == "unknown"
        assert native["os_pid"] == observed["root"]["pid"]
        store.release_thread_references(session_id, message.thread_id)
        assert await store.process_pending_directories() == ()
        assert (directory / "original/content").read_bytes() == b"original-before-unverified-startup"

        if cold:
            # 只强杀 ready 回执中的原插件进程，再由原 Gateway runtime 重启此绑定。
            process_id = plugin.instance["process_id"]
            assert type(process_id) is int
            original_identity = process_identity(process_id)
            assert original_identity is not None
            os.kill(process_id, signal.SIGKILL)
            async with asyncio.timeout(5):
                while is_executing(original_identity):
                    await asyncio.sleep(0.02)
            await plugin.restart()
        unknown = await owner.get_resource(rid)
        assert unknown["reconciliation_status"] == "unknown"
        assert leases.get_lease(operation_lease.lease_id) == operation_lease
        assert leases.get_lease(directory_lease.lease_id) == directory_lease
        runtime = await owner._transport.get_resource(rid)
        for field in ("os_pid", "process_group_id", "process_session_id", "process_start_time", "created_at"):
            assert runtime[field] == native[field]
        assert runtime["status"] == "created" and runtime["reconciliation_status"] == "unknown"
        assert await store.process_pending_directories() == ()

        revision = unknown["owner_revision"]
        assert type(revision) is int
        stopped = await owner.command(
            rid, action="stop", expected_revision=revision,
            holder_id="request:directory-proof", operation_id="directory-proof",
        )
        assert stopped["status"] == "terminated" and stopped["reconciliation_status"] == "verified"
        final_native, = json.loads(state_file.read_text())["terminals"]
        assert final_native["terminal_id"] == rid
        assert final_native["status"] == "terminated" and final_native["reconciliation_status"] == "verified"
        for field in ("os_pid", "process_group_id", "process_session_id", "process_start_time", "created_at"):
            assert final_native[field] == native[field]
        assert not leases.leases_for_resource(rid) and not leases.leases_for_resource(attachment_id)
        cold_ledger = ExternalResourceLeaseLedger(state_path=root / ".boxteam/resources.json")
        for lease in (operation_lease, directory_lease):
            settled = cold_ledger.get_lease(lease.lease_id)
            assert settled is not None and settled.status == "settled"
            assert (settled.resource_id, settled.turn_stream_id, settled.operation_id) == (
                lease.resource_id, lease.turn_stream_id, lease.operation_id,
            )
        assert await store.process_pending_directories() == ()
        assert not directory.exists()
        assert (control / "workload.json").read_bytes() == original_workload
        receipt, = (control / "stop-receipts.jsonl").read_text().splitlines()
        assert json.loads(receipt)["pid"] == native["os_pid"]
        assert all(not is_executing(identity) for identity in observed.values())
        revision = stopped["owner_revision"]
        assert type(revision) is int
        detached = await owner.command(
            rid, action="detach", expected_revision=revision,
            thread=ThreadRef(session_id, message.thread_id),
            holder_id="request:directory-detach", operation_id="directory-detach",
        )
        revision = detached["owner_revision"]
        assert type(revision) is int
        deleted = await owner.command(
            rid, action="delete", expected_revision=revision,
            holder_id="request:directory-delete", operation_id="directory-delete",
        )
        assert deleted["status"] == "deleted" and deleted["reconciliation_status"] == "verified"
        run.write_result("passed", {
            "cold_node": cold, "original_native": native, "original_operation_lease": operation_lease.lease_id,
            "original_directory_lease": directory_lease.lease_id, "stopped": stopped, "deleted": deleted,
            "scope": "真实 Python 创建/上传/管理及 Node HTTP/PTY；停止证明丢失，仅 Node 硬重启，不代 Python Backend 重启或无身份接管",
        })
