"""main 创建 selector 的冻结、重入与 staging 完整性合同。"""

from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from app.core.session_catalog_store import SessionCatalogStore
from app.core.session_control_store import SessionControlStore
from app.core.session_creation import SessionCreationService
from tests.support.graph_binding import TEST_THREAD_GRAPH_BINDING


@pytest.fixture
def creation(tmp_path: Path):
    sessions = tmp_path / "sessions"
    store = SessionCatalogStore(
        tmp_path / "navigation/session-catalog.sqlite", sessions
    )
    yield (
        SessionCreationService(
            store=store, sessions_root=sessions, workspace_id="ws-graph"
        ),
        store,
        sessions,
    )
    store.close()


def _request():
    return {
        "idempotency_key": "graph-frozen",
        "title": "冻结图",
        "parent_node_id": None,
        "graph_binding": TEST_THREAD_GRAPH_BINDING,
        "session_metadata": {
            "kind": "normal",
            "delegation": None,
            "generation_origin": None,
            "current_agent_id": "default",
            "current_provider_id": None,
            "context_source_session_id": None,
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("graph_id", "another-graph"),
        ("graph_revision", 2),
        ("graph_schema_hash", "sha256:" + "c" * 64),
        ("capability_profile_hash", "sha256:" + "d" * 64),
    ],
)
async def test_same_creation_key_rejects_each_selector_drift(creation, field, value):
    service, store, _sessions = creation
    request = _request()
    record = await service.prepare(**request)
    changed = replace(TEST_THREAD_GRAPH_BINDING, **{field: value})
    with pytest.raises(RuntimeError, match="preimage 冲突"):
        await service.prepare(**{**request, "graph_binding": changed})
    assert store.get_creation_record("graph-frozen") == record
    assert record.graph_binding == TEST_THREAD_GRAPH_BINDING
    changed_record = await service.prepare(
        **{**request, "idempotency_key": "another-key", "graph_binding": changed}
    )
    assert changed_record.preimage_hash != record.preimage_hash


@pytest.mark.asyncio
async def test_rebuilt_staging_uses_frozen_selector_and_keeps_business_manifest(
    creation,
):
    service, store, sessions = creation
    request = _request()
    record = await service.prepare(**request)
    shutil.rmtree(sessions / ".staging" / record.session_id)
    assert await service.prepare(**request) == record
    result = await service.publish_prepared(record)
    control = SessionControlStore(
        sessions
        / result.storage_relative_locator.removeprefix("sessions/")
        / "session-control.sqlite"
    )
    try:
        assert (
            control.get_thread_graph_binding(record.main_thread_id)
            == record.graph_binding
        )
    finally:
        control.close()
    assert set(json.loads(record.session_metadata_json)) == set(
        request["session_metadata"]
    )
    assert (
        store.get_creation_record("graph-frozen").graph_binding == record.graph_binding
    )


@pytest.mark.asyncio
async def test_staging_selector_tampering_fails_before_publish(creation):
    service, store, sessions = creation
    record = await service.prepare(**_request())
    control_path = sessions / ".staging" / record.session_id / "session-control.sqlite"
    connection = sqlite3.connect(control_path)
    with connection:
        connection.execute(
            "UPDATE thread_catalog SET graph_revision = graph_revision + 1 WHERE kind = 'main'"
        )
    connection.close()
    with pytest.raises(RuntimeError, match="GraphBinding 与冻结 record 不一致"):
        await service.publish_prepared(record)
    assert store.get_creation_record("graph-frozen").state == "preparing"
    with pytest.raises(KeyError):
        store.get_node(record.session_id)
