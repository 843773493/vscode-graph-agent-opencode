"""文件搜索只委托原 ResourceNavigation；目录根各自保留原选项和游标。"""

from typing import Literal

from pydantic import ValidationError

from app.core.resource_uri import parse_resource_uri, resource_display_uri
from app.schemas.internal_v2.resource_navigation import ResourceNavigationRequest
from app.schemas.internal_v2.search import (
    SearchProviderCapabilityDTO,
    WorkspaceFileSearchTargetDTO,
    WorkspaceSearchRequest,
    WorkspaceSearchResultDTO,
)
from app.services.business.search.service import (
    SearchContractError,
    WorkspaceSearchService,
)
from app.services.infrastructure.resource_platform.sources.resource_navigation import (
    ResourceNavigation,
    navigation_deadline,
)


def register_file_search(service: WorkspaceSearchService, navigation: ResourceNavigation) -> None:
    async def search(request: WorkspaceSearchRequest, gateway_id: str | None) -> WorkspaceSearchResultDTO:
        if gateway_id is None:
            raise SearchContractError("owner_mismatch", "文件搜索缺少已认证 Gateway 身份")
        target = request.file or WorkspaceFileSearchTargetDTO()
        uri = target.uri or resource_display_uri(
            scope="workspace", scope_id=service.workspace_id, kind="files", tail_segments=("root",),
        )
        parsed = parse_resource_uri(uri, local_gateway_id=gateway_id)
        if parsed.kind != "files" or parsed.gateway_authority is not None or (
            parsed.scope == "workspace" and parsed.scope_id != service.workspace_id
        ) or (parsed.scope == "gateway" and parsed.scope_id != gateway_id):
            raise SearchContractError("owner_mismatch", "文件根不属于目标工作区或其 Gateway 文件 owner")
        content = request.operation == "content"
        if content and target.search_options.is_regex != (request.match_mode == "regex"):
            raise SearchContractError("unsupported_option", "文件匹配模式与正则选项不一致")
        try:
            payload = ResourceNavigationRequest.model_validate({
                "uri": uri, "operation": "search", "query": request.query, "limit": request.limit, "cursor": request.cursor,
                "search_mode": "content" if content else request.match_mode,
                "search_options": target.search_options.model_copy(update={"files_only": True}), "max_bytes": target.max_bytes,
            })
        except ValidationError as error:
            raise SearchContractError("invalid_query", str(error)) from error
        page = await navigation.navigate(
            payload, local_gateway_id=gateway_id, deadline=navigation_deadline(payload, deadline_at=request.deadline_at),
        )
        reason: Literal["result_limit", "byte_limit"] | None = None
        if page.truncated:
            if page.truncation_reason == "entry_limit":
                reason = "result_limit"
            elif page.truncation_reason in {"response_byte_limit", "file_byte_limit"}:
                reason = "byte_limit"
            else:
                raise SearchContractError("invalid_response", "文件搜索缺少可识别的受限原因")
        return WorkspaceSearchResultDTO(
            provider_id=f"workspace-files-{request.operation}", workspace_id=service.workspace_id, category="file", operation=request.operation,
            contract_version=1, state="limited" if page.truncated else "complete",
            limited_reason=reason, files=page,
        )

    for operation, modes in (("name", ("path", "fuzzy", "glob")), ("content", ("literal", "regex"))):
        service.register(SearchProviderCapabilityDTO(
            provider_id=f"workspace-files-{operation}", category="file", operations=(operation,), contract_version=1,
            supported_options=("query", "match_mode", "limit", "cursor", "deadline_at", "file"),
            position_kind="file_utf16", max_results=500, cache_policy="none", match_modes=modes,
        ), search)
