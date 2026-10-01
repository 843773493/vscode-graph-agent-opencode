"""hub transit 契约测试：``B → A → C`` 反向发起并以原路响应返回。

用三个真实 loopback channel（hub A 主动 dial B 与 C），断言 spoke 能反向发起、
hub 从 channel 绑定确认真实 origin、只允许一次 transit，且 target 以受信 hub 身份
与最新 policy 重新授权并解析 main thread。拒绝路径断言具体稳定错误码。
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI

from app.gateway.credentials import FederationCredentialStore
from app.gateway.federation.dialer import HubDialRequest, dial_spoke_channel
from app.gateway.federation.errors import (
    FEDERATION_TARGET_NOT_RESOLVABLE,
    FederationError,
)
from app.gateway.federation.grants import (
    MAX_GATEWAY_HOPS,
    MAX_TRANSIT_GATEWAYS,
    GrantTarget,
)
from app.gateway.federation.identity import load_or_create_signing_key
from app.gateway.federation.policy import FederationPolicyStore
from app.gateway.federation.router import router as federation_router
from app.gateway.federation.rpc import FederationRpcService, FederationSpoke
from app.gateway.federation.store import (
    FederationControlStore,
    federation_control_database,
)
from app.gateway.runtime.process import allocate_local_port_in_range

pytestmark = pytest.mark.contract

PORT_RANGE = (8810, 8819)
MAIN_C = "thr_44444444444444444444444444444444"


class _Catalog:
    def __init__(self, mapping: dict[str, tuple[str, ...]]) -> None:
        self.mapping = mapping

    async def local_session_workspaces(self, session_id: str) -> tuple[str, ...]:
        return self.mapping.get(session_id, ())


class _SessionMain:
    def __init__(self, mapping: dict[tuple[str, str, str], str]) -> None:
        self.mapping = mapping

    async def resolve_main_thread(
        self, *, gateway_id: str, workspace_id: str, session_id: str
    ) -> str:
        key = (gateway_id, workspace_id, session_id)
        if key not in self.mapping:
            raise FederationError(
                FEDERATION_TARGET_NOT_RESOLVABLE, "没有该 target 的 main thread"
            )
        return self.mapping[key]


class _SpokeDirectory:
    def __init__(self, spokes: tuple[FederationSpoke, ...]) -> None:
        self._spokes = spokes

    def active_spokes(self) -> tuple[FederationSpoke, ...]:
        return self._spokes


@dataclass
class _Gateway:
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


@asynccontextmanager
async def _start(
    *,
    gateway_id: str,
    tmp_path: Path,
    catalog: _Catalog,
    session_main: _SessionMain,
    role: str,
    spokes: tuple[FederationSpoke, ...] = (),
):
    root = tmp_path / gateway_id
    root.mkdir(parents=True, exist_ok=True)
    control = FederationControlStore(
        database=federation_control_database(gateway_root=root)
    )
    credential_store = FederationCredentialStore(
        storage_path=root / "credentials" / "federation.json"
    )
    service = FederationRpcService(
        gateway_id=gateway_id,
        policy_store=FederationPolicyStore(),
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


def _issue(store: FederationCredentialStore, *, connection_id: str, peer: str) -> str:
    return store.issue(connection_id=connection_id, peer_gateway_id=peer).token


@asynccontextmanager
async def _federation(tmp_path: Path, *, c_catalog: dict[str, tuple[str, ...]]):
    """三个真实 gateway：hub A 主动 dial spoke B 与 spoke C。"""

    async with _start(
        gateway_id="gateway_c",
        tmp_path=tmp_path,
        catalog=_Catalog(c_catalog),
        session_main=_SessionMain({("gateway_c", "ws_c", "ses_c"): MAIN_C}),
        role="spoke",
    ) as gateway_c, _start(
        gateway_id="gateway_b",
        tmp_path=tmp_path,
        catalog=_Catalog({}),
        session_main=_SessionMain({}),
        role="spoke",
    ) as gateway_b, _start(
        gateway_id="gateway_hub",
        tmp_path=tmp_path,
        catalog=_Catalog({}),
        session_main=_SessionMain({}),
        role="hub",
        spokes=(
            FederationSpoke(gateway_id="gateway_b", connection_id="rgw_b"),
            FederationSpoke(gateway_id="gateway_c", connection_id="rgw_c"),
        ),
    ) as hub:
        session_b = await dial_spoke_channel(
            request=HubDialRequest(
                gateway_url=gateway_b.base_url,
                credential_token=_issue(
                    gateway_b.credential_store,
                    connection_id="rgw_hub",
                    peer="gateway_hub",
                ),
                expected_peer_gateway_id="gateway_b",
                connection_id="rgw_b",
            ),
            local_gateway_id="gateway_hub",
            gateway_root=hub.root,
            handler=hub.service.request_handler(),
            channel_epoch=1,
        )
        session_c = await dial_spoke_channel(
            request=HubDialRequest(
                gateway_url=gateway_c.base_url,
                credential_token=_issue(
                    gateway_c.credential_store,
                    connection_id="rgw_hub",
                    peer="gateway_hub",
                ),
                expected_peer_gateway_id="gateway_c",
                connection_id="rgw_c",
            ),
            local_gateway_id="gateway_hub",
            gateway_root=hub.root,
            handler=hub.service.request_handler(),
            channel_epoch=1,
        )
        hub.service.adopt_outbound_channel(session_b)
        hub.service.adopt_outbound_channel(session_c)
        try:
            yield hub, gateway_b, gateway_c
        finally:
            await session_b.close()
            await session_c.close()


@pytest.mark.asyncio
async def test_spoke_b_reaches_spoke_c_through_hub_and_gets_reply(
    tmp_path: Path,
) -> None:
    """B 反向发起 ``B → A → C`` 并以原路响应返回；业务 source 始终是 B。"""

    async with _federation(tmp_path, c_catalog={"ses_c": ("ws_c",)}) as (
        _hub,
        gateway_b,
        _gateway_c,
    ):
        resolved = await gateway_b.service.resolve_session_target(session_id="ses_c")
        assert resolved == {
            "gateway_id": "gateway_c",
            "workspace_id": "ws_c",
            "session_id": "ses_c",
        }
        result = await gateway_b.service.relay_operation(
            target=GrantTarget(
                gateway_id="gateway_c", workspace_id="ws_c", session_id="ses_c"
            ),
            operation="read",
        )
        assert result["relayed_by"] == "gateway_hub"
        assert result["origin_gateway_id"] == "gateway_b"
        assert result["transit_path"] == [
            "gateway_b",
            "gateway_hub",
            "gateway_c",
        ]
        target_envelope = result["result"]
        assert target_envelope["accepted_by"] == "gateway_c"
        assert target_envelope["operation"] == "read"
        assert target_envelope["target"]["resolved_main_thread_id"] == MAIN_C
        assert target_envelope["principal_ref"].startswith("prn_")


@pytest.mark.asyncio
async def test_hub_discovery_matches_only_the_owning_spoke(tmp_path: Path) -> None:
    """hub 只向 active spoke 转发一次；未命中的 spoke 不产生候选。"""

    async with _federation(tmp_path, c_catalog={"ses_c": ("ws_c",)}) as (
        hub,
        gateway_b,
        _gateway_c,
    ):
        # hub 自身无本地候选，唯一命中来自 C。
        resolved = await gateway_b.service.resolve_session_target(session_id="ses_c")
        assert resolved["gateway_id"] == "gateway_c"
        assert hub.service.gateway_id == "gateway_hub"


@pytest.mark.asyncio
async def test_unknown_bare_id_is_not_resolvable(tmp_path: Path) -> None:
    async with _federation(tmp_path, c_catalog={"ses_c": ("ws_c",)}) as (
        _hub,
        gateway_b,
        _gateway_c,
    ):
        with pytest.raises(FederationError) as error:
            await gateway_b.service.resolve_session_target(session_id="ses_absent")
        assert error.value.code == FEDERATION_TARGET_NOT_RESOLVABLE


@pytest.mark.asyncio
async def test_ambiguous_bare_id_returns_candidate_count_only(tmp_path: Path) -> None:
    """多个已授权候选只返回 ``target_ambiguous`` 与候选数，不含 locator。"""

    async with _federation(
        tmp_path, c_catalog={"ses_shared": ("ws_c1", "ws_c2")}
    ) as (_hub, gateway_b, _gateway_c):
        with pytest.raises(FederationError) as error:
            await gateway_b.service.resolve_session_target(session_id="ses_shared")
        assert error.value.code == "target_ambiguous"
        assert error.value.detail == {"candidate_count": 2}


@pytest.mark.asyncio
async def test_target_spoke_rejects_grant_path_mismatch(tmp_path: Path) -> None:
    """target spoke 以 grant 绑定路径复核：声明路径与 grant 不一致必须拒绝。"""

    from app.gateway.federation.protocol import METHOD_RELAY

    async with _federation(tmp_path, c_catalog={"ses_c": ("ws_c",)}) as (
        _hub,
        gateway_b,
        _gateway_c,
    ):
        hub_channel = gateway_b.service.hub_channel
        assert hub_channel is not None
        # visited 比实际路径多一个 Gateway：hub 拼路径时越过预算，必须拒绝。
        with pytest.raises(FederationError) as error:
            await hub_channel.request(
                method=METHOD_RELAY,
                request={
                    "nonce": "fixed-nonce",
                    "operation": "read",
                    "target": {
                        "gateway_id": "gateway_c",
                        "workspace_id": "ws_c",
                        "session_id": "ses_c",
                    },
                    "visited": ["gateway_b", "gateway_extra"],
                    "max_transit_gateways": MAX_TRANSIT_GATEWAYS,
                    "max_gateway_hops": MAX_GATEWAY_HOPS,
                },
                timeout=5.0,
            )
        assert error.value.code == "federation-transit-limit-exceeded"


@pytest.mark.asyncio
async def test_hub_rejects_relay_from_unregistered_spoke(tmp_path: Path) -> None:
    """hub 从 channel 绑定确认真实 origin：未登记 spoke 必须拒绝。"""

    from app.gateway.federation.protocol import METHOD_RELAY

    async with _federation(tmp_path, c_catalog={"ses_c": ("ws_c",)}) as (
        hub,
        gateway_b,
        _gateway_c,
    ):
        # 把 hub 的 spoke 目录换成不含 B 的集合，模拟未登记 origin。
        hub.service.spoke_directory = _SpokeDirectory(
            (FederationSpoke(gateway_id="gateway_c", connection_id="rgw_c"),)
        )
        hub_channel = gateway_b.service.hub_channel
        assert hub_channel is not None
        with pytest.raises(FederationError) as error:
            await hub_channel.request(
                method=METHOD_RELAY,
                request={
                    "nonce": "fixed-nonce-2",
                    "operation": "read",
                    "target": {
                        "gateway_id": "gateway_c",
                        "workspace_id": "ws_c",
                        "session_id": "ses_c",
                    },
                    "visited": ["gateway_b"],
                    "max_transit_gateways": MAX_TRANSIT_GATEWAYS,
                    "max_gateway_hops": MAX_GATEWAY_HOPS,
                },
                timeout=5.0,
            )
        assert error.value.code == "federation-unknown-peer"
