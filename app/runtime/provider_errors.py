"""实际模型错误分类时才加载 Provider SDK 的异常类型。"""

from __future__ import annotations

from typing import TypeVar

import httpx

_PROVIDER_FAILURE_ATTRIBUTE = "_boxteam_provider_failure"
_ProviderError = TypeVar("_ProviderError", bound=Exception)


def mark_provider_failure(error: _ProviderError) -> _ProviderError:
    """在不包装异常的前提下记录它来自上游 Provider 边界。"""
    setattr(error, _PROVIDER_FAILURE_ATTRIBUTE, True)
    return error


def is_provider_failure(error: BaseException) -> bool:
    """判断异常是否由 Provider 请求或其原始流迭代直接产生。"""
    return getattr(error, _PROVIDER_FAILURE_ATTRIBUTE, False) is True


def is_transient_provider_error(error: Exception) -> bool:
    current: BaseException | None = error
    seen: set[int] = set()
    sdk_error_types: tuple[type[BaseException], ...] | None = None
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if is_provider_failure(current):
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
