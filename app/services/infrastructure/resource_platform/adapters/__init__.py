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
from app.services.infrastructure.resource_platform.adapters.memory_state import (
    AuthoritativeMemorySnapshot,
    MemoryStateAdapter,
    MemoryStateReader,
)

__all__ = [
    "AuthenticatedGatewaySnapshot",
    "AuthoritativeMemorySnapshot",
    "FileMonitorBatch",
    "FileMonitorChange",
    "FileMonitorHandle",
    "FileMonitorKey",
    "FileWatchPort",
    "GatewaySnapshotAdapter",
    "GatewaySnapshotReader",
    "MemoryStateAdapter",
    "MemoryStateReader",
    "SharedFileMonitor",
    "WorkspaceFileWatchPort",
]
