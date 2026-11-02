"""真实context已创建、回执尚未交付时硬退出，原创建意图仍可发现。"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Awaitable, Callable, Iterator
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path
from typing import cast
from uuid import uuid4

import httpx
import psutil
import pytest

from app.container import WorkspaceContainerRegistry
from app.core.mounted_workspaces import MountedWorkspace, MountedWorkspaceRegistry
from app.core.path_utils import catalog_provider_scope
from app.core.workspace_identity import load_or_create_workspace_id
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from tests.harness.python.browser_manager import (
    BrowserManagerProcesses,
    close_browser_manager_processes,
    start_browser_manager_processes,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.workspace_services.test_resource_owner_crash_recovery import (
    _assert_force_terminated,
    _publish_barrier,
    _spawn,
)
from tests.support.workspace_tool_runtime import workspace_tool_runtime
from tests.support.workspaces import prepare_default_test_workspace

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="真实Linux Chromium与SIGKILL，不代替Windows")
BrowserService = tuple[BrowserManagerProcesses, Path, Path, TestRunContext, list[psutil.Process]]


@pytest.fixture
def browser_service(
    request: pytest.FixtureRequest, integration_backend_port: int, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[BrowserService]:
    with TestRunContext.from_test_file(Path(request.node.path)) as run:
        runtime = run.runtime_root_for_node(request.node.nodeid)
        runtime.mkdir(parents=True, exist_ok=True)
        control = runtime / "control"
        control.mkdir()
        for name in ("TMPDIR", "TMP", "TEMP"):
            monkeypatch.setenv(name, str(runtime))
        root = prepare_default_test_workspace(
            workspace_root=run.workspace_root / uuid4().hex,
            template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
        )
        preload = control / "context-receipt-barrier.mjs"
        preload.write_text("""
