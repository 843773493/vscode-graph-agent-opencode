from pathlib import Path

import pytest

from app.core.identifier import create_prefixed_id
from app.core.path_utils import get_session_creation_service, get_session_path_resolver
from app.services.business.session_navigation.catalog_query import encode_catalog_cursor
from app.services.business.session_navigation.service import SessionCatalogService
from app.services.business.session_service import SessionService
from app.services.infrastructure.trace_event_store import TraceEventStore
from tests.unit.services.business.session_service_test_support import (
    SessionServiceConfigStub,
)


@pytest.fixture
def catalog(tmp_path: Path) -> SessionCatalogService:
    root = tmp_path / "sessions"
    resolver = get_session_path_resolver(root)
    service = SessionService(
        config_service=SessionServiceConfigStub(),
        trace_event_store=TraceEventStore(sessions_dir=root),
        workspace_id=resolver.workspace_id,
        path_resolver=resolver,
        creation_service=get_session_creation_service(root),
    )
    return SessionCatalogService(session_service=service)


@pytest.mark.asyncio
async def test_fuzzy_directory_recall_precedes_paging_and_preserves_cursors(
    catalog: SessionCatalogService,
) -> None:
    store = catalog._path_resolver.catalog_store
    workspace_id = catalog._path_resolver.workspace_id
    for index in range(105):
        store.create_folder(
            create_prefixed_id("ses"), workspace_id, None, f"Unrelated {index:03d}"
        )
    parent = store.create_folder(
        create_prefixed_id("ses"), workspace_id, None, "Zebra Reports"
    )
    alpha = store.create_folder(
        create_prefixed_id("ses"), workspace_id, parent.node_id, "Alpha Delta"
    )
    amber = store.create_folder(
        create_prefixed_id("ses"), workspace_id, parent.node_id, "Amber Dusk"
    )
    initial = await catalog.list_children(parent_node_id=None, limit=100, cursor=None)
    assert parent.node_id not in {node.node_id for node in initial.items}

    first = await catalog.search(query=" ZR/AD ", limit=1, cursor=None)
    assert first.total == 2
    assert [hit.node.node_id for hit in first.items] == [alpha.node_id]
    assert first.items[0].relative_path == "Zebra Reports/Alpha Delta"
    assert first.cursor is not None
    second = await catalog.search(query="zr/ad", limit=1, cursor=first.cursor)
    assert second.total == 2
    assert [hit.node.node_id for hit in second.items] == [amber.node_id]
    assert second.cursor is None
    assert all(
        hit.node.kind == "folder" and hit.node.session is None
        for hit in [*first.items, *second.items]
    )
    with pytest.raises(ValueError, match="cursor 与当前列表不匹配"):
        await catalog.search(query="zr/am", limit=1, cursor=first.cursor)
    legacy_cursor = encode_catalog_cursor(
        1, first.revision, first.catalog_revision, "search\nzr/ad"
    )
    with pytest.raises(ValueError, match="cursor 与当前列表不匹配"):
        await catalog.search(query="zr/ad", limit=1, cursor=legacy_cursor)

    store.rename_node(parent.node_id, "Other Reports")
    with pytest.raises(ValueError, match="会话目录已更新"):
        await catalog.search(query="zr/ad", limit=1, cursor=first.cursor)
    assert (await catalog.search(query="zr/ad", limit=10, cursor=None)).total == 0
    assert (await catalog.search(query="or/ad", limit=10, cursor=None)).total == 2
    store.set_node_state(parent.node_id, "deleting")
    assert (await catalog.search(query="or/ad", limit=10, cursor=None)).total == 0


@pytest.mark.asyncio
async def test_fuzzy_name_id_and_unicode_do_not_join_unrelated_fields(
    catalog: SessionCatalogService,
) -> None:
    resolver = catalog._path_resolver
    folder = resolver.catalog_store.create_folder(
        create_prefixed_id("ses"), resolver.workspace_id, None, "Straße🧪Target"
    )
    for query in ("STRASSE🧪T", "sße🧪t", folder.node_id[::2]):
        result = await catalog.search(query=query, limit=10, cursor=None)
        assert [hit.node.node_id for hit in result.items] == [folder.node_id]
    assert (await catalog.search(query="targetses_", limit=10, cursor=None)).total == 0
