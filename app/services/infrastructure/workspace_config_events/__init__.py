"""Workspace config event outbox 与 consumer/relay 投递账本垂直链路。"""

from app.services.infrastructure.workspace_config_events.workspace_config_events import (
    WorkspaceConfigEventMixin,
)

__all__ = ["WorkspaceConfigEventMixin"]
