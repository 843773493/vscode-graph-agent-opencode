from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path

import httpx
import pytest

from app.core.workspace_identity import load_or_create_workspace_id
from tests.harness.python.run_context import TestRunContext
from tests.harness.python.terminal_backend import (
    start_terminal_plugin_service,
    stop_terminal_plugin_service,
)
from tests.harness.python.terminal_state import read_terminal_state
from tests.support.ports import TestFilePorts
from tests.support.workspaces import prepare_default_test_workspace


@pytest.fixture
def terminal_backend(integration_backend_port: int, integration_service_ports: TestFilePorts) -> Iterator[tuple[TestRunContext, httpx.Client, str]]:
    run = TestRunContext.from_test_file(Path(__file__)).prepare()
    prepare_default_test_workspace(
        workspace_root=run.workspace_root,
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
    )
    workspace_id = load_or_create_workspace_id(run.workspace_root)
    port = integration_service_ports.port(44)
    with ExitStack() as lifetime:
        service = start_terminal_plugin_service(
            backend_port=port, workspace_root=str(run.workspace_root), artifacts_dir=run.artifacts_dir,
        )
        lifetime.callback(stop_terminal_plugin_service, service)
        client = lifetime.enter_context(httpx.Client(
            base_url=service.http_url, timeout=10, headers=service.headers,
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
    state_file = run.workspace_root / ".boxteam/terminal-manager/terminals.sqlite"
    original = read_terminal_state(state_file)
    payload = {"cols": 101, "rows": 31}
    with sqlite3.connect(state_file) as connection:
        connection.execute("CREATE TRIGGER fail_save BEFORE UPDATE ON terminal_records BEGIN SELECT RAISE(ABORT, 'storage-failure'); END")
    try:
        failed = client.post(f"/api/terminals/{terminal_id}/resize", json=payload)
        assert failed.status_code == 500, failed.text
        body = failed.json()
        assert body["error"] == f"写入终端状态失败: workspace_id={workspace_id}, artifact=terminal_records, stage=事务写入, code=ERR_SQLITE_ERROR"
        assert str(run.workspace_root) not in failed.text
        assert read_terminal_state(state_file) == original
        observed = client.get(f"/api/terminals/{terminal_id}")
        assert observed.status_code == 200, observed.text
        assert observed.json()["data"]["cols"] == payload["cols"]
    finally:
        with sqlite3.connect(state_file) as connection:
            connection.execute("DROP TRIGGER fail_save")
    retried = client.post(f"/api/terminals/{terminal_id}/resize", json=payload)
    assert retried.status_code == 200, retried.text
    record, = read_terminal_state(state_file)["terminals"]
    assert record["terminal_id"] == terminal_id
    assert record["cols"] == payload["cols"]
    assert str(run.workspace_root) not in json.dumps(record)
    diagnostics = "\n".join(file.read_text() for file in run.artifacts_dir.glob("terminal-plugin-*.stderr.log"))
    assert str(state_file) in diagnostics
    assert "[terminal-state-store] 写入终端状态失败" in diagnostics
    (run.artifacts_dir / f"state-write-failures-{time.time_ns()}.json").write_text(
        json.dumps({"receipts": [body], "terminal_id": terminal_id}, ensure_ascii=False, indent=2), encoding="utf-8",
    )
