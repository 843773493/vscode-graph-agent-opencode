"""资源失败的公开说明、持久记录和原异常在日志故障下仍保持一致。"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core import failures
from app.services.infrastructure.browser_manager_client import BrowserManagerClient
from tests.support.resource_owners.browser import BrowserOwnerFixture
from tests.support.resource_owners.sessions import SESSION_A, WORKSPACE_ID


@pytest.fixture(params=[False, True], ids=["logging", "broken-logging"])
def resource_failure_sink(
    browser_owner_bundle: BrowserOwnerFixture, request: pytest.FixtureRequest,
) -> Iterator[tuple[Path, list[BaseException]] | None]:
    if not request.param:
        yield None
        return
    blocked = browser_owner_bundle.root / "blocked-resource-diagnostics"
    blocked.write_bytes(b"keep obstruction")
    errors: list[BaseException] = []

    class FailingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            assert record.exc_info is not None and record.exc_info[1] is not None
            errors.append(record.exc_info[1])
            with (blocked / "service.log").open("a", encoding="utf-8"):
                pass

    handler = FailingHandler(level=logging.ERROR)
    failures.logger.addHandler(handler)
    try:
        yield blocked, errors
    finally:
        failures.logger.removeHandler(handler)
        handler.close()
        assert blocked.read_bytes() == b"keep obstruction"


@pytest.mark.parametrize("native_path", [
    "/private/resource-state.json", r"C:\private\resource-state.json",
    r"\\server\share\resource-state.json", r"\\?\UNC\server\share\resource-state.json",
])
@pytest.mark.parametrize("error_type, status", [(ValueError, 400), (KeyError, 404)])
def test_unknown_builtin_error_never_becomes_public_text_or_loses_its_lease(
    browser_resource_api_client: TestClient, browser_owner_bundle: BrowserOwnerFixture,
    caplog: pytest.LogCaptureFixture, resource_failure_sink: tuple[Path, list[BaseException]] | None,
    native_path: str, error_type: type[Exception], status: int,
) -> None:
    created = browser_resource_api_client.post("/api/v1/resources/browsers", json={"session_id": SESSION_A})
    assert created.status_code == 200, created.text
    resource = created.json()["data"]
    rid = resource["resource_id"]
    cause = OSError(5, "原始诊断", native_path)
    error = error_type(f"读取状态失败: {native_path}")
    error.__cause__ = cause
    browser_owner_bundle.transport.failure = error
    command = {"action": "read", "expected_revision": resource["owner_revision"], "thread": resource["source_thread"]}
    failed = browser_resource_api_client.post(f"/api/v1/resources/{rid}/browser-operation", json=command)
    assert failed.status_code == status, failed.text
    detail = failed.json()["detail"]
    assert "stage=resource_operation_unknown" in detail
    assert f"error={error_type.__name__}" in detail and f"resource_id={rid}" in detail
    assert "resource-state.json" not in failed.text and "读取状态失败" not in failed.text
    assert error.__cause__ is cause
    if resource_failure_sink is None:
        assert any(record.exc_info and record.exc_info[1] is error for record in caplog.records)
        assert str(error) in caplog.text and str(cause) in caplog.text
    else:
        blocked, logged = resource_failure_sink
        assert logged == [error, error]
        assert str(blocked) in "\n".join(error.__notes__)
        assert "diagnostic_sink_error=NotADirectoryError" in detail
    saved = json.loads((browser_owner_bundle.root / ".boxteam/browser-resources.json").read_text())
    assert saved["records"][0]["error"] == detail
    assert "resource-state.json" not in json.dumps(saved)
    browser_owner_bundle.restart()
    observed = browser_resource_api_client.get(f"/api/v1/resources/{rid}")
    assert observed.status_code == 200, observed.text
    current = observed.json()["data"]
    assert current["error"] == detail and current["reconciliation_status"] == "unknown"
    assert current["available_actions"] == ["stop"]
    pending, = browser_owner_bundle.leases.leases_for_resource(rid)
    assert pending.status == "reconcile_required"
    retried = browser_resource_api_client.post(f"/api/v1/resources/{rid}/browser-operation", json=command)
    assert retried.status_code == 409, retried.text
    assert len(browser_owner_bundle.transport.calls) == 1


def test_known_transport_timeout_keeps_recovery_message_when_diagnostics_fail(
    browser_resource_api_client: TestClient, browser_owner_bundle: BrowserOwnerFixture,
    browser_manager_client: BrowserManagerClient, monkeypatch: pytest.MonkeyPatch, resource_http,
    resource_failure_sink: tuple[Path, list[BaseException]] | None,
) -> None:
    created = browser_resource_api_client.post("/api/v1/resources/browsers", json={"session_id": SESSION_A})
    assert created.status_code == 200, created.text
    resource = created.json()["data"]
    rid = resource["resource_id"]
    cause = httpx.ReadTimeout("原始连接诊断: /private/transport-state.json")
    attempts: list[str] = []

    def timeout(request: httpx.Request) -> None:
        attempts.append(str(request.url))
        raise cause

    resource_http(timeout)
    monkeypatch.setattr(browser_owner_bundle.transport, "perform", browser_manager_client.perform)
    command = {"action": "run", "payload": {"timeoutMs": 25},
               "expected_revision": resource["owner_revision"], "thread": resource["source_thread"]}
    failed = browser_resource_api_client.post(f"/api/v1/resources/{rid}/browser-operation", json=command)
    assert failed.status_code == 408, failed.text
    detail = failed.json()["detail"]
    assert "浏览器管理器操作超时" in detail and "timeoutMs=25" in detail
    assert "code=browser_tool_timeout" in detail and "retryable=true" in detail and "recovery=page_reset" in detail
    assert "transport-state.json" not in detail
    if resource_failure_sink is not None:
        blocked, logged = resource_failure_sink
        assert len(logged) == 2 and logged[0] is logged[1]
        assert logged[0].__cause__ is cause and str(blocked) in "\n".join(logged[0].__notes__)
    browser_owner_bundle.restart()
    current = browser_resource_api_client.get(f"/api/v1/resources/{rid}").json()["data"]
    assert current["error"] == detail and current["reconciliation_status"] == "unknown"
    pending, = browser_owner_bundle.leases.leases_for_resource(rid)
    assert pending.status == "reconcile_required"
    assert attempts == [f"http://browser.test/api/gateway/service-plugins/boxteam.developer-tools/browser/services/http/api/browsers/{rid}/run?package_version=0.1.0&workspace_id={WORKSPACE_ID}"]
