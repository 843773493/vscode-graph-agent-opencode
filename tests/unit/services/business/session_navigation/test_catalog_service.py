from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypeVar

import pytest

from app.core.path_utils import get_session_path_resolver
from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.schemas.internal_v2.session import SessionDTO
from app.schemas.internal_v2.session_navigation import (
    SessionFolderCreateRequest,
    SessionFolderUpdateRequest,
)
from app.services.business.session_navigation import SessionCatalogService
from tests.support.canonical_id_at import uuid7_hex_from_name

T = TypeVar("T")


def canonical(name: str) -> str:
    """R17 任务书 §2.2 的确定性 canonical session ID 映射（v7 位 profile）。"""
    return f"ses_{uuid7_hex_from_name(name)}"


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
    session_id = "ses_019c3db33a627857869df585e7b8a084"
    session_dir = session_bundle_factory(sessions_root, session_id)
    _relocate(resolver, session_id, source_folder.node_id)
    catalog = SessionCatalogService(session_service=session_service)
    first = await catalog.export_index()
    first_node = next(
        node
        for node in first.items
        if node.node_id == "ses_019c3db33a627857869df585e7b8a084"
    )
    # 手工挪动日期桶目录后按 ID 解析必须 fail closed。
    moved_path = tmp_path / "手工挪走" / session_dir.name
    moved_path.parent.mkdir(parents=True, exist_ok=True)
    resolver.resolve_session_node(
        "ses_019c3db33a627857869df585e7b8a084"
    ).replace(moved_path)
    with pytest.raises(RuntimeError, match="会话物理目录缺失"):
        session_service.path_resolver.resolve_session_node(
            "ses_019c3db33a627857869df585e7b8a084"
        )

    assert first_node.parent_node_id == source_folder.node_id
    assert first_node.session is not None
    assert first_node.session.session_id == "ses_019c3db33a627857869df585e7b8a084"
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
    session_id = "ses_019bfa471b36790187e1d914607718e5"
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
    session_id = "ses_019c1d3a6c9b7150818dc58e43efafb1"
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
async def test_renaming_folder_does_not_require_descendant_sessions_idle(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    """逻辑导航改名不搬物理存储，不得再要求后代 Session 的 Job 进入 idle。

    8.1-E 明确删除「移动/改名时强制所有后代 Job idle」的旧逻辑：folder 是
    SQLite-only 节点、逻辑移动不动磁盘，活跃 Session 可随时被移动或改名。
    """
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

    before = {
        session_id: resolver.resolve_session_node(session_id)
        for session_id in session_ids
    }

    await catalog.update_folder(
        folder.node_id,
        SessionFolderUpdateRequest(name="任务目录已改名"),
    )

    # 不再经过 idle guard，且物理 locator 不变。
    assert job_service.locked_session_ids == []
    assert resolver.get_node(folder.node_id).name == "任务目录已改名"
    assert {
        session_id: resolver.resolve_session_node(session_id)
        for session_id in session_ids
    } == before


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
async def test_refresh_returns_complete_root_page_without_silent_cap(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    """refresh 无 limit/cursor 入参，必须返回完整根页。

    旧实现把根节点硬切到 500 条却仍报 ``total`` 为真实数量且 ``cursor=None``，
    超过 500 的根节点既不在 items 里也无法继续分页，被静默吞掉。
    """
    sessions_root = tmp_path / "sessions"
    session_service = _SessionService(sessions_root)
    total = 520
    for index in range(total):
        session_bundle_factory(
            sessions_root,
            canonical(f"refresh_truncation_{index:04d}"),
        )
    catalog = SessionCatalogService(session_service=session_service)

    page = await catalog.refresh()

    assert page.total == total
    assert len(page.items) == total
    assert page.cursor is None


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


def _count_full_catalog_aggregations(
    monkeypatch: pytest.MonkeyPatch,
    resolver: SessionCatalogPathResolver,
) -> list[int]:
    """统计 resolver 全 catalog 聚合（``_list_all_catalog_nodes`` BFS）次数。

    ``list_nodes()`` 与本切片前的 ``revision`` 实现都走这一聚合；打桩在聚合
    入口即可同时锁定两条路径，任何复用同一次聚合之外的额外全表扫描都会被计数。
    """
    count = [0]
    original = resolver._list_all_catalog_nodes

    def counted():
        count[0] += 1
        return original()

    monkeypatch.setattr(resolver, "_list_all_catalog_nodes", counted)
    return count


@pytest.mark.asyncio
async def test_snapshot_aggregates_full_catalog_once(
    tmp_path: Path,
    session_bundle_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§10.3b：``refresh()`` 与一次目录 mutation 各只做一次全 catalog 聚合。

    改前 ``_snapshot`` 在 ``revision``(×2) 与 ``list_nodes``(×1) 上共做 3 次
    全表扫描；改后 ``revision`` 取 ``catalog_metadata.generation`` 单行查询，
    只剩 ``list_nodes`` 一次。恢复重复聚合本用例立刻变红。
    """
    sessions_root = tmp_path / "sessions"
    session_service = _SessionService(sessions_root)
    resolver = session_service.path_resolver
    folder = resolver.create_folder(name="目录", parent_node_id=None)
    session_id = canonical("aggregate_once")
    session_bundle_factory(sessions_root, session_id)
    _relocate(resolver, session_id, folder.node_id)
    catalog = SessionCatalogService(session_service=session_service)

    count = _count_full_catalog_aggregations(monkeypatch, resolver)

    # refresh() 走 force 路径，必须只聚合一次。
    await catalog.refresh()
    assert count[0] == 1

    # 一次目录 mutation（新建 folder）后一次读：同样只聚合一次。
    count[0] = 0
    await catalog.create_folder(
        SessionFolderCreateRequest(name="新建目录", parent_folder_id=None)
    )
    await catalog.list_children(parent_node_id=None, limit=50, cursor=None)
    assert count[0] == 1


@pytest.mark.asyncio
async def test_revision_tracks_generation_without_full_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§10.3b：``resolver.revision`` 取 generation，不再触发全 catalog 聚合。"""
    sessions_root = tmp_path / "sessions"
    session_service = _SessionService(sessions_root)
    resolver = session_service.path_resolver

    count = _count_full_catalog_aggregations(monkeypatch, resolver)

    folder = resolver.create_folder(name="目录", parent_node_id=None)
    before = resolver.revision
    assert count[0] == 0
    resolver.update_node_name(folder.node_id, "新名")
    assert resolver.revision == before + 1
    assert count[0] == 0


@pytest.mark.asyncio
async def test_snapshot_cache_invalidates_on_direct_catalog_writes(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    """§10.3b：绕过 catalog 实例直接写 catalog 后，缓存判据必须察觉并重算。

    这是方案 A 的核心前提：``generation`` 只覆盖 SQLite 写，因此凡改变
    ``_snapshot`` 结果的写都必须推进 generation。改名/移动/新建/删除各一条，
    写完不调用 ``catalog.invalidate()``，直接再读必须看到新结果。任一条写绕过
    ``_bump_generation``，对应断言即变红（返回旧缓存）。
    """
    sessions_root = tmp_path / "sessions"
    session_service = _SessionService(sessions_root)
    resolver = session_service.path_resolver
    folder = resolver.create_folder(name="原目录", parent_node_id=None)
    session_id = canonical("cache_invalidate")
    session_bundle_factory(sessions_root, session_id)
    _relocate(resolver, session_id, folder.node_id)
    catalog = SessionCatalogService(session_service=session_service)

    first = await catalog.list_children(parent_node_id=None, limit=50, cursor=None)
    assert [node.node_id for node in first.items] == [folder.node_id]
    assert session_service.get_calls == [session_id]

    # 改名：目录读模型对 folder 直接取目录显示名，命名变化必须可见。
    resolver.update_node_name(folder.node_id, "改名后")
    calls_before = len(session_service.get_calls)
    renamed = await catalog.list_children(
        parent_node_id=None, limit=50, cursor=None
    )
    assert [node.name for node in renamed.items] == ["改名后"]
    assert len(session_service.get_calls) > calls_before  # 触发物理重算

    # 移动：把会话移出目录回到根，根页必须出现该会话。
    resolver.relocate_session(session_id=session_id, parent_node_id=None)
    moved = await catalog.list_children(parent_node_id=None, limit=50, cursor=None)
    assert sorted(node.node_id for node in moved.items) == sorted(
        [folder.node_id, session_id]
    )

    # 新建：新增 folder 必须出现在根页。
    created = resolver.create_folder(name="新目录", parent_node_id=None)
    after_create = await catalog.list_children(
        parent_node_id=None, limit=50, cursor=None
    )
    assert created.node_id in {node.node_id for node in after_create.items}

    # 删除：删除空 folder 后必须从根页消失。
    resolver.delete_folder(created.node_id)
    after_delete = await catalog.list_children(
        parent_node_id=None, limit=50, cursor=None
    )
    assert created.node_id not in {node.node_id for node in after_delete.items}


@pytest.mark.asyncio
async def test_children_cursor_is_scoped_to_parent(tmp_path: Path) -> None:
    """cursor 绑定发牌父节点：拿去翻另一个父节点的页必须显式报错。

    缺陷背景：cursor 只绑定 ``revision``，同一 revision 下把 A 父节点的 cursor
    交给 B 父节点的分页请求时，offset 会落在 B 自身切片的中间——B 的前几项被
    静默跳过、不报错（实测 B 首项 b0 被跳过）。本用例钉死跨父节点复用必须
    fail-closed。
    """
    sessions_root = tmp_path / "sessions"
    session_service = _SessionService(sessions_root)
    resolver = session_service.path_resolver
    folder_a = resolver.create_folder(name="A", parent_node_id=None)
    folder_b = resolver.create_folder(name="B", parent_node_id=None)
    for index in range(3):
        resolver.create_folder(name=f"a{index}", parent_node_id=folder_a.node_id)
        resolver.create_folder(name=f"b{index}", parent_node_id=folder_b.node_id)
    catalog = SessionCatalogService(session_service=session_service)

    first_a = await catalog.list_children(
        parent_node_id=folder_a.node_id,
        limit=1,
        cursor=None,
    )
    assert [node.name for node in first_a.items] == ["a0"]
    assert first_a.cursor is not None

    # 同一父节点内继续翻页仍然正常。
    second_a = await catalog.list_children(
        parent_node_id=folder_a.node_id,
        limit=1,
        cursor=first_a.cursor,
    )
    assert [node.name for node in second_a.items] == ["a1"]

    # 跨父节点复用同一 cursor 必须显式报错，而不是返回被截断的错误页。
    with pytest.raises(ValueError, match="cursor 与当前列表不匹配"):
        await catalog.list_children(
            parent_node_id=folder_b.node_id,
            limit=1,
            cursor=first_a.cursor,
        )


@pytest.mark.asyncio
async def test_search_cursor_is_scoped_to_query(tmp_path: Path) -> None:
    """搜索 cursor 绑定查询词：换查询词复用旧 cursor 必须显式报错。"""
    sessions_root = tmp_path / "sessions"
    session_service = _SessionService(sessions_root)
    resolver = session_service.path_resolver
    for index in range(3):
        resolver.create_folder(name=f"alpha-{index}", parent_node_id=None)
        resolver.create_folder(name=f"beta-{index}", parent_node_id=None)
    catalog = SessionCatalogService(session_service=session_service)

    first = await catalog.search(query="alpha", limit=1, cursor=None)
    assert len(first.items) == 1
    assert first.cursor is not None

    with pytest.raises(ValueError, match="cursor 与当前列表不匹配"):
        await catalog.search(query="beta", limit=1, cursor=first.cursor)
