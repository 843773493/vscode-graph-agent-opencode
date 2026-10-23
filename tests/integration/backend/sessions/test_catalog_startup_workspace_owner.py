"""工作区 catalog 的全部保留记录必须属于其当前后端，历史终态不能被过滤后忽略。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest

from app.container import AppContainer
from app.core import session_subtree_delete
from app.core.identifier import create_prefixed_id
from app.core.session_catalog_store import CatalogMaintenanceRequiredError
from app.core.session_catalog_store.creation_envelope import (
    compute_session_creation_preimage_hash,
)
from app.schemas.internal_v2.session import SessionCreateRequest
from tests.integration.backend.sessions.test_catalog_startup_creation_records import (
    _close_and_files,
    _create_record,
)
from tests.integration.backend.sessions.test_catalog_startup_delete_records import (
    DeletePaused,
    _prepare_delete,
)
from tests.integration.backend.sessions.test_catalog_startup_directories import (
    _catalog_dump,
)
from tests.integration.backend.sessions.test_catalog_startup_identity import (
    startup_container as startup_container,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_navigation_worker_lifespan import (
    _build_owned_container,
)


async def _create_owner_record(
    container: AppContainer, monkeypatch: pytest.MonkeyPatch, owner: str, state: str,
) -> tuple[str, str, str]:
    store = container.session_service.path_resolver.catalog_store
    if owner == "folder":
        operations = container.session_catalog_service.operations
        await operations.start()
        try:
            node_id = await container.session_catalog_service.ensure_folder_path(["只有逻辑目录"])
        finally:
            await operations.stop()
        assert node_id is not None
        if state == "deleting":
            observe = session_subtree_delete.observe_phase

            async def after_mark(operation_id, phase, **kwargs):
                await observe(operation_id, phase, **kwargs)
                if operation_id == "delete-empty-folder" and phase == "deletion.catalog_deleting":
                    raise DeletePaused

            with monkeypatch.context() as interruption:
                interruption.setattr(session_subtree_delete, "observe_phase", after_mark)
                with pytest.raises(DeletePaused):
                    await container.session_service.path_resolver.delete_subtree(
                        idempotency_key="delete-empty-folder", root_node_id=node_id,
                    )
        assert store.get_node(node_id).state == state
        return "nodes", "node_id", node_id
    if owner == "creation":
        record = await _create_record(container, monkeypatch, state)
        return "session_creation_records", "session_creation_idempotency_key", record.session_creation_idempotency_key
    if owner == "delete":
        _, _, key = await _prepare_delete(container, monkeypatch, state)
        return "subtree_delete_records", "subtree_delete_idempotency_key", key
    # 本场景只验证 claim owner 行的启动归属，不冒充完整执行/fork 验收。
    source = await container.session_service.create(SessionCreateRequest(title="保留引用的来源"))
    target = await container.session_service.create(SessionCreateRequest(title="保留引用的目标"))
    claim = store.create_or_get_fork_retention_claim(
        claim_id=create_prefixed_id("fork"), workspace_id=container.workspace_id,
        source_session_id=source.session_id, target_session_id=target.session_id,
        source_lifecycle_generation=1,
    )
    if state == "active":
        store.activate_fork_retention_claim(claim.fork_retention_claim_id, expected_generation=1)
    elif state == "released":
        store.release_fork_retention_claim(claim.fork_retention_claim_id, "原引用显式终结")
    assert store.get_fork_retention_claim(claim.fork_retention_claim_id).state == state
    return "fork_retention_claims", "fork_retention_claim_id", claim.fork_retention_claim_id


@pytest.mark.parametrize("owner,state", [
    ("folder", "active"), ("folder", "deleting"),
    ("creation", "preparing"), ("creation", "published"), ("creation", "aborted"),
    ("delete", "preparing"), ("delete", "completed"),
    ("claim", "preparing"), ("claim", "active"), ("claim", "released"),
])
async def test_container_rejects_retained_record_from_another_workspace(
    startup_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest, owner: str, state: str,
) -> None:
    container = startup_container
    table, key_column, key = await _create_owner_record(container, monkeypatch, owner, state)
    record = (container.session_service.path_resolver.catalog_store.get_creation_record(key)
              if owner == "creation" else None)
    path, files = _close_and_files(container)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(f"UPDATE {table} SET workspace_id=? WHERE {key_column}=?", ("ws-another-owner", key))
        if record is not None:
            # 外来记录即使自身前像一致，也必须与当前后端的 owner 区分。
            preimage = compute_session_creation_preimage_hash(
                workspace_id="ws-another-owner", parent_node_id=record.parent_node_id,
                title=record.display_name, title_source=record.title_source, purpose=record.purpose,
                graph_binding=record.graph_binding, session_metadata=json.loads(record.session_metadata_json),
                request_intent=json.loads(record.request_intent_json) if record.request_intent_json is not None else None,
            )
            connection.execute(
                "UPDATE session_creation_records SET preimage_hash=? WHERE session_creation_idempotency_key=?",
                (preimage, key),
            )
        connection.commit()
    before = _catalog_dump(path)
    with pytest.raises(CatalogMaintenanceRequiredError, match="workspace 不一致") as error:
        _build_owned_container(container.workspace_root, monkeypatch, request)
    assert table in str(error.value) and key in str(error.value)
    assert _catalog_dump(path) == before
    root = container.workspace_root / ".boxteam"
    assert {str(p.relative_to(root)): p.read_bytes() for directory in ("sessions", "orphaned")
            for p in (root / directory).rglob("*") if p.is_file()} == files


async def test_container_keeps_original_workspace_history_after_source_deleted(
    startup_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    container = startup_container
    _, _, key = await _create_owner_record(container, monkeypatch, "claim", "released")
    store = container.session_service.path_resolver.catalog_store
    claim = store.get_fork_retention_claim(key)
    await container.session_service.path_resolver.delete_subtree(
        idempotency_key="delete-released-source", root_node_id=claim.source_session_id,
    )
    path, files = _close_and_files(container)
    before = _catalog_dump(path)
    restarted = _build_owned_container(container.workspace_root, monkeypatch, request)
    cold_store = restarted.session_service.path_resolver.catalog_store
    assert cold_store.get_fork_retention_claim(key) == claim
    with pytest.raises(KeyError):
        cold_store.get_node(claim.source_session_id)
    assert _catalog_dump(path) == before
    root = container.workspace_root / ".boxteam"
    assert {str(p.relative_to(root)): p.read_bytes() for directory in ("sessions", "orphaned")
            for p in (root / directory).rglob("*") if p.is_file()} == files
