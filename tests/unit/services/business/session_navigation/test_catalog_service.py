from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TypeVar

import pytest
from pydantic import ValidationError

from app.core.identifier import create_prefixed_id
from app.core.path_utils import get_session_creation_service, get_session_path_resolver
from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.schemas.internal_v2.session import SessionDTO
from app.schemas.internal_v2.session_navigation import SessionCatalogNodeDTO
from app.services.business.session_navigation import SessionCatalogService
from app.services.business.session_service import SessionService
from app.services.infrastructure.trace_event_store import TraceEventStore
from tests.support.canonical_id_at import uuid7_hex_from_name
from tests.support.session_navigation_queue import (
    create_catalog_intent,
    submit_catalog_intent,
)
from tests.unit.services.business.session_service_test_support import (
    SessionServiceConfigStub,
)

T = TypeVar("T")


@pytest.mark.asyncio
async def test_catalog_public_projections_hide_locator_but_keep_path_search(
    tmp_path: Path, session_bundle_factory,
) -> None:
    sessions_root = tmp_path / "sessions"
    service = _SessionService(sessions_root)
    resolver = service.path_resolver
    session_id = canonical("公开位置边界")
    session_bundle_factory(sessions_root, session_id)
    folder = resolver.catalog_store.create_folder(
        create_prefixed_id("ses"), resolver.workspace_id, None, "逻辑目录",
    )
    _relocate(resolver, session_id, folder.node_id)
    catalog = SessionCatalogService(session_service=service)
    locator = resolver.resolve_session_node(session_id).relative_to(sessions_root).as_posix()
    results = [
        await catalog.export_index(),
        await catalog.list_children(parent_node_id=folder.node_id, limit=10, cursor=None),
        await catalog.breadcrumb(session_id),
    ]
    for query in (session_id, locator, locator.split("/")[0]):
        search = await catalog.search(query=query, limit=10, cursor=None)
        assert [item.node.node_id for item in search.items] == [session_id]
        assert search.items[0].relative_path.startswith("逻辑目录/")
        results.append(search)
    for result in results:
        raw = result.model_dump_json()
        assert "storage_relative_path" not in raw
        assert locator not in raw
        assert str(sessions_root) not in raw
    node = next(node for node in results[0].items if node.node_id == session_id)
    legacy = json.loads(node.model_dump_json())
    legacy["storage_relative_path"] = locator
    with pytest.raises(ValidationError) as caught:
        SessionCatalogNodeDTO.model_validate(legacy)
    assert locator not in str(caught.value)

    resolver.catalog_store.set_node_state(session_id, "deleting")
    pending = await catalog.breadcrumb(session_id)
    assert pending.items[-1].state == "deleting"
    assert pending.items[-1].session is None
    assert "storage_relative_path" not in pending.model_dump_json()
    assert locator not in pending.model_dump_json()


def canonical(name: str) -> str:
    """R17 任务书 §2.2 的确定性 canonical session ID 映射（v7 位 profile）。"""
    return f"ses_{uuid7_hex_from_name(name)}"


def _relocate(
    resolver: SessionCatalogPathResolver,
    session_id: str,
    parent_node_id: str,
) -> None:
    """仅更新 SQLite catalog 中的逻辑父节点。"""
    resolver.get_node(resolver.catalog_store.move_node(session_id, parent_node_id).node_id)


class _SessionService(SessionService):
    """真实 SessionService 与离线配置；记录旧逐会话入口是否被误调用。"""
    def __init__(
        self,
        sessions_root: Path,
        *,
        config_service: SessionServiceConfigStub | None = None,
    ) -> None:
        resolver = get_session_path_resolver(sessions_root)
        super().__init__(
            config_service=config_service or SessionServiceConfigStub(),
            trace_event_store=TraceEventStore(sessions_dir=sessions_root),
            workspace_id=resolver.workspace_id, path_resolver=resolver,
            creation_service=get_session_creation_service(sessions_root),

        )
        self.get_calls: list[str] = []

    async def get(self, session_id: str) -> SessionDTO:
        self.get_calls.append(session_id)
        return await super().get(session_id)


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


