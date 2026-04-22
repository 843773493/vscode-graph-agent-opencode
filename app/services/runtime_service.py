from __future__ import annotations

import os
import time
import asyncio
from datetime import datetime
from typing import Optional
from app.core.path_utils import get_workspace_root, get_artifacts_dir, get_logs_dir, get_cache_dir


class RuntimeService:
    _instance: Optional["RuntimeService"] = None
    _start_time = None

    def __init__(self):
        pass

    @classmethod
    def get_instance(cls) -> "RuntimeService":
        if cls._instance is None:
            cls._instance = RuntimeService()
        return cls._instance

    async def status(self) -> dict:
        if self._start_time is None:
            self._start_time = time.time()
        
        return {
            "pid": os.getpid(),
            "uptime_seconds": int(time.time() - self._start_time),
            "workspace_id": "ws_local",
            "active_jobs": 0,
            "loaded_agents": ["planner", "executor", "reviewer", "summarizer"],
            "storage": {
                "root": str(get_workspace_root()),
                "artifact_dir": str(get_artifacts_dir()),
                "log_dir": str(get_logs_dir()),
                "cache_dir": str(get_cache_dir())
            }
        }

    async def shutdown(self) -> dict:
        asyncio.get_event_loop().call_later(1, asyncio.get_event_loop().stop)
        return {
            "status": "shutdown_scheduled",
            "delay_seconds": 1
        }
