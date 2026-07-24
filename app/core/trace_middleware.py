import logging
import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)


def get_request_id(request: Request) -> str:
    """读取 TraceMiddleware 为当前 HTTP 请求创建的权威 request_id。"""
    request_id = getattr(request.state, "request_id", None)
    if not isinstance(request_id, str) or not request_id:
        raise RuntimeError("TraceMiddleware 未向当前请求注入 request_id")
    return request_id


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
        response.headers["X-Request-ID"] = request_id
        
        # Calculate execution time
        duration = time.perf_counter() - start_time
        
        # Log request with trace info
        logger.info(
            "[TRACE] method=%s path=%s status=%s duration=%.4fs request_id=%s",
            request.method,
            request.url.path,
            response.status_code,
            duration,
            request_id,
        )
        
        return response
