import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar, Token

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

# Gateway 身份头：由 Gateway 侧按请求注入（见「统一虚拟资源寻址」change 的
# requirement「gateway scope 的 scope_id 由 Gateway 身份文件按请求注入推导」）。
# 与 ``X-Request-ID`` 共用 TraceMiddleware 这一套请求级上下文，不新建第二套机制。
GATEWAY_ID_HEADER = "X-BoxTeam-Gateway-Id"

# 当前请求绑定的 gateway_id；请求未携带时保持 None，由消费方 fail-closed。
# 与 request_id 一样，生命周期完全由 TraceMiddleware.dispatch 承担（请求作用域内）。
_CURRENT_GATEWAY_ID: ContextVar[str | None] = ContextVar(
    "boxteam_current_gateway_id",
    default=None,
)


def get_request_id(request: Request) -> str:
    """读取 TraceMiddleware 为当前 HTTP 请求创建的权威 request_id。"""
    request_id = getattr(request.state, "request_id", None)
    if not isinstance(request_id, str) or not request_id:
        raise RuntimeError("TraceMiddleware 未向当前请求注入 request_id")
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
    return _CURRENT_GATEWAY_ID.get()


def require_current_gateway_id(consumer: str) -> str:
    """读取当前作用域的 gateway_id；缺失或非法即 fail-closed 显式拒绝。

    MUST NOT 回退 ``local`` 或任何虚假默认值（AGENTS.md「永不返回虚假的默认值」）。
    """
    gateway_id = _CURRENT_GATEWAY_ID.get()
    if not isinstance(gateway_id, str) or not gateway_id:
        raise RuntimeError(
            f"{consumer} 需要真实的 gateway_id，但当前作用域未绑定：请求未携带 "
            f"{GATEWAY_ID_HEADER} 头，且创建方未显式传递 gateway 身份"
        )
    return gateway_id


class TraceMiddleware(BaseHTTPMiddleware):
    """
    Middleware for request tracing and performance logging.
    
    Adds:
    - Unique request ID for tracing across services
    - Request execution timing
    - Structured logging for all requests
    - Trace headers propagation
    """
    
    async def dispatch(
        self, 
        request: Request, 
        call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming_request_id = request.headers.get("X-Request-ID", "").strip()
        request_id = incoming_request_id or str(uuid.uuid4())
        start_time = time.perf_counter()

        incoming_gateway_id = request.headers.get(GATEWAY_ID_HEADER, "").strip()
        gateway_id_token = set_current_gateway_id(incoming_gateway_id or None)

        # Attach trace info to request state
        request.state.request_id = request_id
        request.state.start_time = start_time

        # Add request ID to response headers
        try:
            response: Response = await call_next(request)
        except Exception as error:
            duration = time.perf_counter() - start_time
            logger.exception(
                "[TRACE] method=%s path=%s status=500 duration=%.4fs request_id=%s",
                request.method,
                request.url.path,
                duration,
                request_id,
            )
            return JSONResponse(
                status_code=500,
                headers={"X-Request-ID": request_id},
                content={
                    "code": 500,
                    "message": f"{type(error).__name__}: {error}",
                    "data": None,
                    "request_id": request_id,
                },
            )
        finally:
            reset_current_gateway_id(gateway_id_token)
        response.headers["X-Request-ID"] = request_id
        
        # Calculate execution time
        duration = time.perf_counter() - start_time
        
        # Log request with trace info
        # 成功请求数量远大于故障请求；放在 DEBUG，避免冷启动/SSE 轮询把服务日志无限放大。
        logger.debug(
            "[TRACE] method=%s path=%s status=%s duration=%.4fs request_id=%s",
            request.method,
            request.url.path,
            response.status_code,
            duration,
            request_id,
        )
        
        return response
