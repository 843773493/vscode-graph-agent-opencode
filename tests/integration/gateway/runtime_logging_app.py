"""显式测试组合：真实进程 HTTP 与 SQLite CAS 失败，不替换业务状态。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

import app.gateway.lifespan as gateway_lifespan
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.runtime import logging_consumer
from app.gateway.runtime.logging_control import RuntimeLoggingProcessControl
from app.schemas.runtime_logging import (
    RuntimeLoggingApply,
    RuntimeLoggingHttpService,
    RuntimeLoggingStatus,
)
from app.services.infrastructure.config.state import ConfigConflictError
from tests.support.e2e_control.gateway_app import app

__all__ = ["app"]

_failure = os.environ["BOXTEAM_LOGGING_TEST_FAILURE"]
_audit = Path(os.environ["BOXTEAM_LOGGING_TEST_AUDIT"])
if _failure not in {"none", "reject", "cas", "rollback"}:
    raise ValueError(f"未知日志测试故障: {_failure}")


def _record(detail: dict[str, object]) -> None:
    with _audit.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(detail, ensure_ascii=False) + "\n")


class FaultLoggingControl(RuntimeLoggingProcessControl):
    async def apply(
        self, *, base_url: str, service: RuntimeLoggingHttpService, pid: int,
        request: RuntimeLoggingApply,
    ) -> RuntimeLoggingStatus:
        original = request
        if (
            _failure in {"reject", "rollback"} and service == "browser" and request.level == "debug"
            or _failure == "rollback" and service == "backend" and request.level == "info"
        ):
            # 向真实消费者发送过期 CAS，仍由其原认证、校验与响应链明确拒绝。
            request = request.model_copy(update={"expected_revision": "test-stale-revision"})
        try:
            state = await super().apply(base_url=base_url, service=service, pid=pid, request=request)
        except httpx.HTTPStatusError as error:
            _record({"service": service, "request": original.model_dump(),
                     "sent": request.model_dump(), "status": error.response.status_code})
            raise
        _record({"service": service, "request": original.model_dump(),
                 "sent": request.model_dump(), "status": 200, "actual": state.model_dump()})
        return state


class FaultPromotionStore(GatewayStateStore):
    def promote_active_config_snapshot(self, **kwargs):
        if _failure == "cas" and kwargs["payload"]["logging"]["runtime"]["level"] == "debug":
            # 只让提交请求的前置条件过期；真实事务必须拒绝，不能预先改写 active。
            kwargs["expected_active_revision"] += 1
            _record({"stage": "promotion", "expected_active_revision": kwargs["expected_active_revision"]})
        try:
            return super().promote_active_config_snapshot(**kwargs)
        except ConfigConflictError as error:
            _record({"stage": "promotion", "error": str(error)})
            raise


logging_consumer.RuntimeLoggingProcessControl = FaultLoggingControl
gateway_lifespan.GatewayStateStore = FaultPromotionStore
