from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypeVar

import pytest

from app.core.path_utils import get_session_path_resolver
from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.schemas.internal_v2.session import SessionDTO
from app.schemas.internal_v2.session_navigation import SessionFolderUpdateRequest
from app.services.business.session_navigation import SessionCatalogService

T = TypeVar("T")


def canonical(name: str) -> str:
    """R17 任务书 §2.2 的确定性 canonical session ID 映射（测试用）。"""
    digest = hashlib.md5(name.encode("utf-8")).hexdigest()
    return f"ses_{digest[:12]}4{digest[13:16]}8{digest[17:]}"


def _relocate(
    resolver: SessionCatalogPathResolver,
    session_id: str,
    parent_node_id: str,
) -> None:
    """仅更新 SQLite catalog 中的逻辑父节点。"""
    resolver.relocate_session(
        session_id=session_id,
        parent_node_id=parent_node_id,
    )


class _SessionService:
    def __init__(self, sessions_root: Path) -> None:
        self.path_resolver = get_session_path_resolver(sessions_root)
        self.path_resolver.initialize()
        self.get_calls: list[str] = []

    def register_change_listener(self, listener) -> None:
        del listener

    async def get(self, session_id: str) -> SessionDTO:
        self.get_calls.append(session_id)
        session_path = self.path_resolver.resolve_session_node_for_runtime(session_id)
        payload = json.loads(
            (session_path / "session.json").read_text(encoding="utf-8")
        )
        # R17：对齐生产 session_service.get 的换源读路径——title 以权威
        # 索引/目录节点显示名为准回填（catalog 模式 manifest 为剥离形态，
        # 不含 title/title_source/parent_session_id）。
        node = self.path_resolver.get_node(session_id)
        payload["title"] = node.name
        payload.setdefault("workspace_id", "ws-test")
        payload.setdefault("current_agent_id", "test-agent")
        return SessionDTO.model_validate(payload)


class _JobService:
    def __init__(self) -> None:
        self.locked_session_ids: list[str] = []

    async def run_sessions_idle_operation(
        self,
        session_ids: list[str],
        operation: Callable[[], Awaitable[T]],
    ) -> T:
        self.locked_session_ids = list(session_ids)
        return await operation()


class _DeleteJobService(_JobService):
    def __init__(self) -> None:
        super().__init__()
        self.deleted_session_ids: list[str] = []

    async def run_sessions_delete_operation(
        self,
        session_ids: list[str],
        operation: Callable[[], Awaitable[T]],
    ) -> T:
        self.deleted_session_ids = list(session_ids)
        return await operation()


