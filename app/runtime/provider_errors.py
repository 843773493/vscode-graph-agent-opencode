"""实际模型错误分类时才加载 Provider SDK 的异常类型。"""

from __future__ import annotations

import httpx

from app.core import failures


def is_transient_provider_error(error: Exception) -> bool:
    current: BaseException | None = error
    seen: set[int] = set()
    sdk_error_types: tuple[type[BaseException], ...] | None = None
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if failures.is_provider_failure(current):
            if isinstance(
                current,
                (
                    ConnectionError,
                    TimeoutError,
                    httpx.TransportError,
                    httpx.TimeoutException,
                ),
            ):
                return True
            status_code = getattr(current, "status_code", None)
            if isinstance(status_code, int) and (
                status_code in {408, 429} or 500 <= status_code <= 599
            ):
                return True
            if sdk_error_types is None:
                from app.runtime.litellm_runtime import litellm

                sdk_error_types = (
                    litellm.APIConnectionError,
                    litellm.BadGatewayError,
                    litellm.InternalServerError,
                    litellm.RateLimitError,
                    litellm.ServiceUnavailableError,
                    litellm.Timeout,
                )
            if isinstance(current, sdk_error_types):
                return True
        current = current.__cause__
    return False