import { writeFile } from 'node:fs/promises';
import { appendFileSync, existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { setTimeout as delay } from 'node:timers/promises';

if (path.basename(process.argv[1] || '') === 'backend.js') {
  const control = process.env.BOXTEAM_TEST_BROWSER_CREATION_CONTROL;
  const root = process.env.BOXTEAM_PROJECT_ROOT;
  if (!control || !root) throw new Error('Browser创建屏障缺少显式控制目录或项目根');
  const { BrowserRuntimePool } = await import(pathToFileURL(path.join(root,
    'src/workspace-services/browser/server/resources/browserRuntimePool.js')));
  const { BrowserSession } = await import(pathToFileURL(path.join(root,
    'src/workspace-services/browser/server/browserSession.js')));
  const stage = () => existsSync(path.join(control, 'stage'))
    ? readFileSync(path.join(control, 'stage'), 'utf8') : 'acquire';
  const pause = async () => {
    while (!existsSync(path.join(control, 'release'))) await delay(10);
  };
  const observe = async (browser, contexts, generation) => {
    await writeFile(path.join(control, 'arrived.json'), JSON.stringify({
      connected: browser.isConnected(), contexts, generation, version: browser.version(),
    }));
  };
  const acquire = BrowserRuntimePool.prototype.acquireContext;
  BrowserRuntimePool.prototype.acquireContext = async function (options) {
    const runtime = await acquire.call(this, options);
    const contextIndex = this.contexts.size;
    runtime.context.on('close', () => appendFileSync(path.join(control, 'closed.jsonl'),
      JSON.stringify({ context_index: contextIndex }) + '\\n'));
    // 只暂扣真实context或注册回执，不代填Session、磁盘记录或终态。
    if (stage() === 'acquire') {
      await observe(runtime.browser, this.contexts.size, runtime.chromiumInstanceGeneration);
      await pause();
    }
    return runtime;
  };
  const register = BrowserSession.prototype._registerPage;
  BrowserSession.prototype._registerPage = async function (page, options) {
    const entry = await register.call(this, page, options);
    if (stage() === 'registered') {
      await observe(this.browser, this.manager.runtimePool.contexts.size, this.chromiumInstanceGeneration);
      await pause();
    }
    return entry;
  };
}
""", encoding="utf-8")
        with monkeypatch.context() as env:
            env.setenv("NODE_OPTIONS", f"--import={preload.as_uri()}")
            env.setenv("BOXTEAM_TEST_BROWSER_CREATION_CONTROL", str(control))
            handle = start_browser_manager_processes(
                workspace_root=root, artifacts_dir=run.artifacts_dir / "original",
                backend_port=integration_backend_port + 68, frontend_port=integration_backend_port + 69,
                backend_host="127.0.0.1",
            )
        native: list[psutil.Process] = []
        try:
            yield handle, root, control, run, native
        finally:
            (control / "release").touch()
            try:
                close_browser_manager_processes(handle)
            finally:
                # psutil实例冻结创建时间，只兜底回收本fixture在硬退出前登记的后代。
                for process in native:
                    try:
                        if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                            process.kill()
                    except psutil.NoSuchProcess:
                        pass
                _, alive = psutil.wait_procs(native, timeout=5)
                assert not [p.pid for p in alive if p.is_running() and p.status() != psutil.STATUS_ZOMBIE]


async def test_rejected_close_and_delete_leave_original_creation_and_interaction_usable(
    browser_service: BrowserService,
) -> None:
    handle, root, control, run, _native = browser_service
    browser_id = f"browser_{uuid4().hex}"
    workspace_id = load_or_create_workspace_id(root)
    headers = {"X-BoxTeam-Workspace-Id": workspace_id, "X-BoxTeam-Resource-Owner": "workspace-browser"}
    state_file = root / ".boxteam/browser-manager/browsers.json"
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{handle.backend_port}", headers=headers, trust_env=False, timeout=15,
    ) as client:
        creating = asyncio.create_task(client.post("/api/browsers", json={
            "browser_id": browser_id, "workspace_id": workspace_id, "owner_revision": 7, "url": "about:blank",
        }))
        try:
            async with asyncio.timeout(8):
                while not (control / "arrived.json").is_file():
                    await asyncio.sleep(0.02)
            observed = json.loads((control / "arrived.json").read_text())
            assert observed["connected"] is True and observed["contexts"] == 1
            frozen = state_file.read_bytes()
            record, = json.loads(frozen)["browsers"]
            assert record["status"] == "created" and record["reconciliation_status"] == "unknown"
            for method, endpoint in (("POST", f"/api/browsers/{browser_id}/close"), ("DELETE", f"/api/browsers/{browser_id}")):
                refused = await client.request(method, endpoint)
                assert refused.status_code == 409 and refused.json()["code"] == "browser_creation_unverified", refused.text
                assert state_file.read_bytes() == frozen and not creating.done()
            # 只释放原生context回执，后续状态与页面均由同一产品入口生成。
            (control / "release").touch()
            created = await creating
            assert created.status_code == 200, created.text
            original = created.json()["data"]
            assert original["browser_id"] == browser_id and original["transport_owner_revision"] == 7
            assert original["status"] == "running" and original["reconciliation_status"] == "verified"
            assert len(original["pages"]) == 1
            navigation = await client.post(f"/api/browsers/{browser_id}/navigate", json={
                "type": "new_tab", "url": "about:blank",
            })
            assert navigation.status_code == 200, navigation.text
            current = navigation.json()["data"]["browser"]
            assert current["browser_id"] == browser_id and current["transport_owner_revision"] == 7
            assert len(current["pages"]) == 2
            assert {page["page_id"] for page in original["pages"]} < {page["page_id"] for page in current["pages"]}
            closed = await client.post(f"/api/browsers/{browser_id}/close", json={})
            assert closed.status_code == 200, closed.text
            assert closed.json()["data"]["browser"]["status"] == "closed"
            final, = json.loads(state_file.read_bytes())["browsers"]
            assert final["status"] == "closed" and final["reconciliation_status"] == "verified"
            health = (await client.get("/health")).json()
            assert health["browser_runtime"]["context_count"] == 0
            run.write_result("passed", {"observed": observed, "created": original, "after_rejection": current,
                "final": final, "scope": "真实Node/HTTP/Chromium；仅暂扣context回执，不代替Python owner、Gateway/Web、冷身份接管或Windows"})
        finally:
            (control / "release").touch()
            await asyncio.gather(creating, return_exceptions=True)


async def test_context_before_receipt_keeps_original_intent_after_hard_restart(browser_service: BrowserService) -> None:
    handle, root, control, run, native = browser_service
    browser_id = f"browser_{uuid4().hex}"
    workspace_id = load_or_create_workspace_id(root)
    url = f"http://127.0.0.1:{handle.backend_port}"
    headers = {"X-BoxTeam-Workspace-Id": workspace_id, "X-BoxTeam-Resource-Owner": "workspace-browser"}
    payload = {"browser_id": browser_id, "workspace_id": workspace_id, "owner_revision": 7, "url": "about:blank"}
    state_file = root / ".boxteam/browser-manager/browsers.json"
    async with httpx.AsyncClient(base_url=url, headers=headers, trust_env=False, timeout=15) as client:
        creating = asyncio.create_task(client.post("/api/browsers", json=payload))
        restarted = None
        try:
            async with asyncio.timeout(8):
                while not (control / "arrived.json").is_file():
                    await asyncio.sleep(0.02)
            observed = json.loads((control / "arrived.json").read_text())
            native.extend(psutil.Process(handle.backend_process.pid).children(recursive=True))
            processes = [{"pid": p.pid, "created_at": p.create_time(), "name": p.name()} for p in native]
            assert any("chrome" in p["name"].lower() or "chromium" in p["name"].lower() for p in processes)
            assert observed["connected"] is True and observed["contexts"] == 1 and observed["generation"] == 1
            record, = json.loads(state_file.read_bytes())["browsers"]
            assert record["browser_id"] == browser_id and record["transport_owner_revision"] == 7
            assert record["status"] == "created" and record["reconciliation_status"] == "unknown"
            assert record["runtime_generation"] is None and "started_at" not in record
            assert not creating.done()
            # 原创建还持有context回执时，管理入口也不能用空Session句柄假报停止。
            closing = await client.post(f"/api/browsers/{browser_id}/close", json={})
            assert closing.status_code == 409 and closing.json()["code"] == "browser_creation_unverified", closing.text
            before_crash, = json.loads(state_file.read_bytes())["browsers"]
            handle.backend_process.kill()
            assert await asyncio.to_thread(handle.backend_process.wait, timeout=5) == -9
            with pytest.raises(httpx.TransportError):
                await creating
            close_browser_manager_processes(handle)
            # 新进程沿原磁盘启动，不替产品补写创建记录、context身份或停止证明。
            restarted = await asyncio.to_thread(
                start_browser_manager_processes, workspace_root=root, artifacts_dir=run.artifacts_dir / "restarted",
                backend_port=handle.backend_port, frontend_port=handle.frontend_port, backend_host="127.0.0.1",
            )
            response = await client.get(f"/api/browsers/{browser_id}")
            assert response.status_code == 200, response.text
            cold = response.json()["data"]
            for field, value in before_crash.items():
                assert cold[field] == value, field
            for method, endpoint in (("POST", f"/api/browsers/{browser_id}/close"), ("DELETE", f"/api/browsers/{browser_id}")):
                refused = await client.request(method, endpoint)
                assert refused.status_code == 409 and refused.json()["code"] == "browser_creation_unverified", refused.text
            duplicate = await client.post("/api/browsers", json=payload)
            assert duplicate.status_code == 500 and "creation_receipt" not in duplicate.json(), duplicate.text
            final, = json.loads(state_file.read_bytes())["browsers"]
            for field in ("browser_id", "workspace_id", "transport_owner_revision", "status", "reconciliation_status", "created_at", "runtime_generation"):
                assert final[field] == record[field], field
            health = (await client.get("/health")).json()
            assert health["browser_count"] == 1 and health["browser_runtime"]["connected"] is False
            run.write_result("passed", {"observed": observed, "native": processes, "before_crash": before_crash,
                                        "cold": cold, "final": final,
                                        "scope": "真实Node/HTTP/Chromium、原context回执屏障与SIGKILL冷恢复；不代表无身份接管、Python lease、Windows或Gateway/Web"})
        finally:
            if not creating.done():
                creating.cancel()
                await asyncio.gather(creating, return_exceptions=True)
            if restarted is not None:
                # 冷unknown按合同阻止正常关闭；只终止本fixture持有的精确Node。
                if restarted.backend_process.poll() is None:
                    restarted.backend_process.kill()
                    await asyncio.to_thread(restarted.backend_process.wait, timeout=5)
                close_browser_manager_processes(restarted)


@pytest.mark.parametrize("stage", ["acquire", "registered"])
async def test_cancel_creation_waits_for_original_context_and_terminal_receipt(
    browser_service: BrowserService, stage: str,
) -> None:
    handle, root, control, run, _native = browser_service
    (control / "stage").write_text(stage)
    (control / "release").touch()
    workspace_id = load_or_create_workspace_id(root)
    target_id, peer_id = f"browser_{uuid4().hex}", f"browser_{uuid4().hex}"
    headers = {"X-BoxTeam-Workspace-Id": workspace_id, "X-BoxTeam-Resource-Owner": "workspace-browser"}
    state_file = root / ".boxteam/browser-manager/browsers.json"
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{handle.backend_port}", headers=headers, trust_env=False, timeout=15,
    ) as client:
        peer = await client.post("/api/browsers", json={
            "browser_id": peer_id, "workspace_id": workspace_id, "owner_revision": 1, "url": "about:blank",
        })
        assert peer.status_code == 200, peer.text
        (control / "release").unlink()
        (control / "arrived.json").unlink()
        creating = asyncio.create_task(client.post("/api/browsers", json={
            "browser_id": target_id, "workspace_id": workspace_id, "owner_revision": 7, "url": "about:blank",
        }))
        cancelling = None
        try:
            async with asyncio.timeout(8):
                while not (control / "arrived.json").exists():
                    await asyncio.sleep(0.02)
            observed = json.loads((control / "arrived.json").read_text())
            assert observed["connected"] is True and observed["contexts"] == 2
            wrong = await client.post(f"/api/browsers/{target_id}/cancel-creation", json={"owner_revision": 8})
            assert wrong.status_code == 200 and wrong.json()["data"]["outcome"] == "present", wrong.text
            assert wrong.json()["data"]["resource"]["transport_owner_revision"] == 7
            assert not (control / "closed.jsonl").exists() and not creating.done()
            synced = await client.post(f"/api/browsers/{target_id}/owner-revision", json={
                "owner_revision": 8, "for_stop": True,
            })
            assert synced.status_code == 200, synced.text
            cancelling = asyncio.create_task(client.post(f"/api/browsers/{target_id}/cancel-creation", json={"owner_revision": 8}))
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(cancelling), 0.1)
            pending = await client.get(f"/api/browsers/{target_id}")
            assert pending.status_code == 200, pending.text
            assert pending.json()["data"]["reconciliation_status"] == "unknown"
            peer_run = await client.post(f"/api/browsers/{peer_id}/run", json={"code": "return 42;"})
            assert peer_run.status_code == 200, peer_run.text
            assert not creating.done() and not cancelling.done()
            (control / "release").touch()
            cancelled, original = await asyncio.gather(cancelling, creating)
            assert cancelled.status_code == 200, cancelled.text
            receipt = cancelled.json()["data"]
            assert (receipt["resource_id"], receipt["workspace_id"], receipt["owner_revision"], receipt["outcome"]) == (
                target_id, workspace_id, 8, "present",
            )
            assert receipt["resource"]["status"] == "failed" and receipt["resource"]["reconciliation_status"] == "verified"
            assert receipt["resource"]["transport_owner_revision"] == 8
            assert original.status_code == (409 if stage == "acquire" else 500), original.text
            if stage == "acquire":
                assert original.json()["code"] == "browser_creation_cancelled"
            failure = original.json()["creation_receipt"]
            assert failure["resource_id"] == target_id and failure["owner_revision"] == 8 and failure["outcome"] == "failed"
            assert failure["resource"]["status"] == "failed" and failure["resource"]["reconciliation_status"] == "verified"
            assert [json.loads(line) for line in (control / "closed.jsonl").read_text().splitlines()] == [{"context_index": 2}]
            health = (await client.get("/health")).json()
            assert health["browser_runtime"]["context_count"] == 1
            target = next(item for item in json.loads(state_file.read_bytes())["browsers"] if item["browser_id"] == target_id)
            assert target["status"] == "failed" and target["reconciliation_status"] == "verified"
            assert target["transport_owner_revision"] == 8
            # 取消完成后走同一管理停止；原context只关闭一次，peer始终保持可用。
            repeated = await client.post(f"/api/browsers/{target_id}/cancel-creation", json={"owner_revision": 8})
            assert repeated.status_code == 200 and repeated.json()["data"]["outcome"] == "present", repeated.text
            closed = await client.post(f"/api/browsers/{target_id}/close", json={})
            assert closed.status_code == 200 and closed.json()["data"]["browser"]["status"] == "closed", closed.text
            assert (await client.post(f"/api/browsers/{peer_id}/run", json={"code": "return 43;"})).status_code == 200
            assert len((control / "closed.jsonl").read_text().splitlines()) == 1
            run.write_result("passed", {"stage": stage, "observed": observed, "cancelled": receipt,
                "creation_failure": failure, "persisted": target, "health": health,
                "scope": "真实Node/HTTP/Chromium；原context/注册回执屏障、管理版本同步及取消，不代Python lease、Backend重启、Gateway/Web或Windows"})
        finally:
            (control / "release").touch()
            await asyncio.gather(*[task for task in (creating, cancelling) if task is not None], return_exceptions=True)
            await client.post(f"/api/browsers/{peer_id}/close", json={})


async def _create_pending_browser(workspace: Path, control: Path, browser_url: str) -> None:
    mount = MountedWorkspace(load_or_create_workspace_id(workspace), workspace, browser_backend_url=browser_url)
    async with workspace_tool_runtime(workspace, startup_mount=mount) as (container, sid, context):
        await container.terminal_steering_service.shutdown()
        binding = context.thread_binding
        assert binding is not None
        owner = container.browser_resource_owner
        original = cast(Callable[..., Awaitable[dict[str, object]]], owner._transport.create_browser)

        async def creating(**arguments: object) -> dict[str, object]:
            rid = str(arguments["browser_id"])
            record = owner._store.get(rid)
            assert record is not None and record.status == "creating"
            lease, = owner._leases.leases_for_resource(rid)
            assert lease.status == "dispatching"
            _publish_barrier(control / "python-created.json", {
                "pid": os.getpid(), "rid": rid, "session_id": sid, "thread_id": binding.thread_id,
                "lease": asdict(lease), "owner_revision": record.owner_revision,
                "reconciliation_status": record.reconciliation_status,
            })
            return await original(**arguments)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(owner._transport, "create_browser", creating)
            await owner.create_browser(session_id=sid, thread_id=binding.thread_id,
                holder_id="request:pending-create", operation_id="pending-create")
        raise AssertionError("原创建应在Node真实启动屏障内等待，不得提前结束")


async def _recover_pending_browser(workspace: Path, control: Path, browser_url: str) -> None:
    identity = json.loads((control / "python-created.json").read_text())
    rid, origin = identity["rid"], identity["lease"]
    mount = MountedWorkspace(load_or_create_workspace_id(workspace), workspace, browser_backend_url=browser_url)
    with (
        WorkspaceContainerRegistry(mounted_workspaces=MountedWorkspaceRegistry([mount]), project_root=Path.cwd()) as registry,
        catalog_provider_scope(registry), ExitStack() as cleanup,
    ):
        container = registry.get(mount.workspace_id)
        cleanup.callback(container.session_catalog.close)
        cleanup.callback(container.workspace_activity_service.store.close)
        owner = container.browser_resource_owner
        # 冷准备只观察并冻结版本；显式stop随后以停止意图同步，不先排空仍在启动的页面注册。
        await owner.recover(())
        restored = owner._store.get(rid)
        assert identity["reconciliation_status"] == "reconcile_required"
        assert restored is not None and restored.status == "creating" and restored.reconciliation_status == "reconcile_required"
        pending = owner._leases.get_lease(origin["lease_id"])
        assert pending is not None and pending.status == "reconcile_required"
        assert {key: value for key, value in asdict(pending).items() if key not in {"status", "updated_at"}} == {
            key: value for key, value in origin.items() if key not in {"status", "updated_at"}
        }
        cancel, stop = owner._transport.cancel_creation, owner._transport.stop_resource
        calls: list[str] = []

        async def cancelling(resource_id: str, *, owner_revision: int) -> dict[str, object]:
            calls.append("cancel_creation")
            assert owner_revision == restored.owner_revision
            _publish_barrier(control / "python-cancelling.json", {
                "pid": os.getpid(), "restored": asdict(restored), "lease": asdict(pending),
            })
            return await cancel(resource_id, owner_revision=owner_revision)

        async def stopping(resource_id: str, *, reason: str | None = None) -> dict[str, object]:
            calls.append("stop")
            assert calls == ["cancel_creation", "stop"]
            return await stop(resource_id, reason=reason)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(owner._transport, "cancel_creation", cancelling)
            patch.setattr(owner._transport, "stop_resource", stopping)
            result = await owner.command(rid, action="stop", expected_revision=restored.owner_revision,
                holder_id="request:pending-stop", operation_id="pending-stop")
        assert result["status"] == "closed" and result["reconciliation_status"] == "verified"
        assert result["source_thread"] == {"session_id": identity["session_id"], "thread_id": identity["thread_id"]}
        assert not owner._leases.leases_for_resource(rid)
        cold = ExternalResourceLeaseLedger(state_path=workspace / ".boxteam/resources.json")
        settled = cold.get_lease(origin["lease_id"])
        assert settled is not None and settled.status == "settled"
        assert {key: value for key, value in asdict(settled).items() if key not in {"status", "updated_at"}} == {
            key: value for key, value in origin.items() if key not in {"status", "updated_at"}
        }
        _publish_barrier(control / "python-recovered.json", {
            "pid": os.getpid(), "result": result, "settled": asdict(settled), "calls": calls,
        })


async def _wait_worker_file(path: Path, worker: subprocess.Popen[str], log: Path) -> None:
    async with asyncio.timeout(30):
        while not path.is_file():
            assert worker.poll() is None, log.read_text()
            await asyncio.sleep(0.02)


@pytest.mark.parametrize("stage", ["acquire", "registered"])
async def test_python_hard_restart_cancels_the_original_native_start_and_settles_only_its_lease(
    browser_service: BrowserService, monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    handle, root, control, run, _native = browser_service
    (control / "stage").write_text(stage)
    monkeypatch.setenv("PYTHONPATH", str(Path.cwd()))
    monkeypatch.setenv("BOXTEAM_PROJECT_ROOT", str(Path.cwd()))
    browser_url = f"http://127.0.0.1:{handle.backend_port}"
    test_file = str(Path(__file__).resolve())
    with ExitStack() as cleanup:
        worker = _spawn(cleanup, [sys.executable, test_file, "create", str(root), str(control), browser_url],
            cwd=Path.cwd(), logs=control / "python-create")
        await _wait_worker_file(control / "arrived.json", worker, control / "python-create.stderr.log")
        identity = json.loads((control / "python-created.json").read_text())
        assert identity["pid"] == worker.pid
        paths = [root / ".boxteam" / name for name in ("browser-resources.json", "resources.json")]
        original_bytes = [path.read_bytes() for path in paths]
        worker.kill()
        await asyncio.to_thread(_assert_force_terminated, worker)
        assert [path.read_bytes() for path in paths] == original_bytes
        recovered = _spawn(cleanup, [sys.executable, test_file, "recover", str(root), str(control), browser_url],
            cwd=Path.cwd(), logs=control / "python-recover")
        try:
            await _wait_worker_file(control / "python-cancelling.json", recovered, control / "python-recover.stderr.log")
            await asyncio.sleep(0.1)
            assert recovered.poll() is None and not (control / "python-recovered.json").exists()
            pending = ExternalResourceLeaseLedger(state_path=root / ".boxteam/resources.json").get_lease(identity["lease"]["lease_id"])
            assert pending is not None and pending.status == "reconcile_required"
        finally:
            (control / "release").touch()
        assert await asyncio.to_thread(recovered.wait, timeout=30) == 0, (control / "python-recover.stderr.log").read_text()
        receipt = json.loads((control / "python-recovered.json").read_text())
        assert receipt["pid"] != identity["pid"] and receipt["calls"] == ["cancel_creation", "stop"]
        headers = {"X-BoxTeam-Workspace-Id": load_or_create_workspace_id(root), "X-BoxTeam-Resource-Owner": "workspace-browser"}
        async with httpx.AsyncClient(base_url=browser_url, headers=headers, trust_env=False, timeout=5) as client:
            native = (await client.get(f"/api/browsers/{identity['rid']}")).json()["data"]
            health = (await client.get("/health")).json()
        assert native["status"] == "closed" and native["reconciliation_status"] == "verified"
        assert native["transport_owner_revision"] == receipt["result"]["owner_revision"]
        assert health["browser_runtime"]["context_count"] == 0
        assert [json.loads(line) for line in (control / "closed.jsonl").read_text().splitlines()] == [{"context_index": 1}]
        run.write_result("passed", {"stage": stage, "original": identity, "recovered": receipt, "native": native,
            "scope": "真实Python硬退出/冷owner、Node/HTTP/Chromium及原lease；不代Gateway/Web、Node硬重启或Windows"})


if __name__ == "__main__":
    mode, workspace_arg, control_arg, browser_url_arg = sys.argv[1:]
    assert mode in {"create", "recover"}
    asyncio.run((_create_pending_browser if mode == "create" else _recover_pending_browser)(
        Path(workspace_arg), Path(control_arg), browser_url_arg,
    ))
