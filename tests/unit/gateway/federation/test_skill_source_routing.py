"""真实 owner、凭据和 replay store 配合内存 channel 的 Skill 路由边界。"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

import httpx
import pytest

from app.gateway.credentials import FederationCredentialStore
from app.gateway.federation.channel import ChannelHandshake, FederationChannelSession
from app.gateway.federation.errors import (
    FEDERATION_AUTHORIZATION_REVOKED,
    FEDERATION_CAPABILITY_DENIED,
    FEDERATION_GRANT_REPLAY,
    FEDERATION_MALFORMED_FRAME,
    FEDERATION_UNKNOWN_PEER,
    FederationError,
)
from app.gateway.federation.identity import FederationPeerIdentity
from app.gateway.federation.policy import FederationPolicyStore
from app.gateway.federation.ports import FederationSpoke
from app.gateway.federation.protocol import METHOD_SKILL_SOURCES
from app.gateway.federation.rpc import FederationRpcService
from app.gateway.federation.skill_sources import (
    GatewaySkillSourcePort,
    relay_gateway_skill_sources,
)
from app.gateway.federation.store import (
    FederationControlStore,
    federation_control_database,
)
from app.gateway.federation.workspace_port import (
    WorkspaceCatalogPort,
    WorkspaceSessionMainPort,
)
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.runtime.skill_sources import GatewaySkillSourceOwner
from app.schemas.gateway.skill_sources import (
    GatewaySkillPublicationDTO,
    GatewaySkillSourcesDTO,
)

WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
HUB = "gateway_0123456789abcdef0123456789abcdef"
SPOKE = "gateway_fedcba9876543210fedcba9876543210"


class _Directory:
    def __init__(self) -> None:
        self.spokes = (FederationSpoke(SPOKE, "registered-b"),)

    def active_spokes(self) -> tuple[FederationSpoke, ...]:
        return self.spokes


class _UnusedTransport:
    async def send_text(self, message: str) -> None:
        raise AssertionError("单元测试不建立网络连接")

    async def receive_text(self) -> str | None:
        raise AssertionError("单元测试不建立网络连接")

    async def close(self, *, code: int, reason: str) -> None:
        pass


class _DirectChannel(FederationChannelSession):
    """只替换 transport，服务自身仍校验真实 channel/credential/epoch。"""

    remote_service: FederationRpcService
    remote_session: FederationChannelSession
    requests: list[dict[str, object]]
    receipt_drift: str | None = None
    after_response: Callable[[], None] | None = None

    async def request(self, *, method, request, timeout, deadline_at):
        assert method == METHOD_SKILL_SOURCES and timeout > 0
        self.requests.append(request)
        result = await self.remote_service.handle_request(
            method=method, request=request, session=self.remote_session, deadline_at=deadline_at,
        )
        if self.after_response is not None:
            self.after_response()
        if self.receipt_drift is not None:
            result[self.receipt_drift] = "another-owner"
        return result


@dataclass
class _Case:
    hub: FederationRpcService
    spoke: FederationRpcService
    directory: _Directory
    outbound: _DirectChannel
    inbound: FederationChannelSession

    def request(self, **updates: object) -> dict[str, object]:
        return {"gateway_id": SPOKE, "workspace_id": WORKSPACE_ID,
                "request_id": "skill-owner-request", "kind": "snapshot", **updates}


@pytest.fixture
async def skill_route_case(tmp_path: Path):
    directory = _Directory()
    client = httpx.AsyncClient(trust_env=False)
    services = []
    registries = []
    owners = []
    try:
        for gateway_id in (HUB, SPOKE):
            root = tmp_path / gateway_id
            skill = root / "skills/shared/SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("正文 " + gateway_id)
            owner = GatewaySkillSourceOwner(gateway_id=gateway_id, skills_root=root / "skills")
            owner._publish()
            owners.append(owner)
            registry = GatewayWorkspaceRegistry(storage_path=root / "registry.json")
            registry.upsert(WorkspaceTarget(
                workspace_id="local-workspace", name="shared", root_path=str(tmp_path / "workspace"),
                backend_workspace_id=WORKSPACE_ID, backend_url="http://unused.test",
                connection_kind="local", managed=False,
            ))
            registries.append(registry)
            services.append(FederationRpcService(
                gateway_id=gateway_id, policy_store=FederationPolicyStore(),
                control_store=FederationControlStore(database=federation_control_database(gateway_root=root)),
                signing_key=object(), catalog=WorkspaceCatalogPort(http_client=client),
                session_main=WorkspaceSessionMainPort(http_client=client),
                credential_store=FederationCredentialStore(storage_path=root / "credentials.json"),
                spoke_directory=directory if gateway_id == HUB else None,
                gateway_skill_sources=GatewaySkillSourcePort(owner=owner, registry=registry),
            ))
        hub, spoke = services
        inbound_credential = spoke.credential_store.issue(connection_id="hub-a", peer_gateway_id=HUB)
        hub.credential_store.put(replace(inbound_credential, connection_id="registered-b"))

        def channel(service, peer, connection_id, role):
            return _DirectChannel(
                transport=_UnusedTransport(),
                handshake=ChannelHandshake(
                    channel_instance_id="chan_" + role, channel_epoch=1,
                    local_gateway_id=service.gateway_id,
                    peer=FederationPeerIdentity(gateway_id=peer, connection_id=connection_id, public_key_pem="unused"),
                    credential_digest=hashlib.sha256(inbound_credential.token.encode()).hexdigest(),
                    local_role=role,
                ), handler=service.request_handler(),
            )

        outbound = channel(hub, SPOKE, "registered-b", "hub")
        inbound = channel(spoke, HUB, "hub-a", "spoke")
        outbound.remote_service, outbound.remote_session, outbound.requests = spoke, inbound, []
        await hub.adopt_outbound_channel(outbound)
        await spoke.register_channel(inbound)
        yield _Case(hub, spoke, directory, outbound, inbound)
    finally:
        for service in services:
            await service.close_channels()
            service.control_store.close()
        for registry in registries:
            registry.close()
        for owner in owners:
            await owner.close()
        await client.aclose()


async def test_registered_direct_owner_returns_its_bytes_and_conditional_revision(skill_route_case):
    case = skill_route_case
    first = await relay_gateway_skill_sources(case.hub, case.request())
    assert first["status"] == 200 and first["gateway_id"] == SPOKE
    assert first["data"]["entries"][0]["content"] == "正文 " + SPOKE
    second = await relay_gateway_skill_sources(case.hub, case.request(if_none_match=first["etag"]))
    assert second["status"] == 304 and second["etag"] == first["etag"]
    assert case.outbound.requests[0]["nonce"] != case.outbound.requests[1]["nonce"]


async def test_publication_notice_uses_same_authenticated_direct_owner_and_deadline(skill_route_case):
    case = skill_route_case
    first = await relay_gateway_skill_sources(case.hub, case.request(kind="publication", publication={}))
    assert first["status"] == 200
    notice = GatewaySkillPublicationDTO.model_validate(first["data"])
    assert notice.gateway_id == SPOKE and notice.workspace_id == WORKSPACE_ID
    assert notice.changed and notice.gap
    snapshot = await relay_gateway_skill_sources(case.hub, case.request())
    assert GatewaySkillSourcesDTO.model_validate(snapshot["data"]).revision == notice.revision
    assert "entries" not in notice.model_dump() and "content" not in notice.model_dump()
    heartbeat = await relay_gateway_skill_sources(case.hub, case.request(kind="publication",
        publication={"after_cursor": notice.cursor, "wait_ms": 1}))
    assert heartbeat["data"] == {**notice.model_dump(), "changed": False, "gap": False}
    with pytest.raises(TimeoutError):
        await relay_gateway_skill_sources(case.hub, case.request(kind="publication",
            publication={"after_cursor": notice.cursor, "wait_ms": 4000}), deadline_at=time.time() + 0.02)
    assert case.spoke.gateway_skill_sources.owner._publications.subscriber_ids == ()


@pytest.mark.parametrize("failure", ["unknown", "duplicate", "connection", "credential", "policy"])
async def test_relay_rejects_untrusted_or_changed_connection_before_disclosure(skill_route_case, failure):
    case = skill_route_case
    expected = FEDERATION_UNKNOWN_PEER
    if failure == "unknown":
        case.directory.spokes = ()
    elif failure == "duplicate":
        case.directory.spokes *= 2
    elif failure == "connection":
        case.directory.spokes = (FederationSpoke(SPOKE, "different-connection"),)
    elif failure == "credential":
        case.hub.credential_store.issue(connection_id="registered-b", peer_gateway_id=HUB)
        expected = FEDERATION_AUTHORIZATION_REVOKED
    else:
        case.hub.policy_store.publish({"default_effect": "deny"})
        expected = FEDERATION_AUTHORIZATION_REVOKED
    with pytest.raises(FederationError) as caught:
        await relay_gateway_skill_sources(case.hub, case.request())
    assert caught.value.code == expected
    assert case.outbound.requests == []


@pytest.mark.parametrize("field", ["gateway_id", "workspace_id", "request_id"])
async def test_relay_rejects_receipt_identity_drift(skill_route_case, field):
    case = skill_route_case
    case.outbound.receipt_drift = field
    with pytest.raises(FederationError) as caught:
        await relay_gateway_skill_sources(case.hub, case.request())
    assert caught.value.code == FEDERATION_MALFORMED_FRAME


@pytest.mark.parametrize("failure", ["connection", "credential", "policy"])
async def test_relay_rechecks_current_connection_and_policy_after_owner_reply(skill_route_case, failure):
    case = skill_route_case

    def change_authority() -> None:
        if failure == "connection":
            case.directory.spokes = (FederationSpoke(SPOKE, "replacement-connection"),)
        elif failure == "credential":
            case.hub.credential_store.issue(connection_id="registered-b", peer_gateway_id=HUB)
        else:
            case.hub.policy_store.publish({"default_effect": "deny"})

    case.outbound.after_response = change_authority
    with pytest.raises(FederationError) as caught:
        await relay_gateway_skill_sources(case.hub, case.request())
    assert caught.value.code == (FEDERATION_UNKNOWN_PEER if failure == "connection" else FEDERATION_AUTHORIZATION_REVOKED)
    assert len(case.outbound.requests) == 1


async def test_spoke_rejects_transit_replay_and_outside_workspace_audience(skill_route_case):
    case = skill_route_case
    outside = await case.spoke.gateway_skill_sources.execute(case.request(workspace_id="outside-workspace"))
    assert outside["status"] == 403 and "data" not in outside
    envelope = case.request(origin_gateway_id=HUB, nonce="one-use-only")
    first = await case.spoke.handle_request(
        method=METHOD_SKILL_SOURCES, request=envelope, session=case.inbound, deadline_at=time.time() + 5,
    )
    assert first["status"] == 200
    with pytest.raises(FederationError) as replay:
        await case.spoke.handle_request(
            method=METHOD_SKILL_SOURCES, request=envelope, session=case.inbound, deadline_at=time.time() + 5,
        )
    assert replay.value.code == FEDERATION_GRANT_REPLAY
    with pytest.raises(FederationError) as transit:
        await case.spoke.handle_request(
            method=METHOD_SKILL_SOURCES, request={**envelope, "origin_gateway_id": "third-gateway"},
            session=case.inbound, deadline_at=time.time() + 5,
        )
    assert transit.value.code == FEDERATION_UNKNOWN_PEER
    with pytest.raises(FederationError) as reversed_channel:
        await case.hub.handle_request(
            method=METHOD_SKILL_SOURCES, request=envelope, session=case.outbound, deadline_at=time.time() + 5,
        )
    assert reversed_channel.value.code == FEDERATION_CAPABILITY_DENIED


async def test_skill_navigation_consumes_original_federation_deadline(skill_route_case, monkeypatch):
    case = skill_route_case
    uri = f"boxteam://gateway/{SPOKE}/resources/skills/shared/SKILL.md"
    owner = case.spoke.gateway_skill_sources.owner
    original = owner._navigation._navigate_local
    seen = []

    def inspect_deadline(parsed, query, gateway_id, deadline, artifact_reader=None):
        seen.append((query.timeout_ms, deadline - time.monotonic()))
        return original(parsed, query, gateway_id, deadline, artifact_reader)

    monkeypatch.setattr(owner._navigation, "_navigate_local", inspect_deadline)
    result = await relay_gateway_skill_sources(case.hub, case.request(kind="navigation",
        navigation={"uri": uri, "operation": "read", "timeout_ms": 1234}), deadline_at=time.time() + 0.5)
    assert result["status"] == 200 and result["data"]["content"] == "正文 " + SPOKE
    query_timeout, remaining = seen.pop()
    assert query_timeout == 1234 and 0 < remaining <= 0.5
