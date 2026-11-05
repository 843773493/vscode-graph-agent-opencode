"""文件续页只改变输出页预算，真实查询及文件版本仍由原 owner 核验。"""

from pathlib import Path

import pytest

from app.schemas.internal_v2.resource_navigation import (
    ResourceNavigationRequest,
    ResourceNavigationSearchOptions,
)
from app.services.infrastructure.resource_platform.sources.resource_navigation import (
    ResourceNavigation,
)
from tests.unit.services.infrastructure.resource_platform.sources.test_resource_navigation import (
    GATEWAY_ID,
)
from tests.unit.services.infrastructure.resource_platform.sources.test_resource_navigation import (
    artifact_case as artifact_case,  # noqa: PLC0414 - 沿原精确产物读取 fixture
)
from tests.unit.services.infrastructure.resource_platform.sources.test_resource_navigation import (
    navigation_case as navigation_case,  # noqa: PLC0414 - 沿原文件 owner 装配
)


@pytest.mark.parametrize("synchronous", [False, True])
@pytest.mark.parametrize("mode", ["list", "fuzzy", "content"])
@pytest.mark.parametrize("next_budget", [
    {"limit": 3}, {"max_response_bytes": 8192}, {"limit": 2, "max_response_bytes": 8192},
], ids=["larger-count", "different-bytes", "both-budgets"])
async def test_page_budget_changes_keep_original_cursor(navigation_case, synchronous, mode, next_budget):
    owner, workspace, uri = navigation_case
    if mode == "content":
        (workspace / "text.txt").write_text("before\n🧪 needle needle\ncontext\nneedle\nafter", encoding="utf-8")
        expected = [(uri + "/text.txt", 2, 4), (uri + "/text.txt", 2, 11), (uri + "/text.txt", 4, 1)]
    else:
        for name in ("needle-a.txt", "needle-b.txt", "needle-c.txt"):
            (workspace / name).write_text("body", encoding="utf-8")
        expected = [(uri + "/" + name,) for name in ("needle-a.txt", "needle-b.txt", "needle-c.txt")]
    arguments: dict[str, object] = {"uri": uri, "operation": "list", "limit": 1}
    if mode != "list":
        arguments.update(operation="search", search_mode=mode, query="needle")
        if mode == "content":
            arguments["search_options"] = {"all_matches": True, "context_lines": 1}
    query = ResourceNavigationRequest.model_validate(arguments)
    observed, cursor = [], None
    for index in range(3):
        # 每页重建，不能借助上次匹配列表或缓存恢复续点。
        current = ResourceNavigation(workspace_root=workspace, workspace_id=owner._workspace_id,
                                     project_root=Path.cwd(), file_paths=owner._file_paths)
        payload = query.model_copy(update={"cursor": cursor, **(next_budget if index else {})})
        page = (current.navigate_sync(payload, local_gateway_id=GATEWAY_ID) if synchronous
                else await current.navigate(payload, local_gateway_id=GATEWAY_ID))
        observed.extend([(hit.uri, hit.line, hit.utf16_column) for hit in page.matches]
                        if mode == "content" else [(node.uri,) for node in page.entries])
        assert len(page.matches if mode == "content" else page.entries) <= payload.limit
        cursor = page.next_cursor
        if not page.truncated:
            assert cursor is None
            break
        assert cursor and page.truncation_reason == "entry_limit"
    else:
        pytest.fail("新页预算下未在三页内取得全部原结果")
    assert observed == expected


@pytest.mark.parametrize("synchronous", [False, True])
@pytest.mark.parametrize("mode", ["fuzzy", "content"])
@pytest.mark.parametrize("count", [2, 3], ids=["exact-limit", "one-extra"])
async def test_exact_resource_or_match_limit_has_no_missing_tail(navigation_case, synchronous, mode, count):
    owner, workspace, uri = navigation_case
    if mode == "content":
        (workspace / "text.txt").write_text("before\n🧪 needle needle\ncontext" + ("\nneedle\nafter" if count == 3 else ""), encoding="utf-8")
    else:
        for index in range(count):
            (workspace / f"needle-{index}.txt").write_text("body", encoding="utf-8")
    query = ResourceNavigationRequest(uri=uri, operation="search", search_mode=mode, query="needle", limit=2,
        search_options=ResourceNavigationSearchOptions(all_matches=True, context_lines=1) if mode == "content" else None)
    first = (owner.navigate_sync(query, local_gateway_id=GATEWAY_ID) if synchronous
             else await owner.navigate(query, local_gateway_id=GATEWAY_ID))
    assert len(first.matches if mode == "content" else first.entries) == 2
    assert first.truncated == (count == 3)
    assert bool(first.next_cursor) == (count == 3)
    if mode == "content":
        assert len(first.entries) == 1 and len(first.contexts) == 2
        assert [(hit.line, hit.utf16_column) for hit in first.matches] == [(2, 4), (2, 11)]
    if count == 3:
        payload = query.model_copy(update={"cursor": first.next_cursor, "limit": 1, "max_response_bytes": 8192})
        last = (owner.navigate_sync(payload, local_gateway_id=GATEWAY_ID) if synchronous
                else await owner.navigate(payload, local_gateway_id=GATEWAY_ID))
        assert not last.truncated and last.next_cursor is None
        assert len(last.matches if mode == "content" else last.entries) == 1
        if mode == "content":
            assert [(hit.line, hit.utf16_column) for hit in last.matches] == [(4, 1)]
        else:
            assert last.entries[0].uri == uri + "/needle-2.txt"


@pytest.mark.parametrize("synchronous", [False, True])
@pytest.mark.parametrize("next_budget", [{"limit": 2}, {"max_response_bytes": 8192}])
async def test_owned_file_cursor_uses_same_page_budget_contract(artifact_case, synchronous, next_budget):
    owner, uri, read, _state = artifact_case
    query = ResourceNavigationRequest(uri=uri, operation="search", search_mode="content", query="hit", limit=1)
    lines, cursor = [], None
    for index in range(3):
        payload = query.model_copy(update={"cursor": cursor, **(next_budget if index else {})})
        page = (owner.navigate_sync(payload, local_gateway_id=GATEWAY_ID, artifact_reader=read) if synchronous
                else await owner.navigate(payload, local_gateway_id=GATEWAY_ID, artifact_reader=read))
        lines.extend(hit.line for hit in page.matches)
        cursor = page.next_cursor
        if not page.truncated:
            assert cursor is None
            break
        assert cursor
    else:
        pytest.fail("原精确产物续页未收敛")
    assert lines == [1, 3, 4]
