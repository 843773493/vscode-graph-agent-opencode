"""受信密钥解析复用连接与请求身份；不重试、不泄漏回执中的密钥。"""

import copy

import httpx
import pytest
from boxteam_plugin_sdk.request_context import (
    reset_current_request_id,
    set_current_request_id,
)

from app.core.trace_middleware import reset_current_gateway_id, set_current_gateway_id
from app.services.infrastructure.gateway_client_common import GatewayTransportConnection
from app.services.infrastructure.gateway_secrets.client import GatewaySecretsClient

GATEWAY_ID = "gateway_00000000000040008000000000000001"
SECRET_ID = "sec_" + "1" * 32


@pytest.fixture
def resolver(monkeypatch: pytest.MonkeyPatch):
    requests = []
    reply = {"status": 200, "request_id": "req_secret_resolve", "data": {
        "gateway_id": GATEWAY_ID, "items": {SECRET_ID: {"revision": 2, "value": "private-test-key"}},
    }}

    async def post(_client, url, **kwargs):
        requests.append({"url": url, **kwargs})
        return httpx.Response(reply["status"], headers={"X-Request-ID": reply["request_id"]},
            request=httpx.Request("POST", url), json={"request_id": reply["request_id"], "data": reply["data"]})

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    monkeypatch.setattr("app.services.infrastructure.gateway_secrets.client.get_gateway_local_token", lambda: "test-local-token")
    request_token = set_current_request_id("req_secret_resolve")
    gateway_token = set_current_gateway_id(GATEWAY_ID)
    try:
        yield GatewaySecretsClient(GatewayTransportConnection(gateway_url="http://127.0.0.1:12345/")), requests, reply
    finally:
        reset_current_gateway_id(gateway_token)
        reset_current_request_id(request_token)


async def test_named_secret_batch_reuses_transport_and_request_identity(resolver) -> None:
    client, requests, _reply = resolver
    assert await client.resolve([]) == {} and requests == []
    resolved = await client.resolve([SECRET_ID, SECRET_ID])
    assert requests == [{"url": "http://127.0.0.1:12345/api/gateway/internal/secrets/resolve",
        "json": {"ids": [SECRET_ID]}, "headers": {"X-Local-Token": "test-local-token", "X-Request-ID": "req_secret_resolve"}}]
    assert resolved[SECRET_ID].revision == 2
    assert resolved[SECRET_ID].value.get_secret_value() == "private-test-key"
    assert "private-test-key" not in repr(resolved)


@pytest.mark.parametrize("fault", ["request_id", "gateway_id", "missing", "rejected", "invalid_value"])
async def test_named_secret_bad_receipt_is_rejected_once_without_secret_in_error(resolver, fault: str) -> None:
    client, requests, reply = resolver
    reply["data"] = copy.deepcopy(reply["data"])
    if fault == "request_id":
        reply["request_id"] = "req_another"
    elif fault == "gateway_id":
        reply["data"]["gateway_id"] = "gateway_00000000000040008000000000000002"
    elif fault == "missing":
        reply["data"]["items"] = {}
    elif fault == "rejected":
        reply["status"] = 404
    else:
        reply["data"]["items"][SECRET_ID]["value"] = {"raw": "private-test-key"}
    with pytest.raises((RuntimeError, ValueError)) as caught:
        await client.resolve([SECRET_ID])
    assert len(requests) == 1
    assert "private-test-key" not in str(caught.value)


async def test_named_secret_requires_gateway_scope_before_network(resolver) -> None:
    client, requests, _reply = resolver
    token = set_current_gateway_id(None)
    try:
        with pytest.raises(RuntimeError, match="gateway_id"):
            await client.resolve([SECRET_ID])
        assert requests == []
    finally:
        reset_current_gateway_id(token)
