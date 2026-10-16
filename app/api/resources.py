"""工作区资源管理；所有命令交给对应领域 owner。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Literal
from urllib.error import URLError

from fastapi import APIRouter, Depends, HTTPException

from app.abstractions.browser_resources import BrowserRequestError
from app.api.deps import (
    get_browser_resource_owner,
    get_request_id,
    get_terminal_resource_owner,
    get_workspace_resource_owner,
    verify_local_token,
)
from app.schemas.canonical_params import CanonicalSessionId, CanonicalThreadId
from app.schemas.internal_v2.common import APIResponse
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

router = APIRouter(prefix="/resources", tags=["resources"])


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
