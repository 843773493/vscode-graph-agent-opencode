"""冻结 ``POST /jobs/{job_id}/control`` 的两类客户端状态冲突落 409。

``JobControlService`` 用 ``JobControlRuntimeError``（``RuntimeError`` 子类）表达
「Job 没有可暂停的执行任务」（``no_task``）与「撤回排队 Job 时队列状态不一致」
（``queue_mismatch``）。两者都是客户端可触发的状态冲突：暂停一个刚结束执行任务
的 running Job、或取消一个已不在 pending 队列的 queued Job，都是正常时序下会
发生的竞态。适配层原先只接 ``ValueError``，这两条直接冒泡成 500 并泄漏
``JobControlRuntimeError:`` 内部类名。

本文件以真实 ``JobService``（不注入 fake 服务）驱动真实状态，并用真实 HTTP 封套
（TestClient + 真实 app）断言**精确**状态码与**精确** detail 文本。
按 tests/unit/api 规范：依赖覆盖隔离 + 不启动真实后端进程。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_job_service, verify_local_token, workspace_container_scope
from app.core.identifier import create_prefixed_id
from app.core.path_utils import workspace_context
from app.main import app
from app.schemas.internal_v2.common import JobStatus
from app.services.business.job.service import JobService, JobState
from tests.support.job_thread_fixture import (
    job_fixture_execution_binding,
    job_fixture_thread_id,
)


class _NeverFinishExecutor:
    async def run(self, state: object) -> str:
        await asyncio.Future()
        raise AssertionError("不可达")


class _RecordingBus:
    async def publish(self, **event: object) -> None:
        pass


def _job(job_id: str, session_id: str, status: JobStatus) -> JobState:
    return JobState(
        job_id=job_id,
        session_id=session_id,
        message="m",
        message_id=f"msg_{job_id}",
        message_created_at=datetime.now(UTC).isoformat(),
        agent_id="default",
        status=status, execution_binding=job_fixture_execution_binding(job_id), thread_id=job_fixture_thread_id(session_id),
    )


@pytest.fixture()
def service(session_lifecycle_guard) -> JobService:
    return JobService(
        job_event_bus=_RecordingBus(),
        job_executor=_NeverFinishExecutor(),
        session_lifecycle_guard=session_lifecycle_guard,
    )


def _client(service: JobService, workspace_root: Path) -> TestClient:
    workspace_id = "00000000-0000-4000-8000-000000000001"

    async def bind_workspace() -> AsyncIterator[None]:
        with workspace_context(workspace_id, workspace_root):
            yield

    app.dependency_overrides[get_job_service] = lambda: service
    app.dependency_overrides[verify_local_token] = lambda: "local"
    app.dependency_overrides[workspace_container_scope] = bind_workspace
    return TestClient(
        app,
        raise_server_exceptions=False,
        headers={"X-BoxTeam-Workspace-Id": workspace_id},
    )


def test_pause_without_execution_task_maps_to_409(service: JobService, tmp_path: Path) -> None:
    """running 但已无执行任务的 Job 被暂停 → 409 + 纯文本 detail（原 500 + 类名）。"""
    job_id = create_prefixed_id("job")
    service._jobs[job_id] = _job(job_id, "ses_a", JobStatus.running)
    client = _client(service, tmp_path)
    try:
        response = client.post(
            f"/api/v1/jobs/{job_id}/control",
            json={"action": "pause"},
            headers={"X-Request-ID": "req_no_task"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    assert response.headers["X-Request-ID"] == "req_no_task"
    assert response.json() == {
        "detail": f"Job 没有可暂停的执行任务: job_id={job_id} status=running",
        "request_id": "req_no_task",
    }


def test_cancel_queued_job_absent_from_queue_maps_to_409(service: JobService, tmp_path: Path) -> None:
    """queued 但不在 pending 队列的 Job 被取消 → 409 + 纯文本 detail。

    说明：``control_service`` 的 ``elif job.status == queued`` 分支写的是
    ``if not self._pending_queue.remove(...)``，但 ``JobPendingQueue.remove`` 只会
    返回 ``QueueEntry``（真值），job 缺失时直接抛 ``ValueError('队列中不存在 Job')``，
    因此 ``JobControlRuntimeError.queue_mismatch`` 在当前实现下不可达；此处锁住的是
    真实可达路径（``ValueError`` 经 ``_job_control_http_error`` 落 409，不泄漏 repr）。
    """
    job_id = create_prefixed_id("job")
    service._jobs[job_id] = _job(job_id, "ses_b", JobStatus.queued)
    client = _client(service, tmp_path)
    try:
        response = client.post(
            f"/api/v1/jobs/{job_id}/control",
            json={"action": "cancel"},
            headers={"X-Request-ID": "req_queue_mismatch"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    assert response.json() == {
        "detail": f"队列中不存在 Job: job_id={job_id}",
        "request_id": "req_queue_mismatch",
    }
    # 绝不泄漏内部异常类名。
    assert "JobControlRuntimeError" not in response.text


def test_unknown_job_still_maps_to_404(service: JobService, tmp_path: Path) -> None:
    """未知 Job 仍落 404：新增的 RuntimeError 归口不得吞掉既有分类。"""
    job_id = create_prefixed_id("job")
    client = _client(service, tmp_path)
    try:
        response = client.post(
            f"/api/v1/jobs/{job_id}/control",
            json={"action": "cancel"},
            headers={"X-Request-ID": "req_missing"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert response.json() == {
        "detail": f"Job {job_id} not found",
        "request_id": "req_missing",
    }