async def _submit_catalog_mutation(
    catalog: SessionCatalogService,
    kind: Literal[
        "create_folder",
        "rename_node",
        "move_node",
        "delete_folder",
        "delete_session",
    ],
    *,
    target_node_id: str | None = None,
    name: str | None = None,
    parent_node_id: str | None = None,
    recursive: bool = False,
):
    intent = create_catalog_intent(
        catalog,
        kind,
        target_node_id=target_node_id,
        name=name,
        parent_node_id=parent_node_id,
        recursive=recursive,
    )
    receipt = await submit_catalog_intent(catalog, intent)
    assert receipt.state == "committed", receipt.error_detail
    return receipt

def _count_catalog_snapshots(
    monkeypatch: pytest.MonkeyPatch,
    resolver: SessionCatalogPathResolver,
) -> list[int]:
    """统计 resolver 原子 catalog snapshot 读取次数。"""
    count = [0]
    original = resolver.catalog_snapshot

    def counted():
        count[0] += 1
        return original()

    monkeypatch.setattr(resolver, "catalog_snapshot", counted)
    return count


@pytest.mark.asyncio
async def test_catalog_cache_detects_manual_physical_move(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_root = tmp_path / "sessions"
    session_service = _SessionService(sessions_root)
    resolver = session_service.path_resolver
    source_folder = resolver.get_node(resolver.catalog_store.create_folder(create_prefixed_id("ses"), resolver.workspace_id, None, '原目录').node_id)
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_root = tmp_path / "sessions"
    session_service = _SessionService(sessions_root)
    resolver = session_service.path_resolver
    folder = resolver.get_node(resolver.catalog_store.create_folder(create_prefixed_id("ses"), resolver.workspace_id, None, '目录元数据').node_id)
    session_id = "ses_019bfa471b36790187e1d914607718e5"
    session_bundle_factory(sessions_root, session_id)
    _relocate(resolver, session_id, folder.node_id)
    catalog = SessionCatalogService(session_service=session_service)
    original_open = Path.open

    def reject_manifest_open(path: Path, *args, **kwargs):
        if path.name == "session.json":
            raise AssertionError("导航快照不得打开会话 manifest")
        return original_open(path, *args, **kwargs)

    def reject_single_node_read(*args, **kwargs):
        raise AssertionError("导航 DTO 与父链必须来自同一次 catalog 快照")

    monkeypatch.setattr(Path, "open", reject_manifest_open)
    monkeypatch.setattr(resolver.catalog_store, "get_node", reject_single_node_read)

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
    assert session_service.get_calls == []


@pytest.mark.asyncio
async def test_catalog_cached_null_provider_tracks_config_default_without_catalog_read(
    tmp_path: Path,
    session_bundle_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NULL provider 继续动态解析配置；缓存不冻结旧默认值或重读会话。"""
    model = {"primary_provider": "primary", "fallback_providers": ["backup"]}
    config = SessionServiceConfigStub({
        "default_agent": "default",
        "agents": {"default": {"model": model}},
        "llm": {"providers": [{"id": "primary"}, {"id": "backup"}]},
    })
    sessions_root = tmp_path / "sessions"
    service = _SessionService(sessions_root, config_service=config)
    resolver = service.path_resolver
    default_id, explicit_id = canonical("dynamic-default"), canonical("explicit-provider")
    session_bundle_factory(sessions_root, default_id)
    session_bundle_factory(sessions_root, explicit_id)
    explicit = resolver.catalog_store.get_node(explicit_id)
    assert explicit.updated_at is not None
    resolver.catalog_store.update_session_configuration(
        explicit_id,
        current_agent_id="default",
        current_provider_id="primary",
        updated_at=datetime.fromisoformat(explicit.updated_at),
    )
    count = _count_catalog_snapshots(monkeypatch, resolver)
    catalog = SessionCatalogService(session_service=service)
    first = await catalog.export_index()
    first_sessions = {node.node_id: node.session for node in first.items}
    assert first_sessions[default_id] is not None
    assert first_sessions[default_id].current_provider_id == "primary"

    model["primary_provider"] = "backup"
    second = await catalog.export_index()
    second_sessions = {node.node_id: node.session for node in second.items}

    assert second_sessions[default_id] is not None
    assert second_sessions[default_id].current_provider_id == "backup"
    assert second_sessions[explicit_id] == first_sessions[explicit_id]
    assert second.catalog_revision == first.catalog_revision
    assert second.revision != first.revision
    assert count == [1]
    assert service.get_calls == []
    assert resolver.catalog_store.get_node(default_id).current_provider_id is None


@pytest.mark.asyncio
async def test_catalog_read_keeps_authoritative_nodes_when_physical_tree_has_orphan(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    """孤立物理目录不能让目录读模型变成空列表。"""
    sessions_root = tmp_path / "sessions"
    session_service = _SessionService(sessions_root)
    resolver = session_service.path_resolver
    folder = resolver.get_node(resolver.catalog_store.create_folder(create_prefixed_id("ses"), resolver.workspace_id, None, '归档').node_id)
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
    assert "storage_relative_path" not in page.items[0].model_dump()
    assert resolver.resolve_session_node(session_id).name == session_id
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
    folder = resolver.get_node(resolver.catalog_store.create_folder(create_prefixed_id("ses"), resolver.workspace_id, None, '任务目录').node_id)
    nested = resolver.get_node(resolver.catalog_store.create_folder(create_prefixed_id("ses"), resolver.workspace_id, folder.node_id, '日期目录').node_id)
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

    await _submit_catalog_mutation(
        catalog,
        "rename_node",
        target_node_id=folder.node_id,
        name="任务目录已改名",
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
        folder = resolver.get_node(resolver.catalog_store.create_folder(create_prefixed_id("ses"), resolver.workspace_id, parent_id, f'needle-{depth:03d}').node_id)
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
    parent = resolver.get_node(resolver.catalog_store.create_folder(create_prefixed_id("ses"), resolver.workspace_id, None, '删除父目录').node_id)
    nested = resolver.get_node(resolver.catalog_store.create_folder(create_prefixed_id("ses"), resolver.workspace_id, parent.node_id, '删除子目录').node_id)
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
        _submit_catalog_mutation(
            catalog,
            "delete_folder",
            target_node_id=parent.node_id,
            recursive=True,
        ),
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

    ``revision`` 只读单行版本，节点与版本通过一次原子 catalog snapshot 读取；
    不得再附加旧 BFS 聚合或重复加载快照。
    """
    sessions_root = tmp_path / "sessions"
    session_service = _SessionService(sessions_root)
    resolver = session_service.path_resolver
    folder = resolver.get_node(resolver.catalog_store.create_folder(create_prefixed_id("ses"), resolver.workspace_id, None, '目录').node_id)
    session_id = canonical("aggregate_once")
    session_bundle_factory(sessions_root, session_id)
    _relocate(resolver, session_id, folder.node_id)
    catalog = SessionCatalogService(session_service=session_service)

    count = _count_catalog_snapshots(monkeypatch, resolver)
    full_scan_count = _count_full_catalog_aggregations(monkeypatch, resolver)

    # refresh() 走 force 路径，必须只聚合一次。
    await catalog.refresh()
    assert count[0] == 1
    assert full_scan_count[0] == 0

    # 一次目录 mutation（新建 folder）后一次读：同样只聚合一次。
    count[0] = 0
    await _submit_catalog_mutation(
        catalog,
        "create_folder",
        name="新建目录",
        parent_node_id=None,
    )
    await catalog.list_children(parent_node_id=None, limit=50, cursor=None)
    assert count[0] == 1
    assert full_scan_count[0] == 0


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

    folder = resolver.get_node(resolver.catalog_store.create_folder(create_prefixed_id("ses"), resolver.workspace_id, None, '目录').node_id)
    before = resolver.revision
    assert count[0] == 0
    resolver.get_node(resolver.catalog_store.rename_node(folder.node_id, '新名').node_id)
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
    folder = resolver.get_node(resolver.catalog_store.create_folder(create_prefixed_id("ses"), resolver.workspace_id, None, '原目录').node_id)
    session_id = canonical("cache_invalidate")
    session_bundle_factory(sessions_root, session_id)
    _relocate(resolver, session_id, folder.node_id)
    catalog = SessionCatalogService(session_service=session_service)

    first = await catalog.list_children(parent_node_id=None, limit=50, cursor=None)
    assert [node.node_id for node in first.items] == [folder.node_id]
    assert session_service.get_calls == []

    # 改名：目录读模型对 folder 直接取目录显示名，命名变化必须可见。
    resolver.get_node(resolver.catalog_store.rename_node(folder.node_id, '改名后').node_id)
    calls_before = len(session_service.get_calls)
    renamed = await catalog.list_children(
        parent_node_id=None, limit=50, cursor=None
    )
    assert [node.name for node in renamed.items] == ["改名后"]
    assert len(session_service.get_calls) == calls_before == 0

    # 移动：把会话移出目录回到根，根页必须出现该会话。
    resolver.get_node(resolver.catalog_store.move_node(session_id, None).node_id)
    moved = await catalog.list_children(parent_node_id=None, limit=50, cursor=None)
    assert sorted(node.node_id for node in moved.items) == sorted(
        [folder.node_id, session_id]
    )

    # 新建：新增 folder 必须出现在根页。
    created = resolver.get_node(resolver.catalog_store.create_folder(create_prefixed_id("ses"), resolver.workspace_id, None, '新目录').node_id)
    after_create = await catalog.list_children(
        parent_node_id=None, limit=50, cursor=None
    )
    assert created.node_id in {node.node_id for node in after_create.items}

    # 删除：删除空 folder 后必须从根页消失。
    resolver.catalog_store.delete_empty_folder(created.node_id)
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
    folder_a = resolver.get_node(resolver.catalog_store.create_folder(create_prefixed_id("ses"), resolver.workspace_id, None, 'A').node_id)
    folder_b = resolver.get_node(resolver.catalog_store.create_folder(create_prefixed_id("ses"), resolver.workspace_id, None, 'B').node_id)
    for index in range(3):
        resolver.get_node(resolver.catalog_store.create_folder(create_prefixed_id("ses"), resolver.workspace_id, folder_a.node_id, f'a{index}').node_id)
        resolver.get_node(resolver.catalog_store.create_folder(create_prefixed_id("ses"), resolver.workspace_id, folder_b.node_id, f'b{index}').node_id)
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
        resolver.get_node(resolver.catalog_store.create_folder(create_prefixed_id("ses"), resolver.workspace_id, None, f'alpha-{index}').node_id)
        resolver.get_node(resolver.catalog_store.create_folder(create_prefixed_id("ses"), resolver.workspace_id, None, f'beta-{index}').node_id)
    catalog = SessionCatalogService(session_service=session_service)

    first = await catalog.search(query="alpha", limit=1, cursor=None)
    assert len(first.items) == 1
    assert first.cursor is not None

    with pytest.raises(ValueError, match="cursor 与当前列表不匹配"):
        await catalog.search(query="beta", limit=1, cursor=first.cursor)

async def test_revision_tracks_node_changes_without_full_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """读取 resolver navigation revision 不触发全量 snapshot。"""
    sessions_root = tmp_path / "sessions"
    session_service = _SessionService(sessions_root)
    resolver = session_service.path_resolver

    count = _count_catalog_snapshots(monkeypatch, resolver)

    folder = resolver.get_node(resolver.catalog_store.create_folder(create_prefixed_id("ses"), resolver.workspace_id, None, '目录').node_id)
    before = resolver.revision
    assert count[0] == 0
    resolver.get_node(resolver.catalog_store.rename_node(folder.node_id, '新名').node_id)
    assert resolver.revision == before + 1
    assert count[0] == 0

async def test_children_cursor_rejects_boolean_and_out_of_range_offsets(
    tmp_path: Path,
) -> None:
    session_service = _SessionService(tmp_path / "sessions")
    resolver = session_service.path_resolver
    for index in range(3):
        resolver.get_node(resolver.catalog_store.create_folder(create_prefixed_id("ses"), resolver.workspace_id, None, f'目录{index}').node_id)
    catalog = SessionCatalogService(session_service=session_service)
    first = await catalog.list_children(parent_node_id=None, limit=1, cursor=None)

    boolean_cursor = catalog._encode_cursor(
        True, first.revision, first.catalog_revision, None
    )
    with pytest.raises(ValueError, match="cursor offset 无效"):
        await catalog.list_children(
            parent_node_id=None, limit=1, cursor=boolean_cursor
        )

    out_of_range_cursor = catalog._encode_cursor(
        first.total, first.revision, first.catalog_revision, None
    )
    with pytest.raises(ValueError, match="offset 超出当前列表范围"):
        await catalog.list_children(
            parent_node_id=None, limit=1, cursor=out_of_range_cursor
        )

async def test_search_cursor_offset_uses_filtered_match_count(tmp_path: Path) -> None:
    session_service = _SessionService(tmp_path / "sessions")
    resolver = session_service.path_resolver
    for index in range(3):
        resolver.get_node(resolver.catalog_store.create_folder(create_prefixed_id("ses"), resolver.workspace_id, None, f'alpha-{index}').node_id)
        resolver.get_node(resolver.catalog_store.create_folder(create_prefixed_id("ses"), resolver.workspace_id, None, f'beta-{index}').node_id)
    catalog = SessionCatalogService(session_service=session_service)
    first = await catalog.search(query="alpha", limit=1, cursor=None)

    out_of_range_cursor = catalog._encode_cursor(
        first.total,
        first.revision,
        first.catalog_revision,
        "search\nalpha",
    )
    with pytest.raises(ValueError, match="offset 超出当前列表范围"):
        await catalog.search(query="alpha", limit=1, cursor=out_of_range_cursor)

async def test_catalog_read_dtos_expose_snapshot_and_node_revisions(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_root = tmp_path / "sessions"
    session_service = _SessionService(sessions_root)
    resolver = session_service.path_resolver
    folder = resolver.get_node(resolver.catalog_store.create_folder(create_prefixed_id("ses"), resolver.workspace_id, None, '版本目录').node_id)
    session_id = canonical("版本会话")
    session_bundle_factory(sessions_root, session_id)
    _relocate(resolver, session_id, folder.node_id)
    catalog = SessionCatalogService(session_service=session_service)
    expected_catalog_revision = resolver.revision

    page = await catalog.list_children(
        parent_node_id=folder.node_id,
        limit=100,
        cursor=None,
    )
    breadcrumb = await catalog.breadcrumb(session_id)
    search = await catalog.search(query="版本", limit=100, cursor=None)
    exported = await catalog.export_index()

    assert page.catalog_revision == expected_catalog_revision
    assert breadcrumb.catalog_revision == expected_catalog_revision
    assert search.catalog_revision == expected_catalog_revision
    assert exported.catalog_revision == expected_catalog_revision
    assert page.items[0].node_revision == resolver.catalog_store.get_node(
        session_id
    ).revision
    assert search.items
    for node in [
        *breadcrumb.items,
        *exported.items,
        *search.items[0].breadcrumb,
    ]:
        assert node.node_revision == resolver.catalog_store.get_node(node.node_id).revision

async def test_deleting_breadcrumb_keeps_catalog_and_node_revisions(
    tmp_path: Path,
) -> None:
    session_service = _SessionService(tmp_path / "sessions")
    resolver = session_service.path_resolver
    folder = resolver.get_node(resolver.catalog_store.create_folder(create_prefixed_id("ses"), resolver.workspace_id, None, '删除中').node_id)
    deleting_node = resolver.catalog_store.set_node_state(folder.node_id, "deleting")
    catalog = SessionCatalogService(session_service=session_service)

    breadcrumb = await catalog.breadcrumb(folder.node_id)

    assert breadcrumb.catalog_revision == resolver.revision
    assert breadcrumb.items[-1].state == "deleting"
    assert breadcrumb.items[-1].node_revision == deleting_node.revision

async def test_catalog_snapshot_keeps_one_revision_when_writer_commits_after_read(
    tmp_path: Path, session_bundle_factory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_root = tmp_path / "sessions"
    service = _SessionService(sessions_root)
    resolver = service.path_resolver
    child_id, parent_id = canonical("snapshot-child"), canonical("snapshot-parent")
    session_bundle_factory(sessions_root, child_id)
    session_bundle_factory(sessions_root, parent_id)
    catalog = SessionCatalogService(session_service=service)
    before = resolver.catalog_store.get_node(child_id)
    original = resolver.catalog_store.read_nodes_snapshot

    def write_after_snapshot():
        revision, rows = original()
        resolver.catalog_store.move_node(child_id, parent_id)
        resolver.catalog_store.update_session_configuration(
            child_id, current_agent_id="default", current_provider_id="backup",
            updated_at=datetime(2026, 10, 9, tzinfo=UTC),
        )
        return revision, rows

    monkeypatch.setattr(resolver.catalog_store, "read_nodes_snapshot", write_after_snapshot)
    first = await catalog.export_index()
    first_child = next(node for node in first.items if node.node_id == child_id)
    assert first_child.session is not None
    assert first_child.parent_node_id is first_child.session.parent_session_id is None
    assert first_child.session.current_provider_id == "primary"
    assert first_child.session.updated_at.isoformat() == before.updated_at
    assert first.catalog_revision < resolver.revision
    assert service.get_calls == []
    monkeypatch.setattr(resolver.catalog_store, "read_nodes_snapshot", original)
    second = await catalog.export_index()
    second_child = next(node for node in second.items if node.node_id == child_id)
    assert second_child.session is not None
    assert second_child.parent_node_id == second_child.session.parent_session_id == parent_id
    assert second_child.session.current_provider_id == "backup"
    assert second_child.session.updated_at == datetime(2026, 10, 9, tzinfo=UTC)
    assert second.catalog_revision == resolver.revision

async def test_cursor_survives_catalog_writes_without_node_changes(
    tmp_path: Path,
) -> None:
    session_service = _SessionService(tmp_path / "sessions")
    resolver = session_service.path_resolver
    for index in range(3):
        resolver.get_node(resolver.catalog_store.create_folder(create_prefixed_id("ses"), resolver.workspace_id, None, f'节点{index}').node_id)
    catalog = SessionCatalogService(session_service=session_service)

    first = await catalog.list_children(parent_node_id=None, limit=1, cursor=None)
    assert first.cursor is not None
    generation_before = resolver.catalog_store.current_generation()
    with resolver.catalog_store.write_transaction():
        pass
    assert resolver.catalog_store.current_generation() == generation_before + 1
    assert resolver.revision == first.catalog_revision

    second = await catalog.list_children(
        parent_node_id=None,
        limit=1,
        cursor=first.cursor,
    )
    assert [node.name for node in second.items] == ["节点1"]
