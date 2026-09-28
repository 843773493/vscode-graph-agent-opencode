"""resource platform 内置适配器的固定装配出口。"""

from app.services.infrastructure.resource_platform.adapters.file_monitor import (
    FileMonitorBatch,
    FileMonitorChange,
    FileMonitorHandle,
    FileMonitorKey,
    FileWatchPort,
    SharedFileMonitor,
    WorkspaceFileWatchPort,
)
from app.services.infrastructure.resource_platform.adapters.gateway_snapshot import (
    AuthenticatedGatewaySnapshot,
    GatewaySnapshotAdapter,
    GatewaySnapshotReader,
)
__all__ = [
    "AuthenticatedGatewaySnapshot",
    "FileMonitorBatch",
    "FileMonitorChange",
    "FileMonitorHandle",
    "FileMonitorKey",
    "FileWatchPort",
    "GatewaySnapshotAdapter",
    "GatewaySnapshotReader",
    "SharedFileMonitor",
    "WorkspaceFileWatchPort",
]
