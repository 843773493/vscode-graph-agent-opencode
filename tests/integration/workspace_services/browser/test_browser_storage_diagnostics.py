"""真实截图存储失败与原生日志次错保留HTTP、操作记录及显式重试合同。"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import AsyncExitStack
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.core.workspace_identity import load_or_create_workspace_id
from app.services.infrastructure.browser_manager_client import BrowserManagerClient
from tests.harness.python.browser_manager import (
    close_browser_manager_processes,
    start_browser_manager_processes,
)
from tests.harness.python.run_context import TestRunContext
from tests.support.workspaces import prepare_default_test_workspace


@pytest.fixture
def browser_service(
    request: pytest.FixtureRequest, integration_backend_port: int, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[BrowserManagerClient, TestRunContext, Path, Path]]:
    with TestRunContext.from_test_file(Path(request.path)) as run:
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
        preload = runtime / "storage-diagnostic-writer.mjs"
        preload.write_text("""
import fs from "node:fs";
import path from "node:path";

// 只在原Node日志writer制造真实ENOTDIR；不替换截图、fs存储或资源状态。
if (path.basename(process.argv[1] || "") === "backend.js") {
  const control = process.env.BOXTEAM_TEST_BROWSER_STORAGE_CONTROL;
  if (!control) throw new Error("Browser存储诊断测试缺少显式control");
  const write = process.stderr.write.bind(process.stderr);
  process.stderr.write = (chunk, ...args) => {
    if (typeof chunk === "string" && chunk.includes("[browser-state-store]")) {
      fs.appendFileSync(path.join(control, "attempts.jsonl"), JSON.stringify({ chunk }) + "\\n");
      if (fs.existsSync(path.join(control, "fail-sink"))) {
        fs.appendFileSync(path.join(control, "log-parent/diagnostic.log"), chunk);
      }
    }
    return write(chunk, ...args);
  };
}
""", encoding="utf-8")
        monkeypatch.setenv("NODE_OPTIONS", f"{os.environ.get('NODE_OPTIONS', '')} --import={preload.as_uri()}".strip())
        monkeypatch.setenv("BOXTEAM_TEST_BROWSER_STORAGE_CONTROL", str(control))
        handle = start_browser_manager_processes(
            workspace_root=root, artifacts_dir=run.artifacts_dir,
            backend_port=integration_backend_port + 60, frontend_port=integration_backend_port + 61,
            backend_host="127.0.0.1",
        )
        run.add_cleanup("Browser进程", lambda: close_browser_manager_processes(handle))
        yield BrowserManagerClient(workspace_id=load_or_create_workspace_id(root),
                                   backend_url=f"http://127.0.0.1:{handle.backend_port}"), run, root, control


@pytest.mark.parametrize("fail_sink", [False, True], ids=["healthy-log", "failed-log"])
async def test_real_screenshot_storage_failure_keeps_operation_and_retry(
    browser_service: tuple[BrowserManagerClient, TestRunContext, Path, Path], fail_sink: bool,
) -> None:
    client, run, root, control = browser_service
    browser_id = f"browser_{uuid4().hex}"
    page = root / "storage.html"
    page.write_text("<title>真实截图存储</title><p>保留原页面</p>", encoding="utf-8")
    async with AsyncExitStack() as cleanup:
        created = await client.create_browser(browser_id=browser_id, owner_revision=1, title="存储诊断", url=page.as_uri())
        cleanup.push_async_callback(client.delete_resource, browser_id)
        state_dir = root / ".boxteam/browser-manager"
        screenshots = state_dir / "screenshots"
        screenshots.write_text("截图目录位置的原始字节", encoding="utf-8")
        (control / "log-parent").write_text("原生日志次错", encoding="utf-8")
        if fail_sink:
            (control / "fail-sink").touch()
        async with httpx.AsyncClient(base_url=client.backend_url, timeout=15, trust_env=False, headers={
            "X-BoxTeam-Workspace-Id": load_or_create_workspace_id(root),
            "X-BoxTeam-Resource-Owner": "workspace-browser",
        }) as http:
            response = await http.post(f"/api/browsers/{browser_id}/screenshot", json={})
            assert response.status_code == 500, response.text
            failed = response.json()
            current = await client.get_resource(browser_id)
            cold = json.loads((state_dir / "browsers.json").read_text(encoding="utf-8"))
            attempts = [json.loads(line) for line in (control / "attempts.jsonl").read_text(encoding="utf-8").splitlines()]
            (run.artifacts_dir / "storage-failure.json").write_text(json.dumps({
                "failure": failed, "resource": current, "cold_records": cold, "diagnostic_attempts": attempts,
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            assert "保存浏览器截图失败:" in failed["error"]
            assert "stage=目录创建" in failed["error"] and "screenshot_id=screenshot_" in failed["error"]
            assert failed["operation_outcome"] == "failed"
            operation = current["last_operation"]
            assert isinstance(operation, dict)
            assert operation["status"] == "failed" and operation["action"] == "screenshot"
            assert operation["error"] == failed["error"]
            assert current["browser_id"] == browser_id and current["status"] == "running"
            assert current["transport_owner_revision"] == 1 and current["pages"] == created["pages"]
            assert screenshots.read_text(encoding="utf-8") == "截图目录位置的原始字节"
            assert len(attempts) == 1 and str(screenshots) in attempts[0]["chunk"]
            assert load_or_create_workspace_id(root) in attempts[0]["chunk"]
            for value in (failed, current, cold):
                serialized = json.dumps(value, ensure_ascii=False)
                for private in (str(screenshots), str(control / "log-parent")):
                    assert json.dumps(private)[1:-1] not in serialized

            screenshots.unlink()
            if fail_sink:
                (control / "fail-sink").unlink()
            retried = await http.post(f"/api/browsers/{browser_id}/screenshot", json={})
            assert retried.status_code == 200, retried.text
            saved = retried.json()["data"]
            assert saved["operation"]["operation_revision"] == operation["operation_revision"] + 1
            assert saved["operation"]["status"] == "completed"
            assert saved["browser"]["pages"] == created["pages"]
            image = await http.get(saved["screenshot_url"])
            assert image.status_code == 200 and image.headers["content-type"] == "image/png"
            assert image.content.startswith(b"\x89PNG\r\n\x1a\n") and len(image.content) == saved["byte_length"]
            assert len(list(screenshots.glob("*.png"))) == 1
            run.write_result("passed", {
                "scope": "真实Node/HTTP/Chromium截图、原生目录冲突与日志writer次错；不代Gateway/Web、其他存储出口网络验收或Backend重启",
                "browser_id": browser_id, "failed_sink": fail_sink,
                "failed_operation": operation, "retried_operation": saved["operation"],
            })
