import logging
import time
import uuid
from contextvars import ContextVar, Token

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.identifier import validate_gateway_id

logger = logging.getLogger(__name__)

# Gateway 身份头：由 Gateway 侧按请求注入（见「统一资源寻址」change 的
# requirement「gateway scope 的 scope_id 由 Gateway 身份文件按请求注入推导」）。
# 与 ``X-Request-ID`` 共用 TraceMiddleware 这一套请求级上下文，不新建第二套机制。
GATEWAY_ID_HEADER = "X-BoxTeam-Gateway-Id"
NAVIGATION_ACTOR_HEADER = "X-BoxTeam-Navigation-Actor"

# 当前请求绑定的 gateway_id；请求未携带时保持 None，由消费方 fail-closed。
# 与 request_id 一样，生命周期由 TraceMiddleware 在请求作用域内承担。
_CURRENT_GATEWAY_ID: ContextVar[str | None] = ContextVar(
    "boxteam_current_gateway_id",
    default=None,
)
_CURRENT_NAVIGATION_ACTOR: ContextVar[str | None] = ContextVar(
    "boxteam_current_navigation_actor",
    default=None,
)


def get_request_id(request: Request) -> str:
    """读取 TraceMiddleware 为当前 HTTP 请求创建的权威 request_id。"""
    request_id = getattr(request.state, "request_id", None)
    if not isinstance(request_id, str) or not request_id:
        raise RuntimeError("TraceMiddleware 未向当前请求注入 request_id")
    return request_id


# 当前作用域绑定的请求级 request_id；与 gateway_id 共用同一套请求级上下文，
# 不新建第二套机制。独立执行根（例如后台 job）必须由创建方显式随任务传递，
# MUST NOT 依赖任何进程级「当前 request」单例，也不得在 job 内部补造第二个 ID。
_CURRENT_REQUEST_ID: ContextVar[str | None] = ContextVar(
    "boxteam_current_request_id",
    default=None,
)


def set_current_request_id(request_id: str | None) -> Token[str | None]:
    """在当前执行作用域内绑定请求级 request_id；返回 token 用于恢复。"""
    return _CURRENT_REQUEST_ID.set(request_id)


def reset_current_request_id(token: Token[str | None]) -> None:
    """恢复先前的 request_id 绑定。"""
    _CURRENT_REQUEST_ID.reset(token)


def get_current_request_id() -> str | None:
    """读取当前作用域绑定的 request_id；未绑定时返回 None。"""
    return _CURRENT_REQUEST_ID.get()


def require_current_request_id(consumer: str) -> str:
    """读取当前作用域的 request_id；缺失或非法即 fail-closed 显式拒绝。

    MUST NOT 回退随机 UUID 或任何虚假默认值（AGENTS.md「永不返回虚假的默认值」
    与「任何一层不得补造第二个请求 ID」）。
    """
    request_id = _CURRENT_REQUEST_ID.get()
    if not isinstance(request_id, str) or not request_id:
        raise RuntimeError(
            f"{consumer} 需要创建请求的权威 request_id，但当前作用域未绑定："
            "调用方必须在请求作用域内传入（或从持久化的 Job 记录携带）"
        )
    return request_id


def set_current_gateway_id(gateway_id: str | None) -> Token[str | None]:
    """在当前执行作用域内绑定 gateway_id；返回 token 用于恢复。"""
    return _CURRENT_GATEWAY_ID.set(gateway_id)


def reset_current_gateway_id(token: Token[str | None]) -> None:
    """恢复先前的 gateway_id 绑定。"""
    _CURRENT_GATEWAY_ID.reset(token)


def get_current_gateway_id() -> str | None:
    """读取当前作用域绑定的 gateway_id；未绑定时返回 None。

    请求作用域由 TraceMiddleware 写入；请求级之外（例如独立执行根的后台任务）
    必须由创建方显式随任务传递，MUST NOT 依赖任何进程级「当前 gateway」单例。
    """
    gateway_id = _CURRENT_GATEWAY_ID.get()
    return validate_gateway_id(gateway_id) if gateway_id is not None else None


def require_current_gateway_id(consumer: str) -> str:
    """读取当前作用域的 gateway_id；缺失或非法即 fail-closed 显式拒绝。

    MUST NOT 回退 ``local`` 或任何虚假默认值（AGENTS.md「永不返回虚假的默认值」）。
    """
    gateway_id = get_current_gateway_id()
    if not isinstance(gateway_id, str) or not gateway_id:
        raise RuntimeError(
            f"{consumer} 需要真实的 gateway_id，但当前作用域未绑定：请求未携带 "
            f"{GATEWAY_ID_HEADER} 头，且创建方未显式传递 gateway 身份"
        )
    return gateway_id


