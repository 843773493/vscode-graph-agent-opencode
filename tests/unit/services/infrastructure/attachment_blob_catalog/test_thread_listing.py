"""精确 Thread 目录只投影附件元数据，不读取或重建原件。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI

from app.abstractions.turn_history import TurnSessionLookupProtocol
from app.api import messages
from app.api.deps import (
    get_attachment_blob_store,
    get_session_service,
    verify_local_token,
)
from app.core.resource_uri import parse_resource_uri
from app.core.trace_middleware import TraceMiddleware
from app.schemas.internal_v2.message import AttachmentRef
from app.services.infrastructure.attachment_blob_catalog.store import (
    AttachmentBlobStore,
)
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)

SESSION = "ses_019b781159d47374805bc4427d081a14"
PEER = "ses_019b9cdd1ad5758788a18bb227323523"


@dataclass(frozen=True)
class ListingCase:
    store: AttachmentBlobStore
    thread: str
    refs: list[AttachmentRef]
    leases: ExternalResourceLeaseLedger


@pytest.fixture
async def listing_case(empty_workspace_scope, session_bundle_factory) -> AsyncIterator[ListingCase]:
    _, root = empty_workspace_scope
    for identity in (SESSION, PEER):
        session_bundle_factory(root / ".boxteam/sessions", identity)
    leases = ExternalResourceLeaseLedger(state_path=root / ".boxteam/resources.json")
    with closing(AttachmentBlobStore(root, leases=leases)) as store:
        thread = store._path_resolver.main_thread_id(SESSION)
        refs = []
        for ordinal in range(3):
            ref, = await store.persist_inline(SESSION, [AttachmentRef(
                file_id="inline:same.txt", name="同名.txt", content_type="text/plain",
                data_url="data:text/plain;base64,Y3dk",
            )], thread_id=thread, message_id=f"msg_listing_{ordinal}")
            refs.append(ref)
        await store.persist_inline(PEER, [AttachmentRef(
            file_id="inline:peer.txt", name="其它会话.txt", content_type="text/plain",
            data_url="data:text/plain;base64,Y3dk",
        )], thread_id=store._path_resolver.main_thread_id(PEER), message_id="msg_peer")
        yield ListingCase(store, thread, refs, leases)


async def test_pages_preserve_upload_identity_owner_and_released_sources(listing_case: ListingCase, monkeypatch: pytest.MonkeyPatch) -> None:
    case = listing_case
    assert len({ref.file_id for ref in case.refs}) == 3
    case.store.release_session_references(SESSION)
    for ref in case.refs:
        attachment_id = parse_resource_uri(ref.file_id).attachment_id
        case.leases.register(resource_id=attachment_id, kind="attachment_directory", lifetime_scope="workspace")
    assert len(await case.store.process_pending_directories()) == 3
    directories = [case.store.resolve_directory_uri(parse_resource_uri(ref.directory_uri), materialize=False) for ref in case.refs]
    assert not any(path.exists() for path in directories)

    queries: list[str] = []
    original = case.store._catalog.thread_originals

    def traced(*args, **kwargs):
        connection = case.store._catalog.connection
        connection.set_trace_callback(queries.append)
        try:
            return original(*args, **kwargs)
        finally:
            connection.set_trace_callback(None)

    monkeypatch.setattr(case.store._catalog, "thread_originals", traced)
    first = await case.store.list_thread_attachments(SESSION, case.thread, limit=2)
    assert len(queries) == 1 and queries[0].startswith("SELECT ")
    assert len(first.items) == 2 and first.next_after
    second = await case.store.list_thread_attachments(SESSION, case.thread, limit=2, after=first.next_after)
    assert len(second.items) == 1 and second.next_after is None
    assert {item.file_id for item in [*first.items, *second.items]} == {ref.file_id for ref in case.refs}
    assert all(item.name == "同名.txt" and item.availability == "available" for item in [*first.items, *second.items])
    assert (await case.store.list_thread_attachments(SESSION, case.store._path_resolver.main_thread_id(PEER))).items == []
    assert not any(path.exists() for path in directories)


@pytest.fixture
async def listing_http(listing_case: ListingCase) -> AsyncIterator[tuple[httpx.AsyncClient, Mock]]:
    case = listing_case
    service = Mock(spec=TurnSessionLookupProtocol)
    service.resolve_thread = AsyncMock(return_value=case.thread)

    @asynccontextmanager
    async def admission(session_id: str):
        assert session_id == SESSION
        yield

    service.session_read_admission.side_effect = admission
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.include_router(messages.router, prefix="/api/v1")
    application.dependency_overrides[verify_local_token] = lambda: "local"
    application.dependency_overrides[get_session_service] = lambda: service
    application.dependency_overrides[get_attachment_blob_store] = lambda: case.store
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://listing",
                                 headers={"X-Request-ID": "thread-attachment-listing"}) as client:
        yield client, service


async def test_http_uses_exact_thread_and_returns_real_attachment_metadata(listing_http, listing_case: ListingCase) -> None:
    client, service = listing_http
    response = await client.get(f"/api/v1/sessions/{SESSION}/threads/{listing_case.thread}/attachments?limit=2")
    assert response.status_code == 200, response.text
    body = response.json()
    assert response.headers["X-Request-ID"] == body["request_id"] == "thread-attachment-listing"
    service.resolve_thread.assert_awaited_once_with(SESSION, listing_case.thread)
    assert body["data"]["session_id"] == SESSION and body["data"]["thread_id"] == listing_case.thread
    assert len(body["data"]["items"]) == 2 and body["data"]["next_after"]
    assert all(item["file_id"] in {ref.file_id for ref in listing_case.refs} and "data_url" not in item for item in body["data"]["items"])


@pytest.mark.parametrize("query", ["limit=0", "limit=201", "after=../other", "after=att_123"])
async def test_http_rejects_invalid_page_before_storage(listing_http, listing_case: ListingCase, query: str) -> None:
    client, service = listing_http
    response = await client.get(f"/api/v1/sessions/{SESSION}/threads/{listing_case.thread}/attachments?{query}")
    assert response.status_code == 422
    service.resolve_thread.assert_not_awaited()
