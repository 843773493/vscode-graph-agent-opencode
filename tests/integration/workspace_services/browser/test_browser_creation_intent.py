"""真实context已创建、回执尚未交付时硬退出，原创建意图仍可发现。"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import httpx
import psutil
import pytest

from app.core.workspace_identity import load_or_create_workspace_id
from tests.harness.python.browser_manager import (
    BrowserManagerProcesses,
    close_browser_manager_processes,
    start_browser_manager_processes,
)
from tests.harness.python.run_context import TestRunContext
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
import { writeFile, access } from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { setTimeout as delay } from 'node:timers/promises';

if (path.basename(process.argv[1] || '') === 'backend.js') {
  const control = process.env.BOXTEAM_TEST_BROWSER_CREATION_CONTROL;
  const root = process.env.BOXTEAM_PROJECT_ROOT;
  if (!control || !root) throw new Error('Browser创建屏障缺少显式控制目录或项目根');
  const { BrowserRuntimePool } = await import(pathToFileURL(path.join(root,
    'src/workspace-services/browser/server/resources/browserRuntimePool.js')));
  const acquire = BrowserRuntimePool.prototype.acquireContext;
  BrowserRuntimePool.prototype.acquireContext = async function (options) {
    const runtime = await acquire.call(this, options);
    // 只暂扣真实context回执；不代填Session、磁盘记录或终态。
    await writeFile(path.join(control, 'arrived.json'), JSON.stringify({
      connected: runtime.browser.isConnected(), contexts: this.contexts.size,
      generation: runtime.chromiumInstanceGeneration, version: runtime.browser.version(),
    }));
    while (true) {
      try { await access(path.join(control, 'release')); break; }
      catch (error) { if (error.code !== 'ENOENT') throw error; }
      await delay(10);
    }
    return runtime;
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
