"""只在显式测试组合中操作真实任务 worker，不写入或伪造业务状态。"""

from __future__ import annotations

import asyncio
from typing import Annotated, Literal

from fastapi import APIRouter, Depends

# isort: off
from tests.support.e2e_control.backend_app import app
# isort: on

from app.api.deps import (
    get_persistent_task_service,
    verify_local_token,
    workspace_container_scope,
)
from app.services.business.persistent_task_service import PersistentTaskService

router = APIRouter(dependencies=[Depends(workspace_container_scope), Depends(verify_local_token)])


@router.post("/_test/task-runtime/{action}")
async def runtime_action(action: Literal["stop", "start", "cancel"],
                         service: Annotated[PersistentTaskService, Depends(get_persistent_task_service)]):
    runtime = service.runtime
    if action == "stop":
        await runtime.stop()
    elif action == "start":
        await runtime.start()
    else:
        assert runtime._worker is not None
        runtime._worker.cancel()
        await asyncio.gather(runtime._worker, return_exceptions=True)
        await asyncio.sleep(0)
    return service.snapshot()


app.include_router(router, prefix="/api/v1/workspaces/{workspace_id}")
app.include_router(router, prefix="/api/v1")
