"""联邦 peer RPC channel 的契约测试（真实 loopback WebSocket）。

本模块在 8790-8799 区间自选空闲端口，启动**最小** FastAPI 应用并只挂载联邦
channel 路由，跑真实 TCP/WebSocket 握手与 RPC 往返；它不是完整 Gateway E2E
（不启动 registry、SSH、工作区后端）。每个拒绝路径都断言具体稳定错误码。
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Self

import pytest
import uvicorn
from fastapi import FastAPI
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from app.gateway.credentials import FederationCredentialStore
from app.gateway.federation.dialer import HubDialRequest, dial_spoke_channel
from app.gateway.federation.errors import (
    CLOSE_CODE_POLICY_VIOLATION,
    CLOSE_CODE_PROTOCOL_ERROR,
    FEDERATION_AUTHORIZATION_REVOKED,
    FEDERATION_CHANNEL_CLOSED,
    FEDERATION_GRANT_EXPIRED,
    FEDERATION_GRANT_INVALID_SIGNATURE,
    FEDERATION_GRANT_REPLAY,
    FEDERATION_HARDENING_ACTIVE,
    FEDERATION_TARGET_NOT_RESOLVABLE,
    FEDERATION_TRANSIT_LIMIT_EXCEEDED,
    FederationError,
)
from app.gateway.federation.grants import (
    MAX_GATEWAY_HOPS,
    MAX_TRANSIT_GATEWAYS,
    GrantTarget,
    sign_grant,
)
from app.gateway.federation.identity import (
    FederationPeerIdentity,
    load_or_create_signing_key,
    public_key_pem,
)
from app.gateway.federation.policy import FederationPolicyStore
from app.gateway.federation.protocol import (
    FRAME_HELLO,
    FRAME_PING,
    FRAME_PONG,
    FRAME_REQUEST,
    FRAME_RESPONSE,
    FRAME_WELCOME,
    METHOD_DISCOVERY,
    METHOD_RELAY,
    METHOD_STATUS,
)
from app.gateway.federation.router import router as federation_router
from app.gateway.federation.rpc import (
    FederationRpcService,
    FederationSpoke,
    new_origin_nonce,
    principal_ref_for_gateway,
)
from app.gateway.federation.store import (
    FederationControlStore,
    federation_control_database,
)
from app.gateway.runtime.process import allocate_local_port_in_range

pytestmark = pytest.mark.contract

PORT_RANGE = (8790, 8799)
CHAIN = ("gateway_origin", "gateway_hub", "gateway_spoke")


class _Catalog:
    def __init__(self, mapping: dict[str, tuple[str, ...]]) -> None:
        self.mapping = mapping

    async def local_session_workspaces(self, session_id: str) -> tuple[str, ...]:
        return self.mapping.get(session_id, ())


class _SessionMain:
    def __init__(
        self,
        mapping: dict[tuple[str, str, str], str],
        *,
        delay_seconds: float = 0.0,
    ) -> None:
        self.mapping = mapping
        self.delay_seconds = delay_seconds

    async def resolve_main_thread(
        self, *, gateway_id: str, workspace_id: str, session_id: str
    ) -> str:
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        key = (gateway_id, workspace_id, session_id)
        if key not in self.mapping:
            raise FederationError(
                FEDERATION_TARGET_NOT_RESOLVABLE, "没有该 target 的 main thread"
            )
        return self.mapping[key]


class _SpokeDirectory:
    def __init__(self, spokes: tuple[FederationSpoke, ...] | None) -> None:
        self._spokes = spokes or ()

    def active_spokes(self) -> tuple[FederationSpoke, ...]:
        return self._spokes


@dataclass
class _Gateway:
    app: FastAPI
    service: FederationRpcService
    server: uvicorn.Server
    port: int
    gateway_id: str
    root: Path
    credential_store: FederationCredentialStore
    control: FederationControlStore

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/api/gateway/federation/channel"


@asynccontextmanager
async def _start_gateway(
    *,
    gateway_id: str,
    tmp_path: Path,
    catalog: _Catalog,
    session_main: _SessionMain,
    role: str,
    spokes: tuple[FederationSpoke, ...] | None = None,
    policy_payload: dict[str, object] | None = None,
):
    root = tmp_path / gateway_id
    root.mkdir(parents=True, exist_ok=True)
    policy = FederationPolicyStore(initial=policy_payload)
    control = FederationControlStore(
        database=federation_control_database(gateway_root=root)
    )
    credential_store = FederationCredentialStore(
        storage_path=root / "credentials" / "federation.json"
    )
    service = FederationRpcService(
        gateway_id=gateway_id,
        policy_store=policy,
        control_store=control,
        signing_key=load_or_create_signing_key(root),
        catalog=catalog,
        session_main=session_main,
        spoke_directory=_SpokeDirectory(spokes) if role == "hub" else None,
    )
    app = FastAPI()
    app.state.federation_rpc_service = service
    app.state.federation_credential_store = credential_store
    app.state.federation_gateway_root = root
    app.include_router(federation_router)
    port = allocate_local_port_in_range(*PORT_RANGE)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    if not server.started:
        raise TimeoutError("Gateway 测试服务未在 10 秒内启动")
    gateway = _Gateway(
        app=app,
        service=service,
        server=server,
        port=port,
        gateway_id=gateway_id,
        root=root,
        credential_store=credential_store,
        control=control,
    )
    try:
        yield gateway
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)
        control.close()


def _hub_identity(gateway_root: Path) -> FederationPeerIdentity:
    """构造受信 hub 的匿名身份（仅用于 hello 公钥绑定）。"""

    private_key = load_or_create_signing_key(gateway_root)
    return FederationPeerIdentity(
        gateway_id="gateway_hub",
        connection_id="rgw_hub",
        public_key_pem=public_key_pem(private_key),
    )


class _RawClient:
    """裸帧客户端：用于构造畸形/伪造/越界输入。"""

    def __init__(self, websocket) -> None:
        self.websocket = websocket

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.websocket.close()

    async def send(self, frame_type: str, payload: dict[str, object]) -> None:
        await self.websocket.send(
            json.dumps({"frame_type": frame_type, "payload": payload})
        )

    async def send_raw(self, raw: str) -> None:
        await self.websocket.send(raw)

    async def recv(self, *, timeout: float = 5.0) -> dict[str, object]:
        raw = await asyncio.wait_for(self.websocket.recv(), timeout=timeout)
        return json.loads(raw)

    async def recv_error(self, *, timeout: float = 5.0) -> dict[str, object]:
        frame = await self.recv(timeout=timeout)
        assert frame["frame_type"] == FRAME_RESPONSE, frame
        assert frame["payload"]["ok"] is False, frame
        return frame["payload"]["error"]

    async def recv_close_code(self, *, timeout: float = 5.0) -> int:
        with pytest.raises((ConnectionClosed, WebSocketDisconnect)) as error:
            await asyncio.wait_for(self.websocket.recv(), timeout=timeout)
        value = error.value
        if isinstance(value, ConnectionClosed):
            assert value.rcvd is not None
            return int(value.rcvd.code)
        return int(getattr(value, "code", 1006))


async def _open_raw(
    *,
    gateway: _Gateway,
    hub: FederationPeerIdentity,
    token: str,
    channel_epoch: int = 1,
) -> _RawClient:
    websocket = await connect(
        gateway.ws_url,
        additional_headers={"X-BoxTeam-Federation-Token": token},
    )
    client = _RawClient(websocket)
    await client.send(
        FRAME_HELLO,
        {
            "gateway_id": hub.gateway_id,
            "public_key_pem": hub.public_key_pem,
            "channel_epoch": channel_epoch,
        },
    )
    welcome = await client.recv()
    assert welcome["frame_type"] == FRAME_WELCOME
    return client


def _issue(spoke: _Gateway, *, hub: FederationPeerIdentity) -> str:
    return spoke.credential_store.issue(
        connection_id=hub.connection_id, peer_gateway_id=hub.gateway_id
    ).token


def _signed_grant(
    *,
    signer_root: Path,
    origin: str,
    audience: str,
    transit_path: tuple[str, ...],
    kind: str = "discovery",
    lifetime_seconds: float = 30.0,
    target: GrantTarget | None = None,
    operation: str | None = None,
) -> dict[str, object]:
    """用 ``signer_root`` 的私钥签发 grant（issuer 恒为受信 hub id）。"""

    return sign_grant(
        private_key=load_or_create_signing_key(signer_root),
        grant_kind=kind,  # type: ignore[arg-type]
        issuer_gateway_id="gateway_hub",
        origin_gateway_id=origin,
        audience_gateway_id=audience,
        transit_path=transit_path,
        request_id="req_test",
        principal_ref=principal_ref_for_gateway(origin),
        lifetime_seconds=lifetime_seconds,
        target=target,
        operation=operation,  # type: ignore[arg-type]
    ).encode()


def _expired_grant(*, signer_root: Path) -> dict[str, object]:
    """构造签名正确但已过期的 grant。"""

    import base64
    from datetime import UTC, datetime, timedelta

    private_key = load_or_create_signing_key(signer_root)
    grant = sign_grant(
        private_key=private_key,
        grant_kind="discovery",
        issuer_gateway_id="gateway_hub",
        origin_gateway_id="gateway_origin",
        audience_gateway_id="gateway_spoke",
        transit_path=CHAIN,
        request_id="req_expired",
        principal_ref=principal_ref_for_gateway("gateway_origin"),
        lifetime_seconds=30.0,
        now=datetime.now(UTC) - timedelta(seconds=120),
    )
    stale = replace(
        grant,
        issued_at=datetime.now(UTC) - timedelta(seconds=120),
        expires_at=datetime.now(UTC) - timedelta(seconds=60),
        signature="",
    )
    signature = private_key.sign(stale.canonical_bytes())
    return replace(
        stale,
        signature=base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii"),
    ).encode()


def _discovery_request(
    *,
    grant: dict[str, object],
    session_id: str = "ses_target",
    nonce: str = "n",
    visited: tuple[str, ...] = ("gateway_origin", "gateway_hub"),
) -> dict[str, object]:
    return {
        "grant": grant,
        "session_id": session_id,
        "nonce": nonce,
        # visited 是「已走过的完整路径」；receiver 侧再拼接自身后必须等于 grant path。
        "visited": list(visited),
        "max_transit_gateways": MAX_TRANSIT_GATEWAYS,
        "max_gateway_hops": MAX_GATEWAY_HOPS,
    }


@asynccontextmanager
async def _spoke_with_hub(tmp_path: Path, **kwargs):
    hub_root = tmp_path / "hub_identity"
    hub_root.mkdir(parents=True, exist_ok=True)
    hub = _hub_identity(hub_root)
    async with _start_gateway(
        gateway_id="gateway_spoke", tmp_path=tmp_path, **kwargs
    ) as spoke:
        yield spoke, hub, hub_root


@pytest.mark.asyncio
async def test_channel_status_round_trip_over_real_websocket(tmp_path: Path) -> None:
    async with _spoke_with_hub(
        tmp_path, catalog=_Catalog({}), session_main=_SessionMain({}), role="spoke"
    ) as (spoke, hub, _root):
        client = await _open_raw(gateway=spoke, hub=hub, token=_issue(spoke, hub=hub))
        async with client:
            await client.send(
                FRAME_REQUEST,
                {"correlation_id": "cor_1", "method": METHOD_STATUS, "request": {}},
            )
            response = await client.recv()
            assert response["frame_type"] == FRAME_RESPONSE
            assert response["payload"]["correlation_id"] == "cor_1"
            assert response["payload"]["ok"] is True
            assert response["payload"]["result"]["gateway_id"] == "gateway_spoke"


@pytest.mark.asyncio
async def test_ping_is_answered_with_pong(tmp_path: Path) -> None:
    async with _spoke_with_hub(
        tmp_path, catalog=_Catalog({}), session_main=_SessionMain({}), role="spoke"
    ) as (spoke, hub, _root):
        client = await _open_raw(gateway=spoke, hub=hub, token=_issue(spoke, hub=hub))
        async with client:
            await client.send(FRAME_PING, {})
            frame = await client.recv()
            assert frame["frame_type"] == FRAME_PONG


@pytest.mark.asyncio
async def test_unknown_method_returns_explicit_error(tmp_path: Path) -> None:
    async with _spoke_with_hub(
        tmp_path, catalog=_Catalog({}), session_main=_SessionMain({}), role="spoke"
    ) as (spoke, hub, _root):
        client = await _open_raw(gateway=spoke, hub=hub, token=_issue(spoke, hub=hub))
        async with client:
            await client.send(
                FRAME_REQUEST,
                {
                    "correlation_id": "cor_x",
                    "method": "federation.nonexistent",
                    "request": {},
                },
            )
            error = await client.recv_error()
            assert error["code"] == "federation-capability-denied"


@pytest.mark.asyncio
async def test_malformed_frame_closes_channel_with_protocol_error(
    tmp_path: Path,
) -> None:
    async with _spoke_with_hub(
        tmp_path, catalog=_Catalog({}), session_main=_SessionMain({}), role="spoke"
    ) as (spoke, hub, _root):
        client = await _open_raw(gateway=spoke, hub=hub, token=_issue(spoke, hub=hub))
        await client.send_raw("this-is-not-json")
        assert await client.recv_close_code() == CLOSE_CODE_PROTOCOL_ERROR


@pytest.mark.asyncio
async def test_missing_credential_is_closed_with_1008(tmp_path: Path) -> None:
    async with _start_gateway(
        gateway_id="gateway_spoke",
        tmp_path=tmp_path,
        catalog=_Catalog({}),
        session_main=_SessionMain({}),
        role="spoke",
    ) as spoke:
        websocket = await connect(spoke.ws_url)
        with pytest.raises(ConnectionClosed) as error:
            await asyncio.wait_for(websocket.recv(), timeout=5)
        assert error.value.rcvd is not None
        assert int(error.value.rcvd.code) == CLOSE_CODE_POLICY_VIOLATION


@pytest.mark.asyncio
async def test_hello_gateway_id_mismatch_is_closed_with_1008(tmp_path: Path) -> None:
    async with _spoke_with_hub(
        tmp_path, catalog=_Catalog({}), session_main=_SessionMain({}), role="spoke"
    ) as (spoke, hub, _root):
        websocket = await connect(
            spoke.ws_url,
            additional_headers={"X-BoxTeam-Federation-Token": _issue(spoke, hub=hub)},
        )
        await websocket.send(
            json.dumps(
                {
                    "frame_type": FRAME_HELLO,
                    "payload": {
                        "gateway_id": "gateway_impostor",
                        "public_key_pem": hub.public_key_pem,
                        "channel_epoch": 1,
                    },
                }
            )
        )
        with pytest.raises(ConnectionClosed) as error:
            await asyncio.wait_for(websocket.recv(), timeout=5)
        assert error.value.rcvd is not None
        assert int(error.value.rcvd.code) == CLOSE_CODE_POLICY_VIOLATION


@pytest.mark.asyncio
async def test_valid_grant_returns_bounded_discovery_match(tmp_path: Path) -> None:
    async with _spoke_with_hub(
        tmp_path,
        catalog=_Catalog({"ses_target": ("ws_a",)}),
        session_main=_SessionMain({}),
        role="spoke",
    ) as (spoke, hub, hub_root):
        client = await _open_raw(gateway=spoke, hub=hub, token=_issue(spoke, hub=hub))
        async with client:
            await client.send(
                FRAME_REQUEST,
                {
                    "correlation_id": "cor_ok",
                    "method": METHOD_DISCOVERY,
                    "request": _discovery_request(
                        grant=_signed_grant(
                            signer_root=hub_root,
                            origin="gateway_origin",
                            audience="gateway_spoke",
                            transit_path=CHAIN,
                        )
                    ),
                },
            )
            result = (await client.recv())["payload"]["result"]
            assert result["matches"] == [
                {
                    "gateway_id": "gateway_spoke",
                    "workspace_id": "ws_a",
                    "session_id": "ses_target",
                }
            ]
            assert result["ambiguity_count"] == 1


@pytest.mark.asyncio
async def test_declared_path_must_match_grant_path(tmp_path: Path) -> None:
    """声明路径与 grant 绑定路径不一致：必须拒绝，而不是按声明路径放行。"""

    async with _spoke_with_hub(
        tmp_path,
        catalog=_Catalog({"ses_target": ("ws_a",)}),
        session_main=_SessionMain({}),
        role="spoke",
    ) as (spoke, hub, hub_root):
        client = await _open_raw(gateway=spoke, hub=hub, token=_issue(spoke, hub=hub))
        request = _discovery_request(
            grant=_signed_grant(
                signer_root=hub_root,
                origin="gateway_origin",
                audience="gateway_spoke",
                transit_path=CHAIN,
            ),
            nonce=new_origin_nonce(),
        )
        # 声明少一个 Gateway：拼自身后与 grant 的 3 段路径不等。
        request["visited"] = ["gateway_hub"]
        async with client:
            await client.send(
                FRAME_REQUEST,
                {
                    "correlation_id": "cor_path",
                    "method": METHOD_DISCOVERY,
                    "request": request,
                },
            )
            error = await client.recv_error()
            assert error["code"] == FEDERATION_TRANSIT_LIMIT_EXCEEDED
            assert error["detail"]["grant"] == list(CHAIN)


@pytest.mark.asyncio
async def test_discovery_hop_limit_is_rejected_loudly(tmp_path: Path) -> None:
    async with _spoke_with_hub(
        tmp_path,
        catalog=_Catalog({"ses_target": ("ws_a",)}),
        session_main=_SessionMain({}),
        role="spoke",
    ) as (spoke, hub, hub_root):
        client = await _open_raw(gateway=spoke, hub=hub, token=_issue(spoke, hub=hub))
        request = _discovery_request(
            grant=_signed_grant(
                signer_root=hub_root,
                origin="gateway_origin",
                audience="gateway_spoke",
                transit_path=CHAIN,
            ),
            nonce=new_origin_nonce(),
        )
        request["max_gateway_hops"] = MAX_GATEWAY_HOPS + 1
        async with client:
            await client.send(
                FRAME_REQUEST,
                {
                    "correlation_id": "cor_hop",
                    "method": METHOD_DISCOVERY,
                    "request": request,
                },
            )
            error = await client.recv_error()
            assert error["code"] == FEDERATION_TRANSIT_LIMIT_EXCEEDED
            assert error["detail"]["max_gateway_hops"] == MAX_GATEWAY_HOPS + 1


@pytest.mark.asyncio
async def test_visited_set_recursion_is_rejected(tmp_path: Path) -> None:
    async with _spoke_with_hub(
        tmp_path,
        catalog=_Catalog({"ses_target": ("ws_a",)}),
        session_main=_SessionMain({}),
        role="spoke",
    ) as (spoke, hub, hub_root):
        client = await _open_raw(gateway=spoke, hub=hub, token=_issue(spoke, hub=hub))
        request = _discovery_request(
            grant=_signed_grant(
                signer_root=hub_root,
                origin="gateway_origin",
                audience="gateway_spoke",
                transit_path=CHAIN,
            ),
            nonce=new_origin_nonce(),
        )
        # 本 Gateway 已在 visited 中 → 递归，必须拒绝。
        request["visited"] = ["gateway_hub", "gateway_spoke"]
        async with client:
            await client.send(
                FRAME_REQUEST,
                {
                    "correlation_id": "cor_rec",
                    "method": METHOD_DISCOVERY,
                    "request": request,
                },
            )
            error = await client.recv_error()
            assert error["code"] == FEDERATION_TRANSIT_LIMIT_EXCEEDED


@pytest.mark.asyncio
async def test_expired_grant_is_rejected(tmp_path: Path) -> None:
    async with _spoke_with_hub(
        tmp_path,
        catalog=_Catalog({"ses_target": ("ws_a",)}),
        session_main=_SessionMain({}),
        role="spoke",
    ) as (spoke, hub, hub_root):
        client = await _open_raw(gateway=spoke, hub=hub, token=_issue(spoke, hub=hub))
        async with client:
            await client.send(
                FRAME_REQUEST,
                {
                    "correlation_id": "cor_exp",
                    "method": METHOD_DISCOVERY,
                    "request": _discovery_request(
                        grant=_expired_grant(signer_root=hub_root)
                    ),
                },
            )
            error = await client.recv_error()
            assert error["code"] == FEDERATION_GRANT_EXPIRED


@pytest.mark.asyncio
async def test_forged_grant_is_rejected_by_signature(tmp_path: Path) -> None:
    attacker_root = tmp_path / "attacker"
    attacker_root.mkdir(parents=True)
    async with _spoke_with_hub(
        tmp_path,
        catalog=_Catalog({"ses_target": ("ws_a",)}),
        session_main=_SessionMain({}),
        role="spoke",
    ) as (spoke, hub, _hub_root):
        client = await _open_raw(gateway=spoke, hub=hub, token=_issue(spoke, hub=hub))
        async with client:
            await client.send(
                FRAME_REQUEST,
                {
                    "correlation_id": "cor_forge",
                    "method": METHOD_DISCOVERY,
                    "request": _discovery_request(
                        grant=_signed_grant(
                            signer_root=attacker_root,
                            origin="gateway_origin",
                            audience="gateway_spoke",
                            transit_path=CHAIN,
                        )
                    ),
                },
            )
            error = await client.recv_error()
            assert error["code"] == FEDERATION_GRANT_INVALID_SIGNATURE


@pytest.mark.asyncio
async def test_grant_replay_is_rejected(tmp_path: Path) -> None:
    async with _spoke_with_hub(
        tmp_path,
        catalog=_Catalog({"ses_target": ("ws_a",)}),
        session_main=_SessionMain({}),
        role="spoke",
    ) as (spoke, hub, hub_root):
        client = await _open_raw(gateway=spoke, hub=hub, token=_issue(spoke, hub=hub))
        request = _discovery_request(
            grant=_signed_grant(
                signer_root=hub_root,
                origin="gateway_origin",
                audience="gateway_spoke",
                transit_path=CHAIN,
            ),
            nonce="fixed-nonce",
        )
        async with client:
            await client.send(
                FRAME_REQUEST,
                {"correlation_id": "cor_1", "method": METHOD_DISCOVERY, "request": request},
            )
            assert (await client.recv())["payload"]["ok"] is True
            await client.send(
                FRAME_REQUEST,
                {"correlation_id": "cor_2", "method": METHOD_DISCOVERY, "request": request},
            )
            error = await client.recv_error()
            assert error["code"] == FEDERATION_GRANT_REPLAY


@pytest.mark.asyncio
async def test_hardening_without_rules_denies_core_capability(tmp_path: Path) -> None:
    async with _spoke_with_hub(
        tmp_path,
        catalog=_Catalog({"ses_target": ("ws_a",)}),
        session_main=_SessionMain({}),
        role="spoke",
        policy_payload={"hardening_enabled": True, "rules": []},
    ) as (spoke, hub, hub_root):
        client = await _open_raw(gateway=spoke, hub=hub, token=_issue(spoke, hub=hub))
        async with client:
            await client.send(
                FRAME_REQUEST,
                {
                    "correlation_id": "cor_hard",
                    "method": METHOD_DISCOVERY,
                    "request": _discovery_request(
                        grant=_signed_grant(
                            signer_root=hub_root,
                            origin="gateway_origin",
                            audience="gateway_spoke",
                            transit_path=CHAIN,
                        )
                    ),
                },
            )
            error = await client.recv_error()
            assert error["code"] == FEDERATION_HARDENING_ACTIVE


@pytest.mark.asyncio
async def test_deny_rule_returns_authorization_revoked(tmp_path: Path) -> None:
    async with _spoke_with_hub(
        tmp_path,
        catalog=_Catalog({"ses_target": ("ws_a",)}),
        session_main=_SessionMain({}),
        role="spoke",
        policy_payload={"rules": [{"capability": "discovery", "effect": "deny"}]},
    ) as (spoke, hub, hub_root):
        client = await _open_raw(gateway=spoke, hub=hub, token=_issue(spoke, hub=hub))
        async with client:
            await client.send(
                FRAME_REQUEST,
                {
                    "correlation_id": "cor_rev",
                    "method": METHOD_DISCOVERY,
                    "request": _discovery_request(
                        grant=_signed_grant(
                            signer_root=hub_root,
                            origin="gateway_origin",
                            audience="gateway_spoke",
                            transit_path=CHAIN,
                        )
                    ),
                },
            )
            error = await client.recv_error()
            assert error["code"] == FEDERATION_AUTHORIZATION_REVOKED
            assert error["detail"]["capability"] == "discovery"


@pytest.mark.asyncio
async def test_peer_disconnect_fails_in_flight_request(tmp_path: Path) -> None:
    """对端在请求在途时断开：in-flight 请求必须以 channel-closed 显式失败。"""

    async with _start_gateway(
        gateway_id="gateway_spoke_a",
        tmp_path=tmp_path,
        catalog=_Catalog({}),
        session_main=_SessionMain({}, delay_seconds=30.0),
        role="spoke",
    ) as spoke:
        hub_root = tmp_path / "hub_dialer"
        hub_root.mkdir(parents=True, exist_ok=True)
        hub = _hub_identity(hub_root)
        async with _start_gateway(
            gateway_id="gateway_hub",
            tmp_path=tmp_path,
            catalog=_Catalog({}),
            session_main=_SessionMain({}),
            role="hub",
            spokes=(FederationSpoke(gateway_id="gateway_spoke_a", connection_id="rgw"),),
        ) as hub_gateway:
            session = await dial_spoke_channel(
                request=HubDialRequest(
                    gateway_url=spoke.base_url,
                    credential_token=_issue(spoke, hub=hub),
                    expected_peer_gateway_id="gateway_spoke_a",
                    connection_id=hub.connection_id,
                ),
                local_gateway_id=hub.gateway_id,
                gateway_root=hub_root,
                handler=hub_gateway.service.request_handler(),
                channel_epoch=1,
            )
            target = GrantTarget(
                gateway_id="gateway_spoke_a",
                workspace_id="ws_a",
                session_id="ses_target",
            )
            grant = _signed_grant(
                signer_root=hub_root,
                origin="gateway_origin",
                audience="gateway_spoke_a",
                transit_path=("gateway_origin", "gateway_hub", "gateway_spoke_a"),
                kind="operation",
                target=target,
                operation="read",
            )
            pending = asyncio.create_task(
                session.request(
                    method=METHOD_RELAY,
                    request={
                        "grant": grant,
                        "operation": "read",
                        "target": target.to_payload(),
                        "visited": ["gateway_origin", "gateway_hub"],
                        "max_transit_gateways": MAX_TRANSIT_GATEWAYS,
                        "max_gateway_hops": MAX_GATEWAY_HOPS,
                    },
                    timeout=25.0,
                )
            )
            # 目标解析被挂住（30s delay），此时对端中途断开。
            await asyncio.sleep(1.0)
            spoke.server.should_exit = True
            with pytest.raises(FederationError) as error:
                await asyncio.wait_for(pending, timeout=25)
            assert error.value.code == FEDERATION_CHANNEL_CLOSED
            await session.close()
