"""hub transit 契约测试：``B → A → C`` 反向发起并以原路响应返回。

用三个真实 loopback channel（hub A 主动 dial B 与 C），断言 spoke 能反向发起、
hub 从 channel 绑定确认真实 origin、只允许一次 transit，且 target 以受信 hub 身份
与最新 policy 重新授权并解析 main thread。拒绝路径断言具体稳定错误码。
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI

from app.core.trace_middleware import reset_current_request_id, set_current_request_id
from app.gateway.credentials import FederationCredentialStore
from app.gateway.federation.dialer import HubDialRequest, dial_spoke_channel
from app.gateway.federation.errors import (
    FEDERATION_AUTHORIZATION_REVOKED,
    FEDERATION_CHANNEL_CLOSED,
    FEDERATION_DEADLINE_EXCEEDED,
    FEDERATION_GRANT_KIND_MISMATCH,
    FEDERATION_TARGET_NOT_RESOLVABLE,
    FEDERATION_UNKNOWN_PEER,
    FederationError,
)
from app.gateway.federation.grants import (
    MAX_GATEWAY_HOPS,
    MAX_TRANSIT_GATEWAYS,
    ContextGrantTarget,
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
from app.services.business.session_context_resource import (
    session_resource_identity,
    session_resource_uri,
)
from tests.support.communication import (
    SOURCE_SESSION,
    SOURCE_THREAD,
    TARGET_SESSION,
    TARGET_THREAD,
    WORKSPACE_ID,
)

pytestmark = pytest.mark.contract

PORT_RANGE = (8810, 8819)
MAIN_C = TARGET_THREAD


@pytest.fixture(autouse=True)
def request_scope():
    token = set_current_request_id("req_transit_contract")
    yield
    reset_current_request_id(token)


@pytest.fixture
def relay_body() -> dict[str, object]:
    workspace = "22222222-2222-4222-8222-222222222222"
    session = "ses_019b9dffc29a779e8564d196f78bb0a6"
    return {
        "observation_id": "obs_transit_contract",
        "source_call_id": "cop_transit_contract",
        "page_identity": "page-0",
        "page_ordinal": 0,
        "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        "operation": "read",
        "source": {
            "gateway_id": "gateway_b",
            "workspace_id": "11111111-1111-4111-8111-111111111111",
            "session_id": SOURCE_SESSION,
            "thread_id": SOURCE_THREAD,
        },
        "target": {
            "gateway_id": "gateway_c",
            "workspace_id": workspace,
            "session_id": session,
            "thread_id": MAIN_C,
        },
        "query": {
            "read": {
                "resource_identity": str(session_resource_identity(workspace, session)),
                "uri": session_resource_uri(workspace, session),
                "scope": "workspace",
                "view": "messages",
            },
        },
    }


class _Catalog:
    def __init__(self, mapping: dict[str, tuple[str, ...]]) -> None:
        self.mapping = mapping

    async def local_session_workspaces(self, session_id: str) -> tuple[str, ...]:
        return self.mapping.get(session_id, ())


class _SessionMain:
    def __init__(self, mapping: dict[tuple[str, str, str], str]) -> None:
        self.mapping = mapping
        self.operations: list[dict[str, object]] = []

    async def resolve_main_thread(
        self, *, gateway_id: str, workspace_id: str, session_id: str
    ) -> str:
        key = (gateway_id, workspace_id, session_id)
        if key not in self.mapping:
            raise FederationError(
                FEDERATION_TARGET_NOT_RESOLVABLE, "没有该 target 的 main thread"
            )
        return self.mapping[key]

    async def execute(self, *, operation, target, body, origin, timeout):
        self.operations.append(
            {
                "operation": operation,
                "target": target,
                "body": body,
                "origin": origin,
                "timeout": timeout,
            }
        )
        return {
            "observation_id": body["observation_id"],
            "items": [{"text": "C 的冷读结果"}],
            "target": target,
        }


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
        credential_store=credential_store,
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
        await service.close_channels()
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)
        control.close()


@asynccontextmanager
async def _federation(tmp_path: Path, *, c_catalog: dict[str, tuple[str, ...]]):
    """三个真实 gateway：hub A 主动 dial spoke B 与 spoke C。"""

    async with (
        _start(
            gateway_id="gateway_c",
            tmp_path=tmp_path,
            catalog=_Catalog(c_catalog),
            session_main=_SessionMain(
                {
                    ("gateway_c", workspace, session): MAIN_C
                    for session, workspaces in c_catalog.items()
                    for workspace in workspaces
                }
            ),
            role="spoke",
        ) as gateway_c,
        _start(
            gateway_id="gateway_b",
            tmp_path=tmp_path,
            catalog=_Catalog({}),
            session_main=_SessionMain({}),
            role="spoke",
        ) as gateway_b,
        _start(
            gateway_id="gateway_hub",
            tmp_path=tmp_path,
            catalog=_Catalog({}),
            session_main=_SessionMain({}),
            role="hub",
            spokes=(
                FederationSpoke(gateway_id="gateway_b", connection_id="rgw_b"),
                FederationSpoke(gateway_id="gateway_c", connection_id="rgw_c"),
            ),
        ) as hub,
    ):
        credential_b = gateway_b.credential_store.issue(
            connection_id="rgw_hub", peer_gateway_id="gateway_hub"
        )
        credential_c = gateway_c.credential_store.issue(
            connection_id="rgw_hub", peer_gateway_id="gateway_hub"
        )
        hub.credential_store.put(replace(credential_b, connection_id="rgw_b"))
        hub.credential_store.put(replace(credential_c, connection_id="rgw_c"))
        session_b = await dial_spoke_channel(
            request=HubDialRequest(
                gateway_url=gateway_b.base_url,
                credential_token=credential_b.token,
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
                credential_token=credential_c.token,
                expected_peer_gateway_id="gateway_c",
                connection_id="rgw_c",
            ),
            local_gateway_id="gateway_hub",
            gateway_root=hub.root,
            handler=hub.service.request_handler(),
            channel_epoch=1,
        )
        await hub.service.adopt_outbound_channel(session_b)
        await hub.service.adopt_outbound_channel(session_c)
        try:
            yield hub, gateway_b, gateway_c
        finally:
            await session_b.close()
            await session_c.close()


@pytest.mark.asyncio
async def test_spoke_b_reaches_spoke_c_through_hub_and_gets_reply(
    tmp_path: Path,
    relay_body: dict[str, object],
) -> None:
    """B 反向发起 ``B → A → C`` 并以原路响应返回；业务 source 始终是 B。"""

    async with _federation(
        tmp_path,
        c_catalog={
            "ses_019b9dffc29a779e8564d196f78bb0a6": (
                "22222222-2222-4222-8222-222222222222",
            )
        },
    ) as (
        _hub,
        gateway_b,
        gateway_c,
    ):
        resolved = await gateway_b.service.resolve_session_target(
            session_id="ses_019b9dffc29a779e8564d196f78bb0a6"
        )
        assert resolved == {
            "gateway_id": "gateway_c",
            "workspace_id": "22222222-2222-4222-8222-222222222222",
            "session_id": "ses_019b9dffc29a779e8564d196f78bb0a6",
            "thread_id": MAIN_C,
            "main_thread_id": MAIN_C,
        }
        result = await gateway_b.service.relay_operation(
            target=GrantTarget(
                gateway_id="gateway_c",
                workspace_id="22222222-2222-4222-8222-222222222222",
                session_id="ses_019b9dffc29a779e8564d196f78bb0a6",
            ),
            operation="read",
            body=relay_body,
        )
        assert result["relayed_by"] == "gateway_hub"
        assert result["origin_gateway_id"] == "gateway_b"
        assert result["transit_path"] == [
            "gateway_b",
            "gateway_hub",
            "gateway_c",
        ]
        target_envelope = result["result"]
        assert target_envelope["items"] == [{"text": "C 的冷读结果"}]
        assert target_envelope["target"] == relay_body["target"]
        assert gateway_c.service.session_main.operations[0]["body"] == relay_body
        assert gateway_c.service.session_main.operations[0]["origin"] == "gateway_b"


@pytest.mark.asyncio
async def test_hub_discovery_matches_only_the_owning_spoke(tmp_path: Path) -> None:
    """hub 只向 active spoke 转发一次；未命中的 spoke 不产生候选。"""

    async with _federation(
        tmp_path,
        c_catalog={
            "ses_019b9dffc29a779e8564d196f78bb0a6": (
                "22222222-2222-4222-8222-222222222222",
            )
        },
    ) as (
        hub,
        gateway_b,
        _gateway_c,
    ):
        # hub 自身无本地候选，唯一命中来自 C。
        resolved = await gateway_b.service.resolve_session_target(
            session_id="ses_019b9dffc29a779e8564d196f78bb0a6"
        )
        assert resolved["gateway_id"] == "gateway_c"
        assert hub.service.gateway_id == "gateway_hub"


@pytest.mark.asyncio
async def test_unknown_bare_id_is_not_resolvable(tmp_path: Path) -> None:
    async with _federation(
        tmp_path,
        c_catalog={
            "ses_019b9dffc29a779e8564d196f78bb0a6": (
                "22222222-2222-4222-8222-222222222222",
            )
        },
    ) as (
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

    async with _federation(tmp_path, c_catalog={"ses_shared": ("ws_c1", "ws_c2")}) as (
        _hub,
        gateway_b,
        _gateway_c,
    ):
        with pytest.raises(FederationError) as error:
            await gateway_b.service.resolve_session_target(session_id="ses_shared")
        assert error.value.code == "target_ambiguous"
        assert error.value.detail == {"candidate_count": 2}


@pytest.mark.asyncio
async def test_target_spoke_rejects_grant_path_mismatch(tmp_path: Path) -> None:
    """target spoke 以 grant 绑定路径复核：声明路径与 grant 不一致必须拒绝。"""

    from app.gateway.federation.protocol import METHOD_RELAY

    async with _federation(
        tmp_path,
        c_catalog={
            "ses_019b9dffc29a779e8564d196f78bb0a6": (
                "22222222-2222-4222-8222-222222222222",
            )
        },
    ) as (
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
                    "request_id": "req_transit_contract",
                    "nonce": "fixed-nonce",
                    "operation": "read",
                    "target": {
                        "gateway_id": "gateway_c",
                        "workspace_id": "22222222-2222-4222-8222-222222222222",
                        "session_id": "ses_019b9dffc29a779e8564d196f78bb0a6",
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

    async with _federation(
        tmp_path,
        c_catalog={
            "ses_019b9dffc29a779e8564d196f78bb0a6": (
                "22222222-2222-4222-8222-222222222222",
            )
        },
    ) as (
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
                    "request_id": "req_transit_contract",
                    "nonce": "fixed-nonce-2",
                    "operation": "read",
                    "target": {
                        "gateway_id": "gateway_c",
                        "workspace_id": "22222222-2222-4222-8222-222222222222",
                        "session_id": "ses_019b9dffc29a779e8564d196f78bb0a6",
                    },
                    "visited": ["gateway_b"],
                    "max_transit_gateways": MAX_TRANSIT_GATEWAYS,
                    "max_gateway_hops": MAX_GATEWAY_HOPS,
                },
                timeout=5.0,
            )
        assert error.value.code == "federation-unknown-peer"


@pytest.mark.asyncio
async def test_relay_operation_propagates_absolute_deadline_across_every_hop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relay_body: dict[str, object],
) -> None:
    """请求方给出的绝对 deadline 必须逐跳透传，而非每跳各自重置为 8.0s 上界。

    B → hub → C 三跳链路上，hub 与 C 入站 handle_request 收到的帧内 ``deadline_at``
    必须逐字等于请求方给出的绝对 deadline；若只透传 ``timeout=remaining``，下游会
    收到 ``None`` 并各自获得一个全新的 ``FEDERATION_DEADLINE_SECONDS=8.0`` 上界，
    整体上界随跳数累加，违反规范「总 deadline」合同。
    """

    async with _federation(
        tmp_path,
        c_catalog={
            "ses_019b9dffc29a779e8564d196f78bb0a6": (
                "22222222-2222-4222-8222-222222222222",
            )
        },
    ) as (
        _hub,
        gateway_b,
        _gateway_c,
    ):
        seen: dict[str, float | None] = {}
        original = FederationRpcService.handle_request

        async def recording(self, *, method, request, session, deadline_at):
            seen[self.gateway_id] = deadline_at
            return await original(
                self,
                method=method,
                request=request,
                session=session,
                deadline_at=deadline_at,
            )

        monkeypatch.setattr(FederationRpcService, "handle_request", recording)
        deadline_at = time.time() + 6.0
        await gateway_b.service.relay_operation(
            target=GrantTarget(
                gateway_id="gateway_c",
                workspace_id="22222222-2222-4222-8222-222222222222",
                session_id="ses_019b9dffc29a779e8564d196f78bb0a6",
            ),
            operation="read",
            body=relay_body,
            deadline_at=deadline_at,
        )
        assert seen["gateway_hub"] == deadline_at
        assert seen["gateway_c"] == deadline_at


class _PausedSessionMain(_SessionMain):
    def __init__(self, *, phase: str, mapping: dict[tuple[str, str, str], str]) -> None:
        super().__init__(mapping)
        self.phase = phase
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def resolve_main_thread(self, **kwargs) -> str:
        result = await super().resolve_main_thread(**kwargs)
        if self.phase == "main":
            self.entered.set()
            await self.release.wait()
        return result

    async def execute(self, **kwargs):
        # 本文件仅验证 RPC disclosure 合同；此 port 不创建真实 ledger 或 Job。
        self.operations.append(kwargs)
        self.entered.set()
        await self.release.wait()
        return {"accepted": True, "operation": kwargs["operation"]}


@pytest.fixture
def paused_main(request: pytest.FixtureRequest) -> _PausedSessionMain:
    return _PausedSessionMain(
        phase=request.param,
        mapping={("gateway_c", "22222222-2222-4222-8222-222222222222", "ses_019b9dffc29a779e8564d196f78bb0a6"): MAIN_C},
    )


@pytest.fixture
def operation_body(relay_body: dict[str, object], operation: str) -> dict[str, object]:
    if operation in {"send", "reply"}:
        return {
            "communication_id": "comm_policy_contract", "source": relay_body["source"], "target": relay_body["target"],
            "request_id": "req_transit_contract", "payload": {
                "content": "撤权边界合同", "kind": "reply" if operation == "reply" else "question",
                "reply_to_communication_id": "comm_question" if operation == "reply" else None,
                "delivery_policy": "after_turn",
            },
        }
    if operation == "wait":
        now = datetime.now(UTC)
        return {**relay_body, "operation": "wait", "query": {
            "selector": {"kind": "job", "selector_id": "job_policy_contract"}, "until": "terminal", "timeout_seconds": 5,
            "deadline": {"timeout_seconds": 5, "admitted_at_utc": now.isoformat(), "deadline_at_utc": (now + timedelta(seconds=5)).isoformat(), "monotonic_deadline_ns": 5_000_000_000, "monotonic_origin_id": "clock_policy_contract"},
        }}
    assert operation == "read"
    return relay_body


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["send", "read", "wait", "reply"])
@pytest.mark.parametrize("revoked_at", ["origin", "hub", "target"])
@pytest.mark.parametrize("paused_main", ["execute"], indirect=True)
async def test_in_flight_operation_rechecks_policy_before_disclosure(
    tmp_path: Path, paused_main: _PausedSessionMain, operation: str, operation_body: dict[str, object], revoked_at: str,
) -> None:
    async with _federation(tmp_path, c_catalog={"ses_019b9dffc29a779e8564d196f78bb0a6": ("22222222-2222-4222-8222-222222222222",)}) as (hub, gateway_b, gateway_c):
        gateway_c.service.session_main = paused_main
        task = asyncio.create_task(gateway_b.service.relay_operation(
            target=GrantTarget("gateway_c", "22222222-2222-4222-8222-222222222222", "ses_019b9dffc29a779e8564d196f78bb0a6"),
            operation=operation, body=operation_body, deadline_at=time.time() + 10,
        ))
        try:
            await asyncio.wait_for(paused_main.entered.wait(), timeout=3)
            revoked = {"origin": gateway_b, "hub": hub, "target": gateway_c}[revoked_at]
            revoked.service.policy_store.publish({"rules": [{"capability": operation, "effect": "deny"}]})
            paused_main.release.set()
            with pytest.raises(FederationError) as failure:
                await task
            assert failure.value.code == FEDERATION_AUTHORIZATION_REVOKED
            assert len(paused_main.operations) == 1
            assert paused_main.operations[0]["origin"] == "gateway_b"
        finally:
            paused_main.release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("paused_main", ["main"], indirect=True)
@pytest.mark.parametrize("operation", ["send", "reply"])
async def test_policy_revoked_during_main_resolution_blocks_admission(
    tmp_path: Path, paused_main: _PausedSessionMain, operation: str,
    operation_body: dict[str, object],
) -> None:
    async with _federation(tmp_path, c_catalog={"ses_019b9dffc29a779e8564d196f78bb0a6": ("22222222-2222-4222-8222-222222222222",)}) as (_hub, gateway_b, gateway_c):
        gateway_c.service.session_main = paused_main
        task = asyncio.create_task(gateway_b.service.relay_operation(
            target=GrantTarget("gateway_c", "22222222-2222-4222-8222-222222222222", "ses_019b9dffc29a779e8564d196f78bb0a6"),
            operation=operation, body=operation_body, deadline_at=time.time() + 10,
        ))
        try:
            await asyncio.wait_for(paused_main.entered.wait(), timeout=3)
            gateway_c.service.policy_store.publish({"rules": [{"capability": operation, "effect": "deny"}]})
            paused_main.release.set()
            with pytest.raises(FederationError) as failure:
                await task
            assert failure.value.code == FEDERATION_AUTHORIZATION_REVOKED
            assert paused_main.operations == []
        finally:
            paused_main.release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("paused_main", ["execute"], indirect=True)
async def test_new_channel_epoch_cancels_old_in_flight_disclosure(
    tmp_path: Path, paused_main: _PausedSessionMain, relay_body: dict[str, object],
) -> None:
    async with _federation(tmp_path, c_catalog={"ses_019b9dffc29a779e8564d196f78bb0a6": ("22222222-2222-4222-8222-222222222222",)}) as (hub, gateway_b, gateway_c):
        gateway_c.service.session_main = paused_main
        old = hub.service.channel_to("gateway_c")
        task = asyncio.create_task(gateway_b.service.relay_operation(
            target=GrantTarget("gateway_c", "22222222-2222-4222-8222-222222222222", "ses_019b9dffc29a779e8564d196f78bb0a6"),
            operation="read", body=relay_body, deadline_at=time.time() + 10,
        ))
        try:
            await asyncio.wait_for(paused_main.entered.wait(), timeout=3)
            credential = gateway_c.credential_store.get("rgw_hub")
            replacement = await dial_spoke_channel(
                request=HubDialRequest(gateway_url=gateway_c.base_url, credential_token=credential.token, expected_peer_gateway_id="gateway_c", connection_id="rgw_c"),
                local_gateway_id="gateway_hub", gateway_root=hub.root, handler=hub.service.request_handler(), channel_epoch=2,
            )
            await hub.service.adopt_outbound_channel(replacement)
            paused_main.release.set()
            with pytest.raises(FederationError) as failure:
                await task
            assert failure.value.code in {FEDERATION_AUTHORIZATION_REVOKED, FEDERATION_CHANNEL_CLOSED}
            assert old.closed
            assert hub.service.channel_to("gateway_c") is replacement
            assert replacement.channel_epoch == 2
        finally:
            paused_main.release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_qualified_discovery_and_context_rejects_share_original_star_channels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """定点查找不查 source catalog，资源 read 沿原 hub 路由给出闭合拒绝码。"""
    async with _federation(tmp_path, c_catalog={TARGET_SESSION: (WORKSPACE_ID,)}) as (
        hub, source, _target,
    ):
        def forbid_local_catalog(*_args, **_kwargs):
            raise AssertionError("qualified remote Session 不得查询本机 cold catalog")
        monkeypatch.setattr(source.service.catalog, "local_session_workspaces", forbid_local_catalog)
        resolved = await source.service.resolve_session_target(
            session_id=TARGET_SESSION, workspace_id=WORKSPACE_ID, gateway_id="gateway_c",
        )
        assert resolved["gateway_id"] == "gateway_c" and resolved["workspace_id"] == WORKSPACE_ID
        assert resolved["main_thread_id"] == TARGET_THREAD
        target = ContextGrantTarget(
            "gateway_c", str(session_resource_identity(WORKSPACE_ID, TARGET_SESSION)),
            session_resource_uri(WORKSPACE_ID, TARGET_SESSION, gateway_authority="gateway_c"), "workspace",
        )
        fields = {key: target.to_payload()[key] for key in ("resource_identity", "uri", "scope")}
        body = {
            "source_gateway_id":"gateway_b", "resource_target":target.to_payload(),
            "read":{**fields, "view":"information"},
        }
        with pytest.raises(FederationError) as error:
            await source.service.relay_operation(
                target=target, operation="send", body=body,
            )
        assert error.value.code == FEDERATION_GRANT_KIND_MISMATCH
        with pytest.raises(FederationError) as error:
            await source.service.relay_operation(
                target=target, operation="read", body=body, deadline_at=time.time() - 1,
            )
        assert error.value.code == FEDERATION_DEADLINE_EXCEEDED
        source.service.policy_store.publish({"rules":[{"capability":"read","effect":"deny"}]})
        with pytest.raises(FederationError) as error:
            await source.service.relay_operation(target=target, operation="read", body=body)
        assert error.value.code == FEDERATION_AUTHORIZATION_REVOKED
        source.service.policy_store.publish({"rules":[]})
        unknown = ContextGrantTarget(
            "gateway_unknown", target.resource_identity,
            session_resource_uri(WORKSPACE_ID, TARGET_SESSION, gateway_authority="gateway_unknown"),
            "workspace",
        )
        with pytest.raises(FederationError) as error:
            await source.service.relay_operation(
                target=unknown, operation="read",
                body={**body, "resource_target":unknown.to_payload(), "read":{**body["read"],"uri":unknown.uri}},
            )
        assert error.value.code == FEDERATION_UNKNOWN_PEER
        channel = hub.service._channels["gateway_c"]
        await channel.close()
        await channel.join()
        hub.service.unregister_channel(channel.channel_instance_id)
        with pytest.raises(FederationError) as error:
            await source.service.relay_operation(target=target, operation="read", body=body)
        assert error.value.code == FEDERATION_CHANNEL_CLOSED
