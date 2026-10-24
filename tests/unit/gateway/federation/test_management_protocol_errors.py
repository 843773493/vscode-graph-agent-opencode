"""联邦发现与管理共用的响应解析只将原始正文写入诊断。"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.gateway.credentials import FederationCredential
from app.gateway.federation import (
    discover_remote_gateway,
    request_remote_gateway_management,
)

NATIVE_PATHS = ["/private/remote-marker", r"C:\private\remote-marker", r"\\host\share\remote-marker"]


@dataclass
class RemoteReply:
    payload: object = None
    status: int = 200
    content: bytes | None = None
    requests: list[httpx.Request] = field(default_factory=list)


@pytest.fixture(params=["management", "manifest", "workspaces"])
def entry(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture
def remote_reply() -> RemoteReply:
    return RemoteReply()


@pytest.fixture
def invoke_remote(
    entry: str, remote_reply: RemoteReply, monkeypatch: pytest.MonkeyPatch,
) -> Callable[[], Awaitable[object]]:
    credential = FederationCredential(
        connection_id="rgw_protocol", peer_gateway_id="gateway_local",
        token="private-federation-token", expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    client_class = httpx.AsyncClient

    def respond(request: httpx.Request) -> httpx.Response:
        remote_reply.requests.append(request)
        if entry == "management" or request.url.path.endswith("/" + entry):
            if remote_reply.content is not None:
                return httpx.Response(remote_reply.status, content=remote_reply.content)
            return httpx.Response(remote_reply.status, json=remote_reply.payload)
        return httpx.Response(200, json={"data": {
            "protocol_version": 1, "gateway_id": "gateway_remote", "items": [],
        }})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **options: client_class(
        **options, transport=httpx.MockTransport(respond), trust_env=False,
    ))
    if entry == "management":
        return lambda: request_remote_gateway_management(
            gateway_url="http://127.0.0.1:41000", credential=credential, method="GET",
            path="/api/gateway/federation/diagnostics", request_id="request-protocol-test",
        )
    return lambda: discover_remote_gateway(
        gateway_url="http://127.0.0.1:41000", credential=credential,
    )


@pytest.mark.parametrize("shape", ["root-string", "root-list", "missing", "null", "data-string", "data-list"])
async def test_missing_data_is_explicit_without_copying_response_body(
    invoke_remote: Callable[[], Awaitable[object]], remote_reply: RemoteReply,
    shape: str, entry: str, caplog: pytest.LogCaptureFixture,
) -> None:
    body = "\n".join(NATIVE_PATHS) + "x" * 2048
    remote_reply.payload = {
        "root-string": body, "root-list": [body], "missing": {"detail": body},
        "null": {"data": None, "detail": body}, "data-string": {"data": body},
        "data-list": {"data": [body]},
    }[shape]
    with pytest.raises(RuntimeError) as caught:
        await invoke_remote()
    assert type(caught.value) is RuntimeError
    assert str(caught.value) == "远程 Gateway 响应缺少 data 对象: status=200"
    assert len(remote_reply.requests) == (1 if entry == "management" else 2)
    for request in remote_reply.requests:
        assert request.headers["X-BoxTeam-Federation-Token"] == "private-federation-token"
        assert request.headers["X-Request-ID"]
    records = [record for record in caplog.records if record.name == "app.gateway.federation"]
    assert len(records) == 1 and records[0].levelno == logging.ERROR
    assert json.loads(records[0].args[-1]) == remote_reply.payload
    assert "private-federation-token" not in caplog.text


async def test_http_rejection_keeps_original_status_and_response(
    invoke_remote: Callable[[], Awaitable[object]], remote_reply: RemoteReply,
    caplog: pytest.LogCaptureFixture,
) -> None:
    remote_reply.status = 409
    remote_reply.payload = {"detail": {"status": "blocked", "blockers": [{"reason": "运行中"}]}}
    with pytest.raises(httpx.HTTPStatusError) as caught:
        await invoke_remote()
    assert caught.value.response.status_code == 409
    assert caught.value.response.json() == remote_reply.payload
    assert not caplog.records


async def test_invalid_json_keeps_existing_decoder_error(
    invoke_remote: Callable[[], Awaitable[object]], remote_reply: RemoteReply,
) -> None:
    remote_reply.content = b'{"invalid":'
    with pytest.raises(json.JSONDecodeError) as caught:
        await invoke_remote()
    assert caught.value.doc == remote_reply.content.decode()


async def test_valid_payload_keeps_original_content(
    invoke_remote: Callable[[], Awaitable[object]], remote_reply: RemoteReply,
    entry: str, caplog: pytest.LogCaptureFixture,
) -> None:
    data = {"protocol_version": 1, "gateway_id": "gateway_remote", "items": [], "tail": "\n".join(NATIVE_PATHS)}
    remote_reply.payload = {"data": data}
    result = await invoke_remote()
    assert result == (data if entry == "management" else (data if entry == "manifest" else {
        "protocol_version": 1, "gateway_id": "gateway_remote", "items": [],
    }, []))
    assert not caplog.records
