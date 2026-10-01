"""长期全双工 peer RPC channel 引擎与 hub 拨号客户端。

hub 通过现有 SSH ``-L`` 到 spoke，再经 loopback 转发地址连接
``ws://.../api/gateway/federation/channel``；连接虽由 hub 发起，但 channel 一旦
建立，双方都能在同一 channel 上发起 request/response/event，因此 spoke 可以
反向发起 ``B → A → C`` 并收到原路响应，不需要反向 SSH 或 B/C 直连。

引擎只负责帧收发、correlation、心跳与断线语义；具体方法由
:class:`FederationRequestHandler` 处理。任何畸形帧、未知方法、心跳超时或对端
中途断开都必须显式失败，绝不静默降级或返回虚假默认值。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Literal, Protocol

logger = logging.getLogger(__name__)

from app.gateway.federation.errors import (
    FEDERATION_CHANNEL_CLOSED,
    FEDERATION_CHANNEL_EPOCH_STALE,
    FEDERATION_CHANNEL_HEARTBEAT_TIMEOUT,
    FEDERATION_MALFORMED_FRAME,
    FEDERATION_UNKNOWN_METHOD,
    FederationError,
    close_code_for,
)
from app.gateway.federation.identity import FederationPeerIdentity
from app.gateway.federation.protocol import (
    FRAME_PING,
    FRAME_PONG,
    FRAME_REQUEST,
    FRAME_RESPONSE,
    KNOWN_METHODS,
    METHOD_STATUS,
    FederationFrame,
    build_request_frame,
    build_response_frame,
    new_correlation_id,
    optional_float,
    require_correlation_id,
    require_mapping,
    require_str,
)

# 心跳间隔与超时；超时必须显式关闭 channel 并让 in-flight 请求失败。
HEARTBEAT_INTERVAL_SECONDS = 2.0
HEARTBEAT_TIMEOUT_SECONDS = 8.0
# 单次 RPC 的上界，避免无界挂起；调用方可在业务层叠加更短 deadline。
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0


class FederationTransport(Protocol):
    """channel 所需的传输最小面；FastAPI WebSocket 与拨号客户端各实现一份。"""

    async def send_text(self, message: str) -> None: ...

    async def receive_text(self) -> str | None: ...

    async def close(self, *, code: int, reason: str) -> None: ...


class FederationRequestHandler(Protocol):
    """按方法名处理入站 request；无法处理必须抛 :class:`FederationError`。"""

    async def handle(
        self,
        *,
        method: str,
        request: dict[str, object],
        session: FederationChannelSession,
        deadline_at: float | None,
    ) -> dict[str, object]: ...


def new_channel_instance_id() -> str:
    return f"chan_{secrets.token_hex(12)}"


@dataclass(frozen=True, slots=True)
class ChannelHandshake:
    """channel 建立后冻结的瞬时身份（不进入任何业务键）。"""

    channel_instance_id: str
    channel_epoch: int
    local_gateway_id: str
    peer: FederationPeerIdentity
    #: 本端在 hub/spoke 拓扑中的角色：``hub`` 接受 spoke 的 origin 请求，
    #: ``spoke`` 只接受受信 hub 签发的 grant。
    local_role: Literal["hub", "spoke"] = "spoke"


class FederationChannelSession:
    """一个已建立 channel 的双向 RPC 引擎。"""

    def __init__(
        self,
        *,
        transport: FederationTransport,
        handshake: ChannelHandshake,
        handler: FederationRequestHandler,
        heartbeat_interval_seconds: float = HEARTBEAT_INTERVAL_SECONDS,
        heartbeat_timeout_seconds: float = HEARTBEAT_TIMEOUT_SECONDS,
        request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self.transport = transport
        self.handshake = handshake
        self._handler = handler
        self._heartbeat_interval = heartbeat_interval_seconds
        self._heartbeat_timeout = heartbeat_timeout_seconds
        self._request_timeout = request_timeout_seconds
        self._pending: dict[str, asyncio.Future[dict[str, object]]] = {}
        self._last_pong_at = asyncio.get_event_loop().time()
        self._closing = asyncio.Event()
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed_reason: FederationError | None = None

    @property
    def channel_instance_id(self) -> str:
        return self.handshake.channel_instance_id

    @property
    def channel_epoch(self) -> int:
        return self.handshake.channel_epoch

    @property
    def peer_gateway_id(self) -> str:
        return self.handshake.peer.gateway_id

    @property
    def local_role(self) -> Literal["hub", "spoke"]:
        return self.handshake.local_role

    @property
    def closed(self) -> bool:
        return self._closing.is_set()

    def start(self) -> None:
        if self._tasks:
            raise RuntimeError("channel session 不允许重复启动")
        self._tasks = {
            asyncio.create_task(self._receive_loop(), name="federation-receive"),
            asyncio.create_task(self._heartbeat_loop(), name="federation-heartbeat"),
        }

    async def wait_closed(self) -> FederationError | None:
        await self._closing.wait()
        return self._closed_reason

    async def join(self) -> None:
        """等待收帧与心跳任务结束；供服务端 handler 与测试收口使用。"""

        await self.wait_closed()
        pending = [task for task in self._tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def request(
        self,
        *,
        method: str,
        request: dict[str, object],
        timeout: float | None = None,
        deadline_at: float | None = None,
    ) -> dict[str, object]:
        """在 channel 上发起一次 RPC；失败显式抛出，绝不返回虚假默认值。"""

        if method not in KNOWN_METHODS:
            raise FederationError(FEDERATION_UNKNOWN_METHOD, f"未知联邦方法: {method!r}")
        if self.closed:
            raise FederationError(FEDERATION_CHANNEL_CLOSED, "channel 已关闭，无法发起请求")
        correlation_id = new_correlation_id()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, object]] = loop.create_future()
        self._pending[correlation_id] = future
        effective_timeout = self._request_timeout if timeout is None else timeout
        if deadline_at is not None:
            effective_timeout = min(
                effective_timeout, max(deadline_at - time.time(), 0.0)
            )
        try:
            await self.transport.send_text(
                build_request_frame(
                    correlation_id=correlation_id,
                    method=method,
                    request=request,
                    deadline_at=deadline_at,
                ).encode()
            )
            return await asyncio.wait_for(future, timeout=effective_timeout)
        finally:
            self._pending.pop(correlation_id, None)

    async def close(self, reason: FederationError | None = None) -> None:
        await self._fail_closed(reason or FederationError(
            FEDERATION_CHANNEL_CLOSED, "channel 被本地关闭"
        ), notify_peer=True)

    async def fail(self, error: FederationError) -> None:
        """以给定联邦错误关闭 channel 并失败全部 in-flight 请求。"""

        await self._fail_closed(error, notify_peer=False)

    async def _fail_closed(
        self,
        error: FederationError,
        *,
        notify_peer: bool,
    ) -> None:
        if self._closing.is_set():
            return
        self._closed_reason = error
        self._closing.set()
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()
        try:
            await self.transport.close(
                code=close_code_for(error),
                reason=f"{error.code}: {error.message}"[:120],
            )
        except Exception as close_error:  # noqa: BLE001 - 关闭失败不掩盖原始错误
            logger.warning(
                "联邦 channel 关闭底层传输失败: channel=%s, error=%s",
                self.channel_instance_id,
                close_error,
            )
        del notify_peer

    async def _receive_loop(self) -> None:
        try:
            while True:
                raw = await self.transport.receive_text()
                if raw is None:
                    await self._fail_closed(
                        FederationError(
                            FEDERATION_CHANNEL_CLOSED, "对端中断了 channel"
                        ),
                        notify_peer=False,
                    )
                    return
                try:
                    frame = FederationFrame.decode(raw)
                except FederationError as error:
                    await self._fail_closed(error, notify_peer=False)
                    return
                if frame.frame_type == FRAME_RESPONSE:
                    try:
                        self._dispatch_response(frame)
                    except FederationError as error:
                        await self._fail_closed(error, notify_peer=False)
                        return
                elif frame.frame_type == FRAME_REQUEST:
                    task = asyncio.create_task(self._serve_request(frame))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
                elif frame.frame_type == FRAME_PING:
                    await self.transport.send_text(
                        FederationFrame(frame_type=FRAME_PONG, payload={}).encode()
                    )
                elif frame.frame_type == FRAME_PONG:
                    self._last_pong_at = asyncio.get_event_loop().time()
                else:
                    await self._fail_closed(
                        FederationError(
                            FEDERATION_MALFORMED_FRAME,
                            f"channel 不接受 {frame.frame_type} 帧",
                        ),
                        notify_peer=False,
                    )
                    return
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - 传输异常统一为显式 channel 失败
            await self._fail_closed(
                FederationError(
                    FEDERATION_CHANNEL_CLOSED, f"channel 收帧失败: {error}"
                ),
                notify_peer=False,
            )

    def _dispatch_response(self, frame: FederationFrame) -> None:
        correlation_id = require_correlation_id(frame.payload)
        future = self._pending.get(correlation_id)
        if future is None:
            # 未知 correlation 的 response 是协议错误，但不宽恕为静默忽略：
            # 记录后立即让 channel 失败，避免半开状态掩盖对端错乱。
            raise FederationError(
                FEDERATION_MALFORMED_FRAME,
                f"收到未知 correlation 的 response: {correlation_id}",
            )
        if future.done():
            return
        if frame.payload.get("ok") is True:
            future.set_result(require_mapping(frame.payload, "result"))
            return
        wire = frame.payload.get("error")
        if not isinstance(wire, dict):
            future.set_exception(
                FederationError(FEDERATION_MALFORMED_FRAME, "response 缺少 error 对象")
            )
            return
        future.set_exception(
            FederationError(
                str(wire.get("code") or FEDERATION_MALFORMED_FRAME),
                str(wire.get("message") or "对端返回未命名错误"),
                detail=(
                    wire.get("detail")
                    if isinstance(wire.get("detail"), dict)
                    else None
                ),
            )
        )

    async def _serve_request(self, frame: FederationFrame) -> None:
        correlation_id = require_correlation_id(frame.payload)
        try:
            method = require_str(frame.payload, "method")
            request = require_mapping(frame.payload, "request")
            deadline_at = optional_float(frame.payload, "deadline_at")
            result = await self._handler.handle(
                method=method,
                request=request,
                session=self,
                deadline_at=deadline_at,
            )
            response = build_response_frame(
                correlation_id=correlation_id, result=result
            )
        except FederationError as error:
            response = build_response_frame(
                correlation_id=correlation_id, error=error
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - 业务异常显式回传，不静默
            response = build_response_frame(
                correlation_id=correlation_id,
                error=FederationError(
                    FEDERATION_MALFORMED_FRAME,
                    f"联邦方法处理失败: {type(error).__name__}: {error}",
                ),
            )
        try:
            await self.transport.send_text(response.encode())
        except Exception as send_error:  # noqa: BLE001 - 对端已断开，交给收帧循环收敛
            logger.warning(
                "联邦 channel 回送 response 失败: channel=%s, error=%s",
                self.channel_instance_id,
                send_error,
            )

    async def _heartbeat_loop(self) -> None:
        try:
            while not self._closing.is_set():
                await asyncio.sleep(self._heartbeat_interval)
                if self._closing.is_set():
                    return
                now = asyncio.get_event_loop().time()
                if now - self._last_pong_at > self._heartbeat_timeout:
                    await self._fail_closed(
                        FederationError(
                            FEDERATION_CHANNEL_HEARTBEAT_TIMEOUT,
                            "channel 心跳超时，显式关闭并失败 in-flight 请求",
                        ),
                        notify_peer=False,
                    )
                    return
                await self.transport.send_text(
                    FederationFrame(frame_type=FRAME_PING, payload={}).encode()
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 心跳失败等价于 channel 断开
            await self._fail_closed(
                FederationError(
                    FEDERATION_CHANNEL_CLOSED, "channel 心跳发送失败"
                ),
                notify_peer=False,
            )


def build_hello_payload(
    *,
    local_gateway_id: str,
    public_key_pem: str,
    channel_epoch: int,
) -> dict[str, object]:
    if channel_epoch < 1:
        raise FederationError(FEDERATION_CHANNEL_EPOCH_STALE, "channel epoch 必须为正")
    return {
        "gateway_id": local_gateway_id,
        "public_key_pem": public_key_pem,
        "channel_epoch": channel_epoch,
    }


def parse_hello_payload(payload: dict[str, object]) -> tuple[str, str, int]:
    gateway_id = require_str(payload, "gateway_id")
    public_key_pem = require_str(payload, "public_key_pem")
    raw_epoch = payload.get("channel_epoch")
    if isinstance(raw_epoch, bool) or not isinstance(raw_epoch, int) or raw_epoch < 1:
        raise FederationError(
            FEDERATION_CHANNEL_EPOCH_STALE, "hello 缺少合法的 channel_epoch"
        )
    return gateway_id, public_key_pem, raw_epoch


def default_status_result(*, gateway_id: str, channel_epoch: int) -> dict[str, object]:
    return {
        "method": METHOD_STATUS,
        "gateway_id": gateway_id,
        "channel_epoch": channel_epoch,
    }


__all__ = [
    "DEFAULT_REQUEST_TIMEOUT_SECONDS",
    "HEARTBEAT_INTERVAL_SECONDS",
    "HEARTBEAT_TIMEOUT_SECONDS",
    "ChannelHandshake",
    "FederationChannelSession",
    "FederationRequestHandler",
    "FederationTransport",
    "build_hello_payload",
    "default_status_result",
    "new_channel_instance_id",
    "parse_hello_payload",
]
