from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import (
    get_request_id,
    get_tool_service,
    get_tool_test_service,
    verify_local_token,
)
from app.schemas.public_v2.common import APIResponse
from app.schemas.public_v2.tool import (
    ToolDTO,
    ToolInvokeRequest,
    ToolInvokeResultDTO,
    ToolSelectionPatchRequest,
)
from app.schemas.public_v2.tool_test import (
    ToolTestRunDTO,
    ToolTestRunListDTO,
    ToolTestStartRequest,
)
from app.services.infrastructure.tool_service import ToolService
from app.tool_testing.service import ToolTestService

router = APIRouter(prefix="/tools", tags=["tools"])


@router.get("", response_model=APIResponse[list[ToolDTO]], summary="获取 Tool 列表")
async def list_tools(
    agent_id: str = "default",
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
    tool_service: ToolService = Depends(get_tool_service),
):
    result = await tool_service.list(agent_id)
    return APIResponse(data=result, request_id=request_id)


@router.patch(
    "/selection",
    response_model=APIResponse[list[ToolDTO]],
    summary="增量更新 Agent 工具开关",
)
async def update_tool_selection(
    payload: ToolSelectionPatchRequest,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
    tool_service: ToolService = Depends(get_tool_service),
):
    result = await tool_service.update_selection(payload)
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/tests",
    response_model=APIResponse[ToolTestRunListDTO],
    summary="获取模型工具测试记录",
)
async def list_tool_tests(
    tool_name: str | None = None,
    limit: int = 20,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
    test_service: ToolTestService = Depends(get_tool_test_service),
):
    result = ToolTestRunListDTO(items=test_service.list(tool_name=tool_name, limit=limit))
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/tests/{run_id}",
    response_model=APIResponse[ToolTestRunDTO],
    summary="获取模型工具测试进度",
)
async def get_tool_test(
    run_id: str,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
    test_service: ToolTestService = Depends(get_tool_test_service),
):
    return APIResponse(data=test_service.get(run_id), request_id=request_id)


@router.post(
    "/{tool_id}/tests",
    response_model=APIResponse[ToolTestRunDTO],
    summary="启动模型工具调用测试",
)
async def start_tool_test(
    tool_id: str,
    payload: ToolTestStartRequest,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
    test_service: ToolTestService = Depends(get_tool_test_service),
):
    result = await test_service.start(tool_name=tool_id, request=payload)
    return APIResponse(data=result, request_id=request_id)


@router.get("/{tool_id}", response_model=APIResponse[ToolDTO], summary="获取 Tool 详情")
async def get_tool(
    tool_id: str,
    agent_id: str = "default",
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
    tool_service: ToolService = Depends(get_tool_service),
):
    result = await tool_service.get(tool_id, agent_id)
    return APIResponse(data=result, request_id=request_id)


@router.post("/{tool_id}/invoke", response_model=APIResponse[ToolInvokeResultDTO], summary="调用 Tool")
async def invoke_tool(
    tool_id: str,
    payload: ToolInvokeRequest,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
    tool_service: ToolService = Depends(get_tool_service),
):
    result = await tool_service.invoke(tool_id, payload)
    return APIResponse(data=result, request_id=request_id)
