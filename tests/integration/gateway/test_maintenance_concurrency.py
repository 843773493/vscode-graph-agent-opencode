"""维护屏障只用于并发正确性；延迟预算由独立无屏障场景验证。"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import close_gateway_process, start_gateway_process
from tests.support.processes.workspace import (
    close_backend_process,
    start_backend_process,
    workspace_backend_id_for_root,
)


@pytest.fixture(scope="module")
def integration_backend_process(integration_workspace_root_path: str, integration_backend_port: int):
    handle = start_backend_process(
        workspace_root=integration_workspace_root_path, port=integration_backend_port,
        log_name="maintenance-concurrency", asgi_app="tests.integration.gateway.maintenance_concurrency_app:app",
    )
    try:
        yield handle.process
    finally:
        close_backend_process(handle)


def test_gateway_reads_activity_while_periodic_worker_is_in_flight(
    request: pytest.FixtureRequest, integration_backend_process,
    integration_backend_port: int, integration_workspace_root_path: str,
) -> None:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    workspace = Path(integration_workspace_root_path)
    port = integration_port_block_for_file(Path(request.node.path)).port(1)
    gateway = start_gateway_process(
        workspace_root=workspace, default_backend_url=f"http://127.0.0.1:{integration_backend_port}",
        port=port, boxteam_home=context.boxteam_home_for_node(request.node.nodeid), artifacts_dir=context.artifacts_dir,
    )
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=3, trust_env=False, headers={
            "X-Local-Token": "local-dev-token", "X-BoxTeam-Workspace-Id": workspace_backend_id_for_root(workspace),
        }) as client:
            session = client.post("/api/v1/sessions", json={"title": "维护在途读取"})
            session.raise_for_status()
            session_id = session.json()["data"]["session_id"]

            def control(action: str) -> dict[str, bool | int | None]:
                response = client.post("/api/v1/__tests/maintenance-barrier", params={"action": action})
                response.raise_for_status()
                return response.json()

            def wait_for(field: str) -> dict[str, bool | int | None]:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    state = control("state")
                    if state[field]:
                        return state
                    time.sleep(0.01)
                raise TimeoutError(f"原维护 worker 未到达 {field} 边界")

            control("arm")
            try:
                blocked = wait_for("entered")
                assert not blocked["finished"]
                assert blocked["worker_thread"] != blocked["event_loop_thread"]
                # 查询/清理对照可以直接准备；Session 身份仍由正式入口创建。
                with sqlite3.connect(workspace / ".boxteam/state/workspace.sqlite") as connection:
                    cursor = connection.execute("SELECT COALESCE(MAX(event_seq),0) FROM workspace_activity").fetchone()[0]
                    connection.executemany(
                        "INSERT INTO workspace_activity(event_id,session_id,status,summary,occurred_at) VALUES (?,?,?,?,?)",
                        [(key, session_id, "completed", key, timestamp.isoformat()) for key, timestamp in (
                            ("expired", datetime.now(UTC) - timedelta(days=40)), ("retained", datetime.now(UTC)),
                        )],
                    )

                def read(expected: list[str]) -> dict[str, object]:
                    response = client.get("/api/v1/session-catalog/events", params={"after": cursor, "limit": 100})
                    response.raise_for_status()
                    body = response.json()
                    assert response.headers["X-Request-ID"] == body["request_id"]
                    assert [item["event_id"] for item in body["data"]["items"]] == expected
                    return body

                before = read(["expired", "retained"])
                assert not control("state")["finished"], "Gateway 读取必须发生在维护 worker 屏障内"
            finally:
                control("release")
            settled = wait_for("finished")
            assert settled["removed"] == 1
            after = read(["retained"])
            (context.artifacts_dir / "concurrent-read.json").write_text(json.dumps({
                "blocked": blocked, "settled": settled, "before": before, "after": after,
                "scope": "真实周期worker及Gateway读取；屏障等待不作延迟样本，引用保护另由owner场景验证",
            }, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        close_gateway_process(gateway)
