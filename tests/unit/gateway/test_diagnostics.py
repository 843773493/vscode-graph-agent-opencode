from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from json import JSONDecodeError
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from fastapi import HTTPException, Request
from pydantic import ValidationError

from app.core.logging_contract import load_runtime_log_contract
from app.gateway.control import router as control_router
from app.gateway.credentials import FederationCredential, FederationCredentialStore
from app.gateway.diagnostics import (
    _candidate_gateway_logs,
    _launcher_log_path,
    _LogCandidate,
    _process_log_path,
    _scope_log_candidates,
    collect_gateway_diagnostics,
)
from app.gateway.registry import GatewayWorkspaceRegistry
from app.schemas.gateway.diagnostics import GatewayDiagnosticLogSource


def _log_candidate(
    log_id: str,
    *,
    source: GatewayDiagnosticLogSource,
    workspace_id: str | None = None,
) -> _LogCandidate:
    return _LogCandidate(
        log_id=log_id,
        source=source,
        service="test",
        label=log_id,
        log_path=Path("candidate-logs") / f"{log_id.replace(':', '-')}.log",
        workspace_id=workspace_id,
    )


def test_workspace_scope_keeps_launcher_and_selected_workspace_logs() -> None:
    gateway_candidates = [
        _log_candidate("gateway:launcher", source="gateway"),
        _log_candidate("gateway:file:local-backend-8010.log", source="gateway"),
    ]
    workspace_candidates = [
        _log_candidate("workspace:home:workspace_api", source="workspace", workspace_id="home"),
        _log_candidate("workspace:other:workspace_api", source="workspace", workspace_id="other"),
    ]

    scoped = _scope_log_candidates(gateway_candidates, workspace_candidates, "home")

    assert [candidate.log_id for candidate in scoped] == [
        "gateway:launcher",
        "workspace:home:workspace_api",
    ]


def test_empty_workspace_scope_keeps_all_log_sources() -> None:
    gateway_candidates = [_log_candidate("gateway:launcher", source="gateway")]
    workspace_candidates = [_log_candidate("workspace:home:workspace_api", source="workspace", workspace_id="home")]

    scoped = _scope_log_candidates(gateway_candidates, workspace_candidates, None)

    assert scoped == [*gateway_candidates, *workspace_candidates]


def test_diagnostics_finds_role_logs_and_launcher_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOXTEAM_HOME", str(tmp_path))
    override = tmp_path / "显式路径/launcher.log"
    monkeypatch.setenv("BOXTEAM_SERVICE_LOG_PATH", str(override))
    contract = load_runtime_log_contract()
    root = contract.root_for(tmp_path)
    paths = [contract.path_for(root, "gateway"), contract.path_for(root, "backend", 12345), contract.path_for(root, "ssh-port-forward", "pf_test")]
    for log_path in paths:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("original diagnostics\n")
    assert _process_log_path("workspace_api", 12345) == paths[1]
    assert _launcher_log_path() == override
    candidates = _candidate_gateway_logs({paths[1].name})
    assert {candidate.log_path for candidate in candidates} == {override, paths[0], paths[2]}


@pytest.mark.parametrize("native_path", [
    "/private/diagnostic-selector-marker", r"C:\private\diagnostic-selector-marker",
    r"\\server\private\diagnostic-selector-marker",
], ids=["posix", "drive", "unc"])
@pytest.mark.parametrize("selector", ["workspace", "log"])
async def test_missing_diagnostic_selection_does_not_expand_the_raw_log_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    native_path: str, selector: str,
) -> None:
    monkeypatch.setenv("BOXTEAM_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("BOXTEAM_SERVICE_LOG_PATH", str(tmp_path / "launcher.log"))
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "registry.json")
    with pytest.raises(LookupError) as caught:
        await collect_gateway_diagnostics(
            registry, gateway_id="gateway_diagnostic_selector",
            selected_workspace_id=native_path if selector == "workspace" else None,
            selected_log_id=native_path if selector == "log" else None,
        )
    assert type(caught.value) is LookupError
    assert "diagnostic-selector-marker" not in str(caught.value)
    assert f"stage=gateway-diagnostic-{selector}" in str(caught.value)
    cause = caught.value.__cause__
    assert type(cause) is LookupError and native_path in cause.args[0]
    assert any(record.exc_info is not None and record.exc_info[1] is cause for record in caplog.records)
    assert registry.targets() == () and registry.registry_revision == 0


@pytest.fixture
def remote_diagnostics(monkeypatch: pytest.MonkeyPatch) -> Iterator[
    tuple[Mock, list[httpx.Response | httpx.RequestError], list[httpx.Request]]
]:
    registry = Mock(spec=GatewayWorkspaceRegistry)
    registry.remote_gateway_connection.return_value = SimpleNamespace(
        name="原远端", remote_gateway_id="gateway_00000000000040008000000000000002",
    )
    registry.remote_gateway_url.return_value = "http://diagnostics.test"
    credentials = Mock(spec=FederationCredentialStore)
    credentials.get.return_value = FederationCredential(
        connection_id="rgw_diagnostics", peer_gateway_id="gateway_peer", token="private-test-token",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    monkeypatch.setattr(control_router, "FederationCredentialStore", lambda **_: credentials)
    responses: list[httpx.Response | httpx.RequestError] = []
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        response = responses.pop(0)
        if isinstance(response, httpx.RequestError):
            raise response
        return response

    client_class = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **options: client_class(
        **options, transport=httpx.MockTransport(respond), trust_env=False,
    ))
    yield registry, responses, requests


