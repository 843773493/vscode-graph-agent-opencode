"""插件复用原 HTTP 服务和本机认证，持有自身采集生命周期。"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, HTTPException

from app.core.runtime_observation import RuntimeObservation
from app.schemas.runtime_observation import (
    RuntimeObservationApplyRequest,
    RuntimeObservationSettings,
    RuntimeObservationStatus,
)


def plugin_observation_router(
    *, instance_id: str, service: str,
    verify_local: Callable[..., object], verify_token: Callable[..., object],
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
            raise HTTPException(409, "诊断配置不属于原插件实例")
        await observation.apply(
            RuntimeObservationSettings(telemetry=payload.telemetry, statistics=payload.statistics), payload.revision,
        )
        return observation.status()

    return router
