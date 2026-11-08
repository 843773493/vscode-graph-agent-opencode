"""真实PTY的resize发布失败与日志次错保持同一资源，并允许原入口重试。"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from contextlib import AsyncExitStack
from pathlib import Path
from uuid import uuid4

import httpx
import psutil
import pytest

from app.core.workspace_identity import load_or_create_workspace_id
from tests.harness.python.run_context import TestRunContext
from tests.harness.python.terminal_backend import (
    TerminalPluginService,
    start_terminal_plugin_service,
    stop_terminal_plugin_service,
)
from tests.support.workspaces import prepare_default_test_workspace


@pytest.fixture
def terminal_service(
    request: pytest.FixtureRequest, integration_backend_port: int, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TerminalPluginService, Path, Path, TestRunContext]]:
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
        preload = runtime / "terminal-diagnostic-writer.mjs"
        preload.write_text("""
import fs from "node:fs";
import path from "node:path";

// 仅在原Node诊断writer制造真实ENOTDIR，不替换PTY、fs状态写入或资源回执。
if (path.basename(process.argv[1] || "") === "backend.js") {
  const control = process.env.BOXTEAM_TEST_TERMINAL_STORAGE_CONTROL;
  if (!control) throw new Error("Terminal存储诊断测试缺少显式control");
  const write = process.stderr.write.bind(process.stderr);
  process.stderr.write = (chunk, ...args) => {
    if (typeof chunk === "string" && chunk.includes("[terminal-state-store]")) {
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
        monkeypatch.setenv("BOXTEAM_TEST_TERMINAL_STORAGE_CONTROL", str(control))
        port = integration_backend_port + 62
        process = start_terminal_plugin_service(backend_port=port, workspace_root=str(root), artifacts_dir=run.artifacts_dir)
        run.add_cleanup("Terminal 插件进程", lambda: stop_terminal_plugin_service(process))
        yield process, root, control, run


@pytest.mark.parametrize("fail_sink", [False, True], ids=["healthy-log", "failed-log"])
async def test_resize_storage_failure_preserves_native_identity_and_same_size_retry(
    terminal_service: tuple[TerminalPluginService, Path, Path, TestRunContext], fail_sink: bool,
) -> None:
    plugin, root, control, run = terminal_service
    terminal_id = f"term_{uuid4().hex}"
    state_dir = root / ".boxteam/terminal-manager"
    state_file = state_dir / "terminals.json"
    backup = control / "original-terminals.json"
    async with AsyncExitStack() as cleanup, httpx.AsyncClient(base_url=plugin.http_url, headers=plugin.headers, timeout=15, trust_env=False) as http:
        response = await http.post("/api/terminals", json={
            "terminal_id": terminal_id, "workspace_id": load_or_create_workspace_id(root), "owner_revision": 1,
            "title": "状态存储诊断", "cwd": str(root), "command": sys.executable,
            "args": ["-c", "import time; time.sleep(120)"], "cols": 100, "rows": 30,
        })
        assert response.status_code == 200, response.text
        cleanup.push_async_callback(http.delete, f"/api/terminals/{terminal_id}")

        def repair_fault() -> None:
            (control / "fail-sink").unlink(missing_ok=True)
            if state_file.is_dir():
                state_file.rmdir()
            if backup.exists():
                backup.replace(state_file)

        # 在资源清理前恢复原文件，失败验收也只能回收本例创建的PTY。
        cleanup.callback(repair_fault)
        created = response.json()["data"]
        root_process = psutil.Process(created["os_pid"])
        worker_process = psutil.Process(created["pty_worker_pid"])
        original_bytes = state_file.read_bytes()
        state_file.replace(backup)
        state_file.mkdir()
        (control / "log-parent").write_text("原生日志次错", encoding="utf-8")
        if fail_sink:
            (control / "fail-sink").touch()
        failed = await http.post(f"/api/terminals/{terminal_id}/resize", json={"cols": 101, "rows": 35})
        assert failed.status_code == 500, failed.text
        payload = failed.json()
        current = (await http.get(f"/api/terminals/{terminal_id}")).json()["data"]
        attempts = [json.loads(line) for line in (control / "attempts.jsonl").read_text(encoding="utf-8").splitlines()]
        (run.artifacts_dir / "storage-failure.json").write_text(json.dumps({
            "failure": payload, "current": current, "original_records": json.loads(original_bytes),
            "diagnostic_attempts": attempts,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        assert payload["error"].startswith("写入终端状态失败:")
        assert "artifact=terminal_records, stage=文件发布" in payload["error"]
        for key in ("terminal_id", "workspace_id", "transport_owner_revision", "os_pid", "process_session_id", "process_start_time", "pty_worker_pid", "status"):
            assert current[key] == created[key]
        assert current["cols"] == 101 and current["rows"] == 35
        assert root_process.is_running() and worker_process.is_running()
        assert backup.read_bytes() == original_bytes and list(state_file.iterdir()) == []
        assert not list(state_dir.glob("*.tmp"))
        assert len(attempts) == 1 and str(state_file) in attempts[0]["chunk"]
        assert load_or_create_workspace_id(root) in attempts[0]["chunk"]
        for value in (payload, current):
            serialized = json.dumps(value, ensure_ascii=False)
            for private in (str(state_file), str(control / "log-parent")):
                assert json.dumps(private)[1:-1] not in serialized

        repair_fault()
        assert state_file.read_bytes() == original_bytes
        # 尺寸已经应用，但失败的落盘不能被同尺寸短路，必须重用原显式保存链。
        retried = await http.post(f"/api/terminals/{terminal_id}/resize", json={"cols": 101, "rows": 35})
        assert retried.status_code == 200, retried.text
        saved = retried.json()["data"]
        assert saved["os_pid"] == created["os_pid"] and saved["pty_worker_pid"] == created["pty_worker_pid"]
        records = json.loads(state_file.read_text(encoding="utf-8"))["terminals"]
        assert len(records) == 1 and records[0]["terminal_id"] == terminal_id
        assert records[0]["cols"] == 101 and records[0]["rows"] == 35
        assert all(records[0][key] == saved[key] for key in records[0])
    assert not root_process.is_running() or root_process.status() == psutil.STATUS_ZOMBIE
    assert not worker_process.is_running() or worker_process.status() == psutil.STATUS_ZOMBIE
    run.write_result("passed", {
        "scope": "真实Node/HTTP/PTY、原生发布冲突、日志次错、同尺寸重试及原进程回收；不代Gateway/Web、创建失败窗口或进程重启",
        "terminal_id": terminal_id, "failed_sink": fail_sink, "failed": payload, "retried": saved,
    })
