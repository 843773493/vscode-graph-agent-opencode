from __future__ import annotations

import os
from datetime import datetime, timezone

from app.services.infrastructure.config_service import ConfigService
from app.schemas.public_v2.workspace import WorkspaceContextDTO, WorkspaceDTO, WorkspaceIndexRebuildDTO, WorkspaceIndexStatusDTO
from app.core.path_utils import get_runtime_workspace_root, get_user_workspace_root


class WorkspaceService:
    def __init__(self, *, config_service: ConfigService):
        self.workspace_id = "ws_local"
        self.root_path = str(get_runtime_workspace_root())
        self.user_workspace_root = str(get_user_workspace_root())
        self.name = os.path.basename(self.root_path)
        self._config_service = config_service

    async def get(self) -> WorkspaceDTO:
        return WorkspaceDTO(
            workspace_id=self.workspace_id,
            root_path=self.root_path,
            name=self.name,
            project_type="python",
            git={
                "enabled": False,
                "root": self.root_path,
                "branch": "main"
            },
            runtime={
                "pid": os.getpid(),
                "started_at": datetime.now(timezone.utc).isoformat() + "Z"
            }
        )

    async def get_context(self) -> WorkspaceContextDTO:
        return WorkspaceContextDTO(
            workspace_id=self.workspace_id,
            root_path=self.root_path,
            project_type="python",
            languages=["python", "javascript", "typescript"],
            git={},
            index_status={"status": "ready", "indexed_at": datetime.now(timezone.utc).isoformat() + "Z"},
            config={}
        )

    async def get_index_status(self) -> WorkspaceIndexStatusDTO:
        return WorkspaceIndexStatusDTO(
            status="ready",
            indexed_files=0,
            last_updated=datetime.now(timezone.utc).isoformat() + "Z",
        )

    async def rebuild_index(self) -> WorkspaceIndexRebuildDTO:
        return WorkspaceIndexRebuildDTO(
            status="started",
            job_id="index_001",
        )