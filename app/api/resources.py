"""工作区资源管理；所有命令交给对应领域 owner。"""

from __future__ import annotations

import asyncio
from asyncio import timeout
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Literal
from urllib.error import URLError

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from app.abstractions.browser_resources import BrowserRequestError
from app.api.deps import (
    get_browser_resource_owner,
    get_configured_skill_sources,
    get_request_id,
    get_resource_navigation,
    get_terminal_resource_owner,
    get_text_mutations,
    get_workspace_resource_owner,
    verify_local_token,
)
from app.core.resource_uri import ResourceUriGrammarError
from app.core.trace_middleware import require_current_gateway_id
from app.gateway.federation.errors import FederationError
from app.schemas.canonical_params import CanonicalSessionId, CanonicalThreadId
from app.schemas.internal_v2.common import APIResponse
from app.schemas.internal_v2.resource_navigation import (
    ResourceNavigationRequest,
    ResourceNavigationResultDTO,
)
from app.schemas.internal_v2.skill_sources import SkillSourcesRefreshDTO
from app.schemas.internal_v2.text_mutation import (
    TextMutationReceiptDTO,
    TextMutationRequest,
    TextMutationTransferDTO,
)
from app.schemas.internal_v2.workspace_resource import (
    BrowserResourceCreateRequest,
    BrowserResourceOperationRequest,
    ResourceTransportGrantDTO,
    TerminalResourceCreateRequest,
    WorkspaceResourceCommandRequest,
    WorkspaceResourceDTO,
    WorkspaceResourceListDTO,
)
from app.services.business.browser_resources.owner import BrowserResourceOwner
from app.services.business.terminal_resources.owner import TerminalResourceOwner
from app.services.business.workspace_resources.facts import public_error
from app.services.business.workspace_resources.owner import (
    ResourceConflict,
    WorkspaceResourceOwner,
)
from app.services.business.workspace_resources.records import ThreadRef
from app.services.infrastructure.resource_platform.sources.configured_skill_sources import (
    ConfiguredSkillSources,
)
from app.services.infrastructure.resource_platform.sources.resource_navigation import (
    NAVIGATION_DEADLINE_HEADER,
    ResourceFileOperationError,
    ResourceNavigation,
    navigation_deadline,
)
from app.services.infrastructure.resource_platform.virtual_resources.resolver import (
    ResourceUriResolveError,
)
from app.services.infrastructure.workspace_files.text_owner import (
    WorkspaceTextMutationOwner,
)
from app.services.infrastructure.workspace_files.text_transfer import (
    unconfirmed_text_mutation,
)

router = APIRouter(prefix="/resources", tags=["resources"])


@router.post("/skill-sources/refresh", response_model=APIResponse[SkillSourcesRefreshDTO])
async def refresh_skill_sources(
    _: str = Depends(verify_local_token), request_id: str = Depends(get_request_id),
    *, owner: Annotated[ConfiguredSkillSources, Depends(get_configured_skill_sources)],
) -> APIResponse[SkillSourcesRefreshDTO]:
    await owner.refresh()
    return APIResponse(data=SkillSourcesRefreshDTO(workspace_id=owner.owner_id,
        revision=owner.publication_revision, available=all(entry.available for entry in owner.entries())), request_id=request_id)


