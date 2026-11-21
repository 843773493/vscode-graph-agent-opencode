"""测试组合只暴露原后端 owner 的一次清理，不替换存储或制造竞争。"""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import Depends

from app.api.deps import get_workspace_activity_service, verify_local_token
from app.core.shielded_cleanup import run_sync_shielded
from app.main import app
from app.services.infrastructure.workspace_state_store import WorkspaceActivityService


@app.post("/api/v1/__tests/maintenance-prune", include_in_schema=False)
async def prune(
    _: Annotated[str, Depends(verify_local_token)],
    service: Annotated[WorkspaceActivityService, Depends(get_workspace_activity_service)],
) -> dict[str, float | int]:
    def collect() -> dict[str, float | int]:
        started = time.perf_counter()
        count = service.prune()
        return {"started": started, "ended": time.perf_counter(), "deleted": count}

    return await run_sync_shielded(collect)
