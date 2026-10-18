from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from json import JSONDecodeError
from unittest.mock import Mock

import httpx
import pytest
from pydantic import ValidationError

from app.abstractions.session_context import WorkspaceSessionContextAccessError
from app.core.identifier import create_uuid_hex
from app.core.resource_uri import thread_resource_uri
from app.core.trace_middleware import (
    reset_current_gateway_id,
    reset_current_request_id,
    set_current_gateway_id,
    set_current_request_id,
)
from app.gateway.federation.grants import ForkGrantTarget
from app.schemas.internal_v2.session import SessionForkRequest
from app.schemas.internal_v2.session_context import (
    SessionContextReadRequest,
)
from app.schemas.internal_v2.session_fork_transfer import SessionForkTransferRequest
from app.services.business.session_context_resource import (
    session_resource_identity,
    session_resource_uri,
    workspace_session_inventory_uri,
)
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.gateway_session_context_client import (
    GatewaySessionContextClient,
)


@pytest.fixture(autouse=True)
def _hermetic_proxy_env(monkeypatch: pytest.MonkeyPatch):
    """R18 测试内 hermetic 修复：清掉本机代理环境变量。

    本文件用例全部针对 127.0.0.1 假端点构造 httpx 客户端（request 均被
    monkeypatch 替换），自身不经任何网络；而环境的 NO_PROXY 含 IPv6 字
    面量（``::1``/``[::1]``）会让 httpx 客户端构造时的代理解析直接抛
    ``InvalidURL: Invalid port: ':1]'``——属外部环境噪声混入测试。与
    R17 的 /tmp→tmp_path 同类测试卫生修复（任务书 §2.2 许可的 monkeypatch
    清 ``*_proxy`` 做法）；清掉后客户端不取代理，与用例意图一致。
    """
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "NO_PROXY",
        "no_proxy",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def gateway_local_token_supplier() -> Callable[[], str]:
    return lambda: "gateway-context-test-token"


@pytest.fixture
async def gateway_clients() -> AsyncIterator[list[GatewaySessionContextClient]]:
    clients: list[GatewaySessionContextClient] = []
    yield clients
    for client in clients:
        await client.aclose()


@pytest.fixture
def gateway_fork_request() -> Iterator[SessionForkTransferRequest]:
    gateway_token = set_current_gateway_id("gateway_local")
    request_token = set_current_request_id("req_gateway_fork")
    source_uri = thread_resource_uri("ws_source", f"ses_{create_uuid_hex()}", f"thr_{create_uuid_hex()}", gateway_authority="gateway_local")
    try:
        yield SessionForkTransferRequest(
            source_gateway_id="gateway_local", action="intent",
            fork_target=ForkGrantTarget(gateway_id="gateway_local", uri=source_uri, fork_role="source"),
            request=SessionForkRequest(
                idempotency_key="gateway-fork", source_uri=source_uri,
                target_container_uri=workspace_session_inventory_uri("ws_target", gateway_authority="gateway_target"),
                mode="full_rollout_copy", source_view_id="view-frozen", source_view_revision=7,
            ),
        )
    finally:
        reset_current_request_id(request_token)
        reset_current_gateway_id(gateway_token)


def _read_request(workspace_id: str):
    session_id = f"ses_{create_uuid_hex()}"
    return SessionContextReadRequest(
        resource_identity=session_resource_identity(workspace_id, session_id),
        uri=session_resource_uri(workspace_id, session_id),
        scope="workspace",
    )


@pytest.mark.asyncio
async def test_gateway_connection_error_includes_target_context(
    monkeypatch: pytest.MonkeyPatch,
    gateway_local_token_supplier: Callable[[], str],
    gateway_clients: list[GatewaySessionContextClient],
):
    async def raise_connect_error(
        _client: httpx.AsyncClient,
        method: str,
        path: str,
        **_kwargs: object,
    ) -> httpx.Response:
        request = httpx.Request(method, path)
        raise httpx.ConnectError("All connection attempts failed", request=request)

    monkeypatch.setattr(httpx.AsyncClient, "request", raise_connect_error)
    client = GatewaySessionContextClient(
        gateway_url="http://127.0.0.1:65530",
        local_token_supplier=gateway_local_token_supplier,
    )
    gateway_clients.append(client)

    with pytest.raises(WorkspaceSessionContextAccessError) as captured:
        await client.read_context_in_workspace(
            "gw_missing_backend",
            _read_request("gw_missing_backend"),
        )

    message = str(captured.value)
    assert "无法连接 Workspace Gateway" in message
    assert "workspace_id=gw_missing_backend" in message
    assert "path=/api/v1/context/read" in message
    assert "error_type=ConnectError" in message
    assert isinstance(captured.value.__cause__, httpx.ConnectError)


