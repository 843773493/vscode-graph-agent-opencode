"""真实 lifespan 在恢复和 GC 前校验日期目录归属，不吸收未知正文。"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
from contextlib import closing, contextmanager
from pathlib import Path

import portalocker
import pytest

from app import main
from app.container import AppContainer
from app.core import session_subtree_delete
from app.core.identifier import create_prefixed_id
from app.core.session_catalog_store import CatalogMaintenanceRequiredError
from app.core.session_lifecycle_gate import NavigationTopologyGate
from app.schemas.internal_v2.session import SessionCreateRequest
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.test_catalog_startup_identity import (
    startup_container as startup_container,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_navigation_worker_lifespan import (
    _build_owned_container,
    _isolate_non_navigation_services,
)


def _catalog_dump(path: Path) -> str:
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        return "\n".join(connection.iterdump())


def _restart_for_lifespan(
    container: AppContainer, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest,
) -> tuple[AppContainer, list[str]]:
    container.workspace_activity_service.store.close()
    container.session_catalog.close()
    restarted = _build_owned_container(container.workspace_root, monkeypatch, request)
    _isolate_non_navigation_services(restarted, monkeypatch)
    monkeypatch.setattr(main, "configure_application_logging", lambda **_: None)
    monkeypatch.setattr(main, "_model_stream_controller", None)
    catalog = restarted.session_service.path_resolver.catalog_store
    verify = catalog.verify_registered_date_directories
    lock_path = NavigationTopologyGate(restarted.session_catalog.sessions_root).lock_path
    observed = []

    def verify_under_topology_guard():
        # 使用独立 fd，确认真实目录检查持有 OS topology guard，而非内存标志。
        with lock_path.open("a+b") as handle, pytest.raises(portalocker.AlreadyLocked):
            portalocker.lock(handle, portalocker.LOCK_EX | portalocker.LOCK_NB)
        observed.append("topology-held")
        verify()

    monkeypatch.setattr(catalog, "verify_registered_date_directories", verify_under_topology_guard)
    return restarted, observed


@pytest.mark.parametrize("rogue", ["unknown-day", "unknown-leaf", "date-symlink"])
async def test_lifespan_rejects_unregistered_session_directories_without_repair(
    startup_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest, rogue: str,
) -> None:
    container = startup_container
    session = await container.session_service.create(SessionCreateRequest(title="已登记的真实会话"))
    resolver = container.session_service.path_resolver
    source = resolver.resolve_session_node(session.session_id)
    manifest = (source / "session.json").read_bytes()
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    if rogue == "date-symlink":
        target = context.artifacts_dir / f"external-date-{session.session_id}"
        target.mkdir()
        unexpected = resolver.sessions_root / "2099"
        unexpected.symlink_to(target, target_is_directory=True)
        marker = target / "untouched.txt"
    else:
        unexpected = ((resolver.sessions_root / "2099/12/31") if rogue == "unknown-day"
                      else source.with_name(create_prefixed_id("ses")))
        unexpected.mkdir(parents=True)
        marker = unexpected / "untouched.txt"
    marker.write_bytes(b"unregistered-original-bytes\n")
    restarted, observed = _restart_for_lifespan(container, monkeypatch, request)
    catalog_path = restarted.session_service.path_resolver.catalog_store.database_path
    before = _catalog_dump(catalog_path)
    with pytest.raises(CatalogMaintenanceRequiredError, match="日期目录|Session 目录"):
        async with main.lifespan(main.app):
            pytest.fail("未知日期树必须在服务 ready 前拒绝，不能被恢复或 GC 吸收")
    assert observed == ["topology-held"]
    assert _catalog_dump(catalog_path) == before
    assert unexpected.is_symlink() if rogue == "date-symlink" else unexpected.is_dir()
    assert marker.read_bytes() == b"unregistered-original-bytes\n"
    assert (source / "session.json").read_bytes() == manifest
    assert restarted.session_catalog_service.operations.worker_state == "stopped"


class CreationPaused(BaseException):
    """保留真实普通创建的 staging/rename 窗口，不执行异常补偿。"""


@pytest.mark.parametrize("renamed", [False, True])
async def test_lifespan_accepts_registered_preparing_creation_directories(
    startup_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest, renamed: bool,
) -> None:
    container = startup_container
    creation = container.session_catalog.creation_service
    rename = creation._rename_staging_to_target
    records = []

    def pause_rename(record, metadata):
        records.append(record)
        if renamed:
            rename(record, metadata)
        raise CreationPaused

    with monkeypatch.context() as interruption:
        interruption.setattr(creation, "_rename_staging_to_target", pause_rename)
        with pytest.raises(CreationPaused):
            await container.session_service.create(SessionCreateRequest(title="尚未发布的合法会话"))
    frozen, = records
    original_directory = container.session_service.path_resolver.resolve_prepared_session_node(frozen)
    manifest = (original_directory / "session.json").read_bytes()
    restarted, observed = _restart_for_lifespan(container, monkeypatch, request)
    async with main.lifespan(main.app):
        assert observed == ["topology-held"]
        catalog = restarted.session_service.path_resolver.catalog_store
        assert catalog.get_creation_record(frozen.session_creation_idempotency_key) == frozen
        assert original_directory.is_dir()
        assert (original_directory / "session.json").read_bytes() == manifest
        with pytest.raises(KeyError):
            catalog.get_node(frozen.session_id)
        assert restarted.session_catalog_service.operations.worker_state == "running"


@pytest.mark.parametrize("resurrected", [False, True], ids=["empty-date", "deleted-directory-reappeared"])
async def test_lifespan_checks_empty_date_bucket_after_real_subtree_delete(
    startup_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest, resurrected: bool,
) -> None:
    container = startup_container
    session = await container.session_service.create(SessionCreateRequest(title="即将删除的会话"))
    resolver = container.session_service.path_resolver
    directory = resolver.resolve_session_node(session.session_id)
    manifest = (directory / "session.json").read_bytes()
    # 由真实删除 owner 留下空日期桶和精确 journal；启动不得把它当作外部孤儿。
    await resolver.delete_subtree(idempotency_key="startup-empty-date", root_node_id=session.session_id)
    date_bucket = resolver.sessions_root / f"{session.created_at:%Y/%m/%d}"
    assert not directory.exists() and date_bucket.is_dir() and not list(date_bucket.iterdir())
    if resurrected:
        # 历史 creation/delete 只能证明日期桶，不能重新授权 tombstone 的旧 leaf。
        directory.mkdir()
        (directory / "session.json").write_bytes(manifest)
    restarted, observed = _restart_for_lifespan(container, monkeypatch, request)
    if resurrected:
        catalog_path = restarted.session_service.path_resolver.catalog_store.database_path
        before = _catalog_dump(catalog_path)
        with pytest.raises(CatalogMaintenanceRequiredError, match="Session 目录"):
            async with main.lifespan(main.app):
                pytest.fail("被删除的目录不能因历史 creation/delete 记录重新成为合法正文")
        assert observed == ["topology-held"]
        assert _catalog_dump(catalog_path) == before
        assert (directory / "session.json").read_bytes() == manifest
        return
    async with main.lifespan(main.app):
        assert observed == ["topology-held"]
        catalog = restarted.session_service.path_resolver.catalog_store
        assert catalog.get_subtree_delete_record("startup-empty-date").state == "completed"
        with pytest.raises(KeyError):
            catalog.get_node(session.session_id)
        assert date_bucket.is_dir() and not list(date_bucket.iterdir())


async def test_cancelled_lifespan_holds_topology_until_directory_worker_finishes(
    startup_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    restarted, observed = _restart_for_lifespan(startup_container, monkeypatch, request)
    catalog = restarted.session_service.path_resolver.catalog_store
    verify = catalog.verify_registered_date_directories
    entered, finished = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def blocked_verify():
        loop.call_soon_threadsafe(entered.set)
        try:
            assert release.wait(timeout=10), "测试必须显式释放目录检查 worker"
            verify()
        finally:
            loop.call_soon_threadsafe(finished.set)

    async def start():
        async with main.lifespan(main.app):
            pytest.fail("启动取消后不得进入 ready")

    monkeypatch.setattr(catalog, "verify_registered_date_directories", blocked_verify)
    task = asyncio.create_task(start())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        lock_path = NavigationTopologyGate(restarted.session_catalog.sessions_root).lock_path
        with lock_path.open("a+b") as handle, pytest.raises(portalocker.AlreadyLocked):
            portalocker.lock(handle, portalocker.LOCK_EX | portalocker.LOCK_NB)
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), timeout=5)
        with pytest.raises(asyncio.CancelledError):
            await task
    assert observed == ["topology-held"]
    assert restarted.session_catalog_service.operations.worker_state == "stopped"


async def test_lifespan_accepts_registered_leaf_isolated_during_directory_scan(
    startup_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    session = await startup_container.session_service.create(SessionCreateRequest(title="扫描时合法删除"))
    directory = startup_container.session_service.path_resolver.resolve_session_node(session.session_id)
    manifest = (directory / "session.json").read_bytes()
    restarted, observed = _restart_for_lifespan(startup_container, monkeypatch, request)
    resolver = restarted.session_service.path_resolver
    marked, drain = asyncio.Event(), asyncio.Event()
    isolated = threading.Event()
    loop = asyncio.get_running_loop()
    observe = session_subtree_delete.observe_phase
    scandir = os.scandir

    async def pause_delete(operation_id, phase, **kwargs):
        await observe(operation_id, phase, **kwargs)
        if operation_id != "startup-scan-delete":
            return
        if phase == "deletion.catalog_deleting":
            marked.set()
            await drain.wait()
        elif phase == f"deletion.renamed/{session.session_id}":
            isolated.set()

    class UncachedEntry:
        # Windows / DT_UNKNOWN 需要实际 stat；不能让 Linux d_type 缓存掩盖消失竞态。
        name = directory.name
        path = str(directory)

        def stat(self, *, follow_symlinks=True):
            return directory.stat(follow_symlinks=follow_symlinks)

        def is_symlink(self):
            return directory.is_symlink()

        def is_dir(self, *, follow_symlinks=True):
            return directory.is_dir()

    def scanned_entries(entries):
        for entry in entries:
            if Path(entry.path) == directory:
                assert directory.is_dir()
                loop.call_soon_threadsafe(drain.set)
                assert isolated.wait(timeout=10), "真实删除必须先隔离目录，再继续扫描分类"
                assert not directory.exists()
                yield UncachedEntry()
            else:
                yield entry

    @contextmanager
    def scan_with_delete(path):
        with scandir(path) as entries:
            yield scanned_entries(entries) if Path(path) == directory.parent else entries

    monkeypatch.setattr(session_subtree_delete, "observe_phase", pause_delete)
    monkeypatch.setattr(os, "scandir", scan_with_delete)
    deletion = asyncio.create_task(resolver.delete_subtree(
        idempotency_key="startup-scan-delete", root_node_id=session.session_id,
    ))
    try:
        await asyncio.wait_for(marked.wait(), timeout=5)
        async with main.lifespan(main.app):
            await asyncio.wait_for(deletion, timeout=5)
            assert observed == ["topology-held"] and isolated.is_set()
            assert resolver.catalog_store.get_subtree_delete_record("startup-scan-delete").state == "completed"
            with pytest.raises(KeyError):
                resolver.catalog_store.get_node(session.session_id)
            isolated_directory = resolver.sessions_root / ".deleting/startup-scan-delete" / session.session_id
            assert (isolated_directory / "session.json").read_bytes() == manifest
            assert directory.parent.is_dir() and not directory.exists()
    finally:
        drain.set()
        if not deletion.done():
            deletion.cancel()
            await asyncio.gather(deletion, return_exceptions=True)


@pytest.mark.parametrize("replacement", ["file", "symlink"])
async def test_lifespan_rejects_registered_leaf_replaced_with_non_directory(
    startup_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest, replacement: str,
) -> None:
    session = await startup_container.session_service.create(SessionCreateRequest(title="仍存在的非法叶目录"))
    directory = startup_container.session_service.path_resolver.resolve_session_node(session.session_id)
    restarted, observed = _restart_for_lifespan(startup_container, monkeypatch, request)
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    original = context.artifacts_dir / f"original-{session.session_id}"
    directory.rename(original)
    manifest = (original / "session.json").read_bytes()
    if replacement == "symlink":
        directory.symlink_to(original, target_is_directory=True)
    else:
        directory.write_bytes(b"not-a-directory\n")
    before = _catalog_dump(restarted.session_service.path_resolver.catalog_store.database_path)
    # 普通文件会在更早的 manifest 初始化失败；符号链接由本次目录归属检查拒绝。
    expected = NotADirectoryError if replacement == "file" else CatalogMaintenanceRequiredError
    with pytest.raises(expected):
        async with main.lifespan(main.app):
            pytest.fail("仍存在的非法叶目录不能按并发消失处理")
    assert observed == ([] if replacement == "file" else ["topology-held"])
    assert _catalog_dump(restarted.session_service.path_resolver.catalog_store.database_path) == before
    assert (original / "session.json").read_bytes() == manifest
    assert directory.is_symlink() if replacement == "symlink" else directory.read_bytes() == b"not-a-directory\n"
