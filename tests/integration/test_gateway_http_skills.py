"""Gateway Skill 真实 owner 借用统一外部 HTTP 出口，代理替身只终止外部服务边界。"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from app.gateway.runtime.consumer_protocol import GatewayRuntimeConsumerTransaction
from app.gateway.runtime.external_http import GatewayExternalHTTPTransport
from app.gateway.runtime.skill_sources import GatewaySkillSourceOwner
from app.gateway.secrets.vault import SecretVault
from app.services.infrastructure.resource_platform.observation.source_update_policy import (
    SourceUpdatePolicy,
)
from app.services.infrastructure.resource_platform.sources.skill_source_config import (
    SkillSourceConfig,
)


@dataclass
class SkillProxy:
    body: bytes
    url: str = ""
    requests: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    stall: bool = False
    reject: bool = False
    writers: set[asyncio.StreamWriter] = field(default_factory=set)

    async def respond(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.writers.add(writer)
        try:
            lines = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            headers = {key.lower(): value.strip() for line in lines[1:] if ":" in line
                       for key, _, value in [line.partition(":")]}
            self.requests.append((lines[0], headers))
            target = httpx.URL(lines[0].split()[1])
            if self.reject:
                status, body, extra = b"503 Service Unavailable", b"", b""
            elif target.path == "/redirect":
                status, body = b"302 Found", b""
                extra = b"Location: http://skill-source.invalid/SKILL.md\r\n"
            elif self.stall:
                # 等客户端的真实超时关闭 socket，不用固定 sleep 伪造 deadline。
                await reader.read()
                return
            else:
                etag = '"' + self.body.decode() + '"'
                status, body = ((b"304 Not Modified", b"") if headers.get("if-none-match") == etag
                                else (b"200 OK", self.body))
                extra = f"ETag: {etag}\r\nLast-Modified: Mon, 23 Nov 2026 10:00:00 GMT\r\n".encode()
            writer.write(b"HTTP/1.1 " + status + b"\r\n" + extra
                         + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            self.writers.discard(writer)


@pytest.fixture
async def proxies() -> AsyncIterator[tuple[SkillProxy, SkillProxy]]:
    endpoints = SkillProxy(b"proxy-A"), SkillProxy(b"proxy-B")
    servers = []
    try:
        for endpoint in endpoints:
            server = await asyncio.start_server(endpoint.respond, "127.0.0.1", 0)
            endpoint.url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            servers.append(server)
        yield endpoints
    finally:
        for server in servers:
            server.close()
            await server.wait_closed()
        for endpoint in endpoints:
            for writer in tuple(endpoint.writers):
                writer.close()
                await writer.wait_closed()


@dataclass
class GatewaySkillHTTPCase:
    owner: GatewaySkillSourceOwner
    client: httpx.AsyncClient
    transport: GatewayExternalHTTPTransport


@pytest.fixture
async def skill_http_case(tmp_path: Path, proxies: tuple[SkillProxy, SkillProxy]) -> AsyncIterator[GatewaySkillHTTPCase]:
    transport = GatewayExternalHTTPTransport(SecretVault(tmp_path / "secrets"))
    await transport.start({"proxy": {"mode": "explicit", "url": proxies[0].url}})
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        config = SkillSourceConfig.model_validate({
            "id": "remote", "name": "remote", "updates": {"mode": "manual"},
            "fetch": {"protocol": "http", "url": "http://skill-source.invalid/redirect", "timeout_seconds": 0.5},
        })
        owner = GatewaySkillSourceOwner(
            gateway_id="gateway_" + "1" * 32, skills_root=tmp_path / "skills",
            update_policy=SourceUpdatePolicy(mode="manual"), sources=(config,), external_http_client=client,
        )
        async with owner.lifespan():
            yield GatewaySkillHTTPCase(owner, client, transport)


async def test_owner_fetch_redirect_conditionals_policy_switch_and_borrowed_lifetime(
    skill_http_case: GatewaySkillHTTPCase, proxies: tuple[SkillProxy, SkillProxy],
) -> None:
    case = skill_http_case
    first, second = proxies
    snapshot = case.owner.snapshot(workspace_id="workspace")
    assert snapshot.available and snapshot.entries[0].content == "proxy-A"
    assert [line for line, _ in first.requests] == [
        "GET http://skill-source.invalid/redirect HTTP/1.1", "GET http://skill-source.invalid/SKILL.md HTTP/1.1",
    ]
    await case.owner.refresh()
    assert case.owner.snapshot(workspace_id="workspace") == snapshot
    assert first.requests[-1][1]["if-none-match"] == '"proxy-A"'
    assert first.requests[-1][1]["if-modified-since"] == "Mon, 23 Nov 2026 10:00:00 GMT"

    transaction = await GatewayRuntimeConsumerTransaction((case.transport.stage(
        payload={"proxy": {"mode": "explicit", "url": second.url}}, generation="skill-proxy-switch",
        fencing_token_digest=None, fence_check=None,
    ),)).apply()
    await transaction.promote()
    await transaction.finalize()
    await case.owner.refresh()
    changed = case.owner.snapshot(workspace_id="workspace")
    assert changed.available and changed.entries[0].content == "proxy-B"
    assert changed.entries[0].revision != snapshot.entries[0].revision
    assert second.requests[-1][1]["if-none-match"] == '"proxy-A"'
    assert len(first.requests) == 4

    await case.owner.close()
    assert not case.client.is_closed
    assert (await case.client.get("http://skill-source.invalid/after-owner-close", timeout=2)).content == b"proxy-B"


async def test_owner_timeout_preserves_accepted_body_and_releases_response(
    skill_http_case: GatewaySkillHTTPCase, proxies: tuple[SkillProxy, SkillProxy],
) -> None:
    case = skill_http_case
    endpoint = proxies[0]
    before = case.owner.snapshot(workspace_id="workspace").entries[0]
    endpoint.stall = True
    await case.owner.refresh()
    failed = case.owner.snapshot(workspace_id="workspace")
    assert not failed.available and not failed.entries[0].available
    assert failed.entries[0].content == before.content
    assert failed.entries[0].revision == before.revision
    assert case.transport._active is not None and case.transport._active.readers == 0
    endpoint.stall = False
    await case.owner.refresh()
    assert case.owner.snapshot(workspace_id="workspace").entries[0] == before


@pytest.mark.parametrize("rotate_secret", [False, True])
async def test_same_transaction_prepares_skill_with_candidate_proxy_and_rotated_secret(
    skill_http_case: GatewaySkillHTTPCase, proxies: tuple[SkillProxy, SkillProxy], rotate_secret: bool,
) -> None:
    case = skill_http_case
    previous, replacement = proxies
    secret_id = "sec_" + "2" * 32
    auth = {"username": "skill-user", "password": "secret:" + secret_id}
    if rotate_secret:
        case.transport.vault.create(secret_id=secret_id, name="proxy", value="first")
        authenticated = await GatewayRuntimeConsumerTransaction((case.transport.stage(
            payload={"proxy": {"mode": "explicit", "url": previous.url, "auth": auth}}, generation="skill-auth",
            fencing_token_digest=None, fence_check=None,
        ),)).apply()
        await authenticated.promote()
        await authenticated.finalize()
        case.transport.vault.update(secret_id, expected_revision=1, name="proxy", value="second")
        case.transport.invalidate_secret(secret_id)

    previous.reject = True
    old_request_count = len(previous.requests)
    candidate_proxy: dict[str, object] = {"mode": "explicit", "url": replacement.url}
    if rotate_secret:
        candidate_proxy["auth"] = auth
    payload = {"proxy": candidate_proxy}
    source = SkillSourceConfig.model_validate({
        "id": "remote", "name": "changed", "updates": {"mode": "manual"},
        "fetch": {"protocol": "http", "url": "http://skill-source.invalid/new-SKILL.md", "timeout_seconds": 0.5},
    })
    # 保持产品顺序：全部 prepare 结束前，外部策略尚未切换且仍持有事务锁。
    async with asyncio.timeout(2):
        transaction = await GatewayRuntimeConsumerTransaction((
            case.transport.stage(payload=payload, generation="skill-and-proxy",
                                 fencing_token_digest=None, fence_check=None),
            case.owner.sources_stage(sources=(source,), generation="skill-and-proxy",
                                     fencing_token_digest=None, fence_check=None,
                                     prepare_http_client=lambda: case.transport.candidate_client(payload)),
        )).apply()
    await transaction.promote()
    await transaction.finalize()
    snapshot = case.owner.snapshot(workspace_id="workspace")
    assert snapshot.available and snapshot.entries[0].content == "proxy-B"
    assert snapshot.entries[0].name == "changed"
    assert len(previous.requests) == old_request_count
    assert [line for line, _ in replacement.requests] == ["GET http://skill-source.invalid/new-SKILL.md HTTP/1.1"]
    if rotate_secret:
        assert replacement.requests[0][1]["proxy-authorization"] == "Basic " + base64.b64encode(b"skill-user:second").decode()
    await case.owner.refresh()
    assert case.owner.snapshot(workspace_id="workspace") == snapshot
    assert replacement.requests[-1][1]["if-none-match"] == '"proxy-B"'
    assert len(previous.requests) == old_request_count
    assert len(case.transport._pools) == 1
    assert case.transport._active is not None and case.transport._active.readers == 0

    # 再改当前策略，证明初始候选 runtime 没有成为来源的永久第二出口。
    previous.reject, replacement.reject = False, True
    subsequent = await GatewayRuntimeConsumerTransaction((case.transport.stage(
        payload={"proxy": {"mode": "explicit", "url": previous.url}}, generation="skill-subsequent-policy",
        fencing_token_digest=None, fence_check=None,
    ),)).apply()
    await subsequent.promote()
    await subsequent.finalize()
    await case.owner.refresh()
    refreshed = case.owner.snapshot(workspace_id="workspace")
    assert refreshed.available and refreshed.entries[0].content == "proxy-A"
    assert refreshed.entries[0].name == "changed"
    assert len(previous.requests) == old_request_count + 1
    assert len(replacement.requests) == 2
