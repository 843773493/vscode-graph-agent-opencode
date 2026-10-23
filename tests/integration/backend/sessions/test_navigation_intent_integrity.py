"""产品入口生成的导航意图在读取、重放与冷启动时保持冻结正文。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app.container import AppContainer
from app.core.identifier import create_prefixed_id
from app.core.session_catalog_store import CatalogMaintenanceRequiredError
from app.schemas.internal_v2.session import SessionCreateRequest
from app.schemas.internal_v2.session_navigation.operations import (
    NavigationMutationEnqueueRequest,
)
from app.services.business.session_navigation.operations_service import (
    local_navigation_scope,
)
from app.services.business.session_navigation.queue_store import (
    NavigationMutationRecord,
)
from tests.integration.backend.sessions.test_catalog_startup_creation_records import (
    _close_and_files,
)
from tests.integration.backend.sessions.test_catalog_startup_identity import (
    _catalog_dump,
)
from tests.integration.backend.sessions.test_catalog_startup_identity import (
    startup_container as startup_container,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_catalog_startup_journal_metadata import (
    _assert_body_unchanged,
)
from tests.integration.backend.sessions.test_navigation_worker_lifespan import (
    _build_owned_container,
)
from tests.support.session_navigation_queue import (
    create_catalog_intent,
    submit_catalog_intent,
)


async def _committed_intent(container: AppContainer) -> NavigationMutationRecord:
    await container.session_service.create(SessionCreateRequest(title="导航记录原件"))
    catalog = container.session_catalog_service
    record = await submit_catalog_intent(catalog, create_catalog_intent(catalog, "create_folder", name="冻结目录意图"))
    assert record.state == "committed"
    return record


def _rewrite(path: Path, operation_id: str, changes: dict[str, object]) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "UPDATE navigation_mutation_records SET " + ", ".join(f"{column}=?" for column in changes)
            + " WHERE operation_id=?", (*changes.values(), operation_id),
        )
        connection.commit()


@pytest.mark.parametrize("corruption", [
    "name", "params-list", "missing-param", "extra-param", "expected-type", "base-revision",
    "client-sequence", "operation-key", "gateway", "actor", "workspace", "target-mirror",
    "creator-mirror", "depends", "depends-object", "preimage", "kind",
])
async def test_container_rejects_corrupted_retained_navigation_intent(
    startup_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest, corruption: str,
) -> None:
    container = startup_container
    record = await _committed_intent(container)
    path, files = _close_and_files(container)
    params = dict(record.params)
    if corruption in ("name", "missing-param", "extra-param", "expected-type", "base-revision"):
        if corruption == "name":
            params["name"] = "外部改写的合法目录名"
        elif corruption == "missing-param":
            del params["expected_revision"]
        elif corruption == "extra-param":
            params["unknown"] = True
        else:
            params["expected_revision" if corruption == "expected-type" else "base_catalog_revision"] = "2" if corruption == "expected-type" else -1
        changes = {"params_json": json.dumps(params)}
    else:
        column, value = {
            "params-list": ("params_json", "[]"),
            "client-sequence": ("client_sequence", 1.5),
            "operation-key": ("operation_id", "invalid-operation"),
            "gateway": ("gateway_id", ""),
            "actor": ("actor", ""),
            "workspace": ("workspace_id", "another-workspace"),
            "target-mirror": ("target_node_id", create_prefixed_id("ses")),
            "creator-mirror": ("created_by_operation_id", create_prefixed_id("op")),
            "depends": ("depends_on_json", json.dumps([create_prefixed_id("op")])),
            "depends-object": ("depends_on_json", "{}"),
            "preimage": ("preimage_hash", "0" * 64),
            "kind": ("kind", "rename_node"),
        }[corruption]
        changes = {column: value}
    _rewrite(path, record.operation_id, changes)
    before, raw = _catalog_dump(path), path.read_bytes()
    with pytest.raises(CatalogMaintenanceRequiredError, match="navigation intent 原始记录损坏"):
        _build_owned_container(container.workspace_root, monkeypatch, request)
    assert _catalog_dump(path) == before and path.read_bytes() == raw
    _assert_body_unchanged(container, files)


async def test_read_and_idempotent_replay_reject_modified_frozen_body(
    startup_container: AppContainer,
) -> None:
    container = startup_container
    catalog = container.session_catalog_service
    original = create_catalog_intent(catalog, "create_folder", name="原始幂等意图")
    record = await submit_catalog_intent(catalog, original)
    operations = catalog.operations
    await operations.start()
    try:
        path = catalog.path_resolver.catalog_store.database_path
        _rewrite(path, record.operation_id, {"params_json": json.dumps({**record.params, "name": "合法但不是原意图"})})
        before = _catalog_dump(path)
        scope = local_navigation_scope(catalog.workspace_id)
        with pytest.raises(ValueError, match="preimage"):
            operations.record(record.operation_id, scope)
        with pytest.raises(ValueError, match="preimage"):
            await operations.enqueue(NavigationMutationEnqueueRequest(intents=[original]), scope)
        assert _catalog_dump(path) == before
    finally:
        await operations.stop()


async def test_queued_intent_is_verified_before_worker_can_recover(
    startup_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    container = startup_container
    catalog = container.session_catalog_service
    blocker = asyncio.Event()

    async def pause_drain() -> list:
        await blocker.wait()
        return []

    # 仅暂停调度：durable acceptance仍由产品入口负责，不人工播种队列行。
    with monkeypatch.context() as scheduling:
        scheduling.setattr(catalog.operations, "drain_once", pause_drain)
        await catalog.operations.start()
        try:
            intent = create_catalog_intent(catalog, "create_folder", name="尚未执行")
            result = await catalog.operations.enqueue(
                NavigationMutationEnqueueRequest(intents=[intent]), local_navigation_scope(catalog.workspace_id),
            )
            assert result.receipts[0].state == "queued"
        finally:
            await catalog.operations.stop()
    path, files = _close_and_files(container)
    _rewrite(path, intent.client_operation_id, {"preimage_hash": "0" * 64})
    before = _catalog_dump(path)
    with pytest.raises(CatalogMaintenanceRequiredError, match="preimage"):
        _build_owned_container(container.workspace_root, monkeypatch, request)
    assert _catalog_dump(path) == before
    _assert_body_unchanged(container, files)


async def test_healthy_intents_keep_original_cas_and_hash_after_node_changes(
    startup_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    container = startup_container
    catalog = container.session_catalog_service
    session = await container.session_service.create(SessionCreateRequest())
    await catalog.operations.start()
    try:
        result = await catalog.auto_title_session(
            session.session_id, expected_title=session.title, title="自动标题冻结意图",
        )
        assert result.session.title == "自动标题冻结意图"
    finally:
        await catalog.operations.stop()
    created = await _committed_intent(container)
    renamed = await submit_catalog_intent(catalog, create_catalog_intent(
        catalog, "rename_node", target_node_id=created.result_node_id, name="后来更名",
    ))
    assert renamed.state == "committed"
    path, files = _close_and_files(container)
    before = _catalog_dump(path)
    restarted = _build_owned_container(container.workspace_root, monkeypatch, request)
    operations = restarted.session_catalog_service.operations
    scope = local_navigation_scope(catalog.workspace_id)
    assert operations.record(created.operation_id, scope) == created
    assert operations.record(renamed.operation_id, scope) == renamed
    assert _catalog_dump(path) == before
    _assert_body_unchanged(container, files)
