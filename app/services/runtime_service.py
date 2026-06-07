from __future__ import annotations

import os
import time
import asyncio
from datetime import datetime
from typing import Optional
from app.core.path_utils import get_workspace_root, get_artifacts_dir, get_logs_dir, get_cache_dir
from app.schemas.public_v2.runtime import RuntimeInfoDTO, RuntimeShutdownResultDTO, RuntimeStorageDTO


class RuntimeService:
    _start_time = None

    def __init__(self, *, job_service):
        self._job_service = job_service

    def get_log_dir(self):
        return get_workspace_root() / ".boxteam" / "logs"

    async def status(self) -> RuntimeInfoDTO:
        if self._start_time is None:
            self._start_time = time.time()
        
        return RuntimeInfoDTO(
            pid=os.getpid(),
            uptime_seconds=int(time.time() - self._start_time),
            workspace_id="ws_local",
            active_jobs=0,
            loaded_agents=["planner", "executor", "reviewer", "summarizer"],
            storage=RuntimeStorageDTO(
                root=str(get_workspace_root()),
                artifact_dir=str(get_artifacts_dir()),
                log_dir=str(get_logs_dir()),
                cache_dir=str(get_cache_dir()),
            ),
        )

    async def shutdown(self) -> RuntimeShutdownResultDTO:
        asyncio.get_event_loop().call_later(1, asyncio.get_event_loop().stop)
        return RuntimeShutdownResultDTO(status="shutdown_scheduled", delay_seconds=1)
