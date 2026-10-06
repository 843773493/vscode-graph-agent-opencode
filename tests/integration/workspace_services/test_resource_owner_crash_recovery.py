"""真实 Node 资源与外置强杀；仅暂停 transport 回执，不伪造 owner 恢复。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import httpx
import pytest

from app.abstractions.workspace_resources import (
    ResourceTransportGrantRejected,
    RetentionScope,
)
from app.container import WorkspaceContainerRegistry
from app.core.mounted_workspaces import MountedWorkspace, MountedWorkspaceRegistry
from app.core.path_utils import catalog_provider_scope
from app.core.workspace_identity import load_or_create_workspace_id
from app.services.business.workspace_resources.owner import ResourceConflict
from app.services.business.workspace_resources.records import ThreadRef
from tests.harness.python.run_context import TestRunContext
from tests.support.ports import integration_port_block_for_file
from tests.support.process_scope import register_test_process
from tests.support.processes import terminate_process
from tests.support.workspace_tool_runtime import workspace_tool_runtime

ResourceKind = Literal["terminal", "browser"]


def _publish_barrier(path: Path, identity: dict[str, object]) -> None:
    pending = path.with_suffix(".pending")
    pending.write_text(json.dumps(identity), encoding="utf-8")
    pending.replace(path)


def _spawn(
    stack: ExitStack, command: list[str], *, cwd: Path, logs: Path,
) -> subprocess.Popen[str]:
    logs.parent.mkdir(parents=True, exist_ok=True)
    stdout = stack.enter_context(logs.with_suffix(".stdout.log").open("w"))
    stderr = stack.enter_context(logs.with_suffix(".stderr.log").open("w"))
    process = subprocess.Popen(command, cwd=cwd, stdout=stdout, stderr=stderr, text=True)
    cleanup = lambda: terminate_process(process)
    stack.callback(cleanup)
    register_test_process(process, cleanup)
    return process


def _wait_ready(process: subprocess.Popen[str], url: str) -> None:
    deadline = time.monotonic() + 30
    with httpx.Client(timeout=1, trust_env=False) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"资源服务提前退出: pid={process.pid}, exit={process.returncode}")
            try:
                response = client.get(url)
            except httpx.ConnectError:
                time.sleep(0.05)
                continue
            response.raise_for_status()
            return
    raise TimeoutError(f"资源服务未就绪: {url}")


@pytest.fixture
def resource_processes(
    request: pytest.FixtureRequest, integration_workspace_root_path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Path, Path]]:
    project = Path.cwd().resolve()
    workspace = Path(integration_workspace_root_path)
    runtime = TestRunContext.from_test_file(Path(request.node.path)).runtime_root_for_node(request.node.nodeid)
    runtime.mkdir(parents=True, exist_ok=True)
    temporary = runtime / "tmp"
    temporary.mkdir(exist_ok=True)
    for name in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.setenv(name, str(temporary))
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("真实资源恢复测试需要 Node")
    workspace_id = load_or_create_workspace_id(workspace)
    ports = integration_port_block_for_file(Path(request.node.path))
    monkeypatch.setenv("PYTHONPATH", str(project))
    with ExitStack() as stack:
        for kind, offset in (("terminal", 40), ("browser", 42)):
            port = ports.port(offset)
            monkeypatch.setenv(f"BOXTEAM_{kind.upper()}_BACKEND_URL", f"http://127.0.0.1:{port}")
            process = _spawn(
                stack,
                [node, "backend.js", "--host", "127.0.0.1", "--port", str(port),
                 "--workspace-root", str(workspace), "--workspace-id", workspace_id],
                cwd=project / "src" / "workspace-services" / kind / "server",
                logs=runtime / f"{kind}-node",
            )
            _wait_ready(process, f"http://127.0.0.1:{port}/health")
        yield workspace, runtime


async def _crash_worker(
    workspace: Path, barrier: Path, kind: ResourceKind, happened: bool,
    retention_scope: RetentionScope,
) -> None:
    async with workspace_tool_runtime(workspace) as (container, session_id, context):
        owner = container.terminal_resource_owner if kind == "terminal" else container.browser_resource_owner
        arguments = {
            "session_id": session_id, "thread_id": context.thread_binding.thread_id,
            "holder_id": "request:crash-create", "operation_id": "crash-create",
            "retention_scope": retention_scope,
        }
        if kind == "terminal":
            created = await container.terminal_resource_owner.create_terminal(**arguments)
        else:
            created = await container.browser_resource_owner.create_browser(**arguments)
        rid = str(created["resource_id"])
        original_stop = owner._transport.stop_resource

        async def pause_receipt(resource_id: str, *, reason: str | None = None) -> dict[str, object]:
            if happened:
                await original_stop(resource_id, reason=reason)
            leases = owner._leases.leases_for_resource(rid)
            assert len(leases) == 1 and leases[0].status == "active"
            _publish_barrier(barrier, {
                "pid": os.getpid(), "rid": rid, "session_id": session_id,
                "thread_id": context.thread_binding.thread_id,
                "lease_id": leases[0].lease_id, "happened": happened,
                "retention_scope": retention_scope,
            })
            # owner 尚未取得任何停止回执；父进程在此窗口 SIGKILL，不走 finally。
            await asyncio.Event().wait()
            raise AssertionError("暂停回执不得恢复执行")

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(owner._transport, "stop_resource", pause_receipt)
            await owner.command(
                rid, action="stop", expected_revision=int(created["owner_revision"]),
                holder_id="request:unknown-stop", operation_id="unknown-stop",
            )


async def _recover_worker(workspace: Path, barrier: Path, output: Path, kind: ResourceKind) -> None:
    identity = json.loads(barrier.read_text(encoding="utf-8"))
    rid = identity["rid"]
    workspace_id = load_or_create_workspace_id(workspace)
    with (
        WorkspaceContainerRegistry(
            mounted_workspaces=MountedWorkspaceRegistry([MountedWorkspace(workspace_id, workspace)]),
            project_root=Path.cwd(),
        ) as registry,
        catalog_provider_scope(registry),
    ):
        container = registry.get(workspace_id)
        owner = container.terminal_resource_owner if kind == "terminal" else container.browser_resource_owner
        original_stop = owner._transport.stop_resource
        original_delete = owner._transport.delete_resource
        calls: list[str] = []

        async def counted_stop(resource_id: str, *, reason: str | None = None) -> dict[str, object]:
            calls.append("stop")
            return await original_stop(resource_id, reason=reason)

        async def counted_delete(resource_id: str) -> dict[str, object]:
            calls.append("delete")
            return await original_delete(resource_id)

        try:
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(owner._transport, "stop_resource", counted_stop)
                patch.setattr(owner._transport, "delete_resource", counted_delete)
                resource = await owner.get_resource(rid)
                lease = owner._leases.get_lease(identity["lease_id"])
                assert lease is not None
                observed = await owner._transport.get_resource(rid)
                assert observed[f"{kind}_id"] == rid
                assert resource["source_thread"] == {
                    "session_id": identity["session_id"], "thread_id": identity["thread_id"],
                }
                assert resource["retention_scope"] == identity["retention_scope"]
                if identity["happened"]:
                    assert resource["status"] == ("terminated" if kind == "terminal" else "closed")
                    assert resource["reconciliation_status"] == "verified"
                    assert lease.status == "settled"
                else:
                    assert resource["reconciliation_status"] == "reconcile_required"
                    assert lease.status == "reconcile_required"
                    assert observed["status"] not in {"terminated", "closed", "deleted"}
                    with pytest.raises(ResourceConflict, match="尚待 owner 核实"):
                        await owner.command(
                            rid, action="stop", expected_revision=int(resource["owner_revision"]),
                            holder_id="request:unknown-stop", operation_id="unknown-stop",
                        )
                    assert (await owner.get_resource(rid))["reconciliation_status"] == "reconcile_required"
                    assert rid in {item["resource_id"] for item in await owner.list_resources()}
                assert calls == []
                result = {
                    "pid": os.getpid(), "resource": resource, "transport": observed,
                    "lease_status": lease.status, "recovery_side_effects": list(calls),
                }
                if not identity["happened"]:
                    # 单独取得真实终态证据，再核实旧 lease；不重放结果未知的操作。
                    await original_stop(rid)
                    await owner.command(
                        rid, action="stop", expected_revision=int(resource["owner_revision"]),
                        holder_id="request:explicit-proof", operation_id="explicit-proof",
                    )
                    assert calls == []
                await owner.cleanup_session(identity["session_id"])
                if identity["retention_scope"] == "workspace":
                    # Session 释放只解除关联，工作区保留资源须由 owner 显式删除。
                    retained = await owner.get_resource(rid)
                    assert retained["retention_scope"] == "workspace"
                    assert retained["source_thread"] == resource["source_thread"]
                    assert retained["thread_associations"] == []
                    assert retained["status"] == ("terminated" if kind == "terminal" else "closed")
                    assert retained["reconciliation_status"] == "verified"
                    assert rid in {item["resource_id"] for item in await owner.list_resources()}
                    assert owner._leases.leases_for_resource(rid) == []
                    assert (await owner._transport.get_resource(rid))["status"] == retained["status"]
                    assert calls == []
                    result["after_session_cleanup"] = retained
                    await owner.command(
                        rid, action="delete", expected_revision=int(retained["owner_revision"]),
                        holder_id="request:workspace-cleanup", operation_id="workspace-cleanup",
                    )
                assert (await owner.get_resource(rid))["status"] == "deleted"
                assert owner._leases.leases_for_resource(rid) == []
                assert rid not in {item["resource_id"] for item in await owner.list_resources()}
                assert (await owner._transport.get_resource(rid))["status"] == "deleted"
                result["cleanup_status"] = "deleted"
                output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            container.workspace_activity_service.store.close()
            container.session_catalog.close()


async def _orphan_worker(workspace: Path, barrier: Path, kind: ResourceKind, reattach: bool) -> None:
    async with workspace_tool_runtime(workspace) as (container, session_id, context):
        owner = container.terminal_resource_owner if kind == "terminal" else container.browser_resource_owner
        arguments = {
            "session_id": session_id, "thread_id": context.thread_binding.thread_id,
            "holder_id": "request:orphan-create", "operation_id": "orphan-create",
        }
        if kind == "terminal":
            created = await container.terminal_resource_owner.create_terminal(**arguments)
        else:
            created = await container.browser_resource_owner.create_browser(**arguments)
        start = datetime(2026, 10, 1, tzinfo=UTC)
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(owner, "_clock", lambda: start)
            detached = await owner.command(
                str(created["resource_id"]), action="detach",
                expected_revision=int(created["owner_revision"]),
                thread=ThreadRef(session_id, context.thread_binding.thread_id),
                holder_id="request:orphan-detach", operation_id="orphan-detach",
            )
            assert detached["thread_associations"] == []
            assert detached["unattached_since"] == start.isoformat()
            assert detached["reclaim_at"] == (start + timedelta(minutes=30)).isoformat()
            _publish_barrier(barrier, {
                "pid": os.getpid(), "rid": created["resource_id"], "session_id": session_id,
                "thread_id": context.thread_binding.thread_id, "reattach": reattach,
                "unattached_since": detached["unattached_since"], "reclaim_at": detached["reclaim_at"],
            })
            await asyncio.Event().wait()


async def _recover_orphan_worker(workspace: Path, barrier: Path, output: Path, kind: ResourceKind) -> None:
    identity = json.loads(barrier.read_text())
    rid = identity["rid"]
    start = datetime.fromisoformat(identity["unattached_since"])
    moment = [start + timedelta(minutes=29, seconds=59)]
    workspace_id = load_or_create_workspace_id(workspace)
    with (
        WorkspaceContainerRegistry(
            mounted_workspaces=MountedWorkspaceRegistry([MountedWorkspace(workspace_id, workspace)]),
            project_root=Path.cwd(),
        ) as registry,
        catalog_provider_scope(registry),
    ):
        container = registry.get(workspace_id)
        owner = container.terminal_resource_owner if kind == "terminal" else container.browser_resource_owner
        try:
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(owner, "_clock", lambda: moment[0])
                resource = await owner.get_resource(rid)
                assert resource["unattached_since"] == identity["unattached_since"]
                assert resource["reclaim_at"] == identity["reclaim_at"]
                assert resource["thread_associations"] == []
                assert resource["reconciliation_status"] == "verified"
                assert owner._leases.leases_for_resource(rid) == []
                assert await owner.sweep_orphans() == []
                if identity["reattach"]:
                    attached = await owner.command(
                        rid, action="attach", expected_revision=int(resource["owner_revision"]),
                        thread=ThreadRef(identity["session_id"], identity["thread_id"]),
                        holder_id="request:orphan-attach", operation_id="orphan-attach",
                    )
                    assert attached["thread_associations"] == [resource["source_thread"]]
                    assert attached["unattached_since"] is None and attached["reclaim_at"] is None
                moment[0] = start + timedelta(minutes=30)
                reclaimed = await owner.sweep_orphans()
                if identity["reattach"]:
                    assert reclaimed == []
                    assert (await owner.get_resource(rid))["status"] not in {"terminated", "closed", "deleted"}
                else:
                    assert [item["resource_id"] for item in reclaimed] == [rid]
                    assert reclaimed[0]["status"] == "deleted"
                    assert reclaimed[0]["reconciliation_status"] == "verified"
                result = {
                    "pid": os.getpid(), "restored": resource, "reattach": identity["reattach"],
                    "reclaimed_at_30_minutes": [item["resource_id"] for item in reclaimed],
                }
                await owner.cleanup_session(identity["session_id"])
                assert (await owner._transport.get_resource(rid))["status"] == "deleted"
                assert rid not in {item["resource_id"] for item in await owner.list_resources()}
                output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            container.workspace_activity_service.store.close()
            container.session_catalog.close()


@pytest.mark.parametrize("kind", ["terminal", "browser"])
@pytest.mark.parametrize("happened", [True, False])
@pytest.mark.parametrize("retention_scope", ["thread", "workspace"])
def test_sigkill_unknown_stop_new_process_preserves_owner_and_does_not_replay(
    request: pytest.FixtureRequest, resource_processes: tuple[Path, Path],
    kind: ResourceKind, happened: bool, retention_scope: RetentionScope,
) -> None:
    workspace, runtime = resource_processes
    barrier = runtime / "stop-receipt-paused.json"
    result = runtime / "recovered.json"
    test_file = str(Path(request.node.path).resolve())
    with ExitStack() as stack:
        child = _spawn(
            stack, [sys.executable, test_file, "crash", str(workspace), str(barrier),
                    kind, str(int(happened)), retention_scope],
            cwd=Path.cwd(), logs=runtime / "crash-worker",
        )
        deadline = time.monotonic() + 45
        while not barrier.exists() and time.monotonic() < deadline:
            assert child.poll() is None, (runtime / "crash-worker.stderr.log").read_text()
            time.sleep(0.05)
        assert barrier.exists(), "未进入真实副作用/未知回执窗口"
        identity = json.loads(barrier.read_text())
        assert identity["pid"] == child.pid and identity["happened"] is happened
        assert identity["retention_scope"] == retention_scope
        assert child.poll() is None
        record = next(item for item in json.loads((workspace / ".boxteam" / f"{kind}-resources.json").read_text())["records"] if item["resource_id"] == identity["rid"])
        assert record["status"] == "stopping"
        assert record["retention_scope"] == retention_scope
        ledger = json.loads((workspace / ".boxteam" / "resources.json").read_text())
        assert next(item for item in ledger["leases"] if item["lease_id"] == identity["lease_id"])["status"] == "active"
        child.kill()
        assert child.wait(timeout=10) == -signal.SIGKILL
        # 恢复进程尚未启动：活实例已经有持久权威记录，不能靠重扫外部实例补造。
        killed_record = next(item for item in json.loads((workspace / ".boxteam" / f"{kind}-resources.json").read_text())["records"] if item["resource_id"] == identity["rid"])
        assert killed_record == record
        assert json.loads((workspace / ".boxteam" / "resources.json").read_text()) == ledger
        backend = os.environ[f"BOXTEAM_{kind.upper()}_BACKEND_URL"]
        with httpx.Client(timeout=5, trust_env=False) as client:
            response = client.get(
                f"{backend}/api/{kind}s/{identity['rid']}",
                headers={
                    "X-BoxTeam-Workspace-Id": load_or_create_workspace_id(workspace),
                    "X-BoxTeam-Resource-Owner": f"workspace-{kind}",
                },
            )
            response.raise_for_status()
            observed = response.json()["data"]
        assert observed[f"{kind}_id"] == killed_record["resource_id"]
        assert (observed["status"] in {"terminated", "closed"}) is happened
        recovered = _spawn(
            stack, [sys.executable, test_file, "recover", str(workspace), str(barrier), kind, str(result)],
            cwd=Path.cwd(), logs=runtime / "recovery-worker",
        )
        assert recovered.wait(timeout=45) == 0, (runtime / "recovery-worker.stderr.log").read_text()
        receipt = json.loads(result.read_text())
        assert receipt["pid"] != identity["pid"]
        assert receipt["resource"]["resource_id"] == identity["rid"]
        assert receipt["resource"]["retention_scope"] == retention_scope
        assert receipt["recovery_side_effects"] == []
        assert receipt["cleanup_status"] == "deleted"


@pytest.mark.parametrize("kind", ["terminal", "browser"])
@pytest.mark.parametrize("reattach", [True, False])
def test_sigkill_restart_keeps_original_orphan_deadline_and_exact_reclaim_boundary(
    request: pytest.FixtureRequest, resource_processes: tuple[Path, Path],
    kind: ResourceKind, reattach: bool,
) -> None:
    workspace, runtime = resource_processes
    barrier = runtime / "detached-orphan.json"
    output = runtime / "recovered-orphan.json"
    test_file = str(Path(request.node.path).resolve())
    with ExitStack() as stack:
        child = _spawn(
            stack, [sys.executable, test_file, "orphan", str(workspace), str(barrier), kind, str(int(reattach))],
            cwd=Path.cwd(), logs=runtime / "orphan-worker",
        )
        deadline = time.monotonic() + 45
        while not barrier.exists() and time.monotonic() < deadline:
            assert child.poll() is None, (runtime / "orphan-worker.stderr.log").read_text()
            time.sleep(0.05)
        assert barrier.exists(), "未完成真实 detach 的持久化"
        identity = json.loads(barrier.read_text())
        assert identity["pid"] == child.pid
        child.kill()
        assert child.wait(timeout=10) == -signal.SIGKILL
        recovered = _spawn(
            stack, [sys.executable, test_file, "recover-orphan", str(workspace), str(barrier), kind, str(output)],
            cwd=Path.cwd(), logs=runtime / "orphan-recovery-worker",
        )
        assert recovered.wait(timeout=45) == 0, (runtime / "orphan-recovery-worker.stderr.log").read_text()
        receipt = json.loads(output.read_text())
        assert receipt["pid"] != identity["pid"]
        assert receipt["restored"]["reclaim_at"] == identity["reclaim_at"]
        assert receipt["reclaimed_at_30_minutes"] == ([] if reattach else [identity["rid"]])


@pytest.mark.parametrize("race", ["closed", "revision"])
@pytest.mark.asyncio
async def test_real_browser_target_rejection_keeps_owner_verified_and_grant_unissued(
    resource_processes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, race: str,
) -> None:
    workspace, runtime = resource_processes
    async with workspace_tool_runtime(workspace) as (container, session_id, context):
        owner = container.browser_resource_owner
        browser = await owner.create_browser(
            session_id=session_id, thread_id=context.thread_binding.thread_id,
            holder_id="request:grant-race-create", operation_id="grant-race-create",
            retention_scope="workspace",
        )
        rid = str(browser["resource_id"])
        revision = int(browser["owner_revision"])
        issue = owner._transport.issue_transport_grant
        rejected: list[dict[str, object]] = []

        async def competing_issue(
            resource_id: str, *, owner_revision: int, association: str | None,
        ) -> dict[str, object]:
            # owner 已完成 get 预检；只在真实 grant HTTP 前改目标事实，不替代回执。
            if race == "closed":
                await owner._transport.stop_resource(resource_id)
            else:
                await owner._transport.sync_owner_revision(resource_id, owner_revision + 1)
            try:
                return await issue(resource_id, owner_revision=owner_revision, association=association)
            except ResourceTransportGrantRejected as error:
                rejected.append(error.snapshot)
                raise

        monkeypatch.setattr(owner._transport, "issue_transport_grant", competing_issue)
        with pytest.raises(ResourceConflict):
            await owner.transport_grant(
                rid, expected_revision=revision,
                holder_id="request:grant-race", operation_id="grant-race",
            )
        assert len(rejected) == 1
        receipt = rejected[0]
        assert receipt["browser_id"] == rid
        assert receipt["workspace_id"] == container.workspace_id
        assert receipt["reconciliation_status"] == "verified"
        assert "grant_token" not in receipt
        current = await owner.get_resource(rid)
        assert current["status"] == ("closed" if race == "closed" else "running")
        assert current["reconciliation_status"] == "verified"
        assert current["owner_revision"] == revision + (race == "revision")
        assert owner._leases.leases_for_resource(rid) == []
        persisted = next(item for item in json.loads(
            (workspace / ".boxteam/browser-resources.json").read_text(),
        )["records"] if item["resource_id"] == rid)
        assert persisted["reconciliation_status"] == "verified"
        assert persisted["owner_revision"] == current["owner_revision"]
        (runtime / "grant-rejection.json").write_text(json.dumps({
            "race": race, "rejection": receipt, "owner": current,
            "qualification": "real Node grant HTTP; only pre-request target race is injected",
        }, indent=2))


if __name__ == "__main__":
    mode, workspace_arg, barrier_arg, kind_arg, final_arg, *scope_args = sys.argv[1:]
    assert kind_arg in {"terminal", "browser"}
    if mode == "crash":
        assert len(scope_args) == 1 and scope_args[0] in {"thread", "workspace"}
        asyncio.run(_crash_worker(
            Path(workspace_arg), Path(barrier_arg), kind_arg, bool(int(final_arg)), scope_args[0],
        ))
    elif mode == "recover":
        asyncio.run(_recover_worker(Path(workspace_arg), Path(barrier_arg), Path(final_arg), kind_arg))
    elif mode == "orphan":
        asyncio.run(_orphan_worker(Path(workspace_arg), Path(barrier_arg), kind_arg, bool(int(final_arg))))
    else:
        assert mode == "recover-orphan"
        asyncio.run(_recover_orphan_worker(Path(workspace_arg), Path(barrier_arg), Path(final_arg), kind_arg))
