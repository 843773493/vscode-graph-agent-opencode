"""模型导航的固定 Gateway 传输、结果归属和失败不回落本机。"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.core.resource_uri import resource_display_uri
from app.core.trace_middleware import reset_current_request_id, set_current_request_id
from app.gateway.federation.errors import (
    FEDERATION_MALFORMED_FRAME,
    FEDERATION_TARGET_NOT_RESOLVABLE,
    FederationError,
)
from app.schemas.internal_v2.resource_navigation import ResourceNavigationRequest
from app.services.infrastructure.gateway_client_common import GatewayTransportConnection
from app.services.infrastructure.resource_platform.sources import (
    gateway_resource_navigation,
    resource_navigation,
)
from app.services.infrastructure.resource_platform.sources.gateway_resource_navigation import (
    GatewayResourceNavigation,
)
from app.services.infrastructure.resource_platform.sources.resource_navigation import (
    NAVIGATION_DEADLINE_HEADER,
    ResourceFileOperationError,
    ResourceNavigation,
)
from app.services.infrastructure.workspace_files.paths import WorkspaceFilePaths

SOURCE_GATEWAY = "gateway_" + "a" * 32
TARGET_GATEWAY = "gateway_" + "b" * 32
SOURCE_WORKSPACE = "00000000-0000-4000-8000-000000000001"
TARGET_WORKSPACE = "00000000-0000-4000-8000-000000000002"


@pytest.fixture
def routed_case(tmp_path: Path, monkeypatch):
    token = set_current_request_id("req-routed-file")
    original_sync, original_async = httpx.Client, httpx.AsyncClient
    received = []
    reply = {"status": 200, "change": None}

    def respond(request: httpx.Request):
        received.append(request)
        if reply.get("stream") is not None:
            return httpx.Response(200, stream=reply["stream"])
        body = json.loads(request.content)
        address = body["uri"]
        node = {"uri": address, "resource_shape": "file", "operations": ["describe", "read", "search"]}
        data = {"node": node, "content": "TARGET BODY", "entries": [node], "matches": [
            {"uri": address, "line": 1, "column": 1, "text": "TARGET BODY", "snippet_start_column": 1}]}
        envelope = {"code": 0, "request_id": "req-routed-file", "data": data}
        if reply["status"] != 200:
            envelope = {"request_id": "req-routed-file", "detail": reply["change"]}
        elif reply["change"] is not None:
            reply["change"](envelope)
        return httpx.Response(reply["status"], json=envelope, headers={"X-Request-ID": "req-routed-file"})

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(httpx, "Client", lambda **options: original_sync(transport=transport, **options))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **options: original_async(transport=transport, **options))
    client = GatewayResourceNavigation(workspace_id=SOURCE_WORKSPACE, connection=GatewayTransportConnection(gateway_url="http://source-gateway.test"),
        local_token_supplier=lambda: "source-local-token")
    (tmp_path / "file.txt").write_text("SOURCE DECOY")
    owner = ResourceNavigation(workspace_root=tmp_path, workspace_id=SOURCE_WORKSPACE,
        file_paths=WorkspaceFilePaths(workspace=tmp_path, workspace_id=SOURCE_WORKSPACE, bindings=None),
        gateway_navigation=client)
    uri = resource_display_uri(scope="workspace", scope_id=TARGET_WORKSPACE, kind="files",
        tail_segments=("root", "file.txt"), gateway_authority=TARGET_GATEWAY)
    try:
        yield owner, uri, received, reply, tmp_path
    finally:
        reset_current_request_id(token)


@pytest.mark.parametrize("synchronous", [False, True])
async def test_remote_uri_uses_configured_gateway_and_keeps_target(routed_case, navigation_clock, synchronous):
    owner, uri, received, _, workspace = routed_case
    request = ResourceNavigationRequest(uri=uri, operation="read", timeout_ms=1234)
    result = owner.navigate_sync(request, local_gateway_id=SOURCE_GATEWAY) if synchronous else await owner.navigate(request, local_gateway_id=SOURCE_GATEWAY)
    assert result.node.uri == result.matches[0].uri == uri and result.content == "TARGET BODY"
    outgoing, = received
    assert str(outgoing.url) == f"http://source-gateway.test/api/gateway/resource-navigation/workspaces/{SOURCE_WORKSPACE}"
    assert outgoing.headers["X-Local-Token"] == "source-local-token"
    assert outgoing.headers["X-Request-ID"] == "req-routed-file"
    assert json.loads(outgoing.content)["uri"] == uri
    assert outgoing.extensions["timeout"]["read"] == pytest.approx(1.234, abs=1e-12)
    assert float(outgoing.headers[NAVIGATION_DEADLINE_HEADER]) == 1001.234
    assert str(workspace) not in outgoing.content.decode() and (workspace / "file.txt").read_text() == "SOURCE DECOY"


@pytest.fixture
def navigation_clock(monkeypatch):
    clock = [50.0]
    value = SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 1000 + clock[0] - 50)
    monkeypatch.setattr(resource_navigation, "time", value)
    monkeypatch.setattr(gateway_resource_navigation, "time", value)
    return clock


@pytest.mark.parametrize("synchronous", [False, True])
async def test_routing_consumes_budget_without_changing_query(routed_case, navigation_clock, monkeypatch, synchronous):
    owner, uri, received, _, _ = routed_case
    route = owner._routed_request

    def slow_route(*args):
        navigation_clock[0] += 0.5
        return route(*args)

    monkeypatch.setattr(owner, "_routed_request", slow_route)
    request = ResourceNavigationRequest(uri=uri, operation="read", timeout_ms=1234)
    if synchronous:
        owner.navigate_sync(request, local_gateway_id=SOURCE_GATEWAY)
    else:
        await owner.navigate(request, local_gateway_id=SOURCE_GATEWAY)
    outgoing, = received
    assert json.loads(outgoing.content)["timeout_ms"] == 1234
    assert outgoing.extensions["timeout"]["read"] == pytest.approx(0.734, abs=1e-12)
    assert float(outgoing.headers[NAVIGATION_DEADLINE_HEADER]) == 1001.234


@pytest.fixture
def trickling_navigation(routed_case):
    class Stream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.01)
                yield b" "

        async def aclose(self):
            self.closed = True

    stream = Stream()
    routed_case[3]["stream"] = stream
    return stream


@pytest.mark.parametrize("synchronous", [False, True])
async def test_trickling_body_cannot_restart_network_timeout(routed_case, trickling_navigation, synchronous):
    owner, uri, _, _, workspace = routed_case
    request = ResourceNavigationRequest(uri=uri, operation="read", timeout_ms=80)
    started = time.monotonic()
    with pytest.raises(ResourceFileOperationError) as failure:
        if synchronous:
            owner.navigate_sync(request, local_gateway_id=SOURCE_GATEWAY)
        else:
            await owner.navigate(request, local_gateway_id=SOURCE_GATEWAY)
    assert failure.value.reason == "deadline"
    assert time.monotonic() - started < 0.8
    assert trickling_navigation.closed
    assert (workspace / "file.txt").read_text() == "SOURCE DECOY"


@pytest.mark.parametrize("synchronous", [False, True])
async def test_other_workspace_without_authority_is_frozen_to_calling_gateway(routed_case, synchronous):
    owner, uri, received, _, _ = routed_case
    local_uri = uri.replace(TARGET_GATEWAY + "/", "")
    request = ResourceNavigationRequest(uri=local_uri, operation="read")
    result = owner.navigate_sync(request, local_gateway_id=SOURCE_GATEWAY) if synchronous else await owner.navigate(request, local_gateway_id=SOURCE_GATEWAY)
    assert result.node.uri == uri.replace(TARGET_GATEWAY, SOURCE_GATEWAY)
    assert json.loads(received[0].content)["uri"] == result.node.uri


@pytest.mark.parametrize("synchronous", [False, True])
@pytest.mark.parametrize("failure", ["identity", "root", "match", "owner", "denied"])
async def test_remote_failures_never_read_local_same_name(routed_case, synchronous, failure):
    owner, uri, received, reply, workspace = routed_case
    if failure == "identity":
        reply["change"] = lambda envelope: envelope.update(request_id="wrong-request")
    elif failure == "root":
        reply["change"] = lambda envelope: envelope["data"]["node"].update(uri=uri.replace(TARGET_WORKSPACE, SOURCE_WORKSPACE))
    elif failure == "match":
        reply["change"] = lambda envelope: envelope["data"]["matches"][0].update(uri=uri + "/outside")
    elif failure == "owner":
        reply.update(status=409, change={"code": "federation-target-owner-rejected", "message": "拒绝",
            "detail": {"owner": "resource_file_operations", "code": "invalid_encoding", "reason": "utf8", "message": "目标编码错误"}})
    else:
        reply.update(status=409, change={"code": FEDERATION_TARGET_NOT_RESOLVABLE, "message": "目标未共享", "detail": {}})
    request = ResourceNavigationRequest(uri=uri, operation="read")
    expected = ResourceFileOperationError if failure == "owner" else FederationError
    with pytest.raises(expected) as error:
        if synchronous:
            owner.navigate_sync(request, local_gateway_id=SOURCE_GATEWAY)
        else:
            await owner.navigate(request, local_gateway_id=SOURCE_GATEWAY)
    if failure == "owner":
        assert error.value.code == "invalid_encoding" and error.value.reason == "utf8"
    elif failure == "denied":
        assert error.value.code == FEDERATION_TARGET_NOT_RESOLVABLE
    assert len(received) == 1 and (workspace / "file.txt").read_text() == "SOURCE DECOY"


@pytest.mark.parametrize("synchronous", [False, True])
@pytest.mark.parametrize("field", ["node", "entries", "matches"])
@pytest.mark.parametrize("path", ["/private/target-secret", r"C:\private\target-secret", r"\\host\private\target-secret"])
async def test_remote_path_reference_is_rejected_before_tool_projection(routed_case, synchronous, field, path):
    owner, uri, received, reply, workspace = routed_case

    def invalid_reference(envelope):
        data = envelope["data"]
        (data[field] if field == "node" else data[field][0])["uri"] = path

    reply["change"] = invalid_reference
    request = ResourceNavigationRequest(uri=uri, operation="read")
    with pytest.raises(FederationError) as failure:
        if synchronous:
            owner.navigate_sync(request, local_gateway_id=SOURCE_GATEWAY)
        else:
            await owner.navigate(request, local_gateway_id=SOURCE_GATEWAY)
    assert failure.value.code == FEDERATION_MALFORMED_FRAME
    assert path not in str(failure.value)
    assert len(received) == 1
    assert (workspace / "file.txt").read_text() == "SOURCE DECOY"


async def test_remote_file_body_and_match_text_keep_native_paths(routed_case):
    owner, uri, _, reply, _ = routed_case
    text = "/private/example\nC:\\private\\example\n\\\\host\\share\\example"

    def native_body(envelope):
        envelope["data"]["content"] = text
        envelope["data"]["matches"][0]["text"] = text

    reply["change"] = native_body
    result = await owner.navigate(ResourceNavigationRequest(uri=uri, operation="read"), local_gateway_id=SOURCE_GATEWAY)
    assert result.content == result.matches[0].text == text
    assert result.node.uri == result.entries[0].uri == result.matches[0].uri == uri
