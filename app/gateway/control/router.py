from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.core.trace_middleware import get_request_id
from app.gateway.auth import verify_gateway_token
from app.gateway.control.catalog_search import GatewaySessionCatalogSearchService
from app.gateway.control.coordinator import (
    GeneratorCapabilityMissingError,
    SessionGeneratorCoordinator,
)
from app.gateway.control.generators import SessionGeneratorStore
from app.gateway.control.navigation import WorkspaceNavigationStore
from app.gateway.control.schemas import (
    GatewaySessionSearchResultsDTO,
    GenerationRunDTO,
    GenerationRunListDTO,
    GeneratorDefinitionCreateRequest,
    GeneratorDefinitionDTO,
    GeneratorDefinitionListDTO,
    GeneratorDefinitionUpdateRequest,
    GeneratorManualRunRequest,
    GeneratorPlacementPreviewDTO,
    GeneratorPlacementPreviewRequest,
    WorkspaceFolderCreateRequest,
    WorkspaceNavigationBreadcrumbDTO,
    WorkspaceNavigationNodeUpdateRequest,
    WorkspaceNavigationPlacementRequest,
    WorkspaceNavigationReorderRequest,
    WorkspaceNavigationTreeDTO,
)
from app.gateway.registry import GatewayWorkspaceRegistry
from app.schemas.public_v2.common import APIResponse

router = APIRouter(prefix="/api/gateway", tags=["gateway-control"])


def _validate_generator_workspace_contract(
    definition: GeneratorDefinitionCreateRequest | GeneratorDefinitionDTO,
) -> None:
    placement_workspace_id = definition.placement.workspace_id
    if definition.execution_workspace_id != placement_workspace_id:
        raise ValueError(
            "当前版本不支持会话存储工作区与 Agent 执行工作区分离；"
            "execution_workspace_id 必须等于 placement.workspace_id"
        )
    strategy_target = definition.session_strategy.target
    if (
        strategy_target is not None
        and strategy_target.workspace_id != placement_workspace_id
    ):
        raise ValueError(
            "当前版本不支持跨工作区策略目标；"
            "session_strategy.target.workspace_id 必须等于 placement.workspace_id"
        )
    if (
        definition.context_source.kind == "live_session"
        and definition.context_source.workspace_id != placement_workspace_id
    ):
        raise ValueError(
            "当前版本不支持跨工作区复制 live_session 上下文；"
            "context_source.workspace_id 必须等于生成会话所在工作区"
        )


def _registry(request: Request) -> GatewayWorkspaceRegistry:
    value = getattr(request.app.state, "registry", None)
    if not isinstance(value, GatewayWorkspaceRegistry):
        raise RuntimeError("Gateway registry 尚未初始化")
    return value


def _navigation_store(request: Request) -> WorkspaceNavigationStore:
    value = getattr(request.app.state, "workspace_navigation_store", None)
    if not isinstance(value, WorkspaceNavigationStore):
        raise RuntimeError("Gateway 工作区导航存储尚未初始化")
    return value


def _generator_store(request: Request) -> SessionGeneratorStore:
    value = getattr(request.app.state, "session_generator_store", None)
    if not isinstance(value, SessionGeneratorStore):
        raise RuntimeError("Gateway 会话生成器存储尚未初始化")
    return value


def _coordinator(request: Request) -> SessionGeneratorCoordinator:
    value = getattr(request.app.state, "session_generator_coordinator", None)
    if not isinstance(value, SessionGeneratorCoordinator):
        raise RuntimeError("Gateway 会话生成协调器尚未初始化")
    return value


def _catalog_search(request: Request) -> GatewaySessionCatalogSearchService:
    value = getattr(request.app.state, "session_catalog_search_service", None)
    if not isinstance(value, GatewaySessionCatalogSearchService):
        raise RuntimeError("Gateway 跨工作区会话搜索服务尚未初始化")
    return value


