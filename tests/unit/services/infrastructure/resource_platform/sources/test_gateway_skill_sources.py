"""认证 Gateway 原始 Skill 来源的接受、失效与恢复边界。"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import socket
import ssl
import time
from contextvars import Context, ContextVar
from pathlib import Path

import httpx
import pytest

from app.core.trace_middleware import (
    get_current_gateway_id,
    get_current_navigation_actor,
    get_current_request_id,
    reset_current_gateway_id,
    reset_current_navigation_actor,
    reset_current_request_id,
    set_current_gateway_id,
    set_current_navigation_actor,
    set_current_request_id,
)
from app.core.workspace_identity import load_or_create_workspace_id
from app.domain.itemized.hashing import canonical_json_bytes
from app.gateway.runtime.skill_sources import GatewaySkillSourceOwner
from app.schemas.internal_v2.resource_navigation import ResourceNavigationRequest
from app.services.infrastructure.gateway_client_common import GatewayTransportConnection
from app.services.infrastructure.resource_platform.observation.resource_observation_channel import (
    ResourceObservationChannel,
)
from app.services.infrastructure.resource_platform.sources.gateway_skill_sources import (
    GatewaySkillSources,
)
from app.services.infrastructure.resource_platform.sources.resource_navigation import (
    ResourceFileOperationError,
)
from app.services.infrastructure.resource_platform.sources.workspace_file_resources import (
    WorkspaceFileResourceRegistry,
)
from app.services.infrastructure.workspace_file_watch_service import (
    WorkspaceFileChangeBatch,
    WorkspaceFileWatchService,
)

GATEWAY_ID = "gateway_0123456789abcdef0123456789abcdef"
SECOND_GATEWAY_ID = "gateway_fedcba9876543210fedcba9876543210"


@pytest.fixture
def request_identity():
    token = set_current_request_id("req-gateway-skill-unit")
    try:
        yield "req-gateway-skill-unit"
    finally:
        reset_current_request_id(token)


@pytest.fixture
async def source_case(tmp_path: Path, request_identity: str):
    assert get_current_request_id() == request_identity
    home, workspace = tmp_path / "gateway-home", tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    skill = home / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    # Gateway 不解释 frontmatter；原始文件即使不是 Skill 文法也可发布。
    skill.write_bytes("原始正文 Ω  \r\n".encode())
    owner = GatewaySkillSourceOwner(gateway_id=GATEWAY_ID, skills_root=home / "skills")
    await owner.start()
    channel = ResourceObservationChannel()
    source = GatewaySkillSources(
        workspace_id=load_or_create_workspace_id(workspace), connection=GatewayTransportConnection(gateway_url="http://gateway.test"),
        local_token_supplier=lambda: "test-local-token", observation_channel=channel,
    )
    try:
        yield owner, source, skill, channel
    finally:
        await source.close()
        await owner.close()


@pytest.fixture
def navigation_transport(monkeypatch: pytest.MonkeyPatch):
    sync_client, async_client = httpx.Client, httpx.AsyncClient

    def install(reply):
        transport = httpx.MockTransport(reply)
        monkeypatch.setattr(httpx, "Client", lambda **kwargs: sync_client(transport=transport, **kwargs))
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: async_client(transport=transport, **kwargs))

    return install


async def test_authentication_failure_blocks_retained_revision_until_authenticated_recovery(
    source_case, request_identity: str,
):
    owner, source, skill, channel = source_case
    mode = "accepted"
    requests = []

    def reply(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["X-Local-Token"] == "test-local-token"
        assert request.headers["X-Request-ID"] == request_identity
        if mode == "denied":
            return httpx.Response(401, json={"detail": "invalid local token"})
        publication = owner.snapshot(workspace_id=source._workspace_id)
        etag = '"' + publication.revision + '"'
        headers = {"ETag": etag, "X-Request-ID": request.headers["X-Request-ID"]}
        if request.headers.get("If-None-Match") == etag:
            return httpx.Response(304, headers=headers)
        return httpx.Response(200, json={"data": publication.model_dump(), "request_id": request.headers["X-Request-ID"]}, headers=headers)

    source._state(GATEWAY_ID).client = httpx.AsyncClient(transport=httpx.MockTransport(reply))
    subscription = channel.subscribe(label="assert unavailable")
    await source.ensure_bound(GATEWAY_ID)
    first = source.snapshot(gateway_id=GATEWAY_ID)
    assert first.entries[0].revision == "sha256:" + hashlib.sha256(skill.read_bytes()).hexdigest()
    assert first.entries[0].content == "原始正文 Ω  \r\n"
    assert str(skill) not in first.model_dump_json()
    assert "test-local-token" not in first.model_dump_json()
    assert (await subscription.next()).available
    await source.reconcile(gateway_id=GATEWAY_ID)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.02):
            await subscription.next()
    mode = "denied"
    await source.reconcile(gateway_id=GATEWAY_ID)
    with pytest.raises(RuntimeError, match="unavailable"):
        source.snapshot(gateway_id=GATEWAY_ID)
    denied = await subscription.next()
    assert not denied.available and denied.revision == first.revision
    mode = "accepted"
    await source.reconcile(gateway_id=GATEWAY_ID)
    assert source.snapshot(gateway_id=GATEWAY_ID) is first
    assert (await subscription.next()).available
    assert requests[-1].headers["If-None-Match"] == '"' + first.revision + '"'
    channel.unsubscribe(subscription)


@pytest.mark.parametrize("first_status", [200, 401])
async def test_same_owner_reconcile_cannot_publish_late_snapshot_or_error_after_newer_result(
    source_case, first_status: int,
):
    owner, source, skill, channel = source_case
    original = owner.snapshot(workspace_id=source._workspace_id)
    first_entered, second_entered, release_first = asyncio.Event(), asyncio.Event(), asyncio.Event()
    requests: list[httpx.Request] = []

    async def reply(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        snapshot = owner.snapshot(workspace_id=source._workspace_id)
        if len(requests) == 1:
            first_entered.set()
            await release_first.wait()
            if first_status != 200:
                return httpx.Response(first_status, json={"detail": "expired credential"})
        else:
            second_entered.set()
        return httpx.Response(200, json={
            "data": snapshot.model_dump(mode="json"), "request_id": request.headers["X-Request-ID"],
        }, headers={"ETag": '"' + snapshot.revision + '"', "X-Request-ID": request.headers["X-Request-ID"]})

    source._state(GATEWAY_ID).client = httpx.AsyncClient(transport=httpx.MockTransport(reply))
    subscription = channel.subscribe(label="同一 owner 的接受顺序")
    first = asyncio.create_task(source.reconcile(gateway_id=GATEWAY_ID))
    second = None
    try:
        await asyncio.wait_for(first_entered.wait(), 1)
        skill.write_text("已发布的新正文\n", encoding="utf-8")
        async with asyncio.timeout(3):
            while owner.snapshot(workspace_id=source._workspace_id).revision == original.revision:
                await asyncio.sleep(0.01)
        latest = owner.snapshot(workspace_id=source._workspace_id)
        second = asyncio.create_task(source.reconcile(gateway_id=GATEWAY_ID))
        # HTTP 获取本身必须排队，不能等拿到陈旧正文后再串行写入。
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(second_entered.wait(), 0.02)
        release_first.set()
        await asyncio.wait_for(asyncio.gather(first, second), 1)
        assert source.snapshot(gateway_id=GATEWAY_ID) == latest
        assert source._state(GATEWAY_ID).etag == '"' + latest.revision + '"'
        assert source._state(GATEWAY_ID).error is None
        assert subscription.pending()[-1].revision == latest.revision
        assert requests[-1].headers.get("If-None-Match") == (
            '"' + original.revision + '"' if first_status == 200 else None
        )
    finally:
        release_first.set()
        await asyncio.gather(*(task for task in (first, second) if task is not None), return_exceptions=True)
        channel.unsubscribe(subscription)


@pytest.mark.parametrize("failure", ["missing-header", "header-drift", "missing-body", "body-drift"])
async def test_snapshot_200_rejects_missing_or_drifted_request_identity(
    source_case, request_identity: str, failure: str,
):
    owner, source, _skill, _channel = source_case
    original = owner.snapshot(workspace_id=source._workspace_id)
    source._accept_snapshot(original, gateway_id=GATEWAY_ID)
    source._state(GATEWAY_ID).etag = '"' + original.revision + '"'

    def reply(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Request-ID"] == request_identity
        payload = {"code": 0, "data": original.model_dump(mode="json"), "request_id": request_identity}
        headers = {"X-Request-ID": request_identity, "ETag": '"' + original.revision + '"'}
        if failure == "missing-header":
            headers.pop("X-Request-ID")
        elif failure == "header-drift":
            headers["X-Request-ID"] = "another-request"
        elif failure == "missing-body":
            payload.pop("request_id")
        elif failure == "body-drift":
            payload["request_id"] = "another-request"
        return httpx.Response(200, json=payload, headers=headers)

    source._state(GATEWAY_ID).client = httpx.AsyncClient(transport=httpx.MockTransport(reply))
    await source.reconcile(gateway_id=GATEWAY_ID)

    assert source._state(GATEWAY_ID).snapshot is original
    assert source._state(GATEWAY_ID).error is not None
    with pytest.raises(RuntimeError, match="unavailable"):
        source.snapshot(gateway_id=GATEWAY_ID)


@pytest.mark.parametrize("failure", [None, "missing-header", "header-drift", "non-empty-body"])
async def test_snapshot_304_requires_valid_request_identity_and_empty_body(
    source_case, request_identity: str, failure: str | None,
):
    owner, source, _skill, _channel = source_case
    original = owner.snapshot(workspace_id=source._workspace_id)
    source._accept_snapshot(original, gateway_id=GATEWAY_ID)
    source._state(GATEWAY_ID).etag = '"' + original.revision + '"'
    replies: list[httpx.Response] = []

    def reply(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Request-ID"] == request_identity
        assert request.headers["If-None-Match"] == '"' + original.revision + '"'
        headers = {"X-Request-ID": request_identity}
        if failure == "missing-header":
            headers.pop("X-Request-ID")
        elif failure == "header-drift":
            headers["X-Request-ID"] = "another-request"
        response = httpx.Response(
            304, headers=headers, content=b"unexpected" if failure == "non-empty-body" else b"",
        )
        replies.append(response)
        return response

    source._state(GATEWAY_ID).client = httpx.AsyncClient(transport=httpx.MockTransport(reply))
    await source.reconcile(gateway_id=GATEWAY_ID)

    assert replies[0].content == (b"unexpected" if failure == "non-empty-body" else b"")
    assert source._state(GATEWAY_ID).snapshot is original
    if failure is None:
        assert source._state(GATEWAY_ID).error is None
        assert source.snapshot(gateway_id=GATEWAY_ID) is original
    else:
        assert source._state(GATEWAY_ID).error is not None
        with pytest.raises(RuntimeError, match="unavailable"):
            source.snapshot(gateway_id=GATEWAY_ID)


@pytest.mark.parametrize("failure", [None, "missing-header", "blank-header", "missing-body", "body-drift"])
async def test_independent_poll_200_validates_gateway_generated_request_identity(
    source_case, request_identity: str, failure: str | None,
):
    owner, source, _skill, _channel = source_case
    original = owner.snapshot(workspace_id=source._workspace_id)
    source._accept_snapshot(original, gateway_id=GATEWAY_ID)
    source._state(GATEWAY_ID).etag = '"' + original.revision + '"'
    generated_request_id = "gateway-independent-poll-200"
    requests: list[httpx.Request] = []

    def reply(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers.get("X-Request-ID") is None
        payload = {"code": 0, "data": original.model_dump(mode="json"), "request_id": generated_request_id}
        headers = {"X-Request-ID": generated_request_id, "ETag": '"' + original.revision + '"'}
        if failure == "missing-header":
            headers.pop("X-Request-ID")
        elif failure == "blank-header":
            headers["X-Request-ID"] = "  "
        elif failure == "missing-body":
            payload.pop("request_id")
        elif failure == "body-drift":
            payload["request_id"] = "another-gateway-request"
        return httpx.Response(200, json=payload, headers=headers)

    source._state(GATEWAY_ID).client = httpx.AsyncClient(transport=httpx.MockTransport(reply))
    await asyncio.create_task(
        source.reconcile(gateway_id=GATEWAY_ID, independent_poll=True), context=Context(),
    )

    assert len(requests) == 1
    assert requests[0].headers.get("X-Request-ID") is None
    if failure is None:
        assert generated_request_id != request_identity
        assert source._state(GATEWAY_ID).error is None
        accepted = source.snapshot(gateway_id=GATEWAY_ID)
        assert accepted == original
        assert accepted.available
    else:
        assert source._state(GATEWAY_ID).snapshot is original
        assert source._state(GATEWAY_ID).error is not None
        with pytest.raises(RuntimeError, match="unavailable"):
            source.snapshot(gateway_id=GATEWAY_ID)


@pytest.mark.parametrize("failure", [None, "missing-header", "blank-header", "non-empty-body"])
async def test_independent_poll_304_requires_valid_request_identity_and_empty_body(
    source_case, failure: str | None,
):
    owner, source, _skill, _channel = source_case
    original = owner.snapshot(workspace_id=source._workspace_id)
    source._accept_snapshot(original, gateway_id=GATEWAY_ID)
    source._state(GATEWAY_ID).etag = '"' + original.revision + '"'
    generated_request_id = "gateway-independent-poll-304"
    requests: list[httpx.Request] = []
    replies: list[httpx.Response] = []

    def reply(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers.get("X-Request-ID") is None
        headers = {"X-Request-ID": generated_request_id}
        if failure == "missing-header":
            headers.pop("X-Request-ID")
        elif failure == "blank-header":
            headers["X-Request-ID"] = "  "
        response = httpx.Response(
            304, headers=headers, content=b"unexpected" if failure == "non-empty-body" else b"",
        )
        replies.append(response)
        return response

    source._state(GATEWAY_ID).client = httpx.AsyncClient(transport=httpx.MockTransport(reply))
    await asyncio.create_task(
        source.reconcile(gateway_id=GATEWAY_ID, independent_poll=True), context=Context(),
    )

    assert len(requests) == len(replies) == 1
    assert requests[0].headers.get("X-Request-ID") is None
    assert replies[0].content == (b"unexpected" if failure == "non-empty-body" else b"")
    assert source._state(GATEWAY_ID).snapshot is original
    if failure is None:
        assert source._state(GATEWAY_ID).error is None
        assert source.snapshot(gateway_id=GATEWAY_ID) is original
    else:
        assert source._state(GATEWAY_ID).error is not None
        with pytest.raises(RuntimeError, match="unavailable"):
            source.snapshot(gateway_id=GATEWAY_ID)


@pytest.mark.parametrize("failure", [None, "missing-header", "header-drift", "missing-body", "body-drift", "node-path", "entry-path", "match-path"])
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_navigation_200_echoes_and_validates_request_identity(
    source_case, navigation_transport, request_identity: str, failure: str | None, mode: str,
):
    owner, source, _skill, _channel = source_case
    request = ResourceNavigationRequest(
        uri=f"boxteam://gateway/{GATEWAY_ID}/resources/skills/demo/SKILL.md",
        operation="read",
    )
    result = await owner.navigate(request)

    def reply(http_request: httpx.Request) -> httpx.Response:
        assert http_request.headers["X-Request-ID"] == request_identity
        assert json.loads(http_request.content)["uri"] == request.uri
        payload = {"code": 0, "data": result.model_dump(mode="json"), "request_id": request_identity}
        headers = {"X-Request-ID": request_identity}
        if failure == "missing-header":
            headers.pop("X-Request-ID")
        elif failure == "header-drift":
            headers["X-Request-ID"] = "another-request"
        elif failure == "missing-body":
            payload.pop("request_id")
        elif failure == "body-drift":
            payload["request_id"] = "another-request"
        elif failure == "node-path":
            payload["data"]["node"]["uri"] = "/private/gateway-secret"
        elif failure == "entry-path":
            payload["data"]["entries"] = [{**payload["data"]["node"], "uri": "/private/gateway-secret"}]
        elif failure == "match-path":
            payload["data"]["matches"] = [{"uri": "/private/gateway-secret", "line": 1, "column": 1,
                                          "text": "正文", "snippet_start_column": 1}]
        return httpx.Response(200, json=payload, headers=headers)

    navigation_transport(reply)

    async def navigate():
        if mode == "sync":
            return source.navigate_sync(request, gateway_id=GATEWAY_ID)
        return await source.navigate(request, gateway_id=GATEWAY_ID)

    if failure is None:
        assert await navigate() == result
    else:
        with pytest.raises(ValueError) as raised:
            await navigate()
        assert "/private/gateway-secret" not in str(raised.value)
    assert source._owners == {}, "普通导航不能创建来源观察 owner"


async def test_initial_binding_requires_request_identity_before_http(source_case):
    _owner, source, _skill, _channel = source_case
    requests: list[httpx.Request] = []
    source._state(GATEWAY_ID).client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: requests.append(request) or httpx.Response(500),
    ))
    token = set_current_request_id(None)
    try:
        with pytest.raises(RuntimeError, match="权威 request_id"):
            await source.ensure_bound(GATEWAY_ID)
    finally:
        reset_current_request_id(token)
    assert requests == []


@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_navigation_preserves_file_execution_error_domain(source_case, navigation_transport, request_identity: str, mode: str):
    _owner, source, _skill, _channel = source_case
    detail = {"owner": "resource_file_operations", "code": "output_limit_exceeded",
              "reason": "scan_limit", "message": "目录扫描达到本次节点上限"}

    def reply(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Request-ID"] == request_identity
        return httpx.Response(400, json={"detail": detail}, headers={"X-Request-ID": request_identity})

    navigation_transport(reply)
    request = ResourceNavigationRequest(
        uri=f"boxteam://gateway/{GATEWAY_ID}/resources/skills/demo", operation="list", scan_limit=1,
    )
    with pytest.raises(ResourceFileOperationError) as failure:
        if mode == "sync":
            source.navigate_sync(request, gateway_id=GATEWAY_ID)
        else:
            await source.navigate(request, gateway_id=GATEWAY_ID)
    assert failure.value.detail() == detail
    assert source._owners == {}


async def test_observer_uses_empty_context_and_gateway_generated_request_ids(
    source_case, request_identity: str,
):
    owner, source, _skill, _channel = source_case
    extra_context: ContextVar[str | None] = ContextVar("skill_observer_probe", default=None)
    gateway_token = set_current_gateway_id("gateway_e1d881d48d86e50c01f49e43069fc7d1")
    actor_token = set_current_navigation_actor("inherited-actor")
    extra_token = extra_context.set("inherited-value")
    observer_requests: list[httpx.Request] = []
    observer_request_ids: list[str] = []
    observer_contexts: list[tuple[str | None, str | None, str | None, str | None]] = []
    observer_replies: list[httpx.Response] = []
    two_polls = asyncio.Event()
    completed_polls = 0
    reconcile = source.reconcile
    publication = owner.snapshot(workspace_id=source._workspace_id)
    etag = '"' + publication.revision + '"'

    async def reconcile_with_poll_completion(
        *, gateway_id: str, wait_for_connection: bool = False, independent_poll: bool = False,
    ) -> None:
        nonlocal completed_polls
        await reconcile(
            gateway_id=gateway_id,
            wait_for_connection=wait_for_connection,
            independent_poll=independent_poll,
        )
        if independent_poll:
            completed_polls += 1
            if completed_polls == 2:
                two_polls.set()

    source.reconcile = reconcile_with_poll_completion

    async def reply(request: httpx.Request) -> httpx.Response:
        request_id = request.headers.get("X-Request-ID")
        if request_id is None:
            observer_requests.append(request)
            observer_request_ids.append(f"gateway-observer-{len(observer_request_ids) + 1}")
            observer_contexts.append((
                get_current_request_id(), get_current_gateway_id(),
                get_current_navigation_actor(), extra_context.get(),
            ))
            response_id = observer_request_ids[-1]
            response = httpx.Response(304, headers={"X-Request-ID": response_id, "ETag": etag})
            observer_replies.append(response)
            return response

        assert request_id == request_identity
        assert get_current_request_id() == request_identity
        assert get_current_gateway_id() == "gateway_e1d881d48d86e50c01f49e43069fc7d1"
        assert get_current_navigation_actor() == "inherited-actor"
        assert extra_context.get() == "inherited-value"
        return httpx.Response(
            200,
            json={"code": 0, "request_id": request_id, "data": publication.model_dump(mode="json")},
            headers={"X-Request-ID": request_id, "ETag": etag},
        )

    source._state(GATEWAY_ID).client = httpx.AsyncClient(transport=httpx.MockTransport(reply))
    try:
        await source.ensure_bound(GATEWAY_ID)
        accepted = source.snapshot(gateway_id=GATEWAY_ID)
    finally:
        extra_context.reset(extra_token)
        reset_current_navigation_actor(actor_token)
        reset_current_gateway_id(gateway_token)

    async with asyncio.timeout(4):
        await two_polls.wait()

    assert completed_polls == 2
    assert len(observer_requests) == len(observer_request_ids) == len(observer_replies) == 2
    assert all(request.headers.get("X-Request-ID") is None for request in observer_requests)
    assert observer_request_ids == ["gateway-observer-1", "gateway-observer-2"]
    assert request_identity not in observer_request_ids
    assert len(set(observer_request_ids)) == 2
    assert observer_contexts == [(None, None, None, None), (None, None, None, None)]
    assert all(reply.status_code == 304 and reply.content == b"" for reply in observer_replies)
    assert source.snapshot(gateway_id=GATEWAY_ID) is accepted
    assert accepted.available


@pytest.mark.parametrize("failure", ["cause", "context", "connect-timeout"])
async def test_initial_binding_waits_only_for_gateway_connection(
    source_case, request_identity: str, failure: str,
):
    owner, source, _skill, _channel = source_case
    attempts = []

    def reply(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        assert request.headers["X-Request-ID"] == request_identity
        if len(attempts) == 1:
            if failure == "connect-timeout":
                raise httpx.ConnectTimeout("Gateway 连接建立超时", request=request)
            try:
                raise ConnectionRefusedError(errno.ECONNREFUSED, "Gateway 尚未监听")
            except ConnectionRefusedError as refused:
                if failure == "context":
                    raise httpx.ConnectError("Gateway 尚未监听", request=request)
                try:
                    raise RuntimeError("传输连接失败") from refused
                except RuntimeError as transport_error:
                    raise httpx.ConnectError("Gateway 尚未监听", request=request) from transport_error
        publication = owner.snapshot(workspace_id=source._workspace_id)
        return httpx.Response(200, json={"data": publication.model_dump(), "request_id": request.headers["X-Request-ID"]},
                              headers={"X-Request-ID": request.headers["X-Request-ID"]})

    source._state(GATEWAY_ID).client = httpx.AsyncClient(transport=httpx.MockTransport(reply))
    await source.ensure_bound(GATEWAY_ID)
    assert len(attempts) == 2
    assert source.snapshot(gateway_id=GATEWAY_ID).available


@pytest.mark.parametrize("failure", ["dns", "network-unreachable", "tls", "unknown", "read-timeout"])
async def test_initial_binding_does_not_retry_other_transport_failures(source_case, failure):
    _owner, source, _skill, _channel = source_case
    attempts = []

    def reply(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        if failure == "read-timeout":
            raise httpx.ReadTimeout("Gateway 已连接但读取超时", request=request)
        cause = {
            "dns": socket.gaierror(socket.EAI_NONAME, "Gateway 名称无法解析"),
            "network-unreachable": OSError(errno.ENETUNREACH, "Gateway 网络不可达"),
            "tls": ssl.SSLError("Gateway TLS 握手失败"),
            "unknown": None,
        }[failure]
        raise httpx.ConnectError("Gateway 连接失败", request=request) from cause

    source._state(GATEWAY_ID).client = httpx.AsyncClient(transport=httpx.MockTransport(reply))
    error_type = "ReadTimeout" if failure == "read-timeout" else "ConnectError"
    with pytest.raises(RuntimeError, match=error_type):
        await source.ensure_bound(GATEWAY_ID)
    assert len(attempts) == 1 and source._state(GATEWAY_ID).task is None
    assert source._state(GATEWAY_ID).snapshot is None


@pytest.mark.parametrize("failure", [401, 403, "protocol", "unavailable"])
async def test_initial_binding_does_not_retry_authenticated_rejection_or_unavailable(source_case, failure):
    owner, source, _skill, _channel = source_case
    attempts = []

    def reply(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        if isinstance(failure, int):
            return httpx.Response(failure, json={"detail": "denied"})
        if failure == "protocol":
            return httpx.Response(200, json={"data": {}, "request_id": request.headers["X-Request-ID"]},
                                  headers={"X-Request-ID": request.headers["X-Request-ID"]})
        publication = owner.snapshot(workspace_id=source._workspace_id)
        payload = publication.model_dump()
        payload["available"] = False
        payload["error_code"] = "source-unavailable"
        payload["revision"] = "sha256:" + hashlib.sha256(
            canonical_json_bytes({"gateway_id": GATEWAY_ID, "available": False,
                "entries": [(entry.source_id, entry.revision, entry.available) for entry in publication.entries]}),
        ).hexdigest()
        return httpx.Response(200, json={"data": payload, "request_id": request.headers["X-Request-ID"]},
                              headers={"X-Request-ID": request.headers["X-Request-ID"]})

    source._state(GATEWAY_ID).client = httpx.AsyncClient(transport=httpx.MockTransport(reply))
    if failure == "unavailable":
        await source.ensure_bound(GATEWAY_ID)
        assert not source.snapshot(gateway_id=GATEWAY_ID).available
    else:
        with pytest.raises(RuntimeError, match="unavailable"):
            await source.ensure_bound(GATEWAY_ID)
    assert len(attempts) == 1


async def test_initial_connection_wait_uses_existing_timeout_budget_per_entry(
    source_case, request_identity: str,
):
    owner, source, _skill, _channel = source_case
    source._connection = GatewayTransportConnection(gateway_url="http://gateway.test", timeout_seconds=0.03)
    attempts = []
    ready = False

    def reply(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        assert request.headers["X-Request-ID"] == request_identity
        if not ready:
            raise httpx.ConnectError("Gateway 始终未监听", request=request) from ConnectionRefusedError(
                errno.ECONNREFUSED, "Gateway 尚未监听",
            )
        publication = owner.snapshot(workspace_id=source._workspace_id)
        return httpx.Response(200, json={"data": publication.model_dump(), "request_id": request.headers["X-Request-ID"]},
                              headers={"X-Request-ID": request.headers["X-Request-ID"]})

    source._state(GATEWAY_ID).client = httpx.AsyncClient(transport=httpx.MockTransport(reply))
    started = asyncio.get_running_loop().time()
    with pytest.raises(RuntimeError, match="TimeoutError"):
        await source.ensure_bound(GATEWAY_ID)
    assert 0.03 <= asyncio.get_running_loop().time() - started < 0.2
    assert len(attempts) == 1 and source._state(GATEWAY_ID).task is None
    assert source._state(GATEWAY_ID).error == "TimeoutError"
    ready = True
    await source.ensure_bound(GATEWAY_ID)
    assert len(attempts) == 2
    assert source.snapshot(gateway_id=GATEWAY_ID).available


async def test_initial_binding_response_cannot_exceed_total_connection_budget(
    source_case, request_identity: str,
):
    owner, source, _skill, _channel = source_case
    source._connection = GatewayTransportConnection(gateway_url="http://gateway.test", timeout_seconds=0.03)
    attempts = []

    async def reply(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        assert request.headers["X-Request-ID"] == request_identity
        await asyncio.sleep(0.3)
        publication = owner.snapshot(workspace_id=source._workspace_id)
        return httpx.Response(200, json={"data": publication.model_dump(), "request_id": request.headers["X-Request-ID"]},
                              headers={"X-Request-ID": request.headers["X-Request-ID"]})

    source._state(GATEWAY_ID).client = httpx.AsyncClient(transport=httpx.MockTransport(reply))
    started = asyncio.get_running_loop().time()
    with pytest.raises(RuntimeError, match="TimeoutError"):
        await source.ensure_bound(GATEWAY_ID)
    assert 0.03 <= asyncio.get_running_loop().time() - started < 0.2
    assert len(attempts) == 1 and source._state(GATEWAY_ID).task is None
    assert source._state(GATEWAY_ID).snapshot is None
    assert source._state(GATEWAY_ID).error == "TimeoutError"


async def test_watcher_gap_is_unavailable_until_full_registered_boundary_reconcile(source_case):
    owner, source, _skill, _channel = source_case
    original = owner.snapshot(workspace_id=source._workspace_id)
    events = owner.file_registry._watch_events
    assert events is not None
    await events.put(WorkspaceFileChangeBatch(error="watch failed at /private/gateway/path"))
    async with asyncio.timeout(3):
        while owner.snapshot(workspace_id=source._workspace_id).available:
            await asyncio.sleep(0.01)
    unavailable = owner.snapshot(workspace_id=source._workspace_id)
    assert unavailable.entries[0].revision == original.entries[0].revision
    assert unavailable.error_code == "source-unavailable"
    assert "/private/gateway/path" not in unavailable.model_dump_json()
    await events.put(WorkspaceFileChangeBatch(overflow=True))
    async with asyncio.timeout(3):
        while not owner.snapshot(workspace_id=source._workspace_id).available:
            await asyncio.sleep(0.01)
    assert owner.snapshot(workspace_id=source._workspace_id) == original


async def test_stopped_file_observation_cannot_publish_retained_raw_snapshot(source_case):
    owner, source, _skill, _channel = source_case
    assert owner.snapshot(workspace_id=source._workspace_id).available
    await owner.file_registry.stop()
    with pytest.raises(RuntimeError, match="尚未启动"):
        owner.snapshot(workspace_id=source._workspace_id)


@pytest.mark.parametrize("field", ["gateway_id", "workspace_id", "revision", "entry_identity", "source_id", "display_uri", "byte_length", "content"])
async def test_wrong_identity_audience_or_bytes_never_replace_accepted_snapshot(source_case, field):
    owner, source, _skill, _channel = source_case
    original = owner.snapshot(workspace_id=source._workspace_id)
    source._accept_snapshot(original, gateway_id=GATEWAY_ID)
    payload = original.model_dump(mode="json")
    target = payload if field in {"gateway_id", "workspace_id", "revision"} else payload["entries"][0]
    target[field] = 999 if field == "byte_length" else "corrupted"
    source._state(GATEWAY_ID).client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, json={"data": payload, "request_id": request.headers["X-Request-ID"]},
        headers={"X-Request-ID": request.headers["X-Request-ID"]},
    )))
    await source.reconcile(gateway_id=GATEWAY_ID)
    with pytest.raises(RuntimeError, match="unavailable"):
        source.snapshot(gateway_id=GATEWAY_ID)
    assert source._state(GATEWAY_ID).snapshot is original


async def test_concurrent_gateway_owners_keep_snapshot_etag_failure_and_observation_isolated(
    source_case, tmp_path: Path,
):
    first_owner, source, _skill, channel = source_case
    second_skill = tmp_path / "second-gateway/skills/demo/SKILL.md"
    second_skill.parent.mkdir(parents=True)
    second_skill.write_text("第二个真实 owner 的正文\n")
    second_owner = GatewaySkillSourceOwner(
        gateway_id=SECOND_GATEWAY_ID, skills_root=second_skill.parent.parent,
    )
    await second_owner.start()
    owners = {GATEWAY_ID: first_owner, SECOND_GATEWAY_ID: second_owner}
    entered = {gateway_id: asyncio.Event() for gateway_id in owners}
    release = asyncio.Event()
    modes = dict.fromkeys(owners, "accepted")
    requests: dict[str, list[httpx.Request]] = {gateway_id: [] for gateway_id in owners}

    def transport(gateway_id: str) -> httpx.MockTransport:
        async def reply(request: httpx.Request) -> httpx.Response:
            assert request.url.path == (
                f"/api/gateway/skill-source-connections/{gateway_id}/workspaces/{source._workspace_id}"
            )
            requests[gateway_id].append(request)
            entered[gateway_id].set()
            await release.wait()
            if modes[gateway_id] == "denied":
                return httpx.Response(403, json={"detail": "owner unavailable"})
            owner = first_owner if modes[gateway_id] == "wrong-owner" else owners[gateway_id]
            snapshot = owner.snapshot(workspace_id=source._workspace_id)
            etag = '"' + snapshot.revision + '"'
            headers = {"ETag": etag, "X-Request-ID": request.headers["X-Request-ID"]}
            if request.headers.get("If-None-Match") == etag:
                return httpx.Response(304, headers=headers)
            return httpx.Response(200, json={
                "data": snapshot.model_dump(), "request_id": request.headers["X-Request-ID"],
            }, headers=headers)

        return httpx.MockTransport(reply)

    for gateway_id in owners:
        source._state(gateway_id).client = httpx.AsyncClient(transport=transport(gateway_id))
    subscription = channel.subscribe(label="分别观察两个真实 owner")
    bindings = [asyncio.create_task(source.ensure_bound(gateway_id)) for gateway_id in (*owners, *owners)]
    try:
        # 两个 owner 的首次请求必须都能进入，不能被同一把绑定锁串行化。
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered.values())), 1)
        release.set()
        await asyncio.gather(*bindings)
        first = source.snapshot(gateway_id=GATEWAY_ID)
        second = source.snapshot(gateway_id=SECOND_GATEWAY_ID)
        assert first.entries[0].content == "原始正文 Ω  \r\n"
        assert second.entries[0].content == "第二个真实 owner 的正文\n"
        assert all(len(items) == 1 for items in requests.values())
        states = [source._state(gateway_id) for gateway_id in owners]
        assert states[0].task is not states[1].task
        assert states[0].client is not states[1].client
        assert states[0].lock is not states[1].lock
        assert {item.directory_id for item in subscription.pending()} == {
            source.directory_id(gateway_id=gateway_id) for gateway_id in owners
        }

        modes[SECOND_GATEWAY_ID] = "denied"
        await source.reconcile(gateway_id=SECOND_GATEWAY_ID)
        with pytest.raises(RuntimeError, match=SECOND_GATEWAY_ID):
            source.snapshot(gateway_id=SECOND_GATEWAY_ID)
        assert source.snapshot(gateway_id=GATEWAY_ID) is first
        failure, = subscription.pending()
        assert failure.directory_id == source.directory_id(gateway_id=SECOND_GATEWAY_ID)
        assert not failure.available

        modes[SECOND_GATEWAY_ID] = "accepted"
        await asyncio.gather(*(source.reconcile(gateway_id=gateway_id) for gateway_id in owners))
        assert source.snapshot(gateway_id=GATEWAY_ID) is first
        assert source.snapshot(gateway_id=SECOND_GATEWAY_ID) is second
        for gateway_id, snapshot in ((GATEWAY_ID, first), (SECOND_GATEWAY_ID, second)):
            assert requests[gateway_id][-1].headers["If-None-Match"] == '"' + snapshot.revision + '"'
        recovery, = subscription.pending()
        assert recovery.directory_id == failure.directory_id and recovery.available

        modes[SECOND_GATEWAY_ID] = "wrong-owner"
        await source.reconcile(gateway_id=SECOND_GATEWAY_ID)
        assert source._state(SECOND_GATEWAY_ID).snapshot is second
        assert source.snapshot(gateway_id=GATEWAY_ID) is first
        with pytest.raises(RuntimeError, match="ValueError"):
            source.snapshot(gateway_id=SECOND_GATEWAY_ID)
    finally:
        release.set()
        await asyncio.gather(*bindings, return_exceptions=True)
        tasks = [state.task for state in source._owners.values() if state.task is not None]
        await source.close()
        assert all(task.done() for task in tasks)
        assert all(state.client is not None and state.client.is_closed for state in source._owners.values())
        channel.unsubscribe(subscription)
        await second_owner.close()


def test_workspace_owner_cannot_register_gateway_directory(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = WorkspaceFileResourceRegistry(
        workspace_root=workspace, watch_service=WorkspaceFileWatchService(workspace_root=workspace),
    )
    with pytest.raises(ValueError, match="已知固定根"):
        registry.register_skill_directory(root=tmp_path / "gateway-home" / "skills", scope="gateway", scope_id=GATEWAY_ID)


@pytest.mark.parametrize("synchronous", [False, True])
async def test_skill_navigation_transport_enforces_total_body_deadline(source_case, navigation_transport, synchronous):
    _owner, source, _skill, _channel = source_case

    class Stream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.01)
                yield b" "

        async def aclose(self):
            self.closed = True

    stream = Stream()
    navigation_transport(lambda request: httpx.Response(200, stream=stream))
    request = ResourceNavigationRequest(
        uri=f"boxteam://gateway/{GATEWAY_ID}/resources/skills/demo/SKILL.md", operation="read", timeout_ms=80,
    )
    started = time.monotonic()
    with pytest.raises(ResourceFileOperationError) as failure:
        if synchronous:
            source.navigate_sync(request, gateway_id=GATEWAY_ID)
        else:
            await source.navigate(request, gateway_id=GATEWAY_ID)
    assert failure.value.reason == "deadline"
    assert time.monotonic() - started < 0.8
    assert stream.closed and source._owners == {}
