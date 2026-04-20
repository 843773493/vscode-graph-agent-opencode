import time
import logging
import uuid
from typing import Callable, Awaitable
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)


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
        # Generate unique request ID
        request_id = str(uuid.uuid4())
        start_time = time.perf_counter()
        
        # Attach trace info to request state
        request.state.request_id = request_id
        request.state.start_time = start_time
        
        # Add request ID to response headers
        response: Response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        
        # Calculate execution time
        duration = time.perf_counter() - start_time
        
        # Log request with trace info
        logger.info(
            f"[TRACE] method={request.method} path={request.url.path} "
            f"status={response.status_code} duration={duration:.4f}s request_id={request_id}"
        )
        
        return response