@router.get(
    "/session-catalog/search",
    response_model=APIResponse[GatewaySessionSearchResultsDTO],
)
async def search_all_workspace_sessions(
    query: str,
    limit_per_workspace: int = 50,
    request_id: str = Depends(get_request_id),
    service: GatewaySessionCatalogSearchService = Depends(_catalog_search),
):
    if not 1 <= limit_per_workspace <= 200:
        raise HTTPException(status_code=400, detail="limit_per_workspace 必须在 1..200")
    try:
        result = await service.search(
            query,
            limit_per_workspace=limit_per_workspace,
            request_id=request_id,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/workspace-navigation",
    response_model=APIResponse[WorkspaceNavigationTreeDTO],
)
async def list_workspace_navigation(
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(_registry),
    store: WorkspaceNavigationStore = Depends(_navigation_store),
):
    return APIResponse(
        data=store.list_tree(registry.targets()),
        request_id=request_id,
    )


@router.post(
    "/workspace-navigation/folders",
    response_model=APIResponse[WorkspaceNavigationTreeDTO],
)
async def create_workspace_folder(
    payload: WorkspaceFolderCreateRequest,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(_registry),
    store: WorkspaceNavigationStore = Depends(_navigation_store),
):
    try:
        result = store.create_folder(payload, registry.targets())
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.patch(
    "/workspace-navigation/nodes/{node_id}",
    response_model=APIResponse[WorkspaceNavigationTreeDTO],
)
async def update_workspace_navigation_node(
    node_id: str,
    payload: WorkspaceNavigationNodeUpdateRequest,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(_registry),
    store: WorkspaceNavigationStore = Depends(_navigation_store),
):
    try:
        result = store.update_node(node_id, payload, registry.targets())
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.put(
    "/workspace-navigation/order",
    response_model=APIResponse[WorkspaceNavigationTreeDTO],
)
async def reorder_workspace_navigation(
    payload: WorkspaceNavigationReorderRequest,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(_registry),
    store: WorkspaceNavigationStore = Depends(_navigation_store),
):
    try:
        result = store.reorder(payload, registry.targets())
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.put(
    "/workspace-navigation/placement",
    response_model=APIResponse[WorkspaceNavigationTreeDTO],
)
async def place_workspace_navigation_node(
    payload: WorkspaceNavigationPlacementRequest,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(_registry),
    store: WorkspaceNavigationStore = Depends(_navigation_store),
):
    try:
        result = store.place(payload, registry.targets())
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.delete(
    "/workspace-navigation/folders/{node_id}",
    response_model=APIResponse[WorkspaceNavigationTreeDTO],
)
async def delete_workspace_folder(
    node_id: str,
    recursive: bool = Query(default=False),
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(_registry),
    store: WorkspaceNavigationStore = Depends(_navigation_store),
):
    try:
        result = store.delete_folder(
            node_id,
            registry.targets(),
            recursive=recursive,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/workspace-navigation/nodes/{node_id}/breadcrumb",
    response_model=APIResponse[WorkspaceNavigationBreadcrumbDTO],
)
async def get_workspace_navigation_breadcrumb(
    node_id: str,
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(_registry),
    store: WorkspaceNavigationStore = Depends(_navigation_store),
):
    try:
        result = store.breadcrumb(node_id, registry.targets())
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/session-generators",
    response_model=APIResponse[GeneratorDefinitionListDTO],
)
async def list_session_generators(
    request_id: str = Depends(get_request_id),
    store: SessionGeneratorStore = Depends(_generator_store),
):
    return APIResponse(data=store.list_definitions(), request_id=request_id)


@router.post(
    "/session-generators",
    response_model=APIResponse[GeneratorDefinitionDTO],
)
async def create_session_generator(
    payload: GeneratorDefinitionCreateRequest,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(_registry),
    store: SessionGeneratorStore = Depends(_generator_store),
    coordinator: SessionGeneratorCoordinator = Depends(_coordinator),
):
    try:
        _validate_generator_workspace_contract(payload)
        registry.resolve(payload.execution_workspace_id)
        registry.resolve(payload.placement.workspace_id)
        if payload.context_source.workspace_id is not None:
            registry.resolve(payload.context_source.workspace_id)
        if payload.session_strategy.target is not None:
            registry.resolve(payload.session_strategy.target.workspace_id)
        capability_error: str | None = None
        try:
            await coordinator.validate_definition_capability(
                payload,
                request_id=request_id,
            )
        except GeneratorCapabilityMissingError as error:
            capability_error = str(error)
        except (httpx.HTTPStatusError, httpx.RequestError) as error:
            if payload.policies.mount_missing != "pause":
                raise
            capability_error = f"目标工作区能力不可用: {type(error).__name__}: {error}"
        result = store.create_definition(payload)
        if capability_error is not None:
            result = store.set_definition_status(
                result.generator_id,
                status="blocked",
                reason=capability_error,
            )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except httpx.HTTPStatusError as error:
        raise HTTPException(
            status_code=502,
            detail=(
                "目标工作区拒绝生成能力查询: "
                f"status={error.response.status_code}, body={error.response.text[:1000]}"
            ),
        ) from error
    except httpx.RequestError as error:
        raise HTTPException(
            status_code=502,
            detail=f"无法连接会话生成目标工作区以校验能力: {error}",
        ) from error
    return APIResponse(data=result, request_id=request_id)


