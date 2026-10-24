"""工作区 HTTP port 的失败只投影稳定事实，原始响应保留诊断。"""

from __future__ import annotations

import copy
import json
from collections.abc import Awaitable, Callable

import httpx
import pytest

from app.core.session_control_communication_ledger.observations import (
    OBSERVATION_ERROR_CODES,
    observation_error_code,
)
from app.gateway.federation.errors import (
    FEDERATION_SESSION_MAIN_UNAVAILABLE,
    FEDERATION_TARGET_OWNER_REJECTED,
    FederationError,
)
from app.schemas.internal_v2.communication import CommunicationAcceptanceRequest
from tests.unit.gateway.test_federation_workspace_port import (
    REQUEST_ID,
    TARGET_SESSION,
    WORKSPACE_ID,
    _HTTPContext,
)
from tests.unit.gateway.test_federation_workspace_port import (
    http_context as http_context,  # noqa: PLC0414 — 复用原 HTTP port 装配
)
from tests.unit.gateway.test_federation_workspace_port import (
    read_body as read_body,  # noqa: PLC0414 — 复用原受认证通信 envelope
)


@pytest.fixture(params=["catalog", "main", "read", "send"])
def entrypoint(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture(params=["/private/workspace/upstream", r"C:\private\workspace\upstream", r"\\server\private\workspace\upstream"])
def native_path(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture
def http_invocation(
    entrypoint: str, http_context: _HTTPContext, read_body: dict[str, object],
) -> Callable[[], Awaitable[object]]:
    if entrypoint == "catalog":
        http_context.data = {"request_id": REQUEST_ID, "data": {"items": [{"session_id": TARGET_SESSION}]}}
        return lambda: http_context.catalog.local_session_workspaces(TARGET_SESSION)
    if entrypoint == "main":
        return lambda: http_context.port.resolve_main_thread(
            gateway_id=read_body["target"]["gateway_id"], workspace_id=WORKSPACE_ID, session_id=TARGET_SESSION,
        )
    if entrypoint == "send":
        request = CommunicationAcceptanceRequest(
            communication_id="comm_http_diagnostics", source=read_body["source"],
            target=read_body["target"], payload={"content": "原通信正文"}, request_id=REQUEST_ID,
        )
        body = request.model_dump(mode="json")
    else:
        body = read_body
    return lambda: http_context.port.execute(
        operation=entrypoint, target=body["target"], body=body, origin="gateway_source", timeout=5,
    )


@pytest.fixture
def http_replies(http_context: _HTTPContext, monkeypatch: pytest.MonkeyPatch) -> list[httpx.Response]:
    replies: list[httpx.Response] = []
    original = httpx.AsyncClient.request

    async def record(client, method, url, **kwargs):
        response = await original(client, method, url, **kwargs)
        replies.append(response)
        return response

    monkeypatch.setattr(httpx.AsyncClient, "request", record)
    return replies


@pytest.mark.parametrize("failure_type", [httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError])
async def test_workspace_connection_error_preserves_type_cause_and_original_retry(
    http_invocation: Callable[[], Awaitable[object]], entrypoint: str,
    native_path: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    failure_type: type[httpx.RequestError],
) -> None:
    sent: list[dict[str, object]] = []
    failures: list[httpx.RequestError] = []
    rejected = True
    original = httpx.AsyncClient.request

    async def request(client, method, url, **kwargs):
        sent.append(copy.deepcopy({
            "method": method, "url": url, "headers": kwargs["headers"],
            "body": kwargs.get("json"), "timeout": kwargs["timeout"],
        }))
        if rejected:
            failure = failure_type("连接失败: " + native_path, request=httpx.Request(method, url, headers=kwargs["headers"]))
            failures.append(failure)
            raise failure
        return await original(client, method, url, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "request", request)
    expected_type = FederationError if entrypoint == "catalog" else failure_type
    with pytest.raises(expected_type) as caught:
        await http_invocation()
    assert len(failures) == 1 and caught.value.__cause__ is failures[0]
    assert native_path in str(failures[0])
    public = str(caught.value)
    assert native_path not in public and "private" not in public
    assert f"request_id={REQUEST_ID}" in public and f"error_type={failure_type.__name__}" in public
    assert any(record.exc_info and record.exc_info[1] is failures[0] for record in caplog.records)
    if entrypoint == "catalog":
        assert caught.value.code == FEDERATION_SESSION_MAIN_UNAVAILABLE
        assert "stage=catalog_request" in public and "private" not in json.dumps(caught.value.to_wire())
    else:
        assert type(caught.value) is failure_type and caught.value.request is failures[0].request
        assert "stage=connection" in public
    rejected = False
    assert await http_invocation() is not None
    assert sent[0] == sent[1]


@pytest.mark.parametrize("failure_format", ["json", "plain", "malformed_json"])
async def test_workspace_http_rejection_logs_body_without_disclosing_or_reclassifying(
    http_invocation: Callable[[], Awaitable[object]], http_context: _HTTPContext,
    http_replies: list[httpx.Response], native_path: str, failure_format: str,
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    valid = copy.deepcopy(http_context.data)
    http_context.status = 409 if failure_format == "json" else 503
    if failure_format == "json":
        http_context.data = {"request_id": REQUEST_ID, "detail": {"message": native_path}}
    else:
        http_context.raw = (("{\"detail\":\"" if failure_format == "malformed_json" else "upstream failure: ") + native_path).encode()
    if failure_format == "malformed_json":
        original = httpx.AsyncClient.request

        async def declared_json(client, method, url, **kwargs):
            response = await original(client, method, url, **kwargs)
            response.headers["Content-Type"] = "application/json"
            return response

        monkeypatch.setattr(httpx.AsyncClient, "request", declared_json)
    with pytest.raises(FederationError) as caught:
        await http_invocation()
    assert caught.value.code == FEDERATION_SESSION_MAIN_UNAVAILABLE
    assert caught.value.__cause__ is None
    assert "private" not in json.dumps(caught.value.to_wire())
    assert f"status={http_context.status}" in str(caught.value)
    assert any(http_replies[0].text[:2000] in record.getMessage() for record in caplog.records)
    http_context.status, http_context.raw, http_context.data = 200, None, valid
    assert await http_invocation() is not None
    first, second = http_context.requests
    assert (first.method, first.url, first.headers, first.content, first.extensions) == (
        second.method, second.url, second.headers, second.content, second.extensions,
    )


async def test_workspace_invalid_success_json_preserves_raw_cause_and_request(
    http_invocation: Callable[[], Awaitable[object]], http_context: _HTTPContext,
    http_replies: list[httpx.Response], native_path: str, caplog: pytest.LogCaptureFixture,
) -> None:
    http_context.raw = ("invalid JSON: " + native_path).encode()
    with pytest.raises(FederationError) as caught:
        await http_invocation()
    assert caught.value.code == FEDERATION_SESSION_MAIN_UNAVAILABLE
    assert "private" not in json.dumps(caught.value.to_wire())
    assert f"request_id={REQUEST_ID}, stage=response_json, error_type=JSONDecodeError" in str(caught.value)
    original = caught.value.__cause__
    assert isinstance(original, json.JSONDecodeError) and original.doc == http_context.raw.decode()
    assert any(record.exc_info and record.exc_info[1] is original for record in caplog.records)
    assert any(http_replies[0].text[:2000] in record.getMessage() for record in caplog.records)
    http_context.raw = None
    assert await http_invocation() is not None
    first, second = http_context.requests
    assert (first.method, first.url, first.headers, first.content, first.extensions) == (
        second.method, second.url, second.headers, second.content, second.extensions,
    )


@pytest.mark.parametrize("code", sorted(OBSERVATION_ERROR_CODES))
async def test_workspace_observation_rejection_keeps_exact_registered_owner_code(
    http_context: _HTTPContext, read_body: dict[str, object], native_path: str,
    http_replies: list[httpx.Response], code: str, caplog: pytest.LogCaptureFixture,
) -> None:
    http_context.status = 409
    http_context.data = {"request_id": REQUEST_ID, "detail": {"code": code, "message": native_path}}
    with pytest.raises(FederationError) as caught:
        await http_context.port.execute(
            operation="read", target=read_body["target"], body=read_body, origin="gateway_source", timeout=5,
        )
    assert caught.value.code == FEDERATION_TARGET_OWNER_REJECTED
    assert caught.value.detail == {"owner": "communication_observation", "code": code}
    assert observation_error_code(caught.value.to_wire()) == code
    assert "private" not in json.dumps(caught.value.to_wire())
    assert any(http_replies[0].text[:2000] in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("owner,path,status", [
    ("fork_source_capture", "/api/v1/sessions/fork-transfer", 409),
    ("resource_file_operations", "/api/v1/resources/navigation", 400),
])
@pytest.mark.parametrize("identity", ["valid", "body_drift", "header_drift"])
async def test_workspace_named_rejection_requires_original_request_identity(
    http_context: _HTTPContext, owner: str, path: str, status: int, identity: str,
) -> None:
    # 本地翻译层把具名 owner 的正文透传，具体字段校验仍由原消费者负责。
    detail = {
        "owner": owner,
        "code": "fork_source_capture_incomplete" if owner == "fork_source_capture" else "output_limit_exceeded",
        "action": "capture",
    }
    http_context.status = status
    http_context.data = {"request_id": REQUEST_ID if identity != "body_drift" else "other", "detail": detail}
    http_context.header_request_id = REQUEST_ID if identity != "header_drift" else "other"
    with pytest.raises(FederationError) as caught:
        await http_context.port._request(
            workspace_id=WORKSPACE_ID, method="POST", path=path, origin="gateway_source", body={},
        )
    if identity == "valid":
        assert caught.value.code == FEDERATION_TARGET_OWNER_REJECTED and caught.value.detail == detail
    else:
        assert caught.value.code == FEDERATION_SESSION_MAIN_UNAVAILABLE and caught.value.detail == {}
        assert "request_id 漂移" in caught.value.message
    request, = http_context.requests
    assert request.headers["X-Request-ID"] == REQUEST_ID
    assert request.headers["X-BoxTeam-Communication-Origin"] == "gateway_source"


@pytest.mark.parametrize("path,status,detail,code,owner_detail", [
    ("/api/v1/context/read", 403, "unshared", "target_not_resolvable", {}),
    ("/api/v1/context/search", 404, "missing", "target_not_resolvable", {}),
    ("/api/v1/resources/navigation", 403, "unshared", "target_not_resolvable", {}),
    ("/api/v1/context/read", 400, {"code": "unsupported_view"}, FEDERATION_TARGET_OWNER_REJECTED, {"owner": "resource_uri_resolver", "code": "unsupported_view"}),
    ("/api/v1/context/search", 409, {"code": "snapshot_changed"}, FEDERATION_TARGET_OWNER_REJECTED, {"owner": "context_revision", "code": "snapshot_changed"}),
    (f"/api/v1/sessions/{TARGET_SESSION}/communication-observations", 409, {"code": "read-snapshot-invalid"}, FEDERATION_TARGET_OWNER_REJECTED, {"owner": "communication_observation", "code": "read-snapshot-invalid"}),
])
@pytest.mark.parametrize("identity", ["valid", "body_drift", "header_drift", "body_missing"])
async def test_workspace_semantic_rejection_requires_original_request_identity(
    http_context: _HTTPContext, path: str, status: int, detail: object,
    code: str, owner_detail: dict[str, object], identity: str,
) -> None:
    http_context.status = status
    http_context.data = {"request_id": REQUEST_ID if identity != "body_drift" else "other", "detail": detail}
    if identity == "body_missing":
        del http_context.data["request_id"]
    http_context.header_request_id = REQUEST_ID if identity != "header_drift" else "other"
    with pytest.raises(FederationError) as caught:
        await http_context.port._request(
            workspace_id=WORKSPACE_ID, method="POST", path=path, origin="gateway_source", body={},
        )
    if identity == "valid":
        assert caught.value.code == code and caught.value.detail == owner_detail
    else:
        assert caught.value.code == FEDERATION_SESSION_MAIN_UNAVAILABLE and caught.value.detail == {}
        assert "request_id 漂移" in caught.value.message
