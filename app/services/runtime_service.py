from __future__ import annotations

import os
import time
import asyncio
from datetime import datetime


class RuntimeService:
    start_time = time.time()

    async def status(self) -> dict:
        return {
            "pid": os.getpid(),
            "uptime_seconds": int(time.time() - self.start_time),
            "workspace_id": "ws_local",
            "active_jobs": 0,
            "loaded_agents": ["planner", "executor", "reviewer", "summarizer"],
            "storage": {
                "root": "./workspace",
                "artifact_dir": "./workspace/artifacts",
                "log_dir": "./workspace/logs",
                "cache_dir": "./workspace/cache"
            }
        }

    async def shutdown(self) -> dict:
        asyncio.get_event_loop().call_later(1, asyncio.get_event_loop().stop)
        return {
            "status": "shutdown_scheduled",
            "delay_seconds": 1
        }
