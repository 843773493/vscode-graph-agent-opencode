"""Runtime 生命周期守卫的状态冲突契约测试。

``RuntimeService`` 用裸 ``RuntimeError`` 表达「当前生命周期状态不允许该动作」。
这类客户端可触发的状态冲突若在适配层漏接，会被 TraceMiddleware 统一转成 500，
并把 ``RuntimeError:`` 类名写进响应体。本文件冻结三个守卫统一落 409 且不泄漏
内部类名的行为。

按 tests/unit/api 规范：直接调用公开处理函数断言错误映射，并用进程内
TestClient 验证真实 HTTP 封套；不启动真实后端进程，文件系统用 pytest 临时目录。
本测试使用真实 ``RuntimeService``（不注入 fake 服务），只改其可达状态。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.deps import get_runtime_service
from app.api.runtime import (
    begin_runtime_drain,
    cancel_runtime_drain,
    force_runtime_drain,
    router,
)
from app.core.background_task_registry import BackgroundTaskRegistry
from app.core.path_utils import get_session_path_resolver
from app.core.trace_middleware import TraceMiddleware
from app.services.business.job.service import JobService
from app.services.infrastructure.background_task_history_store import (
    BackgroundTaskHistoryStore,
)
from app.services.infrastructure.message_stream_store import MessageStreamStore
from app.services.infrastructure.runtime_service import RuntimeService
from app.services.infrastructure.trace_event_store import TraceEventStore


class _NeverFinishExecutor:
    async def run(self, _state: object) -> str:
        await asyncio.Future()
        raise AssertionError("不可达")


class _RecordingJobEventBus:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    async def publish(self, **event: object) -> None:
        self.events.append(event)


def _build_runtime(tmp_path: Path, session_lifecycle_guard) -> RuntimeService:
    """真实 RuntimeService；只替换 executor/bus 这些外部边界。"""
    sessions_dir = tmp_path / ".boxteam" / "sessions"
    jobs = JobService(
        job_event_bus=_RecordingJobEventBus(),
        job_executor=_NeverFinishExecutor(),
        session_lifecycle_guard=session_lifecycle_guard,
    )
    return RuntimeService(
        workspace_id="00000000-0000-4000-8000-000000000001",
        workspace_root=tmp_path,
        job_service=jobs,
        background_task_registry=BackgroundTaskRegistry(
            history_store=BackgroundTaskHistoryStore(sessions_dir=sessions_dir)
        ),
        trace_event_store=TraceEventStore(sessions_dir=sessions_dir),
        message_stream_store=MessageStreamStore(
            path_resolver=get_session_path_resolver(sessions_dir)
        ),
    )


@pytest.mark.asyncio
async def test_cancel_drain_before_draining_is_conflict_not_500(
    tmp_path: Path, session_lifecycle_guard
) -> None:
    runtime = _build_runtime(tmp_path, session_lifecycle_guard=session_lifecycle_guard)

    with pytest.raises(HTTPException) as raised:
        await cancel_runtime_drain(
            _="local",
            request_id="req_conflict",
            runtime_service=runtime,
        )

    assert raised.value.status_code == 409
    assert raised.value.status_code != 500
    assert isinstance(raised.value.detail, str)
    assert "RuntimeError" not in raised.value.detail
    assert raised.value.detail == "只有 draining 状态可以取消排空，当前状态: ready"


@pytest.mark.asyncio
async def test_force_interrupt_before_draining_is_conflict_not_500(
    tmp_path: Path,
    session_lifecycle_guard,
) -> None:
    runtime = _build_runtime(tmp_path, session_lifecycle_guard=session_lifecycle_guard)

    with pytest.raises(HTTPException) as raised:
        await force_runtime_drain(
            _="local",
            request_id="req_conflict",
            runtime_service=runtime,
        )

    assert raised.value.status_code == 409
    assert raised.value.status_code != 500
    assert isinstance(raised.value.detail, str)
    assert "RuntimeError" not in raised.value.detail
    assert raised.value.detail == "强制中断前必须先进入 draining，当前状态: ready"


@pytest.mark.asyncio
async def test_begin_drain_after_stopping_is_conflict_not_500(
    tmp_path: Path, session_lifecycle_guard
) -> None:
    runtime = _build_runtime(tmp_path, session_lifecycle_guard=session_lifecycle_guard)
    await runtime.begin_drain()
    await runtime.force_interrupt()

    with pytest.raises(HTTPException) as raised:
        await begin_runtime_drain(
            _="local",
            request_id="req_conflict",
            runtime_service=runtime,
        )

    assert raised.value.status_code == 409
    assert raised.value.status_code != 500
    assert isinstance(raised.value.detail, str)
    assert "RuntimeError" not in raised.value.detail
    assert raised.value.detail == "Workspace API 已进入 stopping，不能重新开始 drain"


def test_drain_guards_http_envelope_is_409_without_internal_class_name(
    tmp_path: Path,
    session_lifecycle_guard,
) -> None:
    runtime = _build_runtime(tmp_path, session_lifecycle_guard=session_lifecycle_guard)
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_runtime_service] = lambda: runtime
    try:
        with TestClient(app) as client:
            # 未认证仍先落 401，证明该入口经真实依赖链。
            unauthorized = client.post("/api/v1/runtime/drain/force")
            assert unauthorized.status_code == 401
            # 认证后：ready 状态下先 cancel_drain 是状态冲突。
            response = client.post(
                "/api/v1/runtime/drain/cancel",
                headers={"X-Local-Token": "local-dev-token"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    assert response.status_code != 500
    body = response.json()
    assert body["detail"] == "只有 draining 状态可以取消排空，当前状态: ready"
    assert "RuntimeError" not in body["detail"]
    assert "Traceback" not in body["detail"]
