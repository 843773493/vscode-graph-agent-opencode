"""静态装配的进程内周期维护任务。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

from boxteam_plugin_sdk.lifecycle import LifetimeScope

_MAX_INTERVAL_MS = 2_147_483_647


@dataclass(frozen=True, slots=True)
class MaintenanceTask:
    id: str
    interval_ms: int
    run_immediately: bool
    run: Callable[[], Awaitable[object]]


MaintenanceErrorReporter = Callable[[str, object, float, Exception], None]


def _validate_tasks(tasks: Iterable[MaintenanceTask]) -> tuple[MaintenanceTask, ...]:
    selected = tuple(tasks)
    if not selected:
        raise ValueError("维护任务集合不能为空")
    seen: set[str] = set()
    for task in selected:
        if not isinstance(task, MaintenanceTask):
            raise TypeError("维护任务必须是 MaintenanceTask")
        if not isinstance(task.id, str) or not task.id.strip() or task.id in seen:
            raise ValueError(f"维护任务标识为空或重复: {task.id!r}")
        seen.add(task.id)
        if isinstance(task.interval_ms, bool) or not isinstance(task.interval_ms, int):
            raise TypeError(f"维护任务周期必须是整数毫秒: {task.id}")
        if not 1 <= task.interval_ms <= _MAX_INTERVAL_MS:
            raise ValueError(f"维护任务周期超出支持范围: {task.id}")
        if not isinstance(task.run_immediately, bool):
            raise TypeError(f"维护任务首次运行策略必须是布尔值: {task.id}")
        if not callable(task.run):
            raise TypeError(f"维护任务回调不可调用: {task.id}")
    return selected


def start_maintenance(
    tasks: Iterable[MaintenanceTask],
    *,
    scope: LifetimeScope,
    identity: object,
    report_error: MaintenanceErrorReporter,
) -> tuple[asyncio.Future[object], ...]:
    """由原 scope 持有维护循环；按注册顺序返回原任务，供 owner 观察存活状态。"""
    selected = _validate_tasks(tasks)
    if not isinstance(scope, LifetimeScope) or scope.state != "open":
        raise RuntimeError("维护任务需要可用的开放 LifetimeScope")
    if any(label.startswith("maintenance:") for label in scope._tasks.values()):
        raise RuntimeError("同一 LifetimeScope 不允许重复装配维护任务")
    if not callable(report_error):
        raise TypeError("维护错误报告入口不可调用")
    asyncio.get_running_loop()

    handles: list[asyncio.Future[object]] = []
    for task in selected:
        async def run_periodically(item: MaintenanceTask = task) -> None:
            interval = item.interval_ms / 1000
            if not item.run_immediately:
                await asyncio.sleep(interval)
            while True:
                if scope.state != "open":
                    return
                started = time.monotonic()
                try:
                    await item.run()
                except Exception as error:
                    if scope.state != "open":
                        raise
                    elapsed_ms = (time.monotonic() - started) * 1000
                    report_error(item.id, identity, elapsed_ms, error)
                if scope.state != "open":
                    return
                await asyncio.sleep(interval)

        handles.append(scope.start_task(run_periodically, label=f"maintenance:{task.id}"))
    return tuple(handles)

__all__ = ["MaintenanceErrorReporter", "MaintenanceTask", "start_maintenance"]