@pytest.fixture
def diagnostics_request() -> Request:
    return Request({
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/gateway/diagnostics",
        "raw_path": b"/api/gateway/diagnostics",
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 123),
        "server": ("testserver", 80),
    })


@pytest.mark.parametrize("native_path", [
    "/private/diagnostics-marker", r"C:\private\diagnostics-marker", r"\\server\private\diagnostics-marker",
], ids=["posix", "drive", "unc"])
@pytest.mark.parametrize("failure", ["http-403", "http-503", "transport", "validation", "json"])
async def test_remote_diagnostics_failure_keeps_raw_logs_in_their_named_boundary(
    remote_diagnostics: tuple[Mock, list[httpx.Response | httpx.RequestError], list[httpx.Request]],
    diagnostics_request: Request, native_path: str, failure: str, caplog: pytest.LogCaptureFixture,
) -> None:
    registry, responses, requests = remote_diagnostics
    diagnostics = {
        "gateway_id": "gateway_peer", "gateway_name": "来源", "connection_kind": "local",
        "status": "ready", "checked_at": "2026-10-29T00:00:00Z",
        "logs": [{"log_id": "gateway-stderr", "source": "gateway", "service": "gateway",
                  "label": "原始日志", "status": "available", "tail": native_path}],
    }
    native_error = httpx.ReadError("远端诊断读取失败: " + native_path)
    if failure.startswith("http-"):
        responses.append(httpx.Response(int(failure.removeprefix("http-")), json={"detail": native_path}))
        category, stage, status = httpx.HTTPStatusError, "diagnostics_request", 502
    elif failure == "transport":
        responses.append(native_error)
        category, stage, status = httpx.ReadError, "diagnostics_request", 502
    else:
        responses.append(
            httpx.Response(200, json={"data": {**diagnostics, "logs": native_path}})
            if failure == "validation" else httpx.Response(200, content=b'{"data":')
        )
        category = ValidationError if failure == "validation" else JSONDecodeError
        stage, status = "diagnostics_response", 400
    responses.append(httpx.Response(200, json={"data": diagnostics}))

    async def invoke():
        return await control_router.get_gateway_diagnostics(
            request=diagnostics_request,
            gateway_connection_id="rgw_diagnostics", workspace_id=None, log_id=None,
            tail_lines=300, _="local-token", request_id="req_diagnostics_boundary", registry=registry,
        )

    with pytest.raises(HTTPException) as caught:
        await invoke()
    assert caught.value.status_code == status
    assert "diagnostics-marker" not in str(caught.value.detail)
    assert f"stage={stage}" in str(caught.value.detail)
    assert isinstance(caught.value.__cause__, category)
    original = caught.value.__cause__
    diagnostic, = [record for record in caplog.records if record.name == "app.core.failures"]
    assert diagnostic.exc_info is not None and diagnostic.exc_info[1] is original
    if failure == "transport":
        assert original is native_error
    if failure.startswith("http-"):
        assert isinstance(original, httpx.HTTPStatusError)
        assert original.response.json()["detail"] == native_path
        assert original.response.text in caplog.text
        assert f"status={original.response.status_code}" in str(caught.value.detail)
    if isinstance(original, ValidationError):
        assert original.errors()[0]["input"] == native_path
    if failure != "json":
        assert "diagnostics-marker" in caplog.text
    recovered = await invoke()
    assert recovered.data is not None
    assert recovered.data.logs[0].tail == native_path
    assert recovered.data.gateway_name == "原远端"
    assert recovered.data.gateway_connection_id == "rgw_diagnostics"
    assert [request.headers["X-Request-ID"] for request in requests] == ["req_diagnostics_boundary"] * 2
    assert all(request.headers["X-BoxTeam-Federation-Token"] == "private-test-token" for request in requests)
    assert not responses


@pytest.mark.parametrize("error, status", [(KeyError("未知远端"), 404), (ValueError("工作区不属于所选远端"), 400)])
async def test_remote_diagnostics_preflight_preserves_business_rejection(
    remote_diagnostics: tuple[Mock, list[httpx.Response | httpx.RequestError], list[httpx.Request]],
    diagnostics_request: Request, error: Exception, status: int,
) -> None:
    registry, _, requests = remote_diagnostics
    registry.remote_gateway_connection.side_effect = error
    with pytest.raises(HTTPException) as caught:
        await control_router.get_gateway_diagnostics(
            request=diagnostics_request,
            gateway_connection_id="rgw_diagnostics", workspace_id=None, log_id=None,
            tail_lines=300, _="local-token", request_id="req_diagnostics_preflight", registry=registry,
        )
    assert caught.value.status_code == status and caught.value.detail == str(error)
    assert caught.value.__cause__ is error and not requests


@pytest.mark.parametrize("target_gateway_id", [
    "gateway_00000000000040008000000000000001", "", "/private/diagnostic-target",
])
async def test_remote_diagnostics_rejects_conflicting_or_invalid_gateway_before_upstream(
    remote_diagnostics: tuple[Mock, list[httpx.Response | httpx.RequestError], list[httpx.Request]],
    diagnostics_request: Request, target_gateway_id: str,
) -> None:
    registry, _, requests = remote_diagnostics
    with pytest.raises(HTTPException) as caught:
        await control_router.get_gateway_diagnostics(
            request=diagnostics_request,
            gateway_connection_id="rgw_diagnostics", workspace_id=None, log_id=None,
            tail_lines=300, _="local-token", request_id="req_diagnostics_target",
            registry=registry, target_gateway_id=target_gateway_id,
        )
    assert caught.value.status_code == 400
    registry.remote_gateway_url.assert_not_called()
    assert not requests
