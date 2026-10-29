"""真实Chromium启动失败与原生日志故障不改变HTTP创建失败的归属。"""

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
        preload = Path.cwd() / "tests/integration/workspace_services/browser/browser_runtime_diagnostic_failure.mjs"
        monkeypatch.setenv("NODE_OPTIONS", f"{os.environ.get('NODE_OPTIONS', '')} --import={preload.as_uri()}".strip())
        monkeypatch.setenv("BOXTEAM_TEST_PROJECT_ROOT", str(Path.cwd()))
        monkeypatch.setenv("BOXTEAM_TEST_BROWSER_RUNTIME_CONTROL", str(control))
        handle = start_browser_manager_processes(
            workspace_root=root, artifacts_dir=run.artifacts_dir,
            backend_port=integration_backend_port + 60, frontend_port=integration_backend_port + 61,
            backend_host="127.0.0.1",
        )
        run.add_cleanup("Browser进程", lambda: close_browser_manager_processes(handle))
        yield BrowserManagerClient(workspace_id=load_or_create_workspace_id(root),
                                   backend_url=f"http://127.0.0.1:{handle.backend_port}"), run, root, control


@pytest.mark.parametrize("fail_sink", [False, True], ids=["healthy-log", "failed-log"])
async def test_runtime_and_log_failures_keep_original_creation_receipt_and_allow_recovery(
    browser_service: tuple[BrowserManagerClient, TestRunContext, Path, Path], fail_sink: bool,
) -> None:
    client, run, root, control = browser_service
    failed_id, peer_id = [f"browser_{uuid4().hex}" for _ in range(2)]
    (control / "fail-launch").touch()
    (control / "log-parent").write_text("原生日志故障边界", encoding="utf-8")
    if fail_sink:
        (control / "fail-sink").touch()
    workspace_id = load_or_create_workspace_id(root)
    async with AsyncExitStack() as cleanup:
        async with httpx.AsyncClient(base_url=client.backend_url, timeout=15, trust_env=False, headers={
            "X-BoxTeam-Workspace-Id": workspace_id, "X-BoxTeam-Resource-Owner": "workspace-browser",
        }) as http:
            response = await http.post("/api/browsers", json={
                "workspace_id": workspace_id, "browser_id": failed_id, "owner_revision": 1,
                "title": "原运行时失败", "url": "about:blank",
            })
        assert response.status_code == 500, response.text
        payload = response.json()
        cleanup.push_async_callback(client.delete_resource, failed_id)
        receipt = payload["creation_receipt"]
        assert receipt["resource_id"] == failed_id and receipt["owner_revision"] == 1
        assert receipt["workspace_id"] == workspace_id and receipt["outcome"] == "failed"
        failed = await client.get_resource(failed_id)
        assert failed["status"] == "failed" and failed["browser_id"] == failed_id
        assert failed["error_message"] == receipt["resource"]["error_message"]
        before = json.loads((root / ".boxteam/browser-manager/browsers.json").read_text(encoding="utf-8"))

        (control / "fail-launch").unlink()
        if fail_sink:
            (control / "fail-sink").unlink()
        page = root / "recovered.html"
        page.write_text("<title>运行时恢复</title><p>真实页面</p>", encoding="utf-8")
        peer = await client.create_browser(browser_id=peer_id, owner_revision=1, title="恢复后新请求", url=page.as_uri())
        cleanup.push_async_callback(client.delete_resource, peer_id)
        assert peer["browser_id"] == peer_id and peer["status"] == "running"
        pages = peer["pages"]
        assert peer["url"] == page.as_uri() and isinstance(pages, list) and len(pages) == 1
        assert (await client.get_resource(failed_id))["error_message"] == failed["error_message"]
        attempts = [json.loads(line) for line in (control / "diagnostic-attempts.jsonl").read_text(encoding="utf-8").splitlines()]
        assert len(attempts) == 1 and str(control / "missing-chromium") in attempts[0]["chunk"]
        assert "acquire_context" in attempts[0]["chunk"]
        (run.artifacts_dir / "runtime-diagnostics.json").write_text(json.dumps({
            "failure": payload, "cold_record": before, "failed_resource": failed,
            "diagnostic_attempts": attempts, "recovered_browser_id": peer_id,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        assert "stage=acquire_context" in payload["error"]
        for value in (payload, before, failed):
            serialized = json.dumps(value, ensure_ascii=False)
            for private in (str(control / "missing-chromium"), str(control / "log-parent")):
                assert json.dumps(private)[1:-1] not in serialized
    run.write_result("passed", {
        "scope": "真实Node/HTTP、Chromium启动失败及恢复页面；显式替换launch参数和writer边界，不代表Gateway/Web、Python owner或进程重启",
        "failed_id": failed_id, "recovered_id": peer_id, "failed_sink": fail_sink,
    })
