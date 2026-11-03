"""工作区搜索与能力发现；取消、分页和领域状态由搜索服务负责。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError

from app.abstractions.session_context import SessionContextRevisionChangedError
from app.api.deps import (
    get_request_id,
    get_workspace_search_service,
    verify_local_token,
)
from app.api.errors import (
    not_found_http_error,
    session_context_revision_http_error,
    state_conflict_http_error,
)
from app.core.exceptions import NotFoundError
from app.core.resource_uri import ResourceUriGrammarError
from app.core.session_control_thread_catalog.search import (
    ThreadCatalogRevisionChangedError,
)
from app.core.session_lifecycle_gate import SessionDeletionPendingError
from app.core.trace_middleware import require_current_gateway_id
from app.gateway.federation.errors import FederationError
from app.schemas.internal_v2.common import APIResponse
from app.schemas.internal_v2.search import (
    WorkspaceSearchCapabilitiesDTO,
    WorkspaceSearchRequest,
    WorkspaceSearchResultDTO,
)
from app.services.business.search.service import (
    SearchContractError,
    SearchProviderReadError,
    WorkspaceSearchService,
)
from app.services.business.session_navigation.catalog_query import CatalogCursorError
from app.services.business.session_navigation.thread_search import (
    ThreadSearchCursorError,
)
from app.services.business.session_turn_history.workspace_search import (
    WorkspaceMessageSearchCursorError,
)
from app.services.infrastructure.llm_request_log_service import LLMRequestLogError
from app.services.infrastructure.resource_platform.sources.resource_navigation import (
    ResourceFileOperationError,
)
from app.services.infrastructure.resource_platform.virtual_resources.resolver import (
    ResourceUriResolveError,
)
from app.services.infrastructure.turn_history import (
    InvalidTurnCursorError,
    StaleTurnCursorError,
    StaleTurnReferenceError,
)

router = APIRouter(prefix="/search", tags=["search"])


@router.get("/capabilities", response_model=APIResponse[WorkspaceSearchCapabilitiesDTO])
async def search_capabilities(
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    service: Annotated[WorkspaceSearchService, Depends(get_workspace_search_service)],
):
    return APIResponse(data=service.capabilities(), request_id=request_id)


@router.post("", response_model=APIResponse[WorkspaceSearchResultDTO])
async def search_workspace(
    payload: WorkspaceSearchRequest,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    service: Annotated[WorkspaceSearchService, Depends(get_workspace_search_service)],
):
    try:
        result = await service.search(payload, gateway_id=require_current_gateway_id("工作区搜索"))
    except SearchContractError as error:
        raise HTTPException(status_code=400, detail={"code": error.code, "message": str(error)}) from error
    except (CatalogCursorError, ThreadSearchCursorError, ThreadCatalogRevisionChangedError) as error:
        raise HTTPException(status_code=409, detail=error.detail()) from error
    except WorkspaceMessageSearchCursorError as error:
        raise HTTPException(status_code=400 if error.code == "invalid_turn_cursor" else 409,
                            detail=error.detail()) from error
    except LLMRequestLogError as error:
        raise HTTPException(status_code=400 if error.code == "invalid_request_log_cursor" else 409,
                            detail=error.detail()) from error
    except SearchProviderReadError as error:
        raise HTTPException(status_code=500, detail=error.detail()) from error
    except SessionContextRevisionChangedError as error:
        raise session_context_revision_http_error(error) from error
    except ResourceUriGrammarError as error:
        raise HTTPException(400, detail={"owner": "resource_uri_grammar", "code": error.reason_code, "message": str(error)}) from error
    except ResourceUriResolveError as error:
        status = 404 if error.reason_code == "unknown_resource" else 403
        raise HTTPException(status, detail={"owner": "resource_uri_resolver", "code": error.reason_code, "message": str(error)}) from error
    except ResourceFileOperationError as error:
        raise HTTPException(400, detail=error.detail()) from error
    except FederationError as error:
        raise HTTPException(409, detail=error.to_wire()) from error
    except NotFoundError as error:
        raise not_found_http_error(error) from error
    except InvalidTurnCursorError as error:
        raise HTTPException(status_code=400, detail={"code": "invalid_turn_cursor", "message": str(error)}) from error
    except (StaleTurnCursorError, StaleTurnReferenceError) as error:
        raise HTTPException(status_code=409, detail=error.detail.model_dump(mode="json")) from error
    except SessionDeletionPendingError as error:
        raise state_conflict_http_error(error) from error
    except ValidationError:
        raise
    except TimeoutError as error:
        raise HTTPException(status_code=409, detail={"code": "deadline", "message": str(error)}) from error
    except ValueError as error:
        # TODO：随原 owner 的 cursor／revision 异常类型化，细分 owner_rejected 的诊断代码。
        raise HTTPException(status_code=409, detail={"code": "owner_rejected", "message": str(error)}) from error
    # TODO：16.18 迁齐其余原 owner 的具名异常与信息／警告合同。
    return APIResponse(data=result, request_id=request_id)
