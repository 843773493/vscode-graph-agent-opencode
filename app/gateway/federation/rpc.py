"""联邦 hub/spoke 的 RPC 服务：channel 绑定、有界 discovery 与 transit 转发。

拓扑固定为一个中心 hub 与它的直接 spoke：hub 通过 SSH ``-L`` 主动连接 spoke，
双方随后在同一 channel 上双向发起 request/response。

方向与授权模型严格区分：

- ``local_role="spoke"`` 的入站请求来自 hub（唯一受信 peer），必须携带 hub 签发
  的 discovery/operation grant；spoke 只查本地 cold catalog，不得递归转发。
- ``local_role="hub"`` 的入站请求来自直接 spoke；hub 从 channel 绑定取得真实
  origin 并拒绝冒充，再按最新 transit policy 为每个目标 spoke 另签 grant。

裸 ``session_id`` 的全拓扑解析是有界 exact-ID discovery（携带 visited set、
``max_transit_gateways=1``、``max_gateway_hops=2`` 与总 deadline）：source 查询
自己的 local workspace；spoke source 再请求唯一 hub，由 hub 查询自身 local
workspace 及其它 active spoke。未授权存在与不存在统一返回
``target_not_resolvable``，多个已授权候选只返回不含 locator 的
``target_ambiguous`` 与候选数。所有错误显式失败，零 workspace 副作用。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

from app.gateway.federation.channel import (
    FederationChannelSession,
    FederationRequestHandler,
)
from app.gateway.federation.errors import (
    FEDERATION_AUTHORIZATION_REVOKED,
    FEDERATION_CAPABILITY_DENIED,
    FEDERATION_DEADLINE_EXCEEDED,
    FEDERATION_GRANT_KIND_MISMATCH,
    FEDERATION_HARDENING_ACTIVE,
    FEDERATION_MALFORMED_FRAME,
    FEDERATION_SESSION_MAIN_UNAVAILABLE,
    FEDERATION_TARGET_AMBIGUOUS,
    FEDERATION_TARGET_NOT_RESOLVABLE,
    FEDERATION_TRANSIT_LIMIT_EXCEEDED,
    FEDERATION_UNKNOWN_PEER,
    FederationError,
)
from app.gateway.federation.grants import (
    DISCOVERY_GRANT_LIFETIME_SECONDS,
    GRANT_CLOCK_SKEW_SECONDS,
    MAX_GATEWAY_HOPS,
    MAX_TRANSIT_GATEWAYS,
    FederationGrant,
    GrantTarget,
    OperationKind,
    sign_grant,
    verify_grant,
)
from app.gateway.federation.messages import (
    FEDERATION_DEADLINE_SECONDS,
    assert_hop_budget,
    discovery_response,
    grant_preimage,
    remaining_deadline,
    require_discovery_scope,
    require_operation,
    require_target,
)
from app.gateway.federation.policy import Capability, FederationPolicyStore
from app.gateway.federation.ports import (
    FederationCatalogPort,
    FederationSessionMainPort,
    FederationSpoke,
    FederationSpokeDirectoryPort,
    new_origin_nonce,
    principal_ref_for_gateway,
)
from app.gateway.federation.protocol import (
    METHOD_DISCOVERY,
    METHOD_RELAY,
    METHOD_STATUS,
    require_str,
)
from app.gateway.federation.store import (
    TRANSPORT_REPLAY_MARGIN_SECONDS,
    FederationControlStore,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class FederationRpcService:
    """Gateway 侧联邦 RPC 服务：按 channel 的 ``local_role`` 区分 hub/spoke。"""

    gateway_id: str
    policy_store: FederationPolicyStore
    control_store: FederationControlStore
    signing_key: object
    catalog: FederationCatalogPort
    session_main: FederationSessionMainPort
    spoke_directory: FederationSpokeDirectoryPort | None = None
    _channels: dict[str, FederationChannelSession] = field(default_factory=dict)
    _local_channels: dict[str, FederationChannelSession] = field(default_factory=dict)
    _hub_channel: FederationChannelSession | None = None

    # --- channel 生命周期 -------------------------------------------------

    @property
    def local_role(self) -> Literal["hub", "spoke"]:
        """本地在 hub/spoke 拓扑中的角色：有 spoke 目录即 hub。"""

        return "hub" if self.spoke_directory is not None else "spoke"

    def register_channel(self, session: FederationChannelSession) -> None:
        self._local_channels[session.channel_instance_id] = session
        self.control_store.register_channel(
            channel_instance_id=session.channel_instance_id,
            peer_gateway_id=session.peer_gateway_id,
            connection_id=session.handshake.peer.connection_id,
            channel_epoch=session.channel_epoch,
            direction="inbound",
        )
        if session.local_role == "spoke":
            self.adopt_hub_channel(session)

    def unregister_channel(self, channel_instance_id: str) -> None:
        self.control_store.unregister_channel(
            channel_instance_id=channel_instance_id
        )
        self.clear_hub_channel(channel_instance_id)
        self._local_channels.pop(channel_instance_id, None)
        for gateway_id, session in list(self._channels.items()):
            if session.channel_instance_id == channel_instance_id:
                self._channels.pop(gateway_id, None)

    def adopt_outbound_channel(self, session: FederationChannelSession) -> None:
        """hub 记录到 spoke 的 outbound channel（拨号成功后调用）。"""

        self._channels[session.peer_gateway_id] = session

    def adopt_hub_channel(self, session: FederationChannelSession) -> None:
        """spoke 记录它唯一的 hub channel；旧 channel 已关闭时替换。"""

        self._hub_channel = session

    def clear_hub_channel(self, channel_instance_id: str) -> None:
        if (
            self._hub_channel is not None
            and self._hub_channel.channel_instance_id == channel_instance_id
        ):
            self._hub_channel = None

    @property
    def hub_channel(self) -> FederationChannelSession | None:
        """spoke 当前唯一的 hub channel；未连接时为 None（不伪造默认值）。"""

        return self._hub_channel

    def channel_to(self, gateway_id: str) -> FederationChannelSession:
        session = self._channels.get(gateway_id)
        if session is None or session.closed:
            raise FederationError(
                FEDERATION_TARGET_NOT_RESOLVABLE,
                f"目标 Gateway 当前没有可用 channel: {gateway_id}",
            )
        return session

    # --- 入站请求分发 -----------------------------------------------------

    def request_handler(self) -> FederationRequestHandler:
        return _FederationRequestDispatcher(self)

    async def handle_request(
        self,
        *,
        method: str,
        request: dict[str, object],
        session: FederationChannelSession,
        deadline_at: float | None,
    ) -> dict[str, object]:
        if method == METHOD_STATUS:
            return {
                "gateway_id": self.gateway_id,
                "channel_epoch": session.channel_epoch,
                "policy_revision": self.policy_store.snapshot.revision,
            }
        if method == METHOD_DISCOVERY:
            if session.local_role == "spoke":
                return await self._serve_discovery_as_spoke(
                    request=request, session=session, deadline_at=deadline_at
                )
            return await self._serve_discovery_as_hub(
                request=request, session=session, deadline_at=deadline_at
            )
        if method == METHOD_RELAY:
            if session.local_role == "spoke":
                return await self._serve_relay_as_spoke(
                    request=request, session=session, deadline_at=deadline_at
                )
            return await self._serve_relay_as_hub(
                request=request, session=session, deadline_at=deadline_at
            )
        raise FederationError(FEDERATION_CAPABILITY_DENIED, f"不支持的方法: {method!r}")

    # --- hub 侧：来自直接 spoke 的 origin 请求 ------------------------------

    async def resolve_session_target(
        self, *, session_id: str, deadline_at: float | None = None
    ) -> dict[str, object]:
        """有界 exact-ID discovery：本地查询 + （必要时）经唯一 hub 的一次 fan-out。

        返回唯一命中或 ``target_not_resolvable``/``target_ambiguous``；
        命中只含稳定身份（gateway/workspace/session），不含 locator。
        """

        require_str({"session_id": session_id}, "session_id")
        visited: tuple[str, ...] = (self.gateway_id,)
        matches = await self._local_matches(session_id)
        if self.spoke_directory is not None:
            # 本地即 hub：直接向其它 active spoke fan-out 一次。
            remaining = remaining_deadline(deadline_at)
            matches.extend(
                await self._fan_out_discovery(
                    session_id=session_id,
                    origin=self.gateway_id,
                    path=visited,
                    remaining=remaining,
                )
            )
        elif self._hub_channel is not None and not self._hub_channel.closed:
            remaining = remaining_deadline(deadline_at)
            result = await self._hub_channel.request(
                method=METHOD_DISCOVERY,
                request={
                    "session_id": session_id,
                    "nonce": new_origin_nonce(),
                    "visited": list(visited),
                    "max_transit_gateways": MAX_TRANSIT_GATEWAYS,
                    "max_gateway_hops": MAX_GATEWAY_HOPS,
                },
                timeout=remaining,
            )
            raw_matches = result.get("matches")
            if not isinstance(raw_matches, list):
                raise FederationError(
                    FEDERATION_TARGET_NOT_RESOLVABLE,
                    "hub discovery 响应缺少 matches 数组",
                )
            for item in raw_matches:
                if not isinstance(item, dict):
                    raise FederationError(
                        FEDERATION_TARGET_NOT_RESOLVABLE,
                        "hub discovery 候选项必须是对象",
                    )
                matches.append(item)
        if not matches:
            raise FederationError(
                FEDERATION_TARGET_NOT_RESOLVABLE,
                f"裸 session id 在当前有界拓扑内不可解析: {session_id}",
            )
        if len(matches) > 1:
            raise FederationError(
                FEDERATION_TARGET_AMBIGUOUS,
                "裸 session id 命中多个已授权候选，请使用 qualified link",
                detail={"candidate_count": len(matches)},
            )
        return matches[0]

    async def relay_operation(
        self,
        *,
        target: GrantTarget,
        operation: OperationKind,
        deadline_at: float | None = None,
    ) -> dict[str, object]:
        """把一个 operation 经唯一 hub transit 送到目标并取回 target envelope。

        本地即 hub 时直接向目标 spoke 签发 grant；本地为 spoke 时把请求交给唯一
        hub，由 hub 从 channel 绑定确认 origin 后转发。仅允许一次 transit。
        """

        remaining = remaining_deadline(deadline_at)
        if self.spoke_directory is not None:
            assert_hop_budget((self.gateway_id, target.gateway_id))
            grant = self._sign_grant(
                grant_kind="operation",
                origin_gateway_id=self.gateway_id,
                audience_gateway_id=target.gateway_id,
                transit_path=(self.gateway_id, target.gateway_id),
                principal_ref=principal_ref_for_gateway(self.gateway_id),
                lifetime=remaining,
                target=target,
                operation=operation,
            )
            return await self.channel_to(target.gateway_id).request(
                method=METHOD_RELAY,
                request={
                    "grant": grant.encode(),
                    "operation": operation,
                    "target": target.to_payload(),
                    "visited": [self.gateway_id],
                    "max_transit_gateways": MAX_TRANSIT_GATEWAYS,
                    "max_gateway_hops": MAX_GATEWAY_HOPS,
                },
                timeout=remaining,
            )
        hub_channel = self._hub_channel
        if hub_channel is None or hub_channel.closed:
            raise FederationError(
                FEDERATION_TARGET_NOT_RESOLVABLE,
                "本地没有可用 hub channel，无法 transit 到目标 Gateway",
            )
        return await hub_channel.request(
            method=METHOD_RELAY,
            request={
                "nonce": new_origin_nonce(),
                "operation": operation,
                "target": target.to_payload(),
                "visited": [self.gateway_id],
                "max_transit_gateways": MAX_TRANSIT_GATEWAYS,
                "max_gateway_hops": MAX_GATEWAY_HOPS,
            },
            timeout=remaining,
        )

    async def _serve_discovery_as_hub(
        self,
        *,
        request: dict[str, object],
        session: FederationChannelSession,
        deadline_at: float | None,
    ) -> dict[str, object]:
        """hub 查询自身 local workspace 并向其它 active spoke fan-out 一次。"""

        origin = self._channel_bound_origin(session)
        session_id = require_str(request, "session_id")
        self._adopt_origin_envelope(
            request=request, session=session, grant_kind="origin_discovery"
        )
        visited, max_transit, max_hops = require_discovery_scope(
            request, local_gateway_id=self.gateway_id
        )
        del max_transit, max_hops
        path = (*visited, self.gateway_id)
        assert_hop_budget(path)
        remaining = remaining_deadline(deadline_at)
        self._authorize(capability="discovery", origin=origin, session=session)
        matches = await self._local_matches(session_id)
        matches.extend(
            await self._fan_out_discovery(
                session_id=session_id,
                origin=origin,
                path=path,
                remaining=remaining,
            )
        )
        return discovery_response(self.gateway_id, matches)

    async def _serve_relay_as_hub(
        self,
        *,
        request: dict[str, object],
        session: FederationChannelSession,
        deadline_at: float | None,
    ) -> dict[str, object]:
        """hub 中转 ``B → A → C`` 并原路返回 relay envelope。"""

        origin = self._channel_bound_origin(session)
        self._adopt_origin_envelope(
            request=request, session=session, grant_kind="origin_operation"
        )
        operation = require_operation(request)
        target = require_target(request)
        visited, _max_transit, _max_hops = require_discovery_scope(
            request, local_gateway_id=self.gateway_id
        )
        path = (*visited, self.gateway_id, target.gateway_id)
        assert_hop_budget(path)
        if target.gateway_id in visited or target.gateway_id == self.gateway_id:
            raise FederationError(
                FEDERATION_TRANSIT_LIMIT_EXCEEDED,
                "relay 目标已在访问路径中，拒绝回环",
                detail={"path": list(path)},
            )
        self._authorize(capability="transit", origin=origin, session=session)
        principal = principal_ref_for_gateway(origin)
        remaining = remaining_deadline(deadline_at)
        grant = self._sign_grant(
            grant_kind="operation",
            origin_gateway_id=origin,
            audience_gateway_id=target.gateway_id,
            transit_path=path,
            principal_ref=principal,
            lifetime=remaining,
            target=target,
            operation=operation,
        )
        inner = await self.channel_to(target.gateway_id).request(
            method=METHOD_RELAY,
            request={
                "grant": grant.encode(),
                "operation": operation,
                "target": target.to_payload(),
                "visited": list(path[:-1]),
                "max_transit_gateways": MAX_TRANSIT_GATEWAYS,
                "max_gateway_hops": MAX_GATEWAY_HOPS,
            },
            timeout=remaining,
        )
        return {
            "relayed_by": self.gateway_id,
            "origin_gateway_id": origin,
            "transit_path": list(path),
            "target": target.to_payload(),
            "operation": operation,
            "result": inner,
        }

    async def _fan_out_discovery(
        self,
        *,
        session_id: str,
        origin: str,
        path: tuple[str, ...],
        remaining: float,
    ) -> list[dict[str, object]]:
        directory = self.spoke_directory
        if directory is None:
            return []
        principal = principal_ref_for_gateway(origin)
        matches: list[dict[str, object]] = []
        for spoke in directory.active_spokes():
            if spoke.gateway_id in path:
                continue
            channel = self._channels.get(spoke.gateway_id)
            if channel is None or channel.closed:
                raise FederationError(
                    FEDERATION_TARGET_NOT_RESOLVABLE,
                    f"active spoke 缺少可用 channel: {spoke.gateway_id}",
                )
            target_path = (*path, spoke.gateway_id)
            assert_hop_budget(target_path)
            grant = self._sign_grant(
                grant_kind="discovery",
                origin_gateway_id=origin,
                audience_gateway_id=spoke.gateway_id,
                transit_path=target_path,
                principal_ref=principal,
                lifetime=min(
                    max(DISCOVERY_GRANT_LIFETIME_SECONDS, remaining), remaining
                ),
            )
            result = await channel.request(
                method=METHOD_DISCOVERY,
                request={
                    "grant": grant.encode(),
                    "session_id": session_id,
                    "visited": list(path),
                    "max_transit_gateways": MAX_TRANSIT_GATEWAYS,
                    "max_gateway_hops": MAX_GATEWAY_HOPS,
                },
                timeout=remaining,
            )
            raw_matches = result.get("matches")
            if not isinstance(raw_matches, list):
                raise FederationError(
                    FEDERATION_TARGET_NOT_RESOLVABLE,
                    "spoke discovery 响应缺少 matches 数组",
                )
            for item in raw_matches:
                if not isinstance(item, dict):
                    raise FederationError(
                        FEDERATION_TARGET_NOT_RESOLVABLE,
                        "spoke discovery 候选项必须是对象",
                    )
                matches.append(item)
        return matches

    # --- spoke 侧：来自受信 hub 的 granted 请求 ----------------------------

    async def _serve_discovery_as_spoke(
        self,
        *,
        request: dict[str, object],
        session: FederationChannelSession,
        deadline_at: float | None,
    ) -> dict[str, object]:
        """spoke 仅查本地 cold catalog 且不得递归。"""

        grant = self._verify_inbound_grant(
            request=request,
            session=session,
            expected_kind="discovery",
            capability="discovery",
        )
        session_id = require_str(request, "session_id")
        visited, _max_transit, _max_hops = require_discovery_scope(
            request, local_gateway_id=self.gateway_id
        )
        path = (*visited, self.gateway_id)
        assert_hop_budget(path)
        if path != grant.transit_path:
            raise FederationError(
                FEDERATION_TRANSIT_LIMIT_EXCEEDED,
                "discovery 请求声明的路径与 grant 绑定路径不一致",
                detail={"path": list(path), "grant": list(grant.transit_path)},
            )
        _ = deadline_at
        matches = await self._local_matches(session_id)
        return discovery_response(self.gateway_id, matches)

    async def _serve_relay_as_spoke(
        self,
        *,
        request: dict[str, object],
        session: FederationChannelSession,
        deadline_at: float | None,
    ) -> dict[str, object]:
        """target 侧以受信 hub 身份与最新 local policy 重新授权并解析 main thread。"""

        operation = require_operation(request)
        capability: Capability = "reply" if operation == "reply" else operation
        grant = self._verify_inbound_grant(
            request=request,
            session=session,
            expected_kind="operation",
            capability=capability,
        )
        target = require_target(request)
        if grant.target is None or grant.target != target:
            raise FederationError(
                FEDERATION_GRANT_KIND_MISMATCH,
                "grant 绑定的 target 与入站请求不一致",
            )
        if grant.operation != operation:
            raise FederationError(
                FEDERATION_GRANT_KIND_MISMATCH,
                "grant 绑定的 operation 与入站请求不一致",
            )
        visited, _max_transit, _max_hops = require_discovery_scope(
            request, local_gateway_id=self.gateway_id
        )
        path = (*visited, self.gateway_id)
        if path != grant.transit_path:
            raise FederationError(
                FEDERATION_TRANSIT_LIMIT_EXCEEDED,
                "relay 请求声明的路径与 grant 绑定路径不一致",
                detail={"path": list(path), "grant": list(grant.transit_path)},
            )
        _ = deadline_at
        resolved = await self.resolve_target_main_thread(target=target)
        return {
            "accepted_by": self.gateway_id,
            "operation": operation,
            "target": resolved,
            "principal_ref": grant.principal_ref,
        }

    async def resolve_target_main_thread(
        self, *, target: GrantTarget
    ) -> dict[str, object]:
        """从本工作区权威 thread catalog 解析 main pointer，返回稳定地址。

        Gateway 不读取远端 ``.boxteam``、不缓存第二份 main pointer；route hint
        只带短 TTL 且不构成业务事实或授权。
        """

        main_thread_id = await self.session_main.resolve_main_thread(
            gateway_id=self.gateway_id,
            workspace_id=target.workspace_id,
            session_id=target.session_id,
        )
        if not isinstance(main_thread_id, str) or not main_thread_id:
            raise FederationError(
                FEDERATION_SESSION_MAIN_UNAVAILABLE,
                "目标工作区未返回权威 main thread",
            )
        hint = self.control_store.record_route_hint(
            gateway_id=self.gateway_id,
            workspace_id=target.workspace_id,
            session_id=target.session_id,
            resolved_main_thread_id=main_thread_id,
            catalog_revision="resolved-main-thread",
        )
        return {
            "gateway_id": hint.gateway_id,
            "workspace_id": hint.workspace_id,
            "session_id": hint.session_id,
            "resolved_main_thread_id": hint.resolved_main_thread_id,
            "route_expires_at": hint.expires_at.isoformat(),
        }

    # --- 内部工具 ---------------------------------------------------------

    def _channel_bound_origin(self, session: FederationChannelSession) -> str:
        """从 channel 绑定取得真实 origin 并拒绝冒充。"""

        origin = session.peer_gateway_id
        directory = self.spoke_directory
        if directory is not None:
            known = {spoke.gateway_id for spoke in directory.active_spokes()}
            if origin not in known:
                raise FederationError(
                    FEDERATION_UNKNOWN_PEER,
                    "入站请求来自未登记的 spoke",
                    detail={"origin": origin},
                )
        return origin

    def _adopt_origin_envelope(
        self,
        *,
        request: dict[str, object],
        session: FederationChannelSession,
        grant_kind: str,
    ) -> None:
        """hub 对 origin envelope 使用持久 first-use registry（防重放，fail closed）。"""

        nonce = request.get("nonce")
        if not isinstance(nonce, str) or not nonce:
            raise FederationError(
                FEDERATION_MALFORMED_FRAME, "origin envelope 缺少 nonce"
            )
        self.control_store.adopt_grant(
            issuer_gateway_id=session.peer_gateway_id,
            origin_gateway_id=session.peer_gateway_id,
            audience_gateway_id=self.gateway_id,
            grant_kind=grant_kind,
            nonce=nonce,
            preimage=grant_preimage(request),
            expires_at=datetime.now(UTC) + timedelta(
                seconds=GRANT_CLOCK_SKEW_SECONDS + TRANSPORT_REPLAY_MARGIN_SECONDS
            ),
        )

    async def _local_matches(self, session_id: str) -> list[dict[str, object]]:
        workspaces = await self.catalog.local_session_workspaces(session_id)
        return [
            {
                "gateway_id": self.gateway_id,
                "workspace_id": workspace_id,
                "session_id": session_id,
            }
            for workspace_id in workspaces
        ]

    def _authorize(
        self,
        *,
        capability: Capability,
        origin: str,
        session: FederationChannelSession,
    ) -> None:
        snapshot = self.policy_store.snapshot
        if snapshot.hardening_enabled and not snapshot.rules:
            raise FederationError(
                FEDERATION_HARDENING_ACTIVE,
                "hardening 已启用但没有任何显式允许规则，拒绝放行",
                detail={"policy_revision": snapshot.revision},
            )
        if not snapshot.evaluate(
            capability=capability,
            origin_gateway_id=origin,
            principal_ref=principal_ref_for_gateway(origin),
        ):
            raise FederationError(
                FEDERATION_AUTHORIZATION_REVOKED,
                "最新 federation policy 拒绝了该操作",
                detail={
                    "capability": capability,
                    "policy_revision": snapshot.revision,
                },
            )
        _ = session

    def _sign_grant(
        self,
        *,
        grant_kind: Literal["discovery", "operation"],
        origin_gateway_id: str,
        audience_gateway_id: str,
        transit_path: tuple[str, ...],
        principal_ref: str,
        lifetime: float,
        target: GrantTarget | None = None,
        operation: OperationKind | None = None,
    ) -> FederationGrant:
        if lifetime <= 0:
            raise FederationError(FEDERATION_DEADLINE_EXCEEDED, "grant 寿命预算已耗尽")
        return sign_grant(
            private_key=self.signing_key,  # type: ignore[arg-type]
            grant_kind=grant_kind,
            issuer_gateway_id=self.gateway_id,
            origin_gateway_id=origin_gateway_id,
            audience_gateway_id=audience_gateway_id,
            transit_path=transit_path,
            request_id=f"req_{int(time.time() * 1000)}",
            principal_ref=principal_ref,
            lifetime_seconds=lifetime,
            target=target,
            operation=operation,
        )

    def _verify_inbound_grant(
        self,
        *,
        request: dict[str, object],
        session: FederationChannelSession,
        expected_kind: Literal["discovery", "operation"],
        capability: Capability,
    ) -> FederationGrant:
        grant = FederationGrant.decode(request.get("grant"))
        hub_gateway_id = session.peer_gateway_id
        verify_grant(
            grant,
            issuer=session.handshake.peer,
            local_gateway_id=self.gateway_id,
            expected_kind=expected_kind,
            expected_origin=grant.origin_gateway_id,
            expected_path=(grant.origin_gateway_id, hub_gateway_id, self.gateway_id),
        )
        self._authorize(
            capability=capability,
            origin=grant.origin_gateway_id,
            session=session,
        )
        self.control_store.adopt_grant(
            issuer_gateway_id=grant.issuer_gateway_id,
            origin_gateway_id=grant.origin_gateway_id,
            audience_gateway_id=grant.audience_gateway_id,
            grant_kind=grant.grant_kind,
            nonce=grant.nonce,
            preimage=grant_preimage(request),
            expires_at=grant.expires_at,
        )
        return grant


class _FederationRequestDispatcher(FederationRequestHandler):
    def __init__(self, service: FederationRpcService) -> None:
        self._service = service

    async def handle(
        self,
        *,
        method: str,
        request: dict[str, object],
        session: FederationChannelSession,
        deadline_at: float | None,
    ) -> dict[str, object]:
        return await self._service.handle_request(
            method=method,
            request=request,
            session=session,
            deadline_at=deadline_at,
        )


__all__ = [
    "FEDERATION_DEADLINE_SECONDS",
    "FederationCatalogPort",
    "FederationRpcService",
    "FederationSessionMainPort",
    "FederationSpoke",
    "FederationSpokeDirectoryPort",
    "grant_preimage",
    "principal_ref_for_gateway",
]
