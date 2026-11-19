"""共享 WebSocket 复用原 API 的流响应，不拥有业务事件和恢复游标。"""

from __future__ import annotations

import asyncio
import logging
import re
import struct
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from starlette.types import Message

from app.core.shielded_cleanup import run_cleanup_shielded
from app.core.trace_middleware import get_request_id
from app.gateway.auth import verify_gateway_token
from app.schemas.gateway.realtime import RealtimeRequest

router = APIRouter()
logger = logging.getLogger(__name__)
# 只登记订阅入口，禁止把普通写操作或任意网络地址送进此通道。
_STREAM_PATHS = {
    "GET": re.compile(r"/api/v1/(?:workspaces/[^/]+/)?(?:session-catalog/(?:events/stream|snapshot/events)|resources/events|persistent-tasks/events|sessions/[^/]+/(?:traces/stream|threads/[^/]+/questions/events|turns/[^/]+/message-stream))"),
    "POST": re.compile(r"(?:/api/v1/(?:workspaces/[^/]+/)?workspace/files/events|/api/gateway/service-plugins/(?:status|commands|configuration-schemas)/events)"),
}
_ROUTING_HEADERS = {"x-boxteam-target-gateway-id", "x-boxteam-workspace-id", "x-boxteam-gateway-connection-id", "last-event-id"}
_WINDOW_BYTES = 1024 * 1024
_CHUNK_BYTES = 64 * 1024


@dataclass
class _Subscription:
    task: asyncio.Task[None] | None = None
    available: int = _WINDOW_BYTES
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    async def reserve(self, size: int) -> None:
        while self.available < size:
            self.changed.clear()
            await self.changed.wait()
        self.available -= size

    def acknowledge(self, size: int) -> None:
        if size > _WINDOW_BYTES - self.available:
            raise ValueError("实时订阅确认超过实际已发送字节数")
        self.available += size
        self.changed.set()


def _request_scope(websocket: WebSocket, command: RealtimeRequest, token: str) -> dict:
    target = urlsplit(command.path or "")
    method = command.method or "GET"
    if target.scheme or target.netloc or target.fragment or not _STREAM_PATHS[method].fullmatch(target.path):
        raise ValueError("未登记的实时订阅入口")
    headers = {key.lower(): value for key, value in command.headers.items()}
    if not headers.keys() <= _ROUTING_HEADERS:
        raise ValueError("实时订阅只接受目标路由与游标请求头")
    headers.update({"x-local-token": token, "accept": "text/event-stream", "content-type": "application/json"})
    # 每个逻辑请求重新经过原 TraceMiddleware，由它生成唯一请求身份。
    return {**websocket.scope, "type": "http", "method": method, "scheme": "https" if websocket.url.scheme == "wss" else "http",
            "path": target.path, "raw_path": target.path.encode(), "query_string": target.query.encode(),
            "headers": [(name.encode(), value.encode("latin-1")) for name, value in headers.items()], "state": {},
            "asgi": {"version": "3.0", "spec_version": "2.3"}}


@router.websocket("/api/gateway/realtime")
async def realtime(websocket: WebSocket) -> None:
    # 凭据由首个控制帧传递，不能进入 URL、浏览器历史或代理访问日志。
    if websocket.headers.get("sec-fetch-site") not in {None, "same-origin", "same-site"}:
        await websocket.close(code=1008, reason="same_site_required")
        return
    await websocket.accept()
    subscriptions: dict[int, _Subscription] = {}
    send_lock = asyncio.Lock()

    async def send(value: dict) -> None:
        async with send_lock:
            await websocket.send_json(value)

    async def stream(identity: int, command: RealtimeRequest, state: _Subscription, token: str) -> None:
        request_id = None
        try:
            scope = _request_scope(websocket, command, token)
            sent_body = False

            async def receive() -> Message:
                nonlocal sent_body
                if not sent_body:
                    sent_body = True
                    return {"type": "http.request", "body": (command.body or "").encode(), "more_body": False}
                await asyncio.Future()
                raise AssertionError("不可达")

            async def forward(message: Message) -> None:
                nonlocal request_id
                if message["type"] == "http.response.start":
                    headers = {key.decode(): value.decode("latin-1") for key, value in message.get("headers", [])}
                    request_id = headers.get("x-request-id")
                    await send({"type": "headers", "subscription_id": identity, "status": message["status"], "headers": headers})
                elif message["type"] == "http.response.body":
                    body = message.get("body", b"")
                    for offset in range(0, len(body), _CHUNK_BYTES):
                        chunk = body[offset:offset + _CHUNK_BYTES]
                        await state.reserve(len(chunk))
                        async with send_lock:
                            await websocket.send_bytes(struct.pack("!I", identity) + chunk)
                        await asyncio.sleep(0)

            await websocket.app(scope, receive, forward)
            await send({"type": "end", "subscription_id": identity})
        except Exception as error:
            logger.exception("实时订阅失败: subscription=%s request_id=%s", identity, request_id)
            await send({"type": "error", "subscription_id": identity, "message": f"实时订阅失败: {type(error).__name__}", "request_id": request_id})

    try:
        async with asyncio.timeout(15):
            auth = RealtimeRequest.model_validate(await websocket.receive_json())
        if auth.type != "auth":
            raise ValueError("实时连接首帧必须认证")
        token = verify_gateway_token(auth.token)
        await send({"type": "ready", "request_id": get_request_id(websocket)})
        while True:
            command = RealtimeRequest.model_validate(await websocket.receive_json())
            if command.subscription_id is None:
                raise ValueError("实时订阅缺少 subscription_id")
            identity = command.subscription_id
            if command.type == "subscribe":
                if identity in subscriptions:
                    raise ValueError("实时订阅 ID 重复")
                state = _Subscription()
                subscriptions[identity] = state
                state.task = asyncio.create_task(stream(identity, command, state, token))
                # task 可能尚未开始就被取消，不能只依赖 stream 的 finally 回收登记。
                state.task.add_done_callback(lambda _task, identity=identity: subscriptions.pop(identity, None))
            elif command.type == "unsubscribe":
                state = subscriptions.get(identity)
                if state is not None and state.task is not None:
                    state.task.cancel()
                    # 原 task 完成后才释放登记；不能阻塞其它订阅的控制帧。
            elif command.type == "ack":
                state = subscriptions.get(identity)
                if state is not None:
                    if command.consumed_bytes is None:
                        raise ValueError("实时订阅确认缺少 consumed_bytes")
                    state.acknowledge(command.consumed_bytes)
            else:
                raise ValueError("实时连接不能重复认证")
    except WebSocketDisconnect:
        pass
    except (HTTPException, ValidationError, ValueError, TimeoutError) as error:
        await send({"type": "error", "status": error.status_code if isinstance(error, HTTPException) else 400,
                    "message": str(error), "request_id": get_request_id(websocket)})
        await websocket.close(code=1008, reason="realtime_protocol_error")
    finally:
        tasks = [state.task for state in subscriptions.values() if state.task is not None]
        for task in tasks:
            task.cancel()
        async def settle() -> None:
            await asyncio.gather(*tasks, return_exceptions=True)
        await run_cleanup_shielded(settle)
