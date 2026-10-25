"""OwnerThreadCreationFactory 的 SQLite catalog authority 契约测试。"""

from __future__ import annotations

import asyncio
import gc
import sqlite3
import threading
from pathlib import Path

import pytest

from app.core.path_utils import get_session_path_resolver
from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.core.session_catalog_store import SessionCatalogStore
from app.core.session_creation import SessionCreationService
from app.core.session_lifecycle_gate import NavigationTopologyGate
from app.services.orchestration import owner_thread_creation_factory as factory_module
from app.services.orchestration.owner_thread_creation_factory import (
    OwnerThreadCreationFactory,
)
from tests.support.graph_binding import TEST_THREAD_GRAPH_BINDING

SESSION_ID = "ses_019bfb6b5f3c72178c7e8393ce89e7a6"

WORKSPACE_ID = "0197d9a3-7d2a-7c29-8d76-58b3cf3f8a21"

def _sessions_root(tmp_path: Path, label: str) -> Path:
    root = tmp_path / label / ".boxteam" / "sessions"
    root.mkdir(parents=True)
    return root


def test_factory_uses_catalog_resolver(tmp_path: Path) -> None:
    sessions_root = _sessions_root(tmp_path, "catalog-default")
    factory = OwnerThreadCreationFactory(
        sessions_root=sessions_root,
        workspace_id="ws_catalog",
    )
    assert isinstance(factory._resolver, SessionCatalogPathResolver)
    # catalog 权威下入口不因 resolver 模式拒绝，按 catalog 内容正常工作
    #（空 catalog 查未知节点 → 显式 KeyError）。
    with pytest.raises(KeyError, match="会话目录节点不存在"):
        factory.validate_owner_thread(SESSION_ID, "thr_019bfb6b5f3c72178c7e8393ce89e7a7")


async def _create_sessions(sessions_root: Path, count: int) -> list[str]:
    catalog = SessionCatalogStore(
        sessions_root.parent / "navigation" / "session-catalog.sqlite",
        sessions_root,
    )
    creation = SessionCreationService(
        store=catalog,
        sessions_root=sessions_root,
        workspace_id=WORKSPACE_ID,
        gate=NavigationTopologyGate(sessions_root),
    )
    session_ids: list[str] = []
    for index in range(count):
        record = await creation.prepare(
            idempotency_key=f"owner-key-{index}",
            title="owner",
            purpose="normal",
            parent_node_id=None,
            graph_binding=TEST_THREAD_GRAPH_BINDING,
            session_metadata={
                "kind": "normal",
                "delegation": None,
                "generation_origin": None,
                "current_agent_id": "default",
                "current_provider_id": "default_provider",
                "context_source_session_id": None,
            },
        )
        result = await creation.publish_prepared(record)
        session_ids.append(result.session_id)
    catalog.close()
    return session_ids


@pytest.fixture
async def owner_factory(tmp_path: Path) -> tuple[OwnerThreadCreationFactory, list[str]]:
    sessions_root = _sessions_root(tmp_path, "scoped")
    session_ids = await _create_sessions(sessions_root, 2)
    return OwnerThreadCreationFactory(
        sessions_root=sessions_root, workspace_id=WORKSPACE_ID,
        path_resolver=get_session_path_resolver(sessions_root),
    ), session_ids


def _assert_physically_closed(control) -> None:
    # 保留强引用并直接访问原连接，不能只证明closed标记或依赖GC时机。
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        control._connection.execute("SELECT 1")


async def test_sequential_owners_close_connections_without_gc(tmp_path: Path) -> None:
    sessions_root = _sessions_root(tmp_path, "bounded")
    session_ids = await _create_sessions(sessions_root, 104)
    factory = OwnerThreadCreationFactory(
        sessions_root=sessions_root, workspace_id=WORKSPACE_ID,
        path_resolver=get_session_path_resolver(sessions_root),
    )
    retained = []
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        for session_id in session_ids:
            with factory.for_owner_session(session_id) as owner:
                control = owner.control_store
                assert control.get_fence() == ("active", 1)
                retained.append(owner)
            _assert_physically_closed(control)
        for owner in retained:
            _assert_physically_closed(owner.control_store)
        assert not factory._controls
    finally:
        if was_enabled:
            gc.enable()


@pytest.mark.parametrize("error", [None, RuntimeError("scope-failed"), asyncio.CancelledError("scope-cancelled")])
async def test_scope_exit_closes_original_connection(owner_factory, error: BaseException | None) -> None:
    factory, session_ids = owner_factory
    control = None
    try:
        with factory.for_owner_session(session_ids[0]) as owner:
            control = owner.control_store
            if error is not None:
                raise error
    except (RuntimeError, asyncio.CancelledError) as actual:
        assert actual is error
    else:
        assert error is None
    assert control is not None
    _assert_physically_closed(control)
    assert not factory._controls


async def test_nested_same_owner_scopes_release_only_their_connection(owner_factory) -> None:
    factory, session_ids = owner_factory
    with factory.for_owner_session(session_ids[0]) as outer:
        with factory.for_owner_session(session_ids[0]) as inner:
            assert outer.control_store is not inner.control_store
            assert inner.control_store.get_fence() == ("active", 1)
        _assert_physically_closed(inner.control_store)
        assert outer.control_store.get_fence() == ("active", 1)
    _assert_physically_closed(outer.control_store)


async def test_task_cancellation_closes_after_original_work_finishes(owner_factory) -> None:
    factory, session_ids = owner_factory
    started = asyncio.Event()
    controls = []

    async def operation() -> None:
        with factory.for_owner_session(session_ids[0]) as owner:
            controls.append(owner.control_store)
            started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(operation())
    await started.wait()
    assert controls[0].get_fence() == ("active", 1)
    with factory.for_owner_session(session_ids[1]) as peer:
        assert peer.control_store.get_fence() == ("active", 1)
    assert controls[0].get_fence() == ("active", 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    _assert_physically_closed(controls[0])
    assert not factory._controls


async def test_service_construction_failure_closes_control(owner_factory, monkeypatch: pytest.MonkeyPatch) -> None:
    factory, session_ids = owner_factory
    original = RuntimeError("construction-failed")
    controls = []

    def fail_construction(**kwargs):
        controls.append(kwargs["control_store"])
        raise original

    monkeypatch.setattr(factory_module, "ThreadCreationService", fail_construction)
    with pytest.raises(RuntimeError) as caught, factory.for_owner_session(session_ids[0]):
        pytest.fail("构造失败不能发布owner")
    assert caught.value is original
    _assert_physically_closed(controls[0])
    assert not factory._controls


async def test_publication_binding_updates_only_active_scopes(owner_factory) -> None:
    factory, session_ids = owner_factory
    callback = lambda _record: None
    lock = threading.RLock()
    with factory.for_owner_session(session_ids[0]) as first:
        factory.bind_collaboration_publication(callback, lock)
        assert first.control_store._collaboration_publication is callback
        with factory.for_owner_session(session_ids[1]) as second:
            assert second.control_store._collaboration_publication is callback
            assert second.control_store._publication_lock is lock
        _assert_physically_closed(second.control_store)
    _assert_physically_closed(first.control_store)