@pytest.mark.asyncio
async def test_gateway_client_uses_default_runtime_url_with_current_local_token(
    monkeypatch: pytest.MonkeyPatch,
    gateway_clients: list[GatewaySessionContextClient],
):
    captured_requests: list[httpx.Request] = []
    captured_clients: list[httpx.AsyncClient] = []
    current_token = ["gateway-context-token-1"]

    async def capture_request(
        _client: httpx.AsyncClient,
        method: str,
        path: str,
        **kwargs: object,
    ) -> httpx.Response:
        captured_request = httpx.Request(
            method,
            path,
            headers=kwargs["headers"],
        )
        captured_requests.append(captured_request)
        captured_clients.append(_client)
        return httpx.Response(
            404,
            request=captured_request,
            json={"detail": "Gateway 工作区不存在: gw_target"},
        )

    monkeypatch.setattr(httpx.AsyncClient, "request", capture_request)
    client = GatewaySessionContextClient(local_token_supplier=lambda: current_token[0])
    gateway_clients.append(client)

    with pytest.raises(WorkspaceSessionContextAccessError):
        await client.read_context_in_workspace(
            "gw_target",
            _read_request("gw_target"),
        )

    current_token[0] = "gateway-context-token-2"
    with pytest.raises(WorkspaceSessionContextAccessError):
        await client.read_context_in_workspace("gw_target", _read_request("gw_target"))

    assert len(captured_requests) == 2
    assert all(request.url.host == "127.0.0.1" for request in captured_requests)
    assert all(request.url.port == 8014 for request in captured_requests)
    assert all(
        request.headers["X-BoxTeam-Workspace-Id"] == "gw_target"
        for request in captured_requests
    )
    assert [request.headers["X-Local-Token"] for request in captured_requests] == [
        "gateway-context-token-1", "gateway-context-token-2",
    ]
    assert captured_clients[0] is captured_clients[1]
    assert not captured_clients[0].is_closed
    await client.aclose()
    assert captured_clients[0].is_closed


@pytest.mark.asyncio
async def test_gateway_unknown_workspace_is_model_recoverable(
    monkeypatch: pytest.MonkeyPatch,
    gateway_local_token_supplier: Callable[[], str],
    gateway_clients: list[GatewaySessionContextClient],
):
    async def return_not_found(
        _client: httpx.AsyncClient,
        method: str,
        path: str,
        **_kwargs: object,
    ) -> httpx.Response:
        request = httpx.Request(method, path)
        return httpx.Response(
            404,
            request=request,
            json={"detail": "Gateway 工作区不存在: gw_typo"},
        )

    monkeypatch.setattr(httpx.AsyncClient, "request", return_not_found)
    client = GatewaySessionContextClient(
        gateway_url="http://127.0.0.1:8014",
        local_token_supplier=gateway_local_token_supplier,
    )
    gateway_clients.append(client)

    with pytest.raises(WorkspaceSessionContextAccessError) as captured:
        await client.read_context_in_workspace(
            "gw_typo",
            _read_request("gw_typo"),
        )

    message = str(captured.value)
    assert "workspace_id=gw_typo" in message
    assert "status=404" in message
    assert "Gateway 工作区不存在" in message


@pytest.mark.asyncio
async def test_gateway_internal_server_error_still_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
    gateway_local_token_supplier: Callable[[], str],
    gateway_clients: list[GatewaySessionContextClient],
):
    async def return_server_error(
        _client: httpx.AsyncClient,
        method: str,
        path: str,
        **_kwargs: object,
    ) -> httpx.Response:
        request = httpx.Request(method, path)
        return httpx.Response(500, request=request, text="internal invariant broken")

    monkeypatch.setattr(httpx.AsyncClient, "request", return_server_error)
    client = GatewaySessionContextClient(
        gateway_url="http://127.0.0.1:8014",
        local_token_supplier=gateway_local_token_supplier,
    )
    gateway_clients.append(client)

    with pytest.raises(RuntimeError, match="status=500") as captured:
        await client.read_context_in_workspace(
            "gw_valid",
            _read_request("gw_valid"),
        )

    assert not isinstance(captured.value, WorkspaceSessionContextAccessError)


