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
@pytest.mark.parametrize("failure, stage", [
    ("http", None), ("data", "envelope.data"),
    ("terminal", "envelope.terminal"), ("status", "terminal"),
    ("missing_status", "terminal"), ("status_type", "terminal"),
    ("json", "json"), ("utf8", "json"), ("response", "response"),
])
def test_transport_error_body_stays_in_diagnostics_after_owner_restart(
    resource_api_client: TestClient, terminal_owner_bundle: TerminalOwnerFixture,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, native_path: str,
    failure: str, stage: str | None,
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
    diagnostic: object
    if failure == "data":
        diagnostic = {"data": native_path}
    elif failure == "terminal":
        diagnostic = {"data": {"terminal": native_path}}
    elif failure in {"status", "missing_status", "status_type"}:
        diagnostic = {**terminal_owner_bundle.transport.records[rid], "status": native_path}
        if failure == "missing_status":
            del diagnostic["status"]
            diagnostic["diagnostic"] = native_path
        elif failure == "status_type":
            diagnostic["status"] = [native_path]
    else:
        diagnostic = [native_path] if failure == "response" else {"error": f"读取失败: {native_path}", "cwd": native_path}
    envelope = {"data": {"terminal": diagnostic}} if stage == "terminal" else diagnostic
    body = json.dumps(envelope, ensure_ascii=False).encode()
    if failure == "json":
        body += b" trailing"
    elif failure == "utf8":
        body += b"\xff"
    if failure in {"json", "utf8"}:
        diagnostic = body
    elif failure == "http":
        diagnostic = body.decode()

    def rejected(request: Request, *, timeout: int) -> BytesIO:
        assert timeout == 10
        attempted.append((request.method, request.full_url))
        if failure == "http":
            raise HTTPError(request.full_url, 500, "Internal Server Error", {}, BytesIO(body))
        return BytesIO(body)

    monkeypatch.setattr("app.services.infrastructure.terminal_manager_client.urlopen", rejected)
    monkeypatch.setattr(terminal_owner_bundle.transport, "delete_resource", transport.delete_resource)
    command = {"expected_revision": resource["owner_revision"]}
    with caplog.at_level(logging.ERROR):
        failed = resource_api_client.post(f"/api/v1/resources/{rid}/delete", json=command)
    value_error = failure in {"json", "utf8", "missing_status", "status_type"}
    assert failed.status_code == (400 if value_error else 500), failed.text
    error_type = "ValueError" if value_error else "TypeError"
    expected = (
        "TerminalRequestError: 终端管理器请求失败: method=DELETE, status=500；请查看工作区终端服务日志"
        if failure == "http" else
        f"{error_type}: 终端管理器响应协议错误: stage={stage}；请查看工作区终端服务日志"
    )
    if value_error:
        assert failed.json() == {"detail": expected}
        assert failed.headers["X-Request-ID"]
    else:
        assert failed.json()["message"] == expected
        assert failed.headers["X-Request-ID"] == failed.json()["request_id"]
    assert native_path not in failed.text
    diagnostic_records = [
        record for record in caplog.records
        if record.name == "app.services.infrastructure.terminal_manager_client"
        and isinstance(record.args, tuple) and record.args[-1] == diagnostic
    ]
    assert len(diagnostic_records) == 1
    cause_types = {
        "http": HTTPError, "status": TypeError,
        "missing_status": ValueError, "status_type": ValueError,
        "json": json.JSONDecodeError, "utf8": UnicodeDecodeError,
    }
    if failure in cause_types:
        diagnostic_record = diagnostic_records[0]
        assert diagnostic_record.exc_info is not None
        cause = diagnostic_record.exc_info[1]
        assert type(cause) is cause_types[failure]
        assert any(
            record.exc_info is not None and record.exc_info[1].__cause__ is cause
            for record in caplog.records
            if record.name == "app.services.business.workspace_resources.owner"
        )
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
