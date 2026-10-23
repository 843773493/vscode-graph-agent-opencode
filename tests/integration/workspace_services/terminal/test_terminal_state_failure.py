from __future__ import annotations

import json
import os
import shutil
import time
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path

import httpx
import pytest

from app.core.workspace_identity import load_or_create_workspace_id
from tests.harness.python.run_context import TestRunContext
from tests.harness.python.terminal_backend import start_terminal_backend
from tests.support.processes.workspace import terminate_process
from tests.support.workspaces import prepare_default_test_workspace


@pytest.fixture
def terminal_backend(integration_backend_port: int) -> Iterator[tuple[TestRunContext, httpx.Client, str]]:
    run = TestRunContext.from_test_file(Path(__file__)).prepare()
    prepare_default_test_workspace(
        workspace_root=run.workspace_root,
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
    )
    workspace_id = load_or_create_workspace_id(run.workspace_root)
    port = integration_backend_port + 64
    with ExitStack() as lifetime:
        process = start_terminal_backend(
            backend_port=port, workspace_root=str(run.workspace_root), artifacts_dir=run.artifacts_dir,
        )
        lifetime.callback(terminate_process, process)
        client = lifetime.enter_context(httpx.Client(
            base_url=f"http://127.0.0.1:{port}", timeout=10,
            headers={"X-BoxTeam-Workspace-Id": workspace_id, "X-BoxTeam-Resource-Owner": "workspace-terminal"},
        ))
        try:
            yield run, client, workspace_id
        finally:
            listed = client.get("/api/terminals")
            assert listed.status_code == 200, listed.text
            for terminal in listed.json()["data"]:
                deleted = client.delete(f"/api/terminals/{terminal['terminal_id']}")
                assert deleted.status_code == 200, deleted.text


def test_state_write_failure_keeps_native_details_in_logs_and_allows_explicit_retry(
    terminal_backend: tuple[TestRunContext, httpx.Client, str],
) -> None:
    run, client, workspace_id = terminal_backend
    node = shutil.which("node")
    assert node is not None
    # 真实PTY内的受控进程不产生输出，故障期间不会有无关的输出持久化回调。
    terminal_id = "term_" + os.urandom(16).hex()
    created = client.post("/api/terminals", json={
        "workspace_id": workspace_id, "terminal_id": terminal_id, "owner_revision": 1,
        "command": node, "args": ["-e", "setInterval(() => {}, 1000)"],
    })
    assert created.status_code == 200, created.text
    assert created.json()["data"]["status"] == "running"
    state_file = run.workspace_root / ".boxteam/terminal-manager/terminals.json"
    state_dir = state_file.parent
    receipts: list[dict[str, object]] = []
    for index, (mode, stage, codes) in enumerate([
        ("directory", "目录创建", {"EEXIST"}),
        ("permission", "正文写入", {"EACCES"}),
        ("publish", "文件发布", {"EISDIR", "EPERM", "EEXIST"}),
    ]):
        if mode == "permission" and (os.name == "nt" or os.geteuid() == 0):
            continue  # Windows需真实ACL、root需非特权目标；不以chmod冒充拒绝。
        original = state_file.read_bytes()
        parked = state_dir.with_name(state_dir.name + "-backup") if mode == "directory" else state_file.with_suffix(".backup")
        permissions = state_dir.stat().st_mode & 0o777
        if mode == "directory":
            state_dir.rename(parked)
            state_dir.write_bytes(b"not-a-directory")
        elif mode == "permission":
            state_dir.chmod(0o500)
        else:
            state_file.rename(parked)
            state_file.mkdir()
        payload = {"cols": 101 + index, "rows": 31 + index}
        try:
            failed = client.post(f"/api/terminals/{terminal_id}/resize", json=payload)
            assert failed.status_code == 500, failed.text
            body = failed.json()
            assert body["error"].startswith(f"写入终端状态失败: workspace_id={workspace_id}, artifact=terminal_records, stage={stage}, code=")
            assert body["error"].rsplit("=", 1)[1] in codes
            assert str(run.workspace_root) not in failed.text
            old_file = parked / state_file.name if mode == "directory" else state_file if mode == "permission" else parked
            assert old_file.read_bytes() == original
            if mode != "directory":
                assert list(state_dir.glob("terminals.json.*.tmp")) == []
            observed = client.get(f"/api/terminals/{terminal_id}")
            assert observed.status_code == 200, observed.text
            assert observed.json()["data"]["cols"] == payload["cols"]
            receipts.append(body)
        finally:
            if mode == "directory":
                state_dir.unlink()
                parked.rename(state_dir)
            elif mode == "permission":
                state_dir.chmod(permissions)
            else:
                state_file.rmdir()
                parked.rename(state_file)
        retried = client.post(f"/api/terminals/{terminal_id}/resize", json=payload)
        assert retried.status_code == 200, retried.text
        record, = json.loads(state_file.read_text())["terminals"]
        assert record["terminal_id"] == terminal_id
        assert record["cols"] == payload["cols"]
        assert str(run.workspace_root) not in json.dumps(record)
    diagnostics = "\n".join(file.read_text() for file in run.artifacts_dir.glob("terminal-backend-*.stderr.log"))
    assert str(state_file) in diagnostics
    assert "[terminal-state-store] 写入终端状态失败" in diagnostics
    (run.artifacts_dir / f"state-write-failures-{time.time_ns()}.json").write_text(
        json.dumps({"receipts": receipts, "terminal_id": terminal_id}, ensure_ascii=False, indent=2), encoding="utf-8",
    )