@router.post("/navigation", response_model=APIResponse[ResourceNavigationResultDTO])
async def navigate_resource(
    body: ResourceNavigationRequest, request: Request,
    _: str = Depends(verify_local_token), request_id: str = Depends(get_request_id),
    *, owner: Annotated[ResourceNavigation, Depends(get_resource_navigation)],
):
    try:
        deadline = navigation_deadline(body, deadline_at=request.headers.get(NAVIGATION_DEADLINE_HEADER))
        result = await owner.navigate(body, local_gateway_id=require_current_gateway_id("资源导航"), deadline=deadline)
        envelope = APIResponse(data=result, request_id=request_id)
        if len(envelope.model_dump_json().encode("utf-8")) > body.max_response_bytes:
            raise ResourceFileOperationError(
                "output_limit_exceeded", "导航响应及请求回执超过总字节上限", reason="response_byte_limit",
            )
        ResourceNavigation._check_deadline(deadline)
        return envelope
    except ResourceUriGrammarError as error:
        raise HTTPException(400, detail={"owner": "resource_uri_grammar", "code": error.reason_code, "message": str(error)}) from error
    except ResourceUriResolveError as error:
        status = 404 if error.reason_code == "unknown_resource" else 403
        raise HTTPException(status, detail={"owner": "resource_uri_resolver", "code": error.reason_code, "message": str(error)}) from error
    except ResourceFileOperationError as error:
        raise HTTPException(400, detail=error.detail()) from error
    except FederationError as error:
        raise HTTPException(409, detail=error.to_wire()) from error
    except httpx.HTTPStatusError as error:
        raise HTTPException(error.response.status_code, detail=error.response.json()["detail"]) from error
    except httpx.RequestError as error:
        raise HTTPException(502, detail={"owner": "gateway_transport", "type": type(error).__name__}) from error
    except (OSError, UnicodeError) as error:
        raise HTTPException(400, detail={"owner": "resource_navigation", "type": type(error).__name__, "errno": getattr(error, "errno", None)}) from error


@router.post("/text-mutations", response_model=APIResponse[TextMutationTransferDTO])
async def mutate_text(
    body: TextMutationRequest,
    _: str = Depends(verify_local_token), request_id: str = Depends(get_request_id),
    *, owner: Annotated[WorkspaceTextMutationOwner, Depends(get_text_mutations)],
):
    try:
        async with asyncio.timeout(body.timeout_ms / 1000):
            result = await asyncio.to_thread(owner.execute_transfer, body)
    except TimeoutError as error:
        # 工作线程尚未确认停止；目标 journal 继续反映实际效果，不伪报未执行。
        result = TextMutationTransferDTO(receipt=TextMutationReceiptDTO.model_validate(unconfirmed_text_mutation(body.command, error)))
    return APIResponse(data=result, request_id=request_id)


@contextmanager
def _owner_errors() -> Iterator[None]:
    try:
        yield
    except ResourceConflict as error:
        raise HTTPException(409, detail=str(error)) from error
    except KeyError as error:
        raise HTTPException(404, detail=public_error(error)) from error
    except ValueError as error:
        raise HTTPException(400, detail=public_error(error)) from error
    except BrowserRequestError as error:
        raise HTTPException(error.status, detail=public_error(error)) from error
    except (ConnectionError, TimeoutError, URLError) as error:
        raise HTTPException(503, detail=public_error(error)) from error


@router.get("", response_model=APIResponse[WorkspaceResourceListDTO])
async def list_resources(
    *,
    kind: Literal["terminal", "browser"] | None = None,
    retention_scope: Literal["thread", "workspace"] | None = None,
    session_id: CanonicalSessionId | None = None,
    thread_id: CanonicalThreadId | None = None,
    source_session_id: CanonicalSessionId | None = None,
    source_thread_id: CanonicalThreadId | None = None,
    _: str = Depends(verify_local_token), request_id: str = Depends(get_request_id),
    terminal_owner: Annotated[TerminalResourceOwner, Depends(get_terminal_resource_owner)],
    browser_owner: Annotated[BrowserResourceOwner, Depends(get_browser_resource_owner)],
):
    owners = [terminal_owner, browser_owner] if kind is None else [terminal_owner if kind == "terminal" else browser_owner]
    items = []
    with _owner_errors():
        # HTTP入口的两个领域查询共用预算，领域内部仍沿各自原owner读取。
        # TODO(5.4)：同步存储/序列化及不合作取消仍需原执行owner收敛。
        async with timeout(30):
            for owner in owners:
                items.extend(await owner.list_resources(
                    session_id=session_id, thread_id=thread_id, retention_scope=retention_scope,
                    source_session_id=source_session_id, source_thread_id=source_thread_id,
                ))
    return APIResponse(data=WorkspaceResourceListDTO(
        workspace_id=terminal_owner.workspace_id, items=[WorkspaceResourceDTO.from_owner(item) for item in items],
    ), request_id=request_id)