@pytest.mark.asyncio
async def test_gateway_url_is_resolved_from_current_config_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    gateway_local_token_supplier: Callable[[], str],
    gateway_clients: list[GatewaySessionContextClient],
) -> None:
    config_service = Mock(spec=ConfigService)
    current_url = ["http://gateway-a"]
    config_service.get_gateway_connection_url.side_effect = lambda: current_url[0]
    current_timeout = [30]
    config_service.get_gateway_connection_timeout_seconds.side_effect = lambda: current_timeout[0]
    captured_urls: list[str] = []
    captured_timeouts: list[object] = []
    captured_clients: list[httpx.AsyncClient] = []

    async def capture_request(
        client: httpx.AsyncClient,
        method: str,
        path: str,
        **kwargs: object,
    ) -> httpx.Response:
        captured_urls.append(path)
        captured_timeouts.append(kwargs["timeout"])
        captured_clients.append(client)
        request = httpx.Request(method, path)
        return httpx.Response(
            200,
            request=request,
            json={
                "data": {
                    **kwargs["json"],
                    "view": "overview",
                    "revision": "rev-1",
                }
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "request", capture_request)
    client = GatewaySessionContextClient(
        config_service=config_service,
        local_token_supplier=gateway_local_token_supplier,
    )
    gateway_clients.append(client)

    await client.read_context_in_workspace(
        "gw_target",
        _read_request("gw_target"),
    )
    current_url[0] = "http://gateway-b"
    current_timeout[0] = 12
    await client.read_context_in_workspace(
        "gw_target",
        _read_request("gw_target"),
    )

    assert captured_urls == [
        "http://gateway-a/api/v1/context/read",
        "http://gateway-b/api/v1/context/read",
    ]
    assert captured_timeouts == [30, 12]
    assert captured_clients[0] is captured_clients[1]


@pytest.mark.asyncio
async def test_gateway_pool_keeps_concurrent_relay_request_identities(
    monkeypatch: pytest.MonkeyPatch,
    gateway_clients: list[GatewaySessionContextClient],
) -> None:
    requests: list[httpx.Request] = []
    http_clients: list[httpx.AsyncClient] = []
    both_entered = asyncio.Event()

    async def capture_request(
        http_client: httpx.AsyncClient,
        method: str,
        url: str,
        *,
        json: dict[str, object],
        headers: dict[str, str],
        timeout: float,
    ) -> httpx.Response:
        request = httpx.Request(method, url, json=json, headers=headers)
        requests.append(request)
        http_clients.append(http_client)
        if len(requests) == 2:
            both_entered.set()
        await both_entered.wait()
        read = json["body"]["read"]
        return httpx.Response(
            200, request=request, headers={"X-Request-ID": headers["X-Request-ID"]},
            json={
                "request_id": headers["X-Request-ID"],
                "data": {**read, "view": "overview", "revision": "rev-1"},
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "request", capture_request)
    client = GatewaySessionContextClient(local_token_supplier=lambda: "gateway-pool-token")
    gateway_clients.append(client)
    reads = [_read_request(f"ws_{create_uuid_hex()}") for _ in range(2)]

    async def query(index: int):
        request_token = set_current_request_id(f"req_gateway_pool_{index}")
        gateway_token = set_current_gateway_id("gateway_local")
        try:
            return await client.read_resource_context(reads[index])
        finally:
            reset_current_gateway_id(gateway_token)
            reset_current_request_id(request_token)

    results = await asyncio.gather(query(0), query(1))
    assert [result.resource_identity for result in results] == [read.resource_identity for read in reads]
    assert http_clients[0] is http_clients[1]
    assert [request.headers["X-Request-ID"] for request in requests] == [
        "req_gateway_pool_0", "req_gateway_pool_1",
    ]
    assert all(request.headers["X-Local-Token"] == "gateway-pool-token" for request in requests)
    assert all("X-BoxTeam-Workspace-Id" not in request.headers for request in requests)
    assert not http_clients[0].is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fault", "cause"),
    [("json", JSONDecodeError), ("envelope", TypeError), ("schema", ValidationError), ("action", ValueError)],
    ids=["json", "envelope", "schema", "action"],
)
async def test_gateway_fork_classifies_success_response_decode_failures(
    monkeypatch: pytest.MonkeyPatch, gateway_clients: list[GatewaySessionContextClient],
    gateway_fork_request: SessionForkTransferRequest, fault: str, cause: type[Exception],
) -> None:
    async def respond(
        _client: httpx.AsyncClient, method: str, url: str, *, headers: dict[str, str], **_kwargs: object,
    ) -> httpx.Response:
        request = httpx.Request(method, url, headers=headers)
        response_headers = {"X-Request-ID": headers["X-Request-ID"]}
        if fault == "json":
            return httpx.Response(200, request=request, headers=response_headers, text="not JSON")
        data = [] if fault == "envelope" else {"intent": []} if fault == "schema" else {}
        return httpx.Response(200, request=request, headers=response_headers,
                              json={"request_id": headers["X-Request-ID"], "data": data})

    monkeypatch.setattr(httpx.AsyncClient, "request", respond)
    client = GatewaySessionContextClient(local_token_supplier=lambda: "fork-token")
    gateway_clients.append(client)

    with pytest.raises(RuntimeError, match="fork_response_protocol_invalid") as caught:
        await client.fork_transfer(gateway_fork_request)
    assert isinstance(caught.value.__cause__, cause)
    assert not isinstance(caught.value, WorkspaceSessionContextAccessError)


@pytest.mark.asyncio
async def test_gateway_fork_decodes_valid_intent_with_same_request_id(
    monkeypatch: pytest.MonkeyPatch, gateway_clients: list[GatewaySessionContextClient],
    gateway_fork_request: SessionForkTransferRequest,
) -> None:
    intent = {"request": gateway_fork_request.request.model_dump(mode="json")}

    async def respond(
        _client: httpx.AsyncClient, method: str, url: str, *, headers: dict[str, str], **_kwargs: object,
    ) -> httpx.Response:
        assert headers["X-Request-ID"] == "req_gateway_fork"
        return httpx.Response(200, request=httpx.Request(method, url, headers=headers),
                              headers={"X-Request-ID": headers["X-Request-ID"]},
                              json={"request_id": headers["X-Request-ID"], "data": {"intent": intent}})

    monkeypatch.setattr(httpx.AsyncClient, "request", respond)
    client = GatewaySessionContextClient(local_token_supplier=lambda: "fork-token")
    gateway_clients.append(client)

    assert (await client.fork_transfer(gateway_fork_request)).intent == intent


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 403, 404, 422, 500])
async def test_gateway_fork_preserves_http_owner_failure_attribution(
    monkeypatch: pytest.MonkeyPatch, gateway_clients: list[GatewaySessionContextClient],
    gateway_fork_request: SessionForkTransferRequest, status: int,
) -> None:
    async def respond(_client: httpx.AsyncClient, method: str, url: str, **_kwargs: object) -> httpx.Response:
        return httpx.Response(status, request=httpx.Request(method, url), text="owner refused")

    monkeypatch.setattr(httpx.AsyncClient, "request", respond)
    client = GatewaySessionContextClient(local_token_supplier=lambda: "fork-token")
    gateway_clients.append(client)

    with pytest.raises(RuntimeError, match=f"status={status}") as caught:
        await client.fork_transfer(gateway_fork_request)
    assert isinstance(caught.value, WorkspaceSessionContextAccessError) is (status != 500)
    assert caught.value.__cause__ is None
    assert "fork_response_protocol_invalid" not in str(caught.value)


