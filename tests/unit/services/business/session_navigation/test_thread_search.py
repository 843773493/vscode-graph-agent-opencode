"""目录预算、main 身份与续查语义；child 产品创建另由真实 HTTP 验证。"""

import pytest

from app.schemas.internal_v2.session import SessionCreateRequest
from app.services.business.session_navigation import thread_search
from app.services.business.session_navigation.service import SessionCatalogService
from tests.unit.services.business.session_navigation.test_fuzzy_catalog_search import (
    catalog as catalog,  # noqa: PLC0414 - 复用真实目录/Session owner fixture
)

GATEWAY = "gateway_0123456789abcdef0123456789abcdef"


@pytest.mark.asyncio
async def test_main_identity_and_scan_budget_continue_without_skips(
    catalog: SessionCatalogService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = catalog._session_service
    owners = [
        await sessions.create(SessionCreateRequest(title="Matching Main"))
        for _ in range(3)
    ]
    monkeypatch.setattr(thread_search, "_MAX_SCANNED_NODES", 1)
    cursor = None
    found = []
    for page_number in range(3):
        page = await thread_search.search_session_threads(
            catalog,
            sessions,
            gateway_id=GATEWAY,
            query=" MtcM ",
            limit=10,
            cursor=cursor,
        )
        assert page.scanned_sessions == page.scanned_threads == 1
        assert page.complete is (page_number == 2)
        assert page.limited_reason == (None if page.complete else "scan_limit")
        found.extend(page.items)
        cursor = page.cursor
    assert cursor is None
    assert {(hit.session_id, hit.thread_id) for hit in found} == {
        (owner.session_id, await sessions.resolve_main_thread(owner.session_id))
        for owner in owners
    }
    assert all(hit.kind == "main" and hit.session_id != hit.thread_id for hit in found)
    assert all(hit.thread_uri.startswith(f"boxteam://{GATEWAY}/") for hit in found)


@pytest.mark.asyncio
async def test_cursor_rejects_query_gateway_and_catalog_changes(
    catalog: SessionCatalogService,
) -> None:
    sessions = catalog._session_service
    for _ in range(2):
        await sessions.create(SessionCreateRequest(title="Matching Main"))
    first = await thread_search.search_session_threads(
        catalog,
        sessions,
        gateway_id=GATEWAY,
        query="mtc",
        limit=1,
        cursor=None,
    )
    assert first.cursor is not None and first.limited_reason == "result_limit"
    for gateway, query, reason in ((GATEWAY, "other", "query_mismatch"), ("gateway_other", "mtc", "owner_mismatch")):
        with pytest.raises(thread_search.ThreadSearchCursorError, match="cursor 与查询或目录版本不匹配") as rejected:
            await thread_search.search_session_threads(
                catalog,
                sessions,
                gateway_id=gateway,
                query=query,
                limit=1,
                cursor=first.cursor,
            )
        assert rejected.value.reason == reason
        assert rejected.value.code == "invalid_thread_cursor"
    await sessions.create(SessionCreateRequest(title="Later"))
    with pytest.raises(thread_search.ThreadSearchCursorError, match="cursor 与查询或目录版本不匹配") as rejected:
        await thread_search.search_session_threads(
            catalog,
            sessions,
            gateway_id=GATEWAY,
            query="mtc",
            limit=1,
            cursor=first.cursor,
        )
    assert rejected.value.reason == "catalog_revision"
    assert rejected.value.code == "stale_thread_cursor"


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor,reason", [("not-base64!", "malformed"), ("MTIz", "malformed"),
                                         ("e30", "malformed")])
async def test_invalid_thread_cursor_shape_is_named_even_on_empty_catalog(
    catalog: SessionCatalogService, cursor: str, reason: str,
) -> None:
    with pytest.raises(thread_search.ThreadSearchCursorError) as rejected:
        await thread_search.search_session_threads(catalog, catalog._session_service, gateway_id=GATEWAY,
                                                  query="needle", limit=1, cursor=cursor)
    assert rejected.value.reason == reason
    assert rejected.value.detail()["owner"] == "session_thread_search"
    assert rejected.value.code == "invalid_thread_cursor"


@pytest.mark.asyncio
async def test_no_match_is_complete_and_deadline_is_distinct(
    catalog: SessionCatalogService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = catalog._session_service
    await sessions.create(SessionCreateRequest(title="Ordinary"))
    page = await thread_search.search_session_threads(
        catalog,
        sessions,
        gateway_id=GATEWAY,
        query="unique-no-match🧪",
        limit=10,
        cursor=None,
    )
    assert page.items == [] and page.complete and page.cursor is None
    monkeypatch.setattr(thread_search, "_QUERY_SECONDS", 0)
    page = await thread_search.search_session_threads(
        catalog,
        sessions,
        gateway_id=GATEWAY,
        query="unique-no-match🧪",
        limit=10,
        cursor=None,
    )
    assert not page.complete and page.items == []
    assert page.limited_reason == "deadline" and page.cursor is not None
