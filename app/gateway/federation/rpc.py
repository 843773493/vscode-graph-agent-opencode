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

import asyncio
import hashlib
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

from app.core.trace_middleware import (
    require_current_request_id,
    reset_current_gateway_id,
    reset_current_request_id,
    set_current_gateway_id,
    set_current_request_id,
)
from app.gateway.credentials import FederationCredentialStore
from app.gateway.federation.channel import (
    FederationChannelSession,
    FederationRequestHandler,
)
from app.gateway.federation.errors import (
    FEDERATION_AUTHORIZATION_REVOKED,
    FEDERATION_CAPABILITY_DENIED,
    FEDERATION_CHANNEL_CLOSED,
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
    ContextGrantTarget,
    FederationGrant,
    ForkGrantTarget,
    GrantTarget,
    OperationKind,
    ResourceNavigationGrantTarget,
    sign_grant,
    validate_grant_operation,
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
    METHOD_SKILL_SOURCES,
    METHOD_STATUS,
    require_str,
)
from app.gateway.federation.skill_sources import (
    GatewaySkillSourcePort,
    serve_gateway_skill_sources,
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
    credential_store: FederationCredentialStore
    spoke_directory: FederationSpokeDirectoryPort | None = None
    gateway_skill_sources: GatewaySkillSourcePort | None = None
    _channels: dict[str, FederationChannelSession] = field(default_factory=dict)
    _local_channels: dict[str, FederationChannelSession] = field(default_factory=dict)
    _hub_channel: FederationChannelSession | None = None

    # --- channel 生命周期 -------------------------------------------------

    @property
    def local_role(self) -> Literal["hub", "spoke"]:
        """本地在 hub/spoke 拓扑中的角色：有 spoke 目录即 hub。"""

        return "hub" if self.spoke_directory is not None and self.spoke_directory.active_spokes() else "spoke"

    async def register_channel(self, session: FederationChannelSession) -> None:
        if session.local_role == "spoke" and self._hub_channel is not None and not self._hub_channel.closed and self._hub_channel.peer_gateway_id != session.peer_gateway_id:
            raise FederationError(FEDERATION_UNKNOWN_PEER, "spoke 只能接入一个受信 hub")
        await self._register_channel(session, direction="inbound")

    async def _register_channel(self, session: FederationChannelSession, *, direction: str) -> None:
        previous = self._hub_channel if session.local_role == "spoke" else self._channels.get(session.peer_gateway_id)
        self.control_store.register_channel(
            channel_instance_id=session.channel_instance_id,
            peer_gateway_id=session.peer_gateway_id,
            connection_id=session.handshake.peer.connection_id,
            channel_epoch=session.channel_epoch,
            direction=direction,
        )
        self._local_channels[session.channel_instance_id] = session
        if session.local_role == "spoke":
            self._hub_channel = session
        else:
            self._channels[session.peer_gateway_id] = session
        if previous is not None and previous.channel_instance_id != session.channel_instance_id:
            await previous.close(FederationError(FEDERATION_AUTHORIZATION_REVOKED, "channel 已被新的 epoch 替代"))
            await previous.join()
            self.unregister_channel(previous.channel_instance_id)

    def unregister_channel(self, channel_instance_id: str) -> None:
        self.control_store.unregister_channel(
            channel_instance_id=channel_instance_id
        )
        self.clear_hub_channel(channel_instance_id)
        self._local_channels.pop(channel_instance_id, None)
        for gateway_id, session in list(self._channels.items()):
            if session.channel_instance_id == channel_instance_id:
                self._channels.pop(gateway_id, None)

    async def adopt_outbound_channel(self, session: FederationChannelSession) -> None:
        """hub 记录到 spoke 的 outbound channel（拨号成功后调用）。"""

        await self._register_channel(session, direction="outbound")

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

    def channel_to(self, gateway_id: str, *, qualified: bool = False) -> FederationChannelSession:
        session = self._channels.get(gateway_id)
        if qualified and self.spoke_directory is not None and gateway_id not in {spoke.gateway_id for spoke in self.spoke_directory.active_spokes()}:
            raise FederationError(FEDERATION_UNKNOWN_PEER, "authority 指向未登记的直接 spoke")
        if session is None or session.closed:
            raise FederationError(
                FEDERATION_CHANNEL_CLOSED if qualified else FEDERATION_TARGET_NOT_RESOLVABLE,
                f"目标 Gateway 当前没有可用 channel: {gateway_id}",
            )
        self._check_channel(session)
        return session

    async def close_channels(self) -> None:
        sessions = tuple(self._local_channels.values())
        first_error: BaseException | None = None
        cancellation_error: asyncio.CancelledError | None = None
        released_sessions: list[FederationChannelSession] = []

        def retain_error(error: BaseException, action: str) -> None:
            nonlocal first_error
            if first_error is None:
                first_error = error
            elif error is not first_error:
                first_error.add_note(
                    f"{action}：{type(error).__name__}: {error}"
                )

        def retain_cancellation(error: asyncio.CancelledError) -> None:
            nonlocal cancellation_error
            if cancellation_error is None:
                cancellation_error = error
            elif error is not cancellation_error:
                cancellation_error.add_note(
                    f"清理期间再次收到取消：{type(error).__name__}: {error}"
                )

        async def await_batch(
            batch: asyncio.Future[list[None | BaseException]],
        ) -> list[None | BaseException]:
            while True:
                try:
                    return await asyncio.shield(batch)
                except asyncio.CancelledError as error:
                    retain_cancellation(error)

        close_tasks = [asyncio.create_task(session.close()) for session in sessions]
        close_batch = asyncio.gather(*close_tasks, return_exceptions=True)
        close_results = await await_batch(close_batch)
        for error in close_results:
            if isinstance(error, BaseException):
                retain_error(error, "关闭其他联邦 channel 时也失败")

        join_tasks = [asyncio.create_task(session.join()) for session in sessions]
        join_batch = asyncio.gather(*join_tasks, return_exceptions=True)
        join_results = await await_batch(join_batch)
        for session, result in zip(sessions, join_results, strict=True):
            if isinstance(result, BaseException):
                retain_error(result, "等待其他联邦 channel 结束时也失败")
            else:
                released_sessions.append(session)

        unregister_error: Exception | None = None
        for session in released_sessions:
            try:
                self.unregister_channel(session.channel_instance_id)
            except Exception as error:  # noqa: BLE001 - 注销失败后仍须尝试其余 channel
                if unregister_error is None:
                    unregister_error = error
                else:
                    unregister_error.add_note(
                        "注销其他联邦 channel 时也失败："
                        f"{type(error).__name__}: {error}"
                    )
        if unregister_error is not None:
            if first_error is None:
                first_error = unregister_error
            else:
                first_error.add_note(
                    "注销已关闭的联邦 channel 时也失败："
                    f"{type(unregister_error).__name__}: {unregister_error}"
                )
        if cancellation_error is not None:
            if first_error is not None and first_error is not cancellation_error:
                cancellation_error.add_note(
                    "外层取消前后的 channel 清理也失败："
                    f"{type(first_error).__name__}: {first_error}"
                )
                for note in getattr(first_error, "__notes__", ()):
                    if note not in getattr(cancellation_error, "__notes__", ()):
                        cancellation_error.add_note(note)
            raise cancellation_error
        if first_error is not None:
            raise first_error

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
        if method == METHOD_SKILL_SOURCES:
            return await serve_gateway_skill_sources(
                self, request, session=session, deadline_at=deadline_at,
            )
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
        self, *, session_id: str, workspace_id: str | None = None,
        gateway_id: str | None = None, deadline_at: float | None = None,
    ) -> dict[str, object]:
        """有界 exact-ID discovery：本地查询 + （必要时）经唯一 hub 的一次 fan-out。

        返回唯一命中或 ``target_not_resolvable``/``target_ambiguous``；
        命中只含稳定身份（gateway/workspace/session），不含 locator。
        """

        require_str({"session_id": session_id}, "session_id")
        if gateway_id is not None:
            require_str({"gateway_id": gateway_id}, "gateway_id")
        if deadline_at is None:
            deadline_at = time.time() + FEDERATION_DEADLINE_SECONDS
        remaining_deadline(deadline_at)
        self._authorize(capability="discovery", origin=self.gateway_id, session=None)
        visited: tuple[str, ...] = (self.gateway_id,)
        matches = await self._local_matches(session_id, workspace_id=workspace_id) if gateway_id in {None, self.gateway_id} else []
        if gateway_id == self.gateway_id:
            pass
        elif self.local_role == "hub":
            # 本地即 hub：直接向其它 active spoke fan-out 一次。
            remaining = remaining_deadline(deadline_at)
            matches.extend(
                await self._fan_out_discovery(
                    session_id=session_id,
                    workspace_id=workspace_id,
                    gateway_id=gateway_id,
                    origin=self.gateway_id,
                    origin_channel=None,
                    path=visited,
                    remaining=remaining,
                    deadline_at=deadline_at,
                )
            )
        elif self._hub_channel is not None and not self._hub_channel.closed:
            remaining = remaining_deadline(deadline_at)
            channel = self._hub_channel
            self._check_channel(channel)
            result = await channel.request(
                method=METHOD_DISCOVERY,
                request={
                    "session_id": session_id,
                    "workspace_id": workspace_id,
                    "gateway_id": gateway_id,
                    "request_id": require_current_request_id("联邦 discovery"),
                    "nonce": new_origin_nonce(),
                    "visited": list(visited),
                    "max_transit_gateways": MAX_TRANSIT_GATEWAYS,
                    "max_gateway_hops": MAX_GATEWAY_HOPS,
                },
                timeout=remaining,
                deadline_at=deadline_at,
            )
            self._check_channel(channel)
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
        elif gateway_id is not None:
            code = FEDERATION_CHANNEL_CLOSED if self.credential_store.list_all() else FEDERATION_UNKNOWN_PEER
            raise FederationError(code, "当前 Gateway 没有可用的唯一 hub channel")
        if gateway_id is not None:
            matches = [item for item in matches if item.get("gateway_id") == gateway_id]
        if workspace_id is not None:
            matches = [item for item in matches if item.get("workspace_id") == workspace_id]
        self._authorize(capability="discovery", origin=self.gateway_id, session=None)
        if not matches:
            raise FederationError(
                FEDERATION_TARGET_NOT_RESOLVABLE,
                f"目标 Session 在指定 Gateway/workspace 范围内不可解析: {session_id}",
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
        target: GrantTarget | ContextGrantTarget | ForkGrantTarget | ResourceNavigationGrantTarget,
        operation: OperationKind,
        body: dict[str, object],
        deadline_at: float | None = None,
    ) -> dict[str, object]:
        """把一个 operation 经唯一 hub transit 送到目标并取回 target envelope。

        本地即 hub 时直接向目标 spoke 签发 grant；本地为 spoke 时把请求交给唯一
        hub，由 hub 从 channel 绑定确认 origin 后转发。仅允许一次 transit。
        """

        validate_grant_operation(target, operation)
        if deadline_at is None:
            deadline_at = time.time() + FEDERATION_DEADLINE_SECONDS
        remaining = remaining_deadline(deadline_at)
        self._authorize(capability=operation, origin=self.gateway_id, session=None)
        if target.gateway_id == self.gateway_id:
            return await self._execute_local(target=target, operation=operation, body=body, origin=self.gateway_id, remaining=remaining)
        if self.local_role == "hub":
            assert_hop_budget((self.gateway_id, target.gateway_id))
            forwarded = {
                "body": body, "request_id": require_current_request_id("联邦 relay"),
                "operation": operation, "target": target.to_payload(), "visited": [self.gateway_id],
                "max_transit_gateways": MAX_TRANSIT_GATEWAYS, "max_gateway_hops": MAX_GATEWAY_HOPS,
            }
            grant = self._sign_grant(
                grant_kind="operation",
                origin_gateway_id=self.gateway_id,
                audience_gateway_id=target.gateway_id,
                transit_path=(self.gateway_id, target.gateway_id),
                principal_ref=principal_ref_for_gateway(self.gateway_id),
                lifetime=remaining,
                target=target,
                operation=operation,
                request_preimage=forwarded,
            )
            channel = self.channel_to(target.gateway_id, qualified=isinstance(target, (ContextGrantTarget, ForkGrantTarget, ResourceNavigationGrantTarget)))
            result = await channel.request(
                method=METHOD_RELAY,
                request={**forwarded, "grant": grant.encode()},
                timeout=remaining,
                deadline_at=deadline_at,
            )
            self._check_channel(channel)
            self._authorize(capability=operation, origin=self.gateway_id, session=None)
            return result
        hub_channel = self._hub_channel
        if hub_channel is None or hub_channel.closed:
            raise FederationError(
                FEDERATION_CHANNEL_CLOSED if isinstance(target, (ContextGrantTarget, ForkGrantTarget, ResourceNavigationGrantTarget)) else FEDERATION_TARGET_NOT_RESOLVABLE,
                "本地没有可用 hub channel，无法 transit 到目标 Gateway",
            )
        self._check_channel(hub_channel)
        result = await hub_channel.request(
            method=METHOD_RELAY,
            request={
                "nonce": new_origin_nonce(),
                "body": body,
                "request_id": require_current_request_id("联邦 relay"),
                "operation": operation,
                "target": target.to_payload(),
                "visited": [self.gateway_id],
                "max_transit_gateways": MAX_TRANSIT_GATEWAYS,
                "max_gateway_hops": MAX_GATEWAY_HOPS,
            },
            timeout=remaining,
            deadline_at=deadline_at,
        )
        self._check_channel(hub_channel)
        self._authorize(capability=operation, origin=self.gateway_id, session=None)
        return result

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
        if visited != (origin,):
            raise FederationError(FEDERATION_TRANSIT_LIMIT_EXCEEDED, "origin discovery visited 与认证 channel 不一致")
        path = (*visited, self.gateway_id)
        assert_hop_budget(path)
        remaining = remaining_deadline(deadline_at)
        self._authorize(capability="discovery", origin=origin, session=session)
        gateway_id = request.get("gateway_id")
        workspace_id = request.get("workspace_id")
        if gateway_id is not None:
            gateway_id = require_str(request, "gateway_id")
        if workspace_id is not None:
            workspace_id = require_str(request, "workspace_id")
        # 直接查询 hub 本地候选不经过中转；继续向 spoke 查询才需要 transit。
        if gateway_id != self.gateway_id:
            self._authorize(capability="transit", origin=origin, session=session)
        matches = await self._local_matches(session_id, workspace_id=workspace_id) if gateway_id in {None, self.gateway_id} else []
        if gateway_id != self.gateway_id:
            matches.extend(
                await self._fan_out_discovery(
                    session_id=session_id,
                    workspace_id=workspace_id,
                    gateway_id=gateway_id,
                    origin=origin,
                    origin_channel=session,
                    path=path,
                    remaining=remaining,
                    deadline_at=deadline_at,
                )
            )
        self._authorize(capability="discovery", origin=origin, session=session)
        if gateway_id != self.gateway_id:
            self._authorize(capability="transit", origin=origin, session=session)
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
        validate_grant_operation(target, operation)
        visited, _max_transit, _max_hops = require_discovery_scope(
            request, local_gateway_id=self.gateway_id
        )
        if visited != (origin,):
            raise FederationError(FEDERATION_TRANSIT_LIMIT_EXCEEDED, "origin relay visited 与认证 channel 不一致")
        path = (*visited, self.gateway_id) if target.gateway_id == self.gateway_id else (*visited, self.gateway_id, target.gateway_id)
        assert_hop_budget(path)
        if target.gateway_id in visited:
            raise FederationError(FEDERATION_TRANSIT_LIMIT_EXCEEDED, "relay 目标造成回环")
        self._authorize(capability="transit", origin=origin, session=session)
        self._authorize(capability=operation, origin=origin, session=session)
        if target.gateway_id == self.gateway_id:
            result = await self._execute_local(target=target, operation=operation, body=request.get("body"), origin=origin, remaining=remaining_deadline(deadline_at))
            self._authorize(capability="transit", origin=origin, session=session)
            self._authorize(capability=operation, origin=origin, session=session)
            return result
        principal = principal_ref_for_gateway(origin)
        remaining = remaining_deadline(deadline_at)
        forwarded = {
            "body": request.get("body"), "request_id": require_current_request_id("联邦 relay"),
            "operation": operation, "target": target.to_payload(), "visited": list(path[:-1]),
            "max_transit_gateways": MAX_TRANSIT_GATEWAYS, "max_gateway_hops": MAX_GATEWAY_HOPS,
        }
        grant = self._sign_grant(
            grant_kind="operation",
            origin_gateway_id=origin,
            audience_gateway_id=target.gateway_id,
            transit_path=path,
            principal_ref=principal,
            lifetime=remaining,
            target=target,
            operation=operation,
            request_preimage=forwarded,
        )
        channel = self.channel_to(target.gateway_id, qualified=isinstance(target, (ContextGrantTarget, ForkGrantTarget, ResourceNavigationGrantTarget)))
        inner = await channel.request(
            method=METHOD_RELAY,
            request={**forwarded, "grant": grant.encode()},
            timeout=remaining,
            deadline_at=deadline_at,
        )
        self._check_channel(channel)
        self._authorize(capability="transit", origin=origin, session=session)
        self._authorize(capability=operation, origin=origin, session=session)
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
        workspace_id: str | None,
        gateway_id: str | None,
        origin: str,
        origin_channel: FederationChannelSession | None,
        path: tuple[str, ...],
        remaining: float,
        deadline_at: float | None,
    ) -> list[dict[str, object]]:
        directory = self.spoke_directory
        spokes = () if directory is None else directory.active_spokes()
        if gateway_id is not None:
            spokes = tuple(spoke for spoke in spokes if spoke.gateway_id == gateway_id)
            if not spokes:
                raise FederationError(FEDERATION_UNKNOWN_PEER, "authority 指向未登记的直接 spoke")
        principal = principal_ref_for_gateway(origin)
        matches: list[dict[str, object]] = []
        for spoke in spokes:
            if spoke.gateway_id in path:
                if gateway_id is not None:
                    raise FederationError(FEDERATION_TRANSIT_LIMIT_EXCEEDED, "discovery authority 指向已访问 Gateway")
                continue
            self._authorize(capability="discovery", origin=origin, session=origin_channel)
            if origin != self.gateway_id:
                self._authorize(capability="transit", origin=origin, session=origin_channel)
            remaining = remaining_deadline(deadline_at)
            channel = self.channel_to(spoke.gateway_id, qualified=gateway_id is not None)
            target_path = (*path, spoke.gateway_id)
            assert_hop_budget(target_path)
            forwarded = {
                "request_id": require_current_request_id("联邦 discovery"), "session_id": session_id,
                "workspace_id": workspace_id, "gateway_id": gateway_id,
                "visited": list(path), "max_transit_gateways": MAX_TRANSIT_GATEWAYS,
                "max_gateway_hops": MAX_GATEWAY_HOPS,
            }
            grant = self._sign_grant(
                grant_kind="discovery",
                origin_gateway_id=origin,
                audience_gateway_id=spoke.gateway_id,
                transit_path=target_path,
                principal_ref=principal,
                lifetime=min(DISCOVERY_GRANT_LIFETIME_SECONDS, remaining),
                request_preimage=forwarded,
            )
            result = await channel.request(
                method=METHOD_DISCOVERY,
                request={**forwarded, "grant": grant.encode()},
                timeout=remaining,
                deadline_at=deadline_at,
            )
            self._check_channel(channel)
            self._authorize(capability="discovery", origin=origin, session=origin_channel)
            if origin != self.gateway_id:
                self._authorize(capability="transit", origin=origin, session=origin_channel)
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
        remaining_deadline(deadline_at)
        gateway_id = request.get("gateway_id")
        if gateway_id is not None and gateway_id != self.gateway_id:
            raise FederationError(FEDERATION_UNKNOWN_PEER, "spoke 不得解析其它 Gateway authority")
        workspace_id = request.get("workspace_id")
        if workspace_id is not None:
            workspace_id = require_str(request, "workspace_id")
        matches = await self._local_matches(session_id, workspace_id=workspace_id)
        self._verify_grant_current(grant, request, session)
        self._authorize(capability="discovery", origin=grant.origin_gateway_id, session=session)
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
        validate_grant_operation(target, operation)
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
        if operation == "wait":
            from app.schemas.internal_v2.communication import (
                CommunicationObservationRequest,
            )
            body = CommunicationObservationRequest.model_validate(request.get("body"))
            wait_deadline = datetime.fromisoformat(body.query["deadline"]["deadline_at_utc"])
            effective = max((wait_deadline - datetime.now(UTC)).total_seconds(), 0)
            self._verify_grant_current(grant, request, session, minimum_remaining_seconds=effective + GRANT_CLOCK_SKEW_SECONDS)
        result = await self._execute_local(target=target, operation=operation, body=request.get("body"), origin=grant.origin_gateway_id, remaining=remaining_deadline(deadline_at))
        self._verify_grant_current(grant, request, session)
        self._authorize(capability=capability, origin=grant.origin_gateway_id, session=session)
        return result

    async def _execute_local(self, *, target: GrantTarget | ContextGrantTarget | ForkGrantTarget | ResourceNavigationGrantTarget, operation: OperationKind, body: dict[str, object], origin: str, remaining: float) -> dict[str, object]:
        if target.gateway_id != self.gateway_id:
            raise FederationError(FEDERATION_TARGET_NOT_RESOLVABLE, "target audience 不是本 Gateway")
        self._authorize(capability=operation, origin=origin, session=None)
        if not isinstance(body, dict):
            raise FederationError(FEDERATION_MALFORMED_FRAME, "relay 必须包含完整业务 envelope")
        validate_grant_operation(target, operation)
        if isinstance(target, ForkGrantTarget):
            from app.schemas.internal_v2.session_fork_transfer import (
                SessionForkTransferRequest,
            )

            transfer = SessionForkTransferRequest.model_validate(body)
            if transfer.fork_target != target or transfer.source_gateway_id != origin:
                raise FederationError(FEDERATION_MALFORMED_FRAME, "fork operation 的 URI/role/origin 与独立 grant 不一致")
            frozen_target = target.to_payload()
        elif isinstance(target, ResourceNavigationGrantTarget):
            from app.schemas.internal_v2.communication import (
                ResourceNavigationRelayRequest,
            )

            query = ResourceNavigationRelayRequest.model_validate(body)
            if query.navigation_target != target or query.source_gateway_id != origin:
                raise FederationError(FEDERATION_MALFORMED_FRAME, "导航 URI/origin 与 read grant 不一致")
            frozen_target = target.to_payload()
        elif isinstance(target, ContextGrantTarget):
            from app.schemas.internal_v2.communication import ContextQueryRequest
            if operation != "read":
                raise FederationError(FEDERATION_GRANT_KIND_MISMATCH, "资源目标只接受 read capability")
            query = ContextQueryRequest.model_validate(body)
            if query.resource_target != target or query.source_gateway_id != origin:
                raise FederationError(FEDERATION_MALFORMED_FRAME, "上下文查询资源或 origin 与 read grant 不一致")
            frozen_target = target.to_payload()
        elif operation in {"read", "wait"}:
            from app.schemas.internal_v2.communication import (
                CommunicationObservationRequest,
            )
            observed = CommunicationObservationRequest.model_validate(body)
            if (observed.target.gateway_id, observed.target.workspace_id, observed.target.session_id, observed.operation) != (target.gateway_id, target.workspace_id, target.session_id, operation):
                raise FederationError(FEDERATION_MALFORMED_FRAME, "冻结 observation target 与 grant 不一致")
            frozen_target = observed.model_dump(mode="json")["target"]
        else:
            resolved = await self.resolve_target_main_thread(target=target)
            frozen_target = {"gateway_id": self.gateway_id, "workspace_id": target.workspace_id, "session_id": target.session_id, "thread_id": resolved["resolved_main_thread_id"]}
        self._authorize(capability=operation, origin=origin, session=None)
        gateway_token = set_current_gateway_id(self.gateway_id)
        try:
            result = await self.session_main.execute(operation=operation, target=frozen_target, body=body, origin=origin, timeout=remaining)
        finally:
            reset_current_gateway_id(gateway_token)
        self._authorize(capability=operation, origin=origin, session=None)
        return result

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

    async def _local_matches(self, session_id: str, *, workspace_id: str | None = None) -> list[dict[str, object]]:
        workspaces = await self.catalog.local_session_workspaces(session_id)
        matches = []
        for candidate in workspaces:
            if workspace_id is not None and candidate != workspace_id:
                continue
            main = await self.session_main.resolve_main_thread(gateway_id=self.gateway_id, workspace_id=candidate, session_id=session_id)
            matches.append({"gateway_id": self.gateway_id, "workspace_id": candidate, "session_id": session_id, "thread_id": main, "main_thread_id": main})
        return matches

    def _authorize(
        self,
        *,
        capability: Capability,
        origin: str,
        session: FederationChannelSession | None,
    ) -> None:
        if session is not None:
            self._check_channel(session)
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
                detail={"capability": capability, "policy_revision": snapshot.revision},
            )

    def _check_channel(self, session: FederationChannelSession) -> None:
        if session.closed or self._local_channels.get(session.channel_instance_id) is not session:
            raise FederationError(FEDERATION_AUTHORIZATION_REVOKED, "channel 未登记或已关闭")
        try:
            credential = self.credential_store.get(session.handshake.peer.connection_id)
        except (LookupError, PermissionError) as error:
            raise FederationError(FEDERATION_AUTHORIZATION_REVOKED, f"channel 的当前凭据不可用: {error}") from error
        expected_peer = self.gateway_id if session.local_role == "hub" else session.peer_gateway_id
        if credential.peer_gateway_id != expected_peer or not secrets.compare_digest(hashlib.sha256(credential.token.encode()).hexdigest(), session.handshake.credential_digest):
            raise FederationError(FEDERATION_AUTHORIZATION_REVOKED, "channel 凭据已变更或不属于当前 peer")
        current = self._hub_channel if session.local_role == "spoke" else self._channels.get(session.peer_gateway_id)
        if current is not session:
            raise FederationError(FEDERATION_AUTHORIZATION_REVOKED, "旧 channel 已被新的 epoch 替代")

    def _verify_grant_current(self, grant: FederationGrant, request: dict[str, object], session: FederationChannelSession, *, minimum_remaining_seconds: float = 0.0) -> None:
        verify_grant(
            grant, issuer=session.handshake.peer, local_gateway_id=self.gateway_id,
            expected_kind=grant.grant_kind, request_preimage=grant_preimage(request),
            expected_origin=grant.origin_gateway_id,
            expected_path=((session.peer_gateway_id, self.gateway_id) if grant.origin_gateway_id == session.peer_gateway_id else (grant.origin_gateway_id, session.peer_gateway_id, self.gateway_id)),
            minimum_remaining_seconds=minimum_remaining_seconds,
        )

    def _sign_grant(
        self,
        *,
        grant_kind: Literal["discovery", "operation"],
        origin_gateway_id: str,
        audience_gateway_id: str,
        transit_path: tuple[str, ...],
        principal_ref: str,
        lifetime: float,
        request_preimage: dict[str, object],
        target: GrantTarget | ContextGrantTarget | ForkGrantTarget | ResourceNavigationGrantTarget | None = None,
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
            request_id=require_current_request_id("grant 签发"),
            request_preimage=request_preimage,
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
            request_preimage=grant_preimage(request),
            expected_origin=grant.origin_gateway_id,
            expected_path=((hub_gateway_id, self.gateway_id) if grant.origin_gateway_id == hub_gateway_id else (grant.origin_gateway_id, hub_gateway_id, self.gateway_id)),
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
        request_id = require_str(request, "request_id") if method != METHOD_STATUS else None
        request_token = set_current_request_id(request_id)
        gateway_token = set_current_gateway_id(self._service.gateway_id)
        try:
            return await self._service.handle_request(method=method, request=request, session=session, deadline_at=deadline_at)
        finally:
            reset_current_gateway_id(gateway_token)
            reset_current_request_id(request_token)



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