@pytest.mark.asyncio
async def test_gateway_fork_preserves_connection_failure_attribution(
    monkeypatch: pytest.MonkeyPatch, gateway_clients: list[GatewaySessionContextClient],
    gateway_fork_request: SessionForkTransferRequest,
) -> None:
    failure = httpx.ConnectError("fork channel closed")

    async def fail(_client: httpx.AsyncClient, _method: str, _url: str, **_kwargs: object) -> httpx.Response:
        raise failure

    monkeypatch.setattr(httpx.AsyncClient, "request", fail)
    client = GatewaySessionContextClient(local_token_supplier=lambda: "fork-token")
    gateway_clients.append(client)

    with pytest.raises(WorkspaceSessionContextAccessError) as caught:
        await client.fork_transfer(gateway_fork_request)
    assert caught.value.__cause__ is failure


@pytest.mark.asyncio
async def test_gateway_fork_keeps_local_origin_error_as_input_failure(
    monkeypatch: pytest.MonkeyPatch, gateway_clients: list[GatewaySessionContextClient],
    gateway_fork_request: SessionForkTransferRequest,
) -> None:
    async def fail(_client: httpx.AsyncClient, _method: str, _url: str, **_kwargs: object) -> httpx.Response:
        pytest.fail("本地 origin 不匹配不得发出 HTTP 请求")

    monkeypatch.setattr(httpx.AsyncClient, "request", fail)
    client = GatewaySessionContextClient(local_token_supplier=lambda: "fork-token")
    gateway_clients.append(client)

    with pytest.raises(ValueError, match="origin 与当前 Gateway 不一致"):
        await client.fork_transfer(gateway_fork_request.model_copy(update={"source_gateway_id": "gateway_other"}))
