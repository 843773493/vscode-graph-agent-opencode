"""Gateway 全局 config event outbox 与 consumer/relay 投递账本垂直链路。"""

from app.gateway.control.gateway_config_events.gateway_config_events import (
    GatewayConfigEventMixin,
)

__all__ = ["GatewayConfigEventMixin"]
