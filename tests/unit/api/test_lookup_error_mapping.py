"""冻结「查不到资源」在 API 适配层统一映射为 404，而不是泄漏成 500。

本仓库约定：业务服务用 ValueError（Job/Agent）或 FileNotFoundError（工具测试
记录）表达「按 ID 查询的目标不存在」，适配层负责翻译成 404；同一适配层的
get_tool/get_session/list_child_threads 已如此处理，这里锁住其余同类入口。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api import agents as agents_api
from app.api import jobs as jobs_api
from app.api import tools as tools_api
from app.schemas.internal_v2.common import ControlAction
from app.schemas.internal_v2.job import JobControlRequest


class _MissingAgentService:
    async def get(self, agent_id: str):
        raise ValueError(f"Agent {agent_id} not found")


class _MissingJobService:
    async def get(self, job_id: str):
        raise ValueError(f"Job {job_id} not found")

    async def list_steps(self, job_id: str):
        raise ValueError(f"Job {job_id} not found")

    async def control(self, job_id: str, control_request: JobControlRequest):
        raise ValueError(f"Job {job_id} not found")


class _UnsupportedActionJobService:
    async def control(self, job_id: str, control_request: JobControlRequest):
        raise ValueError(f"Job 控制动作尚未实现: {control_request.action.value}")


class _ConflictingStateJobService:
    async def control(self, job_id: str, control_request: JobControlRequest):
        raise ValueError(
            f"只有 running、streaming 或 waiting_input 的 Job 可以暂停: job_id={job_id}"
        )


class _MissingToolTestService:
    def get(self, run_id: str):
        raise FileNotFoundError(f"工具测试记录不存在或已被新测试覆盖: {run_id}")


@pytest.mark.asyncio
async def test_get_agent_maps_missing_agent_to_404() -> None:
    with pytest.raises(HTTPException) as captured:
        await agents_api.get_agent(
            "nope",
            _="local",
            request_id="req_agent",
            agent_service=_MissingAgentService(),
        )

    assert captured.value.status_code == 404


@pytest.mark.asyncio
async def test_get_job_maps_missing_job_to_404() -> None:
    with pytest.raises(HTTPException) as captured:
        await jobs_api.get_job(
            "nope",
            _="local",
            request_id="req_job",
            job_service=_MissingJobService(),
        )

    assert captured.value.status_code == 404


@pytest.mark.asyncio
async def test_list_job_steps_maps_missing_job_to_404() -> None:
    with pytest.raises(HTTPException) as captured:
        await jobs_api.list_job_steps(
            "nope",
            _="local",
            request_id="req_job_steps",
            job_service=_MissingJobService(),
        )

    assert captured.value.status_code == 404


@pytest.mark.asyncio
async def test_get_tool_test_maps_missing_record_to_404() -> None:
    with pytest.raises(HTTPException) as captured:
        await tools_api.get_tool_test(
            "nope",
            _="local",
            request_id="req_tool_test",
            test_service=_MissingToolTestService(),
        )

    assert captured.value.status_code == 404


@pytest.mark.asyncio
async def test_control_job_maps_missing_job_to_404() -> None:
    with pytest.raises(HTTPException) as captured:
        await jobs_api.control_job(
            "nope",
            JobControlRequest(action=ControlAction.pause),
            _="local",
            request_id="req_control",
            job_service=_MissingJobService(),
        )

    assert captured.value.status_code == 404
    assert captured.value.detail == "Job nope not found"


@pytest.mark.asyncio
async def test_control_job_maps_unimplemented_action_to_501() -> None:
    with pytest.raises(HTTPException) as captured:
        await jobs_api.control_job(
            "job_001",
            JobControlRequest(action=ControlAction.skip),
            _="local",
            request_id="req_control_skip",
            job_service=_UnsupportedActionJobService(),
        )

    assert captured.value.status_code == 501


@pytest.mark.asyncio
async def test_control_job_maps_conflicting_state_to_409() -> None:
    with pytest.raises(HTTPException) as captured:
        await jobs_api.control_job(
            "job_001",
            JobControlRequest(action=ControlAction.pause),
            _="local",
            request_id="req_control_conflict",
            job_service=_ConflictingStateJobService(),
        )

    assert captured.value.status_code == 409
