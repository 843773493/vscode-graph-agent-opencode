"""文件 Provider 的准入和原 owner 委托；真实扫描与网络由集成场景验证。"""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.schemas.internal_v2.resource_navigation import ResourceNavigationResultDTO
from app.schemas.internal_v2.search import WorkspaceSearchRequest
from app.services.business.search.files import register_file_search
from app.services.business.search.service import (
    SearchContractError,
    WorkspaceSearchService,
)
from app.services.infrastructure.resource_platform.sources.resource_navigation import (
    ResourceNavigation,
)
from app.services.infrastructure.workspace_files.paths import WorkspaceFilePaths

ROOT = "boxteam://workspace/ws/resources/files/root"
GATEWAY = "gateway_file_search_test"


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[WorkspaceSearchService, AsyncMock]:
    result = ResourceNavigationResultDTO.model_validate({
        "node": {"uri": ROOT, "resource_shape": "directory", "operations": ["search"]},
    })
    query = AsyncMock(return_value=result)
    navigation = ResourceNavigation(workspace_root=tmp_path, workspace_id="ws", project_root=tmp_path,
        file_paths=WorkspaceFilePaths(workspace=tmp_path, workspace_id="ws", bindings=None))
    monkeypatch.setattr(navigation, "navigate", query)
    service = WorkspaceSearchService("ws")
    register_file_search(service, navigation)
    return service, query


async def test_default_root_and_explicit_options_call_original_owner(service) -> None:
    search, query = service
    request = WorkspaceSearchRequest(workspace_id="ws", category="file", query="needle")
    result = await search.search(request, gateway_id=GATEWAY)
    assert result.provider_id == "workspace-files-name" and result.files.node.uri == ROOT
    payload, = query.call_args.args
    assert payload.uri == ROOT and payload.search_mode == "fuzzy" and payload.search_options.files_only
    assert query.call_args.kwargs["local_gateway_id"] == GATEWAY
    assert query.call_args.kwargs["deadline"] is not None
    await search.search(WorkspaceSearchRequest.model_validate({**request.model_dump(), "operation": "content", "match_mode": "regex",
        "file": {"search_options": {"is_regex": True, "all_matches": True, "include_globs": ["*.txt"]}, "max_bytes": 1024}}), gateway_id=GATEWAY)
    payload, = query.call_args.args
    assert payload.search_mode == "content" and payload.search_options.include_globs == ("*.txt",)
    assert payload.search_options.is_regex and payload.max_bytes == 1024


@pytest.mark.parametrize("uri", [
    "boxteam://workspace/another/resources/files/root",
    "boxteam://another-gateway/workspace/ws/resources/files/root",
    "boxteam://workspace/ws/resources/skills/demo",
])
async def test_wrong_owner_or_kind_fails_before_original_read(service, uri: str) -> None:
    search, query = service
    request = WorkspaceSearchRequest.model_validate({"workspace_id": "ws", "category": "file", "query": "needle", "file": {"uri": uri}})
    with pytest.raises(SearchContractError) as failure:
        await search.search(request, gateway_id=GATEWAY)
    assert failure.value.code == "owner_mismatch"
    query.assert_not_called()


async def test_missing_identity_and_inconsistent_regex_never_dispatch(service) -> None:
    search, query = service
    request = WorkspaceSearchRequest(workspace_id="ws", category="file", query="needle")
    with pytest.raises(SearchContractError, match="Gateway"):
        await search.search(request)
    with pytest.raises(SearchContractError) as failure:
        await search.search(request.model_copy(update={"operation": "content", "match_mode": "regex"}), gateway_id=GATEWAY)
    assert failure.value.code == "unsupported_option"
    query.assert_not_called()