@router.patch(
    "/session-generators/{generator_id}",
    response_model=APIResponse[GeneratorDefinitionDTO],
)
async def update_session_generator(
    generator_id: str,
    payload: GeneratorDefinitionUpdateRequest,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(_registry),
    store: SessionGeneratorStore = Depends(_generator_store),
    coordinator: SessionGeneratorCoordinator = Depends(_coordinator),
):
    try:
        current = store.get_definition(generator_id)
        candidate = current.model_copy(
            update=payload.model_dump(exclude_unset=True)
        )
        _validate_generator_workspace_contract(candidate)
        registry.resolve(candidate.execution_workspace_id)
        registry.resolve(candidate.placement.workspace_id)
        if candidate.context_source.workspace_id is not None:
            registry.resolve(candidate.context_source.workspace_id)
        if candidate.session_strategy.target is not None:
            registry.resolve(candidate.session_strategy.target.workspace_id)
        await coordinator.validate_definition_capability(
            candidate,
            request_id=request_id,
        )
        result = store.update_definition(generator_id, payload)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (LookupError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except httpx.HTTPStatusError as error:
        raise HTTPException(
            status_code=502,
            detail=(
                "目标工作区拒绝生成能力查询: "
                f"status={error.response.status_code}, body={error.response.text[:1000]}"
            ),
        ) from error
    except httpx.RequestError as error:
        raise HTTPException(
            status_code=502,
            detail=f"无法连接会话生成目标工作区以校验能力: {error}",
        ) from error
    return APIResponse(data=result, request_id=request_id)


@router.delete(
    "/session-generators/{generator_id}",
    response_model=APIResponse[GeneratorDefinitionDTO],
)
async def delete_session_generator(
    generator_id: str,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    store: SessionGeneratorStore = Depends(_generator_store),
):
    try:
        result = store.delete_definition(generator_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.post(
    "/session-generators/preview-placement",
    response_model=APIResponse[GeneratorPlacementPreviewDTO],
)
async def preview_generator_placement(
    payload: GeneratorPlacementPreviewRequest,
    request_id: str = Depends(get_request_id),
    store: SessionGeneratorStore = Depends(_generator_store),
):
    try:
        result = store.preview(payload)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/session-generators/{generator_id}/runs",
    response_model=APIResponse[GenerationRunListDTO],
)
async def list_generation_runs(
    generator_id: str,
    request_id: str = Depends(get_request_id),
    store: SessionGeneratorStore = Depends(_generator_store),
):
    try:
        store.get_definition(generator_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return APIResponse(data=store.list_runs(generator_id), request_id=request_id)


@router.post(
    "/session-generators/{generator_id}/run",
    response_model=APIResponse[GenerationRunDTO],
)
async def run_session_generator(
    generator_id: str,
    payload: GeneratorManualRunRequest,
    request: Request,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    coordinator: SessionGeneratorCoordinator = Depends(_coordinator),
):
    try:
        result = await coordinator.run_manual(
            generator_id,
            payload,
            request=request,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except httpx.HTTPStatusError as error:
        raise HTTPException(
            status_code=502,
            detail=(
                "目标工作区拒绝会话生成: "
                f"status={error.response.status_code}, body={error.response.text[:1000]}"
            ),
        ) from error
    except httpx.RequestError as error:
        raise HTTPException(
            status_code=502,
            detail=f"无法连接会话生成目标工作区: {error}",
        ) from error
    return APIResponse(data=result, request_id=request_id)
