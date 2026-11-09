"""按实例持有有界本地采集；关闭后释放采样缓冲、计数与定时任务。"""

from __future__ import annotations

import asyncio
import logging
import random
from collections import deque
from collections.abc import Callable, Iterable
from typing import Self

import psutil

from app.schemas.runtime_observation import (
    RuntimeObservationSettings,
    RuntimeObservationStatus,
    RuntimeProcessResources,
    RuntimeRequestSample,
    RuntimeRequestStatistics,
)

logger = logging.getLogger(__name__)


class RuntimeObservation:
    def __init__(self, *, instance_id: str, revision: str, settings: RuntimeObservationSettings,
                 processes: Callable[[], Iterable[tuple[str, int]]], collect_requests: bool = True) -> None:
        self.instance_id = instance_id
        self.revision = revision
        self.settings = settings
        self._processes = processes
        self._collect_requests = collect_requests
        self._samples: deque[RuntimeRequestSample] | None = None
        self._requests: RuntimeRequestStatistics | None = None
        self._resources: list[RuntimeProcessResources] | None = None
        self._task: asyncio.Task[None] | None = None
        self._collection_error: str | None = None

    async def __aenter__(self) -> Self:
        await self.apply(self.settings, self.revision)
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def apply(self, settings: RuntimeObservationSettings, revision: str) -> None:
        # 原配置 owner 串行调用；只在设置变化边界创建或结束采集，不做候选事务或补偿。
        if self._task is not None and (
            not settings.statistics.enabled
            or settings.statistics.collection_interval_ms != self.settings.statistics.collection_interval_ms
        ):
            await self._stop_timer()
        if not settings.telemetry.enabled or not self._collect_requests:
            self._samples = None
        elif self._samples is None or self._samples.maxlen != settings.telemetry.sample_buffer_limit:
            self._samples = deque(self._samples or (), maxlen=settings.telemetry.sample_buffer_limit)
        if not settings.statistics.enabled:
            self._requests = None
            self._resources = None
            self._collection_error = None
        elif self._collect_requests and self._requests is None:
            self._requests = RuntimeRequestStatistics()
        self.settings, self.revision = settings, revision
        if settings.statistics.enabled and self._task is None:
            self._resources = self._collect_resources()
            self._task = asyncio.create_task(self._collect(), name="runtime-observation")

    def record_request(self, *, status_code: int, duration_ms: float, failed: bool, proxy: bool) -> None:
        # 复用 TraceMiddleware 的原计时；关闭时不取随机数、不创建样本或维护计数。
        if self._requests is not None:
            counters = self._requests
            counters.count += 1
            counters.errors += int(failed)
            counters.duration_ms += duration_ms
            if proxy:
                counters.proxy_count += 1
                counters.proxy_errors += int(failed)
                counters.proxy_duration_ms += duration_ms
        if self._samples is not None and self.settings.telemetry.sample_ratio > 0 and (
            self.settings.telemetry.sample_ratio == 1 or random.random() < self.settings.telemetry.sample_ratio
        ):
            self._samples.append(RuntimeRequestSample(
                kind="proxy" if proxy else "http", status_code=status_code, duration_ms=duration_ms, failed=failed,
            ))

    def status(self) -> RuntimeObservationStatus:
        return RuntimeObservationStatus(
            instance_id=self.instance_id, applied_revision=self.revision,
            telemetry=self.settings.telemetry, statistics=self.settings.statistics,
            requests=self._requests.model_copy() if self._requests is not None else None,
            samples=list(self._samples or ()), processes=list(self._resources or ()),
            collection_error=self._collection_error,
            unsupported_metrics=["plugin_internal_stages"] if self._collect_requests else ["http_requests", "plugin_internal_stages"],
        )

    def _collect_resources(self) -> list[RuntimeProcessResources]:
        # 只读取原 owner 已持有的 PID；不扫描进程树、不探活服务或发现新实例。
        result = []
        for service, pid in self._processes():
            try:
                process = psutil.Process(pid)
                with process.oneshot():
                    memory, cpu = process.memory_info(), process.cpu_times()
                result.append(RuntimeProcessResources(
                    service=service, pid=pid, rss_bytes=memory.rss, cpu_seconds=cpu.user + cpu.system,
                ))
            except (psutil.NoSuchProcess, psutil.AccessDenied) as error:
                result.append(RuntimeProcessResources(service=service, pid=pid, error=type(error).__name__))
        return result

    async def _collect(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.settings.statistics.collection_interval_ms / 1000)
                self._resources = self._collect_resources()
        except Exception as error:
            self._collection_error = f"进程资源采集失败: {type(error).__name__}；完整原因见原始服务日志"
            logger.exception("本地运行时资源采集失败")

    async def _stop_timer(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def close(self) -> None:
        await self._stop_timer()
        self._samples = None
        self._requests = None
        self._resources = None