def set_current_navigation_actor(actor: str | None) -> Token[str | None]:
    """在当前请求作用域绑定 Gateway 认证后的 navigation actor。"""
    return _CURRENT_NAVIGATION_ACTOR.set(actor)


def reset_current_navigation_actor(token: Token[str | None]) -> None:
    """恢复先前的 navigation actor 绑定。"""
    _CURRENT_NAVIGATION_ACTOR.reset(token)


def get_current_navigation_actor() -> str | None:
    """读取当前请求作用域中的 navigation actor；未绑定时返回 None。"""
    return _CURRENT_NAVIGATION_ACTOR.get()


def require_current_navigation_actor(consumer: str) -> str:
    """读取 Gateway 已认证的 navigation actor，缺失时显式失败。"""
    actor = _CURRENT_NAVIGATION_ACTOR.get()
    if not isinstance(actor, str) or not actor:
        raise RuntimeError(
            f"{consumer} 需要 Gateway 已认证的 navigation actor，但当前作用域未绑定："
            f"请求未携带 {NAVIGATION_ACTOR_HEADER} 头，且创建方未显式传递身份"
        )
    return actor


class TraceMiddleware:
    """
    Middleware for request tracing and performance logging.
    
    Adds:
    - Unique request ID for tracing across services
    - Request execution timing
    - Structured logging for all requests
    - Trace headers propagation
    """
    
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        incoming_request_id = request.headers.get("X-Request-ID", "").strip()
        request_id = incoming_request_id or str(uuid.uuid4())
        start_time = time.perf_counter()

        incoming_gateway_id = request.headers.get(GATEWAY_ID_HEADER, "").strip()
        gateway_id_token = set_current_gateway_id(incoming_gateway_id or None)
        incoming_navigation_actor = request.headers.get(
            NAVIGATION_ACTOR_HEADER, ""
        ).strip()
        navigation_actor_token = set_current_navigation_actor(
            incoming_navigation_actor or None
        )

        # Attach trace info to request state
        request.state.request_id = request_id
        request.state.start_time = start_time
        # 同时绑定到请求级 ContextVar：后台 job 创建点在请求作用域内读取该值，
        # 显式随 job 携带，避免下游补造第二个 request ID。
        request_id_token = set_current_request_id(request_id)

        response_started = False
        response_status = 500

        async def traced_send(message: Message) -> None:
            nonlocal response_started, response_status
            if message["type"] == "http.response.start":
                response_started = True
                response_status = int(message["status"])
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != b"x-request-id"
                ]
                headers.append((b"x-request-id", request_id.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            # 直接调用 ASGI 入口，保留取消、已开始响应后的错误和流式响应原有语义。
            await self.app(scope, receive, traced_send)
            if not response_started:
                raise RuntimeError("No response returned.")
        except Exception as error:
            if response_started:
                logger.exception(
                    "[TRACE] method=%s path=%s status=%s duration=%.4fs request_id=%s response_started=true",
                    request.method,
                    request.url.path,
                    response_status,
                    time.perf_counter() - start_time,
                    request_id,
                )
                raise
            duration = time.perf_counter() - start_time
            logger.exception(
                "[TRACE] method=%s path=%s status=500 duration=%.4fs request_id=%s",
                request.method,
                request.url.path,
                duration,
                request_id,
            )
            message = f"{type(error).__name__}: {error}"
            for note in getattr(error, "__notes__", ()):
                message = f"{message}\n{note}"
            response = JSONResponse(
                status_code=500,
                headers={"X-Request-ID": request_id},
                content={
                    "code": 500,
                    "message": message,
                    "data": None,
                    "request_id": request_id,
                },
            )
            await response(scope, receive, traced_send)
            return
        finally:
            reset_current_request_id(request_id_token)
            reset_current_navigation_actor(navigation_actor_token)
            reset_current_gateway_id(gateway_id_token)

        logger.debug(
            "[TRACE] method=%s path=%s status=%s duration=%.4fs request_id=%s",
            request.method,
            request.url.path,
            response_status,
            time.perf_counter() - start_time,
            request_id,
        )
