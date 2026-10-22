"""通过真实 Gateway 生命周期验证身份 CLI 的公开错误与恢复。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import ExitStack, closing
from pathlib import Path

import httpx
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    acquire_gateway_guest,
    close_gateway_process,
    reset_gateway_persistent_state,
    start_gateway_process,
)
from tests.support.processes.workspace import (
    close_backend_process,
    start_backend_process,
)


@pytest.mark.asyncio
async def test_identity_cli_failure_is_path_free_and_recovery_preserves_binding(
    request: pytest.FixtureRequest, integration_workspace_root_path: str,
) -> None:
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    primary = Path(integration_workspace_root_path)
    target = primary / "managed-private-root"
    target.mkdir()
    reset_gateway_persistent_state(workspace_root=primary)
    ports = integration_port_block_for_file(Path(request.node.fspath))
    evidence = {}
    with ExitStack() as processes:
        backend = start_backend_process(
            workspace_root=str(primary), port=ports.port(0), log_name="primary-backend",
        )
        processes.callback(close_backend_process, backend)
        gateway = start_gateway_process(
            workspace_root=primary, default_backend_url=f"http://127.0.0.1:{backend.port}",
            port=ports.port(1), artifacts_dir=context.artifacts_dir,
        )
        processes.callback(close_gateway_process, gateway)
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS, timeout=120,
        ) as client:
            await acquire_gateway_guest(client)
            registered = await client.post(
                "/api/gateway/workspaces/local", json={"root_path": str(target), "name": "identity-test"},
            )
            assert registered.status_code == 200, registered.text
            item = next(item for item in registered.json()["data"]["items"] if item["name"] == "identity-test")
            workspace_id = item["workspace_id"]
            backend_id = item["backend_workspace_id"]
            identity = target / ".boxteam/workspace-identity.json"
            original = identity.read_bytes()
            assert json.loads(original)["workspace_id"] == backend_id
            stopped = await client.post(f"/api/gateway/workspaces/{workspace_id}/runtime/stop")
            assert stopped.status_code == 200, stopped.text
            assert stopped.json()["data"]["status"] == "stopped"
            stopped_item = next(
                item for item in stopped.json()["data"]["workspaces"]["items"]
                if item["workspace_id"] == workspace_id
            )
            assert stopped_item["status"] == "offline"

            # 外部损坏仅用于触发失败；目标、绑定和停机状态全部由产品入口建立。
            processes.callback(identity.write_bytes, original)
            damaged = json.dumps({"workspace_id": str(target / "private-identity-marker")}).encode()
            identity.write_bytes(damaged)
            failed = await client.post(f"/api/gateway/workspaces/{workspace_id}/runtime/start")
            assert failed.status_code == 502, failed.text
            assert failed.json()["detail"] == "Workspace backend UUID CLI 执行失败: exit=1"
            assert failed.headers["X-Request-ID"]
            assert str(target) not in failed.text
            assert "private-identity-marker" not in failed.text
            assert identity.read_bytes() == damaged, "身份损坏不能静默初始化替代 UUID"
            evidence["failed-start"] = {"status": failed.status_code, "body": failed.json()}
            for health in (False, True):
                response = await client.get("/api/gateway/workspaces", params={"check_health": health})
                assert response.status_code == 200, response.text
                body = response.json()
                assert body["request_id"] == response.headers["X-Request-ID"]
                item = next(item for item in body["data"]["items"] if item["workspace_id"] == workspace_id)
                assert item["backend_workspace_id"] == backend_id
                assert item["status"] == "offline"
                assert item["connection_error"] == stopped_item["connection_error"]
                assert str(target) not in response.text
                assert "private-identity-marker" not in response.text
                evidence[f"failed-list-{health}"] = item

            state = context.boxteam_home / "state/gateway/gateway.sqlite"
            with closing(sqlite3.connect(f"{state.as_uri()}?mode=ro", uri=True)) as connection:
                row, = connection.execute(
                    "SELECT payload_json FROM gateway_workspace_registry WHERE workspace_id=?", (workspace_id,),
                ).fetchone()
                record = json.loads(row)
                assert record["root_path"] == str(target)
                assert record["backend_workspace_id"] == backend_id
                assert record["desired_running"] is False
                assert record["connection_error"] is None, "前置身份校验失败不应发布新的运行时状态"
                assert "private-identity-marker" not in str(record["connection_error"])
                assert str(target) not in str(record["connection_error"])
                evidence["registry-after-failure"] = record

            identity.write_bytes(original)
            recovered = await client.post(f"/api/gateway/workspaces/{workspace_id}/runtime/start")
            assert recovered.status_code == 200, recovered.text
            assert recovered.json()["data"]["status"] == "started"
            assert identity.read_bytes() == original
            response = await client.get("/api/gateway/workspaces", params={"check_health": True})
            assert response.status_code == 200, response.text
            item = next(item for item in response.json()["data"]["items"] if item["workspace_id"] == workspace_id)
            assert item["backend_workspace_id"] == backend_id
            assert item["status"] == "ready"
            assert item["connection_error"] is None
            evidence["recovered"] = item
        logs = (context.artifacts_dir / "gateway.stderr.log").read_text()
        assert str(identity) in logs
        assert "private-identity-marker" in logs
        assert "stdout=" in logs and "stderr=" in logs
        (context.artifacts_dir / "managed-identity-errors.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
