from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError

from app.abstractions.job_service import JobServiceProtocol
from app.api.deps import (
    get_artifact_service,
    get_job_service,
    get_request_id,
    get_tool_approval_service,
    verify_local_token,
)
from app.api.errors import state_conflict_http_error, unimplemented_http_error
from app.schemas.canonical_params import CanonicalSessionId, CanonicalThreadId
from app.schemas.internal_v2.artifact import ArtifactDTO
from app.schemas.internal_v2.common import APIResponse, ControlAction
from app.schemas.internal_v2.job import (
    JobControlRequest,
    JobControlResponseDTO,
    JobDTO,
    StepDTO,
)
from app.schemas.internal_v2.tool_approval import (
    ToolApprovalCommandDTO,
    ToolApprovalDTO,
    ToolApprovalSubmitRequest,
)
from app.services.business.job.control_service import JobControlRuntimeError
from app.services.infrastructure.artifact_service import ArtifactService
from app.services.orchestration.execution_step.approval import ToolApprovalService

router = APIRouter(prefix="/jobs", tags=["jobs"])

# JobControlService 当前只实现了这三个动作，其余动作由它显式拒绝为「尚未实现」。
UNIMPLEMENTED_CONTROL_ACTIONS = frozenset(
    set(ControlAction)
    - {ControlAction.pause, ControlAction.resume, ControlAction.cancel}
)


def _job_control_http_error(
    job_id: str,
    payload: JobControlRequest,
    error: Exception,
) -> HTTPException:
    """把 Job 控制被拒落成对客户端有意义的响应，而不是无上下文 500。

    ``JobService.control`` 与 ``JobControlService`` 已用同一个类型名和同一句
    「Job {id} not found」表达未知 Job；本适配层对 ``get_job``/``list_job_steps``
    也按同一句文本落 404，这里保持一致。

    本 helper 是 Job 控制被拒的唯一归口：``JobControlValueError``（未知 Job /
    动作未实现 / 状态不允许）与 ``JobControlRuntimeError``（``no_task`` /
    ``queue_mismatch``）都经此翻译。后者是客户端可触发的状态冲突（暂停一个已无
    执行任务的 running Job、或队列与 Job 状态不一致），必须落 409 而非冒泡 500 并
    泄漏 ``JobControlRuntimeError:`` 内部类名。只按精确类型判定，不捕获宽泛
    ``RuntimeError``，避免把服务端故障一并吞成 409。

    TODO: ``JobControlValueError`` 目前把「未知 Job」「动作未实现」「状态不允许」
    压在同一类型上，只能靠文本区分；业务层暴露独立错误身份后应改为按类型判定。
    """
    if isinstance(error, ValueError):
        if error.args == (f"Job {job_id} not found",):
            return HTTPException(status_code=404, detail=str(error))
        if payload.action in UNIMPLEMENTED_CONTROL_ACTIONS:
            return unimplemented_http_error(error)
    # 状态不允许该动作、会话已有其他 active Job，或 Job 执行任务已消失/队列不一致：
    # 都是状态冲突而非服务端故障。
    return state_conflict_http_error(error)


@router.get(
    "/tool-approvals", response_model=APIResponse[list[ToolApprovalDTO]],
    summary="读取当前会话线程的活动工具审批",
)
async def list_thread_tool_approvals(
    session_id: CanonicalSessionId,
    thread_id: CanonicalThreadId,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    approvals: Annotated[ToolApprovalService, Depends(get_tool_approval_service)],
):
    return APIResponse(data=approvals.list_for_thread(session_id, thread_id), request_id=request_id)


@router.get("/{job_id}", response_model=APIResponse[JobDTO], summary="获取任务详情")
async def get_job(
    job_id: str,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    job_service: Annotated[JobServiceProtocol, Depends(get_job_service)],
):
    try:
        result = await job_service.get(job_id)
    except ValidationError:
        raise
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/{job_id}/steps", response_model=APIResponse[list[StepDTO]], summary="获取任务步骤"
)
async def list_job_steps(
    job_id: str,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    job_service: Annotated[JobServiceProtocol, Depends(get_job_service)],
):
    try:
        result = await job_service.list_steps(job_id)
    except ValidationError:
        raise
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/{job_id}/approvals", response_model=APIResponse[list[ToolApprovalDTO]],
    summary="读取当前任务的工具审批",
)
async def list_tool_approvals(
    job_id: str,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    job_service: Annotated[JobServiceProtocol, Depends(get_job_service)],
    approvals: Annotated[ToolApprovalService, Depends(get_tool_approval_service)],
):
    try:
        await job_service.get(job_id)
    except ValidationError:
        raise
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return APIResponse(data=approvals.list(job_id), request_id=request_id)


@router.post(
    "/{job_id}/approvals", response_model=APIResponse[ToolApprovalCommandDTO],
    summary="向原检查点提交工具审批决定",
)
async def submit_tool_approvals(
    job_id: str,
    payload: ToolApprovalSubmitRequest,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    job_service: Annotated[JobServiceProtocol, Depends(get_job_service)],
    approvals: Annotated[ToolApprovalService, Depends(get_tool_approval_service)],
):
    try:
        await job_service.get(job_id)
    except ValidationError:
        raise
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    try:
        result = await approvals.submit(job_id, payload)
    except ValidationError:
        raise
    except ValueError as error:
        raise state_conflict_http_error(error) from error
    return APIResponse(data=result, request_id=request_id)


@router.post(
    "/{job_id}/control",
    response_model=APIResponse[JobControlResponseDTO],
    summary="控制任务",
)
async def control_job(
    job_id: str,
    payload: JobControlRequest,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    job_service: Annotated[JobServiceProtocol, Depends(get_job_service)],
):
    try:
        result = await job_service.control(job_id, payload)
    except ValidationError:
        raise
    except (ValueError, JobControlRuntimeError) as error:
        raise _job_control_http_error(job_id, payload, error) from error
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/{job_id}/artifacts",
    response_model=APIResponse[list[ArtifactDTO]],
    summary="获取任务产物列表",
)
async def list_job_artifacts(
    job_id: str,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    artifact_service: Annotated[ArtifactService, Depends(get_artifact_service)],
):
    try:
        result = await artifact_service.list_by_job(job_id)
    except RuntimeError as error:
        # 产物存储尚未实现：服务端能力缺失，显式落 501 而不是无上下文的 500。
        raise unimplemented_http_error(error) from error
    return APIResponse(data=result, request_id=request_id)
