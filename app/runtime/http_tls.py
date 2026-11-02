"""原生辅助服务复用 HTTPX 的默认信任上下文，避免每次请求重载证书。"""

from __future__ import annotations

from functools import cache
from ssl import SSLContext


@cache
def auxiliary_service_tls_context() -> SSLContext:
    import httpx

    return httpx.create_ssl_context(trust_env=False)
