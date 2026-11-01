"""创建回执前强杀 Python；冷 owner 不重放创建，沿原管理链核实占用。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path

import psutil
import pytest

from app.abstractions.workspace_resources import ResourceKind, ResourceRequestError
from app.container import WorkspaceContainerRegistry
from app.core.mounted_workspaces import MountedWorkspaceRegistry
from app.core.path_utils import catalog_provider_scope
from app.services.business.workspace_resources.owner import ResourceConflict
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from tests.integration.workspace_services.test_resource_owner_crash_recovery import (
    _assert_force_terminated,
    _publish_barrier,
    _spawn,
    _startup_mount,
)
from tests.integration.workspace_services.test_resource_owner_crash_recovery import (
    resource_processes as resource_processes,  # noqa: PLC0414 - 原真实双服务装具按本文件隔离端口/工作区。
)
from tests.support.workspace_tool_runtime import workspace_tool_runtime


async def _create_worker(workspace: Path, barrier: Path, kind: ResourceKind, happened: bool) -> None:
    async with workspace_tool_runtime(workspace, startup_mount=_startup_mount(workspace)) as (container, sid, context):
        await container.terminal_steering_service.shutdown()
        binding = context.thread_binding
        assert binding is not None
        owner = container.terminal_resource_owner if kind == "terminal" else container.browser_resource_owner
        method = f"create_{kind}"
        original = getattr(owner._transport, method)

        async def receive(**arguments: object) -> dict[str, object]:
            runtime = await original(**arguments) if happened else None
            rid = str(arguments[f"{kind}_id"])
            record = owner._store.get(rid)
            assert record is not None and record.status == "creating"
            lease, = owner._leases.leases_for_resource(rid)
            assert lease.status == "dispatching"
            _publish_barrier(barrier, {
                "pid": os.getpid(), "rid": rid, "session_id": sid,
                "thread_id": binding.thread_id,
                "lease": asdict(lease), "runtime": runtime,
                "owner_revision": record.owner_revision, "happened": happened,
            })
            # 只暂停原 transport 边界；父进程强杀后没有 finally 或测试代写恢复事实。
            await asyncio.Event().wait()
            raise AssertionError("创建回执不得恢复")

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(owner._transport, method, receive)
            if kind == "terminal":
                await container.terminal_resource_owner.create_terminal(
                    session_id=sid, thread_id=binding.thread_id,
                    holder_id="request:crash-create", operation_id="crash-create",
                )
            else:
                await container.browser_resource_owner.create_browser(
                    session_id=sid, thread_id=binding.thread_id,
                    holder_id="request:crash-create", operation_id="crash-create",
                )


async def _recover_worker(workspace: Path, barrier: Path, output: Path, kind: ResourceKind) -> None:
    identity = json.loads(barrier.read_text())
    rid, original_lease = identity["rid"], identity["lease"]
    mount = _startup_mount(workspace)
    with (
        WorkspaceContainerRegistry(
            mounted_workspaces=MountedWorkspaceRegistry([mount]), project_root=Path.cwd(),
        ) as registry,
        catalog_provider_scope(registry),
        ExitStack() as cleanup,
    ):
        container = registry.get(mount.workspace_id)
        cleanup.callback(container.session_catalog.close)
        cleanup.callback(container.workspace_activity_service.store.close)
        owner = container.terminal_resource_owner if kind == "terminal" else container.browser_resource_owner
        calls: list[str] = []
        create = getattr(owner._transport, f"create_{kind}")
        stop, delete = owner._transport.stop_resource, owner._transport.delete_resource
        cancel = owner._transport.cancel_creation

        async def creating(**arguments: object) -> dict[str, object]:
            calls.append("create")
            return await create(**arguments)

        async def stopping(resource_id: str, *, reason: str | None = None) -> dict[str, object]:
            calls.append("stop")
            return await stop(resource_id, reason=reason)

        async def deleting(resource_id: str) -> dict[str, object]:
            calls.append("delete")
            return await delete(resource_id)

        async def cancelling(resource_id: str, *, owner_revision: int) -> dict[str, object]:
            calls.append("cancel_creation")
            return await cancel(resource_id, owner_revision=owner_revision)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(owner._transport, f"create_{kind}", creating)
            patch.setattr(owner._transport, "stop_resource", stopping)
            patch.setattr(owner._transport, "delete_resource", deleting)
            patch.setattr(owner._transport, "cancel_creation", cancelling)
            restored = await owner.get_resource(rid)
            expected_status = "reconcile_required" if identity["happened"] else "unknown"
            assert restored["source_thread"] == {
                "session_id": identity["session_id"], "thread_id": identity["thread_id"],
            }
            assert restored["thread_associations"] == [restored["source_thread"]]
            assert restored["reconciliation_status"] == expected_status
            lease = owner._leases.get_lease(original_lease["lease_id"])
            assert lease is not None and lease.status == "reconcile_required"
            assert {key: value for key, value in asdict(lease).items() if key not in {"status", "updated_at"}} == {
                key: value for key, value in original_lease.items() if key not in {"status", "updated_at"}
            }
            listed = next(item for item in await owner.list_resources() if item["resource_id"] == rid)
            assert listed["reconciliation_status"] == expected_status
            assert (await owner.get_resource(rid))["owner_revision"] == restored["owner_revision"]
            assert calls == []
            actions = restored["available_actions"]
            assert isinstance(actions, list)
            revision = restored["owner_revision"]
            assert type(revision) is int
            result: dict[str, object] = {"pid": os.getpid(), "restored": restored, "listed": listed}
            if identity["happened"]:
                runtime = await owner._transport.get_resource(rid)
                assert runtime[f"{kind}_id"] == rid
                identity_fields = ("os_pid", "pty_worker_pid", "created_at") if kind == "terminal" else (
                    "active_page_id", "created_at",
                )
                for field in identity_fields:
                    assert runtime[field] == identity["runtime"][field]
                assert "stop" in actions
                with pytest.raises(ResourceConflict, match="原 operation"):
                    await owner.command(rid, action="stop", expected_revision=revision,
                        holder_id="request:crash-create", operation_id="crash-create")
                assert calls == []
                stopped = await owner.command(rid, action="stop", expected_revision=revision,
                    holder_id="request:creation-proof", operation_id="creation-proof")
                assert stopped["status"] == ("terminated" if kind == "terminal" else "closed")
                assert stopped["reconciliation_status"] == "verified"
                assert calls == ["stop"]
                assert not owner._leases.leases_for_resource(rid)
                cold = ExternalResourceLeaseLedger(state_path=workspace / ".boxteam/resources.json")
                settled = cold.get_lease(lease.lease_id)
                assert settled is not None and settled.status == "settled"
                assert {key: value for key, value in asdict(settled).items() if key not in {"status", "updated_at"}} == {key: value for key, value in asdict(lease).items() if key not in {"status", "updated_at"}}
                await owner.cleanup_session(identity["session_id"])
                assert (await owner._transport.get_resource(rid))["status"] == "deleted"
                assert calls == ["stop", "delete"]
                assert rid not in {item["resource_id"] for item in await owner.list_resources()}
                result["stopped"] = stopped
            else:
                assert restored["status"] == "creating"
                assert actions == ["stop"]
                # 清单和精确详情都没有实例，也不能据此结清已丢失的创建回执。
                assert await owner._transport.list_resources() == []
                with pytest.raises(ResourceConflict, match="原 operation"):
                    await owner.command(rid, action="stop", expected_revision=revision,
                        holder_id="request:crash-create", operation_id="crash-create")
                assert calls == []
                assert owner._leases.get_lease(lease.lease_id) == lease
                cold = ExternalResourceLeaseLedger(state_path=workspace / ".boxteam/resources.json")
                assert cold.get_lease(lease.lease_id) == lease
                assert (await owner.get_resource(rid))["reconciliation_status"] == "unknown"
                cancelled = await owner.command(rid, action="stop", expected_revision=revision,
                    holder_id="request:missing-creation-proof", operation_id="missing-creation-proof")
                assert cancelled["status"] == "deleted" and cancelled["reconciliation_status"] == "verified"
                assert cancelled["thread_associations"] == [] and cancelled["source_thread"] == restored["source_thread"]
                assert calls == ["cancel_creation"] and not owner._leases.leases_for_resource(rid)
                settled = ExternalResourceLeaseLedger(state_path=workspace / ".boxteam/resources.json").get_lease(lease.lease_id)
                assert settled is not None and settled.status == "settled"
                assert settled.operation_id == lease.operation_id and settled.turn_stream_id == lease.turn_stream_id
                runtime = await owner._transport.get_resource(rid)
                assert runtime["release_reason"] == "creation_cancelled" and runtime["status"] == "deleted"
                assert runtime["reconciliation_status"] == "verified"
                # 正式旧请求迟到也不得创建；直接调用原transport以区别请求尝试与真实副作用。
                with pytest.raises(ResourceRequestError):
                    if kind == "terminal":
                        await create(terminal_id=rid, owner_revision=revision, title="迟到创建", cwd=str(workspace))
                    else:
                        await create(browser_id=rid, owner_revision=revision, title="迟到创建", url="about:blank")
                assert [item[f"{kind}_id"] for item in await owner._transport.list_resources()] == [rid]
                assert rid not in {item["resource_id"] for item in await owner.list_resources()}
                result["cancelled"] = cancelled
            result["side_effects"] = calls
            output.write_text(json.dumps(result, ensure_ascii=False, indent=2))


@pytest.mark.parametrize("kind", ["terminal", "browser"])
@pytest.mark.parametrize("happened", [False, True], ids=["before-dispatch", "receipt-lost"])
def test_creation_hard_restart_preserves_exact_lease_until_original_owner_proof(
    request: pytest.FixtureRequest, resource_processes: tuple[Path, Path], kind: ResourceKind, happened: bool,
) -> None:
    workspace, runtime = resource_processes
    barrier, output = runtime / "creation-paused.json", runtime / "recovered.json"
    test_file = str(Path(request.node.path).resolve())
    with ExitStack() as stack:
        worker = _spawn(stack,
            [sys.executable, test_file, "create", str(workspace), str(barrier), kind, str(int(happened))],
            cwd=Path.cwd(), logs=runtime / "creation-worker")
        deadline = time.monotonic() + 30
        while not barrier.exists() and time.monotonic() < deadline:
            assert worker.poll() is None, (runtime / "creation-worker.stderr.log").read_text()
            time.sleep(0.05)
        assert barrier.exists(), "创建未到达原transport派发/回执边界"
        identity = json.loads(barrier.read_text())
        assert identity["pid"] == worker.pid and identity["happened"] is happened
        paths = [workspace / ".boxteam" / name for name in (f"{kind}-resources.json", "resources.json")]
        original_bytes = [path.read_bytes() for path in paths]
        worker.kill()
        _assert_force_terminated(worker)
        assert [path.read_bytes() for path in paths] == original_bytes
        recovered = _spawn(stack,
            [sys.executable, test_file, "recover", str(workspace), str(barrier), kind, str(output)],
            cwd=Path.cwd(), logs=runtime / "recovery-worker")
        assert recovered.wait(timeout=40) == 0, (runtime / "recovery-worker.stderr.log").read_text()
        receipt = json.loads(output.read_text())
        assert receipt["pid"] != identity["pid"]
        assert receipt["side_effects"] == (["stop", "delete"] if happened else ["cancel_creation"])
        if happened and kind == "terminal":
            running_pids = {process.info["pid"] for process in psutil.process_iter(attrs=["pid", "status"])
                            if process.info["status"] != psutil.STATUS_ZOMBIE}
            for field in ("os_pid", "pty_worker_pid"):
                pid = identity["runtime"][field]
                # Proto Struct 的 number 允许整数值以 float 交付；身份数值必须精确且为正。
                assert type(pid) in {int, float} and pid > 0 and pid == int(pid)
                assert int(pid) not in running_pids


if __name__ == "__main__":
    mode, workspace_arg, barrier_arg, kind_arg, final_arg = sys.argv[1:]
    assert kind_arg == "terminal" or kind_arg == "browser"
    if mode == "create":
        asyncio.run(_create_worker(Path(workspace_arg), Path(barrier_arg), kind_arg, bool(int(final_arg))))
    else:
        assert mode == "recover"
        asyncio.run(_recover_worker(Path(workspace_arg), Path(barrier_arg), Path(final_arg), kind_arg))
