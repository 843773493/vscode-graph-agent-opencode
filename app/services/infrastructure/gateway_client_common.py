"""两个 Gateway 会话 client 共用的传输常量与连接解析（唯一定义点）。"""

from __future__ import annotations

from app.services.infrastructure.config_service import ConfigService

DEFAULT_GATEWAY_URL = "http://127.0.0.1:8014"
DEFAULT_GATEWAY_TIMEOUT_SECONDS = 30

# Gateway 明确以 4xx/5xx 拒绝请求时，属于可归因的调用失败（模型/工作区侧可恢复），
# 调用方按领域错误处理；其余非 2xx 视为传输层异常。两个 client 语义一致。
MODEL_RECOVERABLE_HTTP_STATUSES = frozenset(
    {400, 401, 403, 404, 409, 422, 502, 503, 504}
)


class GatewayTransportConnection:
    """解析 Gateway 连接地址与超时：显式参数优先，其次配置服务，最后内置默认值。

    只承载传输参数解析，不包含请求组装或响应解码（那属于各 client 的垂直链路）。
    """

    def __init__(
        self,
        *,
        gateway_url: str | None = None,
        timeout_seconds: float | None = None,
        config_service: ConfigService | None = None,
    ) -> None:
        self._config_service = config_service
        self._gateway_url_from_config = gateway_url is None and config_service is not None
        self._timeout_from_config = timeout_seconds is None and config_service is not None
        self._gateway_url = (
            gateway_url
            if gateway_url is not None
            else (
                config_service.get_gateway_connection_url()
                if config_service is not None
                else DEFAULT_GATEWAY_URL
            )
        ).rstrip("/")
        self._timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else (
                config_service.get_gateway_connection_timeout_seconds()
                if config_service is not None
                else DEFAULT_GATEWAY_TIMEOUT_SECONDS
            )
        )

    def resolve_gateway_url(self) -> str:
        if self._gateway_url_from_config and self._config_service is not None:
            return self._config_service.get_gateway_connection_url().rstrip("/")
        return self._gateway_url

    def resolve_timeout_seconds(self) -> float:
        if self._timeout_from_config and self._config_service is not None:
            return self._config_service.get_gateway_connection_timeout_seconds()
        return self._timeout_seconds


__all__ = [
    "DEFAULT_GATEWAY_TIMEOUT_SECONDS",
    "DEFAULT_GATEWAY_URL",
    "MODEL_RECOVERABLE_HTTP_STATUSES",
    "GatewayTransportConnection",
]
