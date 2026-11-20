"""只在测试组合中缩短周期，并在原维护 worker 的存储入口设置屏障。"""

from __future__ import annotations

from dataclasses import replace
from threading import Event, get_ident
from typing import Annotated, Literal

from fastapi import Depends

from app import workspace_lifecycle
from app.api.deps import get_workspace_activity_service, verify_local_token
from app.main import app
from app.services.infrastructure.workspace_state_store import WorkspaceActivityService

_start_maintenance = workspace_lifecycle.start_maintenance
_entered, _release, _finished = Event(), Event(), Event()
_worker_thread: int | None = None
_removed: int | None = None


def start_maintenance(tasks, **kwargs) -> None:
    # 仍由正式 Workspace 生命周期装配及持有，只有测试进程使用短周期。
    _start_maintenance([
        replace(task, interval_ms=100) if task.id == "workspace-activity-prune" else task
        for task in tasks
    ], **kwargs)


workspace_lifecycle.start_maintenance = start_maintenance


@app.post("/api/v1/__tests/maintenance-barrier", include_in_schema=False)
async def barrier(
    action: Literal["arm", "state", "release"],
    _: Annotated[str, Depends(verify_local_token)],
    service: Annotated[WorkspaceActivityService, Depends(get_workspace_activity_service)],
) -> dict[str, bool | int | None]:
    if action == "arm":
        original = service.store.prune_activity

        def prune(*, retention_days: int = 30) -> int:
            global _worker_thread, _removed
            _worker_thread = get_ident()
            _entered.set()
            try:
                if not _release.wait(20):
                    raise TimeoutError("测试未释放原周期维护 worker")
                _removed = original(retention_days=retention_days)
                return _removed
            finally:
                service.store.prune_activity = original
                _finished.set()

        service.store.prune_activity = prune
    elif action == "release":
        _release.set()
    return {
        "entered": _entered.is_set(), "finished": _finished.is_set(),
        "worker_thread": _worker_thread, "event_loop_thread": get_ident(), "removed": _removed,
    }
