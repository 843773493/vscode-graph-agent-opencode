"""插件实例持有的运行时诊断合同与本地采集。"""

from __future__ import annotations

import asyncio
import os
import random
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from typing import Self, cast

import psutil
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from boxteam_plugin_sdk.http import (
    verify_local_control_request,
    verify_runtime_control_token,
)


class TelemetryRuntimeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    enabled: bool = False
    sample_ratio: float = Field(default=1, ge=0, le=1, allow_inf_nan=False)
    sample_buffer_limit: int = Field(default=256, ge=1, le=4096)


class StatisticsRuntimeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    enabled: bool = False
    collection_interval_ms: int = Field(default=5000, ge=1000, le=60000)


class RuntimeObservationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    telemetry: TelemetryRuntimeSettings
    statistics: StatisticsRuntimeSettings

    @classmethod
    def from_gateway_config(cls, payload: dict[str, object]) -> RuntimeObservationSettings:
        return cls.model_validate({name: cast(dict[str, object], payload[name])["runtime"]
                                   for name in ("telemetry", "statistics")})


class RuntimeObservationApplyRequest(RuntimeObservationSettings):
    instance_id: str = Field(min_length=1)
    revision: str = Field(min_length=1)


class RuntimeRequestSample(BaseModel):
    kind: str
    status_code: int
    duration_ms: float
    failed: bool


class RuntimeRequestStatistics(BaseModel):
    count: int = 0
    errors: int = 0
    duration_ms: float = 0
    proxy_count: int = 0
    proxy_errors: int = 0
    proxy_duration_ms: float = 0


class RuntimeProcessResources(BaseModel):
    service: str
    pid: int
    rss_bytes: int | None = None
    cpu_seconds: float | None = None
    error: str | None = None


class RuntimeObservationStatus(BaseModel):
    instance_id: str
    applied_revision: str
    telemetry: TelemetryRuntimeSettings
    statistics: StatisticsRuntimeSettings
    requests: RuntimeRequestStatistics | None
    samples: list[RuntimeRequestSample]
    processes: list[RuntimeProcessResources]
    collection_error: str | None = None
    unsupported_metrics: list[str] = Field(default_factory=lambda: ["plugin_internal_stages"])


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
                kind="proxy" if proxy else "http", status_code=status_code,
                duration_ms=duration_ms, failed=failed,
            ))

    def status(self) -> RuntimeObservationStatus:
        return RuntimeObservationStatus(
            instance_id=self.instance_id, applied_revision=self.revision,
            telemetry=self.settings.telemetry, statistics=self.settings.statistics,
            requests=self._requests.model_copy() if self._requests is not None else None,
            samples=list(self._samples or ()), processes=list(self._resources or ()),
            collection_error=self._collection_error,
            unsupported_metrics=["plugin_internal_stages"] if self._collect_requests else [
                "http_requests", "plugin_internal_stages",
            ],
        )

    def _collect_resources(self) -> list[RuntimeProcessResources]:
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
        import logging

        try:
            while True:
                await asyncio.sleep(self.settings.statistics.collection_interval_ms / 1000)
                self._resources = self._collect_resources()
        except Exception as error:
            logging.getLogger(__name__).exception("本地运行时资源采集失败")
            self._collection_error = f"进程资源采集失败: {type(error).__name__}；完整原因见原始服务日志"

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


def plugin_observation_router(
    *, instance_id: str, service: str,
    verify_local: Callable[..., object] = verify_local_control_request,
    verify_token: Callable[..., object] = verify_runtime_control_token,
) -> APIRouter:
    initial = RuntimeObservationApplyRequest.model_validate_json(os.environ["BOXTEAM_RUNTIME_OBSERVATION"])
    if initial.instance_id != instance_id:
        raise ValueError("插件启动诊断设置不属于原实例")
    observation = RuntimeObservation(
        instance_id=instance_id, revision=initial.revision,
        settings=RuntimeObservationSettings(telemetry=initial.telemetry, statistics=initial.statistics),
        processes=lambda: [(service, os.getpid())], collect_requests=False,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with observation:
            app.state.runtime_observation = observation
            yield

    router = APIRouter(include_in_schema=False, lifespan=lifespan,
        dependencies=[Depends(verify_local), Depends(verify_token)])

    @router.get("/internal/plugin/observation")
    async def status() -> RuntimeObservationStatus:
        return observation.status()

    @router.post("/internal/plugin/observation")
    async def apply(payload: RuntimeObservationApplyRequest) -> RuntimeObservationStatus:
        if payload.instance_id != instance_id:
            raise HTTPException(status_code=409, detail="诊断配置不属于原插件实例")
        await observation.apply(
            RuntimeObservationSettings(telemetry=payload.telemetry, statistics=payload.statistics), payload.revision,
        )
        return observation.status()

    return router


__all__ = [
    "RuntimeObservation", "RuntimeObservationApplyRequest", "RuntimeObservationSettings",
    "RuntimeObservationStatus", "RuntimeProcessResources", "RuntimeRequestSample",
    "RuntimeRequestStatistics", "StatisticsRuntimeSettings", "TelemetryRuntimeSettings",
    "plugin_observation_router",
]
