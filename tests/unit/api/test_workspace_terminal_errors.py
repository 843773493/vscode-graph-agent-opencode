"""终端传输拒绝的正文只进入诊断，不进入普通 API 与资源记录。"""

from __future__ import annotations

import json
import logging
from io import BytesIO
from urllib.error import HTTPError
from urllib.request import Request

import pytest
from fastapi.testclient import TestClient

from app.services.infrastructure.terminal_manager_client import TerminalManagerClient
from tests.support.resource_owners.sessions import SESSION_A, WORKSPACE_ID
from tests.support.resource_owners.terminal import TerminalOwnerFixture


@pytest.mark.parametrize("native_path", [
    "/private/workspace/key", r"C:\Users\owner\secret.key",
    r"\\server\share\secret.key", r"\\?\UNC\server\share\secret.key",
])
def test_http_error_body_stays_in_diagnostics_after_owner_restart(
    resource_api_client: TestClient, terminal_owner_bundle: TerminalOwnerFixture,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, native_path: str,
) -> None:
    created = resource_api_client.post("/api/v1/resources/terminals", json={"session_id": SESSION_A})
    assert created.status_code == 200, created.text
    resource = created.json()["data"]
    rid = resource["resource_id"]
    detached = resource_api_client.post(f"/api/v1/resources/{rid}/detach", json={
        "expected_revision": resource["owner_revision"], "thread": resource["source_thread"],
    })
    assert detached.status_code == 200, detached.text
    resource = detached.json()["data"]
    transport = TerminalManagerClient(backend_url="http://terminal.test", workspace_id=WORKSPACE_ID)
    attempted: list[tuple[str, str]] = []
    body = json.dumps({"error": f"读取失败: {native_path}", "cwd": native_path}, ensure_ascii=False)

    def rejected(request: Request, *, timeout: int) -> None:
        assert timeout == 10
        attempted.append((request.method, request.full_url))
        raise HTTPError(request.full_url, 500, "Internal Server Error", {}, BytesIO(body.encode()))

    monkeypatch.setattr("app.services.infrastructure.terminal_manager_client.urlopen", rejected)
    monkeypatch.setattr(terminal_owner_bundle.transport, "delete_resource", transport.delete_resource)
    command = {"expected_revision": resource["owner_revision"]}
    with caplog.at_level(logging.ERROR):
        failed = resource_api_client.post(f"/api/v1/resources/{rid}/delete", json=command)
    assert failed.status_code == 500
    expected = "TerminalRequestError: 终端管理器请求失败: method=DELETE, status=500；请查看工作区终端服务日志"
    assert failed.json()["message"] == expected
    assert failed.headers["X-Request-ID"] == failed.json()["request_id"]
    assert native_path not in failed.text
    assert body in caplog.text
    terminal_owner_bundle.restart()
    observed = resource_api_client.get(f"/api/v1/resources/{rid}")
    assert observed.status_code == 200, observed.text
    current = observed.json()["data"]
    assert current["error"] == expected
    assert current["reconciliation_status"] == "unknown"
    assert current["available_actions"] == []
    listed = resource_api_client.get("/api/v1/resources?kind=terminal")
    assert listed.status_code == 200, listed.text
    assert listed.json()["data"]["items"] == [current]
    assert native_path not in (terminal_owner_bundle.root / ".boxteam/terminal-resources.json").read_text()
    assert terminal_owner_bundle.leases.leases_for_resource(rid) == []
    retry = resource_api_client.post(f"/api/v1/resources/{rid}/delete", json=command)
    assert retry.status_code == 409, retry.text
    assert attempted == [("DELETE", f"http://terminal.test/api/terminals/{rid}")]
