"""产品删除入口产生冻结 journal，冷启动先验记录自洽，保留原件和合法中间态。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app.container import AppContainer
from app.core import session_subtree_delete
from app.core.identifier import create_prefixed_id
from app.core.session_catalog_store import (
    CatalogMaintenanceRequiredError,
    uuid7_embedded_utc_date,
)
from app.schemas.internal_v2.session import SessionCreateRequest
from tests.integration.backend.sessions.test_catalog_startup_directories import (
    _catalog_dump,
)
from tests.integration.backend.sessions.test_catalog_startup_identity import (
    startup_container as startup_container,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_navigation_worker_lifespan import (
    _build_owned_container,
)


class DeletePaused(BaseException):
    """在真实事务/隔离边界中断，不替产品预写进度或回滚。"""


async def _prepare_delete(
    container: AppContainer, monkeypatch: pytest.MonkeyPatch, state: str,
) -> tuple[str, str, str]:
    root = await container.session_service.create(SessionCreateRequest(title="删除记录的根"))
    child = await container.session_service.create(SessionCreateRequest(title="删除记录的子会话", folder_id=root.session_id))
    resolver = container.session_service.path_resolver
    store = resolver.catalog_store
    key = "startup-delete-journal"
    original_observe = session_subtree_delete.observe_phase
    original_progress = store.record_drain_progress

    def before_mark(*args, **kwargs):
        raise DeletePaused

    async def after_mark(operation_id, phase, **kwargs):
        await original_observe(operation_id, phase, **kwargs)
        if operation_id == key and phase == "deletion.catalog_deleting":
            raise DeletePaused

    def after_progress(*args, **kwargs):
        original_progress(*args, **kwargs)
        raise DeletePaused

    with monkeypatch.context() as interruption:
        if state == "preparing":
            interruption.setattr(store, "mark_subtree_deleting", before_mark)
        elif state == "deleting":
            interruption.setattr(session_subtree_delete, "observe_phase", after_mark)
        elif state == "draining":
            interruption.setattr(store, "record_drain_progress", after_progress)
        if state == "completed":
            await resolver.delete_subtree(idempotency_key=key, root_node_id=root.session_id)
        else:
            with pytest.raises(DeletePaused):
                await resolver.delete_subtree(idempotency_key=key, root_node_id=root.session_id)
    record = store.get_subtree_delete_record(key)
    assert record.state == state
    assert set(record.frozen_session_locators) == {root.session_id, child.session_id}
    if state == "draining":
        assert len(record.drained_session_ids) == 1
    return root.session_id, child.session_id, key


def _close_and_snapshot(container: AppContainer) -> tuple[Path, str, dict[str, bytes]]:
    path = container.session_service.path_resolver.catalog_store.database_path
    container.workspace_activity_service.store.close()
    container.session_catalog.close()
    root = container.workspace_root / ".boxteam/sessions"
    return path, _catalog_dump(path), {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("state,corruption", [
    ("deleting", "missing-root"),
    ("completed", "empty-frozen"),
    ("completed", "locator-outside-frozen"),
    ("completed", "locator-owner-mismatch"),
    ("completed", "drained-outside-frozen"),
    ("completed", "incomplete-drain"),
    ("deleting", "premature-drain"),
    ("draining", "empty-progress"),
    ("completed", "malformed-frozen"),
])
async def test_container_rejects_corrupt_delete_journal_before_recovery(
    startup_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest, state: str, corruption: str,
) -> None:
    container = startup_container
    root_id, child_id, key = await _prepare_delete(container, monkeypatch, state)
    record = container.session_service.path_resolver.catalog_store.get_subtree_delete_record(key)
    path, _, files = _close_and_snapshot(container)
    outsider = create_prefixed_id("ses")
    if corruption == "missing-root":
        column, value = "root_node_id", outsider
    elif corruption in ("empty-frozen", "malformed-frozen"):
        column, value = "frozen_node_ids", "[]" if corruption == "empty-frozen" else "{broken"
    elif corruption in ("locator-outside-frozen", "locator-owner-mismatch"):
        column = "frozen_session_locators"
        locators = dict(record.frozen_session_locators)
        if corruption == "locator-outside-frozen":
            # 使用真实合法的 UUIDv7 日期 locator，避免仅因形态错误被拒绝。
            locators[outsider] = f"sessions/{uuid7_embedded_utc_date(outsider[4:]):%Y/%m/%d}/{outsider}"
        else:
            locators[root_id] = locators[child_id]
        value = json.dumps([{"session_id": sid, "storage_relative_locator": locator} for sid, locator in locators.items()])
    else:
        column = "drained_session_ids"
        ids = [outsider] if corruption == "drained-outside-frozen" else ([root_id] if corruption == "premature-drain" else [])
        value = json.dumps(ids)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(f"UPDATE subtree_delete_records SET {column}=? WHERE subtree_delete_idempotency_key=?", (value, key))
        connection.commit()
    before = _catalog_dump(path)
    with pytest.raises(CatalogMaintenanceRequiredError, match="subtree delete record"):
        _build_owned_container(container.workspace_root, monkeypatch, request)
    assert _catalog_dump(path) == before
    sessions_root = container.workspace_root / ".boxteam/sessions"
    assert {str(p.relative_to(sessions_root)): p.read_bytes() for p in sessions_root.rglob("*") if p.is_file()} == files


@pytest.mark.parametrize("state", [
    "preparing", "deleting", "draining", "completed", "preparing-drift", "aborted-preparing", "aborted-partial",
])
async def test_container_preserves_valid_delete_journal_and_cold_owner_recovery(
    startup_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest, state: str,
) -> None:
    container = startup_container
    initial = "draining" if state == "aborted-partial" else ("preparing" if "preparing" in state else state)
    root_id, _, key = await _prepare_delete(container, monkeypatch, initial)
    store = container.session_service.path_resolver.catalog_store
    if state == "preparing-drift":
        # 合法导航变化不改写已冻结的 preparing 前像；后续 mark CAS 自己裁决冲突。
        store.rename_node(root_id, "冻结后重新命名")
    if state.startswith("aborted-"):
        store.abort_subtree_delete(key, "人工终结原删除意图")
    frozen = store.get_subtree_delete_record(key)
    path, before, files = _close_and_snapshot(container)
    restarted = _build_owned_container(container.workspace_root, monkeypatch, request)
    resolver = restarted.session_service.path_resolver
    assert resolver.catalog_store.get_subtree_delete_record(key) == frozen
    assert _catalog_dump(path) == before
    sessions_root = container.workspace_root / ".boxteam/sessions"
    assert {str(p.relative_to(sessions_root)): p.read_bytes() for p in sessions_root.rglob("*") if p.is_file()} == files
    if state.startswith("aborted-"):
        with pytest.raises(RuntimeError, match="已中止"):
            await resolver.delete_subtree(idempotency_key=key, root_node_id=root_id)
        return
    if state == "preparing-drift":
        with pytest.raises(RuntimeError, match="revision 已漂移"):
            await resolver.delete_subtree(idempotency_key=key, root_node_id=root_id)
        assert resolver.catalog_store.get_subtree_delete_record(key).state == "aborted"
        assert resolver.catalog_store.get_node(root_id).state == "active"
        return
    result = await resolver.delete_subtree(idempotency_key=key, root_node_id=root_id)
    assert result.record_state == "completed"
    recovered = resolver.catalog_store.get_subtree_delete_record(key)
    assert recovered.frozen_node_ids == frozen.frozen_node_ids
    assert recovered.frozen_session_locators == frozen.frozen_session_locators
    assert set(recovered.drained_session_ids) == set(frozen.frozen_session_locators)