@pytest.mark.asyncio
async def test_catalog_cache_detects_manual_physical_move(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_root = tmp_path / "sessions"
    session_service = _SessionService(sessions_root)
    resolver = session_service.path_resolver
    source_folder = resolver.create_folder(name="原目录", parent_node_id=None)
    session_id = "ses_c5e66e7374644cf18313e592100ccfad"
    session_dir = session_bundle_factory(sessions_root, session_id)
    _relocate(resolver, session_id, source_folder.node_id)
    catalog = SessionCatalogService(session_service=session_service)
    first = await catalog.export_index()
    first_node = next(
        node
        for node in first.items
        if node.node_id == "ses_c5e66e7374644cf18313e592100ccfad"
    )
    # 手工挪动日期桶目录后按 ID 解析必须 fail closed。
    moved_path = tmp_path / "手工挪走" / session_dir.name
    moved_path.parent.mkdir(parents=True, exist_ok=True)
    resolver.resolve_session_node(
        "ses_c5e66e7374644cf18313e592100ccfad"
    ).replace(moved_path)
    with pytest.raises(RuntimeError, match="会话物理目录缺失"):
        session_service.path_resolver.resolve_session_node(
            "ses_c5e66e7374644cf18313e592100ccfad"
        )

    assert first_node.parent_node_id == source_folder.node_id
    assert first_node.session is not None
    assert first_node.session.session_id == "ses_c5e66e7374644cf18313e592100ccfad"
    assert first_node.session.title == first_node.name
    assert first_node.session.parent_session_id is None


@pytest.mark.asyncio
async def test_catalog_snapshot_enriches_session_nodes_and_reuses_cached_metadata(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_root = tmp_path / "sessions"
    session_service = _SessionService(sessions_root)
    resolver = session_service.path_resolver
    folder = resolver.create_folder(name="目录元数据", parent_node_id=None)
    session_id = "ses_e15deaf2c9814eb98a80eb270589c96e"
    session_bundle_factory(sessions_root, session_id)
    _relocate(resolver, session_id, folder.node_id)
    catalog = SessionCatalogService(session_service=session_service)

    first = await catalog.list_children(
        parent_node_id=folder.node_id,
        limit=100,
        cursor=None,
    )
    second = await catalog.list_children(
        parent_node_id=folder.node_id,
        limit=100,
        cursor=None,
    )

    assert len(first.items) == 1
    session_node = first.items[0]
    assert session_node.session is not None
    assert session_node.session.session_id == session_id
    assert session_node.session.title == session_node.name
    # manifest workspace_id 由工厂写入 resolver 真实绑定值。
    assert session_node.session.workspace_id == resolver._workspace_id
    assert second.items[0].session == session_node.session
    assert session_service.get_calls == [session_id]


@pytest.mark.asyncio
async def test_catalog_read_keeps_authoritative_nodes_when_physical_tree_has_orphan(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    """孤立物理目录不能让目录读模型变成空列表。"""
    sessions_root = tmp_path / "sessions"
    session_service = _SessionService(sessions_root)
    resolver = session_service.path_resolver
    folder = resolver.create_folder(name="归档", parent_node_id=None)
    session_id = "ses_ad68159b274149068514905d0d25cfe3"
    session_bundle_factory(sessions_root, session_id)
    _relocate(resolver, session_id, folder.node_id)

    # 模拟历史重启留下的空根目录：不修改索引，也不删除它，验证业务读的边界。
    (sessions_root / session_id).mkdir()
    catalog = SessionCatalogService(session_service=session_service)

    page = await catalog.list_children(
        parent_node_id=folder.node_id,
        limit=100,
        cursor=None,
    )

    assert [node.node_id for node in page.items] == [session_id]
    # 目录读模型不扫盘；未登记的孤立目录不影响 catalog 投影。
    locator = page.items[0].storage_relative_path
    assert locator is not None
    assert locator.split("/")[-1] == session_id
    assert page.consistency_warning is None
    assert (sessions_root / session_id).is_dir()
    await catalog.refresh()


@pytest.mark.asyncio
async def test_moving_folder_uses_idle_guard_for_every_descendant_session(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_root = tmp_path / "sessions"
    session_service = _SessionService(sessions_root)
    resolver = session_service.path_resolver
    folder = resolver.create_folder(name="任务目录", parent_node_id=None)
    nested = resolver.create_folder(
        name="日期目录",
        parent_node_id=folder.node_id,
    )
    session_ids = [
        canonical("ses_guard_alpha"),
        canonical("ses_guard_beta"),
    ]
    for session_id in session_ids:
        session_bundle_factory(sessions_root, session_id)
        _relocate(resolver, session_id, nested.node_id)
    job_service = _JobService()
    catalog = SessionCatalogService(
        session_service=session_service,
        job_service=job_service,
    )

    await catalog.update_folder(
        folder.node_id,
        SessionFolderUpdateRequest(name="任务目录已改名"),
    )

    assert job_service.locked_session_ids == sorted(session_ids)
    assert resolver.get_node(folder.node_id).name == "任务目录已改名"


@pytest.mark.asyncio
async def test_deep_search_builds_breadcrumbs_only_for_returned_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_root = tmp_path / "sessions"
    session_service = _SessionService(sessions_root)
    resolver = session_service.path_resolver
    parent_id: str | None = None
    for depth in range(100):
        folder = resolver.create_folder(
            name=f"needle-{depth:03d}",
            parent_node_id=parent_id,
        )
        parent_id = folder.node_id
    catalog = SessionCatalogService(session_service=session_service)
    breadcrumb_calls = 0
    original = SessionCatalogService._breadcrumb_items

    def count_breadcrumbs(node, nodes_by_id):
        nonlocal breadcrumb_calls
        breadcrumb_calls += 1
        return original(node, nodes_by_id)

    monkeypatch.setattr(catalog, "_breadcrumb_items", count_breadcrumbs)

    result = await catalog.search(query="needle", limit=2, cursor=None)

    assert result.total == 100
    assert len(result.items) == 2
    assert result.cursor is not None
    assert breadcrumb_calls == 2


@pytest.mark.asyncio
async def test_recursive_delete_uses_catalog_subtree_protocol_without_folder_paths(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_root = tmp_path / "sessions"
    session_service = _SessionService(sessions_root)
    resolver = session_service.path_resolver
    parent = resolver.create_folder(name="删除父目录", parent_node_id=None)
    nested = resolver.create_folder(name="删除子目录", parent_node_id=parent.node_id)
    session_ids = [
        canonical("delete_catalog_alpha"),
        canonical("delete_catalog_beta"),
    ]
    session_dirs: list[Path] = []
    for session_id in session_ids:
        session_dir = session_bundle_factory(sessions_root, session_id)
        session_dirs.append(session_dir)
        _relocate(resolver, session_id, nested.node_id)

    job_service = _DeleteJobService()
    catalog = SessionCatalogService(
        session_service=session_service,
        job_service=job_service,
    )

    await asyncio.wait_for(
        catalog.delete_folder(parent.node_id, recursive=True),
        timeout=2,
    )

    assert job_service.deleted_session_ids == sorted(session_ids)
    assert not any(session_dir.exists() for session_dir in session_dirs)
    assert all(
        node.node_id not in {parent.node_id, nested.node_id, *session_ids}
        for node in resolver.list_nodes()
    )
    deleting_root = sessions_root / ".deleting"
    deleting_keys = sorted(path.name for path in deleting_root.iterdir())
    assert len(deleting_keys) == 1
    assert sorted(
        path.name for path in (deleting_root / deleting_keys[0]).iterdir()
    ) == sorted(session_ids)
