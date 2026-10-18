"""认证 Gateway 原始 Skill 来源的接受、失效与恢复边界。"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import socket
import ssl
from pathlib import Path

import httpx
import pytest

from app.core.workspace_identity import load_or_create_workspace_id
from app.domain.itemized.hashing import canonical_json_bytes
from app.gateway.runtime.skill_sources import GatewaySkillSourceOwner
from app.services.infrastructure.gateway_client_common import GatewayTransportConnection
from app.services.infrastructure.resource_platform.observation.resource_observation_channel import (
    ResourceObservationChannel,
)
from app.services.infrastructure.resource_platform.sources.gateway_skill_sources import (
    GatewaySkillSources,
)
from app.services.infrastructure.resource_platform.sources.workspace_file_resources import (
    WorkspaceFileResourceRegistry,
)
from app.services.infrastructure.workspace_file_watch_service import (
    WorkspaceFileChangeBatch,
    WorkspaceFileWatchService,
)

GATEWAY_ID = "gateway_0123456789abcdef0123456789abcdef"


@pytest.fixture
async def source_case(tmp_path: Path):
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


async def test_authentication_failure_blocks_retained_revision_until_authenticated_recovery(source_case):
    owner, source, skill, channel = source_case
    mode = "accepted"
    requests = []

    def reply(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["X-Local-Token"] == "test-local-token"
        if mode == "denied":
            return httpx.Response(401, json={"detail": "invalid local token"})
        publication = owner.snapshot(workspace_id=source._workspace_id)
        etag = '"' + publication.revision + '"'
        if request.headers.get("If-None-Match") == etag:
            return httpx.Response(304, headers={"ETag": etag})
        return httpx.Response(200, json={"data": publication.model_dump(), "request_id": "raw-source-request"}, headers={"ETag": etag})

    source._client = httpx.AsyncClient(transport=httpx.MockTransport(reply))
    subscription = channel.subscribe(label="assert unavailable")
    await source.ensure_bound(GATEWAY_ID)
    first = source.snapshot(gateway_id=GATEWAY_ID)
    assert first.entries[0].revision == "sha256:" + hashlib.sha256(skill.read_bytes()).hexdigest()
    assert first.entries[0].content == "原始正文 Ω  \r\n"
    assert str(skill) not in first.model_dump_json()
    assert "test-local-token" not in first.model_dump_json()
    assert (await subscription.next()).available
    await source.reconcile()
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.02):
            await subscription.next()
    mode = "denied"
    await source.reconcile()
    with pytest.raises(RuntimeError, match="unavailable"):
        source.snapshot(gateway_id=GATEWAY_ID)
    denied = await subscription.next()
    assert not denied.available and denied.revision == first.revision
    mode = "accepted"
    await source.reconcile()
    assert source.snapshot(gateway_id=GATEWAY_ID) is first
    assert (await subscription.next()).available
    assert requests[-1].headers["If-None-Match"] == '"' + first.revision + '"'
    channel.unsubscribe(subscription)


@pytest.mark.parametrize("failure", ["cause", "context", "connect-timeout"])
async def test_initial_binding_waits_only_for_gateway_connection(source_case, failure):
    owner, source, _skill, _channel = source_case
    attempts = []

    def reply(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
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
        return httpx.Response(200, json={"data": publication.model_dump(), "request_id": "initial-ready"})

    source._client = httpx.AsyncClient(transport=httpx.MockTransport(reply))
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

    source._client = httpx.AsyncClient(transport=httpx.MockTransport(reply))
    error_type = "ReadTimeout" if failure == "read-timeout" else "ConnectError"
    with pytest.raises(RuntimeError, match=error_type):
        await source.ensure_bound(GATEWAY_ID)
    assert len(attempts) == 1 and source._task is None
    assert source._snapshot is None


@pytest.mark.parametrize("failure", [401, 403, "protocol", "unavailable"])
async def test_initial_binding_does_not_retry_authenticated_rejection_or_unavailable(source_case, failure):
    owner, source, _skill, _channel = source_case
    attempts = []

    def reply(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        if isinstance(failure, int):
            return httpx.Response(failure, json={"detail": "denied"})
        if failure == "protocol":
            return httpx.Response(200, json={"data": {}, "request_id": "invalid"})
        publication = owner.snapshot(workspace_id=source._workspace_id)
        payload = publication.model_dump()
        payload["available"] = False
        payload["error_code"] = "source-unavailable"
        payload["revision"] = "sha256:" + hashlib.sha256(
            canonical_json_bytes({"gateway_id": GATEWAY_ID, "available": False,
                "entries": [(entry.source_id, entry.revision, entry.available) for entry in publication.entries]}),
        ).hexdigest()
        return httpx.Response(200, json={"data": payload, "request_id": "unavailable"})

    source._client = httpx.AsyncClient(transport=httpx.MockTransport(reply))
    if failure == "unavailable":
        await source.ensure_bound(GATEWAY_ID)
        assert not source.snapshot(gateway_id=GATEWAY_ID).available
    else:
        with pytest.raises(RuntimeError, match="unavailable"):
            await source.ensure_bound(GATEWAY_ID)
    assert len(attempts) == 1


async def test_initial_connection_wait_uses_existing_timeout_budget_per_entry(source_case):
    owner, source, _skill, _channel = source_case
    source._connection = GatewayTransportConnection(gateway_url="http://gateway.test", timeout_seconds=0.03)
    attempts = []
    ready = False

    def reply(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        if not ready:
            raise httpx.ConnectError("Gateway 始终未监听", request=request) from ConnectionRefusedError(
                errno.ECONNREFUSED, "Gateway 尚未监听",
            )
        publication = owner.snapshot(workspace_id=source._workspace_id)
        return httpx.Response(200, json={"data": publication.model_dump(), "request_id": "new-entry"})

    source._client = httpx.AsyncClient(transport=httpx.MockTransport(reply))
    started = asyncio.get_running_loop().time()
    with pytest.raises(RuntimeError, match="TimeoutError"):
        await source.ensure_bound(GATEWAY_ID)
    assert 0.03 <= asyncio.get_running_loop().time() - started < 0.2
    assert len(attempts) == 1 and source._task is None
    assert source._error == "TimeoutError"
    ready = True
    await source.ensure_bound(GATEWAY_ID)
    assert len(attempts) == 2
    assert source.snapshot(gateway_id=GATEWAY_ID).available


async def test_initial_binding_response_cannot_exceed_total_connection_budget(source_case):
    owner, source, _skill, _channel = source_case
    source._connection = GatewayTransportConnection(gateway_url="http://gateway.test", timeout_seconds=0.03)
    attempts = []

    async def reply(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        await asyncio.sleep(0.3)
        publication = owner.snapshot(workspace_id=source._workspace_id)
        return httpx.Response(200, json={"data": publication.model_dump(), "request_id": "late-response"})

    source._client = httpx.AsyncClient(transport=httpx.MockTransport(reply))
    started = asyncio.get_running_loop().time()
    with pytest.raises(RuntimeError, match="TimeoutError"):
        await source.ensure_bound(GATEWAY_ID)
    assert 0.03 <= asyncio.get_running_loop().time() - started < 0.2
    assert len(attempts) == 1 and source._task is None
    assert source._snapshot is None
    assert source._error == "TimeoutError"


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
    source._gateway_id = GATEWAY_ID
    source._accept_snapshot(original)
    payload = original.model_dump(mode="json")
    target = payload if field in {"gateway_id", "workspace_id", "revision"} else payload["entries"][0]
    target[field] = 999 if field == "byte_length" else "corrupted"
    source._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(
        200, json={"data": payload, "request_id": "wrong-source-request"},
    )))
    await source.reconcile()
    with pytest.raises(RuntimeError, match="unavailable"):
        source.snapshot(gateway_id=GATEWAY_ID)
    assert source._snapshot is original


def test_workspace_owner_cannot_register_gateway_directory(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = WorkspaceFileResourceRegistry(
        workspace_root=workspace, watch_service=WorkspaceFileWatchService(workspace_root=workspace),
    )
    with pytest.raises(ValueError, match="已知固定根"):
        registry.register_skill_directory(root=tmp_path / "gateway-home" / "skills", scope="gateway", scope_id=GATEWAY_ID)
