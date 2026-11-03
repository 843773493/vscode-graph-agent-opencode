from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError
from pydantic_core import PydanticSerializationError

from app.abstractions.session_context import SessionContextRevisionChangedError
from app.api.deps import get_request_id, get_session_context_query_service
from app.api.errors import (
    not_found_http_error,
    session_context_revision_http_error,
    state_conflict_http_error,
)
from app.core.exceptions import NotFoundError
from app.core.session_lifecycle_gate import SessionDeletionPendingError
from app.schemas.internal_v2.common import APIResponse
from app.schemas.internal_v2.session_context import (
    SessionContextReadRequest,
    SessionContextReadResultDTO,
    SessionContextSearchRequest,
    SessionContextSearchResultDTO,
)
from app.services.business.session_context_query_service import (
    SessionContextQueryService,
)
from app.services.infrastructure.resource_platform.virtual_resources.resolver import (
    ResourceUriResolveError,
)

router = APIRouter(prefix="/context", tags=["context"])


@router.post(
    "/read",
    response_model=APIResponse[SessionContextReadResultDTO],
    summary="读取当前工作区的结构化上下文资源",
)
async def read_context(
    payload: SessionContextReadRequest,
    request_id: Annotated[str, Depends(get_request_id)],
    query_service: Annotated[
        SessionContextQueryService, Depends(get_session_context_query_service)
    ],
):
    try:
        result = await query_service.read_context(payload)
    except SessionContextRevisionChangedError as error:
        raise session_context_revision_http_error(error) from error
    except (KeyError, NotFoundError) as error:
        raise not_found_http_error(error) from error
    except ResourceUriResolveError as error:
        raise HTTPException(
            status_code=400,
            detail={"code": error.reason_code, "message": str(error)},
        ) from error
    except (ValidationError, PydanticSerializationError):
        raise
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except SessionDeletionPendingError as error:
        raise state_conflict_http_error(error) from error
    except RuntimeError as error:
        if not any(
            code in str(error)
            for code in (
                "source-mismatch",
                "plan-order-integrity",
                "detail-unavailable",
            )
        ):
            raise
        raise state_conflict_http_error(error) from error
    return APIResponse(data=result, request_id=request_id)


@router.post(
    "/search",
    response_model=APIResponse[SessionContextSearchResultDTO],
    summary="搜索当前工作区的结构化上下文资源",
)
async def search_context(
    payload: SessionContextSearchRequest,
    request_id: Annotated[str, Depends(get_request_id)],
    query_service: Annotated[
        SessionContextQueryService, Depends(get_session_context_query_service)
    ],
):
    try:
        result = await query_service.search_context(payload)
    except SessionContextRevisionChangedError as error:
        raise session_context_revision_http_error(error) from error
    except (KeyError, NotFoundError) as error:
        raise not_found_http_error(error) from error
    except ResourceUriResolveError as error:
        raise HTTPException(
            status_code=400,
            detail={"code": error.reason_code, "message": str(error)},
        ) from error
    except (ValidationError, PydanticSerializationError):
        raise
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except SessionDeletionPendingError as error:
        raise state_conflict_http_error(error) from error
    return APIResponse(data=result, request_id=request_id)