@router.post("/terminals", response_model=APIResponse[WorkspaceResourceDTO])
async def create_terminal(
    *,
    body: TerminalResourceCreateRequest,
    _: str = Depends(verify_local_token), request_id: str = Depends(get_request_id),
    owner: Annotated[TerminalResourceOwner, Depends(get_terminal_resource_owner)],
):
    with _owner_errors():
        result = await owner.create_terminal(
            session_id=body.session_id, thread_id=body.thread_id,
            retention_scope=body.retention_scope, title=body.title,
            cwd=body.cwd_relative, cols=body.cols, rows=body.rows,
            holder_id=f"request:{request_id}", operation_id=request_id,
        )
    return APIResponse(data=WorkspaceResourceDTO.from_owner(result), request_id=request_id)


@router.post("/browsers", response_model=APIResponse[WorkspaceResourceDTO])
async def create_browser(
    *, body: BrowserResourceCreateRequest,
    _: str = Depends(verify_local_token), request_id: str = Depends(get_request_id),
    owner: Annotated[BrowserResourceOwner, Depends(get_browser_resource_owner)],
):
    with _owner_errors():
        result = await owner.create_browser(
            **body.model_dump(), holder_id=f"request:{request_id}", operation_id=request_id,
        )
    return APIResponse(data=WorkspaceResourceDTO.from_owner(result), request_id=request_id)


@router.post("/{resource_id}/browser-operation", response_model=APIResponse[dict[str, object]])
async def browser_operation(
    *, resource_id: str, body: BrowserResourceOperationRequest,
    _: str = Depends(verify_local_token), request_id: str = Depends(get_request_id),
    owner: Annotated[BrowserResourceOwner, Depends(get_browser_resource_owner)],
):
    with _owner_errors():
        result = await owner.perform(
            resource_id, action=body.action, payload=body.payload,
            expected_revision=body.expected_revision,
            thread=None if body.thread is None else ThreadRef(**body.thread.model_dump()),
            holder_id=f"request:{request_id}", operation_id=request_id,
        )
    result["browser"] = WorkspaceResourceDTO.from_owner(result["browser"])
    return APIResponse(data=result, request_id=request_id)


@router.get("/{resource_id}", response_model=APIResponse[WorkspaceResourceDTO])
async def get_resource(
    *,
    resource_id: str,
    _: str = Depends(verify_local_token), request_id: str = Depends(get_request_id),
    owner: Annotated[WorkspaceResourceOwner, Depends(get_workspace_resource_owner)],
):
    with _owner_errors():
        result = await owner.get_resource(resource_id)
    return APIResponse(data=WorkspaceResourceDTO.from_owner(result), request_id=request_id)


@router.post("/{resource_id}/transport-grant", response_model=APIResponse[ResourceTransportGrantDTO])
async def resource_transport_grant(
    *,
    resource_id: str, body: WorkspaceResourceCommandRequest,
    _: str = Depends(verify_local_token), request_id: str = Depends(get_request_id),
    owner: Annotated[WorkspaceResourceOwner, Depends(get_workspace_resource_owner)],
):
    with _owner_errors():
        result = await owner.transport_grant(
            resource_id, expected_revision=body.expected_revision,
            thread=None if body.thread is None else ThreadRef(**body.thread.model_dump()),
            holder_id=f"request:{request_id}", operation_id=request_id,
        )
    result["resource"] = WorkspaceResourceDTO.from_owner(result["resource"])
    return APIResponse(data=ResourceTransportGrantDTO.model_validate(result), request_id=request_id)


@router.post("/{resource_id}/{action}", response_model=APIResponse[WorkspaceResourceDTO])
async def control_resource(
    *,
    resource_id: str, action: Literal["promote", "attach", "detach", "stop", "delete"],
    body: WorkspaceResourceCommandRequest,
    _: str = Depends(verify_local_token), request_id: str = Depends(get_request_id),
    owner: Annotated[WorkspaceResourceOwner, Depends(get_workspace_resource_owner)],
):
    with _owner_errors():
        result = await owner.command(
            resource_id, action=action, expected_revision=body.expected_revision,
            thread=None if body.thread is None else ThreadRef(**body.thread.model_dump()),
            holder_id=f"request:{request_id}", operation_id=request_id,
        )
    return APIResponse(data=WorkspaceResourceDTO.from_owner(result), request_id=request_id)
