"""SessionCatalogPathResolver 新模型解析器测试（OpenSpec 8.2-切片3a，R15）。

覆盖：读方法（SQLite 直读投影、fail closed）、节点投影（folder path=None、
updated_at==created_at、name==display_name）、导航写（逻辑移动不搬磁盘）、
删除适配（begin/finish 两阶段、单调用删除）、属性语义与
新旧 resolver 在等价小树上的语义对照抽查。
只使用 tmp_path，不触碰真实工作区。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.session_catalog_resolver import (
    SessionCatalogPathResolver,
    SessionChildSummary,
)
from app.core.session_catalog_store import (
    SessionCatalogStore,
    validate_session_id,
)
from app.core.session_control_store import SessionControlStore
from app.core.session_creation import SessionCreationService
from app.core.session_lifecycle_gate import NavigationTopologyGate
from app.core.session_paths import SessionPathResolver
from app.core.session_subtree_delete import SessionSubtreeDeleteService

WORKSPACE_ID = "ws-resolver"


# ----------------------------------------------------------------------
# 测试辅助
# ----------------------------------------------------------------------


def make_metadata(**overrides: object) -> dict[str, object]:
    """构造调用方 session_metadata（剥离 manifest 六字段闭集）。"""
    metadata: dict[str, object] = {
        "kind": "normal",
        "delegation": None,
        "generation_origin": None,
        "current_agent_id": "default",
        "current_provider_id": "default_provider",
        "context_source_session_id": None,
    }
    metadata.update(overrides)
    return metadata


def make_node_id() -> str:
    """生成满足 UUIDv4 位 profile 的节点 ID（folder 与 session 同形）。"""
    return f"ses_{uuid.uuid4().hex}"


def make_thread_id() -> str:
    """生成满足 UUIDv4 位 profile 的 thread ID。"""
    return f"thr_{uuid.uuid4().hex}"


def read_manifest(directory: Path) -> dict[str, object]:
    return json.loads(
        (directory / "session.json").read_text(encoding="utf-8")
    )


def publish_child_thread(
    session_dir: Path,
    *,
    thread_id: str,
    created_at: datetime,
) -> Path:
    """用 R20 store API 发布最小 child，再创建其受检物理目录。"""
    control = SessionControlStore(session_dir / "session-control.sqlite")
    key = f"resolver-{thread_id}"
    try:
        control.create_or_get_thread_creation_record(
            idempotency_key=key,
            initial_state="idle",
            preimage_hash="a" * 64,
            graph_binding=json.dumps({"graph_id": "resolver"}),
            capability_profile=json.dumps({}),
            created_at=created_at,
            thread_id=thread_id,
        )
        manifest = "{}"
        control.freeze_thread_creation_artifact_manifest(
            key,
            artifact_manifest=manifest,
            artifact_manifest_hash=hashlib.sha256(
                manifest.encode("utf-8")
            ).hexdigest(),
        )
        published = control.publish_thread_creation_record(key)
        locator = published.final_relative_locator
    finally:
        control.close()
    thread_dir = session_dir / locator
    thread_dir.mkdir(parents=True)
    return thread_dir


def directory_fingerprint(directory: Path) -> tuple[int, bytes, list[str]]:
    """抓取目录不可动指纹（mtime、session.json 字节、条目名集）。"""
    manifest_bytes = (directory / "session.json").read_bytes()
    entries = sorted(entry.name for entry in directory.iterdir())
    return directory.stat().st_mtime_ns, manifest_bytes, entries


async def allocated_session(
    resolver: SessionCatalogPathResolver,
    *,
    title: str = "测试会话",
    parent_node_id: str | None = None,
    manifest_overrides: dict[str, object] | None = None,
) -> tuple[str, Path]:
    """通过唯一 ``SessionCreationService`` 创建一个测试会话。"""
    metadata = make_metadata(**(manifest_overrides or {}))
    service = SessionCreationService(
        store=resolver.catalog_store,
        sessions_root=resolver.sessions_root,
        workspace_id=WORKSPACE_ID,
        gate=NavigationTopologyGate(resolver.sessions_root),
    )
    result = await service.create(
        idempotency_key=f"resolver-test-{uuid.uuid4().hex}",
        title=title,
        parent_node_id=parent_node_id,
        session_metadata=metadata,
    )
    return result.session_id, resolver.resolve_session_node(result.session_id)


async def build_catalog_tree(
    resolver: SessionCatalogPathResolver,
) -> dict[str, str]:
    """构造嵌套树：根folder F1{会话 S1{folder F2{会话 S2}}, 会话 S3}。"""
    f1 = resolver.create_folder(name="团队", parent_node_id=None)
    s1, _ = await allocated_session(
        resolver, title="根会话", parent_node_id=f1.node_id
    )
    f2 = resolver.create_folder(name="子文件夹", parent_node_id=s1)
    s2, _ = await allocated_session(
        resolver,
        title="子会话",
        parent_node_id=f2.node_id,
    )
    s3, _ = await allocated_session(resolver, title="旁会话")
    return {
        "f1": f1.node_id,
        "s1": s1,
        "f2": f2.node_id,
        "s2": s2,
        "s3": s3,
    }


# ----------------------------------------------------------------------
# fixture
# ----------------------------------------------------------------------


@pytest.fixture
def sessions_root(tmp_path: Path) -> Path:
    # parent 即 .boxteam/ 根：orphaned 隔离区落位 .boxteam/orphaned/。
    return tmp_path / ".boxteam" / "sessions"


@pytest.fixture
def store(tmp_path: Path, sessions_root: Path) -> SessionCatalogStore:
    catalog = SessionCatalogStore(
        tmp_path / ".boxteam" / "navigation" / "session-catalog.sqlite",
        sessions_root,
    )
    yield catalog
    catalog.close()


@pytest.fixture
def delete_service(
    store: SessionCatalogStore, sessions_root: Path
) -> SessionSubtreeDeleteService:
    return SessionSubtreeDeleteService(
        store=store,
        sessions_root=sessions_root,
        workspace_id=WORKSPACE_ID,
        gate=NavigationTopologyGate(sessions_root),
    )


@pytest.fixture
def resolver(
    store: SessionCatalogStore,
    sessions_root: Path,
    delete_service: SessionSubtreeDeleteService,
) -> SessionCatalogPathResolver:
    return SessionCatalogPathResolver(
        store=store,
        sessions_root=sessions_root,
        workspace_id=WORKSPACE_ID,
        delete_service=delete_service,
    )


# ----------------------------------------------------------------------
# 构造与兼容属性
# ----------------------------------------------------------------------


class TestConstructor:
    def test_rejects_non_store(self, sessions_root: Path) -> None:
        with pytest.raises(TypeError, match="store 必须是"):
            SessionCatalogPathResolver(
                store=object(),  # type: ignore[arg-type]
                sessions_root=sessions_root,
                workspace_id=WORKSPACE_ID,
                delete_service=None,  # type: ignore[arg-type]
            )

    def test_rejects_sessions_root_mismatch(
        self,
        store: SessionCatalogStore,
        tmp_path: Path,
        delete_service: SessionSubtreeDeleteService,
    ) -> None:
        with pytest.raises(ValueError, match="sessions_root"):
            SessionCatalogPathResolver(
                store=store,
                sessions_root=tmp_path / "other" / "sessions",
                workspace_id=WORKSPACE_ID,
                delete_service=delete_service,
            )

    def test_rejects_empty_workspace_id(
        self,
        store: SessionCatalogStore,
        sessions_root: Path,
        delete_service: SessionSubtreeDeleteService,
    ) -> None:
        with pytest.raises(ValueError, match="workspace_id"):
            SessionCatalogPathResolver(
                store=store,
                sessions_root=sessions_root,
                workspace_id="",
                delete_service=delete_service,
            )

    def test_index_path_is_store_database_path(
        self, resolver: SessionCatalogPathResolver, store: SessionCatalogStore
    ) -> None:
        assert resolver.index_path == store.database_path
        assert resolver.sessions_root == store.sessions_root


class TestInitializeAndProperties:
    def test_initialize_passes_on_consistent_catalog(
        self, resolver: SessionCatalogPathResolver
    ) -> None:
        resolver.create_folder(name="空目录", parent_node_id=None)
        resolver.initialize()

    def test_initialize_fail_closed_on_cycle(
        self, resolver: SessionCatalogPathResolver, store: SessionCatalogStore
    ) -> None:
        # 注入父子环（先合法插入再 UPDATE 成环，绕过外键插入顺序）。
        store.connection.execute(
            "INSERT INTO nodes (node_id, kind, parent_node_id, display_name, "
            "state, revision, workspace_id) "
            "VALUES ('ses_a', 'folder', NULL, 'a', 'active', 1, ?)",
            (WORKSPACE_ID,),
        )
        store.connection.execute(
            "INSERT INTO nodes (node_id, kind, parent_node_id, display_name, "
            "state, revision, workspace_id) "
            "VALUES ('ses_b', 'folder', 'ses_a', 'b', 'active', 1, ?)",
            (WORKSPACE_ID,),
        )
        store.connection.execute(
            "UPDATE nodes SET parent_node_id = 'ses_b' WHERE node_id = 'ses_a'"
        )
        with pytest.raises(RuntimeError, match="循环"):
            resolver.initialize()

    def test_physical_tree_error_is_none(
        self, resolver: SessionCatalogPathResolver
    ) -> None:
        assert resolver.physical_tree_error is None

    def test_legacy_inline_attachment_migration_record_empty(
        self, resolver: SessionCatalogPathResolver
    ) -> None:
        assert resolver.legacy_inline_attachment_migration_record == {}
        # 每次返回独立空 dict，调用方改动不污染实例状态。
        record = resolver.legacy_inline_attachment_migration_record
        record["x"] = 1
        assert resolver.legacy_inline_attachment_migration_record == {}

    def test_revision_sums_and_bumps_after_rename(
        self, resolver: SessionCatalogPathResolver
    ) -> None:
        folder = resolver.create_folder(name="目录", parent_node_id=None)
        before = resolver.revision
        assert before == 1  # folder 初始 revision=1，sum 即 1。
        resolver.update_node_name(folder.node_id, "新名")
        assert resolver.revision == before + 1

    def test_authoritative_revision_equals_revision(
        self, resolver: SessionCatalogPathResolver
    ) -> None:
        resolver.create_folder(name="目录", parent_node_id=None)
        assert resolver.authoritative_revision == resolver.revision

    def test_invalidate_noop_and_refresh_returns_projection(
        self, resolver: SessionCatalogPathResolver
    ) -> None:
        resolver.invalidate()
        folder = resolver.create_folder(name="目录", parent_node_id=None)
        nodes = resolver.refresh()
        assert [node.node_id for node in nodes] == [folder.node_id]
        # refresh 形参保留签名兼容，行为与直读一致。
        assert resolver.list_nodes(refresh=True) == nodes


# ----------------------------------------------------------------------
# 节点投影
# ----------------------------------------------------------------------


class TestProjection:
    @pytest.mark.asyncio
    async def test_session_projection_fields_complete(
        self, resolver: SessionCatalogPathResolver
    ) -> None:
        session_id, session_dir = await allocated_session(resolver)
        node = resolver.get_node(session_id)
        assert node.node_id == session_id
        assert node.kind == "session"
        assert node.path == session_dir
        assert node.path.is_absolute()
        assert node.name == "测试会话"
        assert node.parent_node_id is None
        assert node.created_at == node.updated_at
        assert node.created_at is not None and node.created_at.tzinfo is not None

    def test_folder_projection_path_is_none(
        self, resolver: SessionCatalogPathResolver
    ) -> None:
        folder = resolver.create_folder(name="目录", parent_node_id=None)
        node = resolver.get_node(folder.node_id)
        assert node.kind == "folder"
        assert node.path is None
        # folder 在 catalog 中无时间字段，投影不伪造时间值。
        assert node.created_at is None
        assert node.updated_at is None
        assert node.name == "目录"

    @pytest.mark.asyncio
    async def test_list_nodes_sorted_and_authoritative_equal(
        self, resolver: SessionCatalogPathResolver
    ) -> None:
        ids = await build_catalog_tree(resolver)
        nodes = resolver.list_nodes()
        assert [node.node_id for node in nodes] == sorted(
            ids.values()
        )
        assert resolver.list_authoritative_nodes() == nodes


# ----------------------------------------------------------------------
# 读方法
# ----------------------------------------------------------------------


class TestReadMethods:
    def test_get_node_missing_raises_key_error(
        self, resolver: SessionCatalogPathResolver
    ) -> None:
        with pytest.raises(KeyError):
            resolver.get_node(make_node_id())

    @pytest.mark.asyncio
    async def test_resolve_session_node_returns_bucket_dir(
        self, resolver: SessionCatalogPathResolver, sessions_root: Path
    ) -> None:
        session_id, session_dir = await allocated_session(resolver)
        resolved = resolver.resolve_session_node(session_id)
        assert resolved == session_dir
        assert resolved.is_relative_to(sessions_root)
        assert resolved.name == session_id  # 日期桶叶名 == session_id

    @pytest.mark.asyncio
    async def test_resolve_session_node_missing_dir_fail_closed(
        self, resolver: SessionCatalogPathResolver
    ) -> None:
        session_id, session_dir = await allocated_session(resolver)
        shutil.rmtree(session_dir)
        with pytest.raises(RuntimeError, match="缺失或不是普通目录"):
            resolver.resolve_session_node(session_id)

    def test_resolve_session_node_rejects_folder(
        self, resolver: SessionCatalogPathResolver
    ) -> None:
        folder = resolver.create_folder(name="目录", parent_node_id=None)
        with pytest.raises(RuntimeError, match="节点不是会话"):
            resolver.resolve_session_node(folder.node_id)

    @pytest.mark.asyncio
    async def test_resolve_session_node_for_runtime_matches_resolve(
        self, resolver: SessionCatalogPathResolver
    ) -> None:
        session_id, session_dir = await allocated_session(resolver)
        assert (
            resolver.resolve_session_node_for_runtime(session_id) == session_dir
        )

    @pytest.mark.asyncio
    async def test_resolve_thread_node_main_folds_to_session_dir(
        self, resolver: SessionCatalogPathResolver
    ) -> None:
        session_id, session_dir = await allocated_session(resolver)
        main_thread_id = resolver.get_node(session_id)
        assert main_thread_id.kind == "session"
        catalog_main_thread_id = str(
            store_main_thread_id(resolver, session_id)
        )
        assert resolver.resolve_thread_node(session_id, "main") == session_dir
        assert resolver.resolve_thread_node(session_id, session_id) == session_dir
        assert (
            resolver.resolve_thread_node(session_id, catalog_main_thread_id)
            == session_dir
        )

    @pytest.mark.asyncio
    async def test_resolve_thread_node_non_main_resolves_threads_subdir(
        self, resolver: SessionCatalogPathResolver
    ) -> None:
        session_id, session_dir = await allocated_session(resolver)
        thread_id = make_thread_id()
        thread_dir = publish_child_thread(
            session_dir,
            thread_id=thread_id,
            created_at=datetime(2026, 6, 1, tzinfo=UTC),
        )
        assert resolver.resolve_thread_node(session_id, thread_id) == thread_dir

    @pytest.mark.asyncio
    async def test_resolve_thread_node_non_main_missing_threads_root_fail_closed(
        self, resolver: SessionCatalogPathResolver
    ) -> None:
        session_id, _ = await allocated_session(resolver)
        # 没有 thread_catalog child row 时，物理目录是否存在都不影响可见性。
        with pytest.raises(KeyError, match="child thread"):
            resolver.resolve_thread_node(session_id, make_thread_id())

    @pytest.mark.asyncio
    async def test_resolve_thread_node_rejects_non_canonical_thread_id(
        self, resolver: SessionCatalogPathResolver
    ) -> None:
        session_id, _ = await allocated_session(resolver)
        with pytest.raises(ValueError, match="thread_id"):
            resolver.resolve_thread_node(session_id, "../escape")

    @pytest.mark.asyncio
    async def test_resolve_thread_node_ignores_unregistered_date_bucket_directories(
        self, resolver: SessionCatalogPathResolver
    ) -> None:
        session_id, session_dir = await allocated_session(resolver)
        thread_id = make_thread_id()
        (session_dir / "threads" / "2026" / "06" / "01" / thread_id).mkdir(
            parents=True
        )
        (session_dir / "threads" / "2026" / "06" / "02" / thread_id).mkdir(
            parents=True
        )
        with pytest.raises(KeyError, match="child thread"):
            resolver.resolve_thread_node(session_id, thread_id)

    @pytest.mark.asyncio
    async def test_resolve_thread_node_published_missing_directory_fails_closed(
        self, resolver: SessionCatalogPathResolver
    ) -> None:
        session_id, session_dir = await allocated_session(resolver)
        thread_id = make_thread_id()
        publish_child_thread(
            session_dir,
            thread_id=thread_id,
            created_at=datetime(2026, 6, 1, tzinfo=UTC),
        )
        thread_dir = session_dir / "threads" / "2026" / "06" / "01" / thread_id
        shutil.rmtree(thread_dir)
        with pytest.raises(RuntimeError, match="冻结 locator 缺失"):
            resolver.resolve_thread_node(session_id, thread_id)

    def test_resolve_folder_dir_always_runtime_error(
        self, resolver: SessionCatalogPathResolver
    ) -> None:
        folder = resolver.create_folder(name="目录", parent_node_id=None)
        with pytest.raises(RuntimeError, match="folder 无物理目录"):
            resolver.resolve_folder_dir(folder.node_id)
        with pytest.raises(KeyError):
            resolver.resolve_folder_dir(make_node_id())

    @pytest.mark.asyncio
    async def test_relative_and_workspace_relative_path(
        self, resolver: SessionCatalogPathResolver
    ) -> None:
        session_id, _ = await allocated_session(resolver)
        relative = resolver.relative_path(session_id)
        assert relative.startswith("20")  # YYYY/MM/DD/ses_x
        assert relative.endswith(session_id)
        workspace_relative = resolver.workspace_relative_path(session_id)
        assert workspace_relative == f".boxteam/sessions/{relative}"

    def test_relative_path_rejects_folder(
        self, resolver: SessionCatalogPathResolver
    ) -> None:
        folder = resolver.create_folder(name="目录", parent_node_id=None)
        with pytest.raises(RuntimeError, match="folder 无物理目录"):
            resolver.relative_path(folder.node_id)

    @pytest.mark.asyncio
    async def test_child_nodes_sorted_children(
        self, resolver: SessionCatalogPathResolver
    ) -> None:
        ids = await build_catalog_tree(resolver)
        root_children = resolver.child_nodes(None)
        # 根级：F1 与 S3（S1 挂在 F1 下，S2 挂在 F2 下）。
        assert [node.node_id for node in root_children] == sorted(
            [ids["f1"], ids["s3"]]
        )
        f1_children = resolver.child_nodes(ids["f1"])
        assert [node.node_id for node in f1_children] == [ids["s1"]]
        f2_children = resolver.child_nodes(ids["f2"])
        assert [node.node_id for node in f2_children] == [ids["s2"]]

    def test_child_nodes_missing_node_raises_key_error(
        self, resolver: SessionCatalogPathResolver
    ) -> None:
        with pytest.raises(KeyError):
            resolver.child_nodes(make_node_id())

    @pytest.mark.asyncio
    async def test_child_session_summary_titles_and_pagination(
        self, resolver: SessionCatalogPathResolver
    ) -> None:
        session_id, _ = await allocated_session(resolver, title="父会话")
        child_titles = ["子一", "子二", "子三"]
        child_ids = []
        for title in child_titles:
            child_id, _ = await allocated_session(
                resolver,
                title=title,
                parent_node_id=session_id,
            )
            child_ids.append(child_id)
        count, summaries, has_more = resolver.child_session_summary(
            session_id, limit=2
        )
        assert count == 3
        assert has_more is True
        assert [item.session_id for item in summaries] == sorted(child_ids)[:2]
        for item in summaries:
            assert isinstance(item, SessionChildSummary)
            assert item.title in child_titles  # title == catalog display_name
            assert item.created_at  # catalog created_at ISO 文本
        count_full, all_summaries, has_more_full = resolver.child_session_summary(
            session_id, limit=10
        )
        assert count_full == 3
        assert has_more_full is False
        assert [item.session_id for item in all_summaries] == sorted(child_ids)

    @pytest.mark.asyncio
    async def test_child_session_summary_includes_sessions_under_child_folder(
        self, resolver: SessionCatalogPathResolver
    ) -> None:
        ids = await build_catalog_tree(resolver)
        # S1 的直接逻辑子会话：S2（经 F2 间接挂载）；不含 S1 自身/S3。
        count, summaries, has_more = resolver.child_session_summary(
            ids["s1"], limit=10
        )
        assert (count, has_more) == (1, False)
        assert [item.session_id for item in summaries] == [ids["s2"]]
        assert summaries[0].title == "子会话"

    @pytest.mark.asyncio
    async def test_child_session_summary_rejects_invalid_input(
        self, resolver: SessionCatalogPathResolver
    ) -> None:
        folder = resolver.create_folder(name="目录", parent_node_id=None)
        with pytest.raises(KeyError):
            resolver.child_session_summary(folder.node_id, limit=10)
        with pytest.raises(KeyError):
            resolver.child_session_summary(make_node_id(), limit=10)
        session_id, _ = await allocated_session(resolver)
        with pytest.raises(ValueError, match="limit"):
            resolver.child_session_summary(session_id, limit=0)

    @pytest.mark.asyncio
    async def test_descendant_session_ids_nested_tree(
        self, resolver: SessionCatalogPathResolver
    ) -> None:
        ids = await build_catalog_tree(resolver)
        assert resolver.descendant_session_ids(ids["f1"]) == sorted(
            [ids["s1"], ids["s2"]]
        )
        assert resolver.descendant_session_ids(ids["s1"]) == [ids["s2"]]
        assert resolver.descendant_session_ids(
            ids["s1"], include_self=True
        ) == sorted([ids["s1"], ids["s2"]])
        with pytest.raises(KeyError):
            resolver.descendant_session_ids(make_node_id())

    @pytest.mark.asyncio
    async def test_nearest_session_ancestor_old_semantics(
        self, resolver: SessionCatalogPathResolver
    ) -> None:
        ids = await build_catalog_tree(resolver)
        # 旧语义包含传入节点本身：session 传入即返回自身。
        assert resolver.nearest_session_ancestor(ids["s1"]) == ids["s1"]
        assert resolver.nearest_session_ancestor(ids["f2"]) == ids["s1"]
        assert resolver.nearest_session_ancestor(ids["f1"]) is None
        assert resolver.nearest_session_ancestor(None) is None
        with pytest.raises(RuntimeError, match="物理会话节点不存在"):
            resolver.nearest_session_ancestor(make_node_id())

    @pytest.mark.asyncio
    async def test_breadcrumb_chain(
        self, resolver: SessionCatalogPathResolver
    ) -> None:
        ids = await build_catalog_tree(resolver)
        chain = resolver.breadcrumb(ids["s2"])
        assert [node.node_id for node in chain] == [
            ids["f1"],
            ids["s1"],
            ids["f2"],
            ids["s2"],
        ]


def store_main_thread_id(
    resolver: SessionCatalogPathResolver, session_id: str
) -> str | None:
    """从 catalog 行读取 main_thread_id（测试辅助）。"""
    return resolver._store.get_node(session_id).main_thread_id


# ----------------------------------------------------------------------
# 导航写方法（SQLite-only，不动物理）
# ----------------------------------------------------------------------


class TestNavigationWrites:
    @pytest.mark.asyncio
    async def test_update_node_name_only_changes_catalog(
        self, resolver: SessionCatalogPathResolver
    ) -> None:
        session_id, session_dir = await allocated_session(resolver)
        manifest_before = (session_dir / "session.json").read_bytes()
        revision_before = resolver.revision
        node = resolver.update_node_name(session_id, "新标题")
        assert node.name == "新标题"
        assert resolver.get_node(session_id).name == "新标题"
        # 物理目录与 manifest 不动（旧语义 rename 物理目录名，新语义仅 SQLite）。
        assert (session_dir / "session.json").read_bytes() == manifest_before
        assert session_dir.name == session_id
        assert resolver.revision == revision_before + 1

    @pytest.mark.asyncio
    async def test_move_node_changes_parent_without_disk_move(
        self, resolver: SessionCatalogPathResolver
    ) -> None:
        target = resolver.create_folder(name="目标", parent_node_id=None)
        mover = resolver.create_folder(name="被移动夹", parent_node_id=None)
        child_id, child_dir = await allocated_session(
            resolver, title="子会话", parent_node_id=mover.node_id
        )
        fingerprint = directory_fingerprint(child_dir)
        node = resolver.move_node(
            node_id=mover.node_id, parent_node_id=target.node_id
        )
        assert node.parent_node_id == target.node_id
        # 逻辑移动不搬磁盘：子会话物理目录路径、mtime、字节、条目集不变。
        assert resolver.resolve_session_node(child_id) == child_dir
        assert directory_fingerprint(child_dir) == fingerprint

    @pytest.mark.asyncio
    async def test_move_node_rejects_session_node(
        self, resolver: SessionCatalogPathResolver
    ) -> None:
        session_id, _ = await allocated_session(resolver)
        with pytest.raises(ValueError, match="relocate_session"):
            resolver.move_node(node_id=session_id, parent_node_id=None)

    def test_move_node_rejects_renaming_name(
        self, resolver: SessionCatalogPathResolver
    ) -> None:
        folder = resolver.create_folder(name="原名", parent_node_id=None)
        with pytest.raises(ValueError, match="update_node_name"):
            resolver.move_node(
                node_id=folder.node_id, parent_node_id=None, name="改名"
            )
        # 同名 name 保持兼容放行。
        moved = resolver.move_node(
            node_id=folder.node_id, parent_node_id=None, name="原名"
        )
        assert moved.name == "原名"

    def test_move_node_cycle_rejected(
        self, resolver: SessionCatalogPathResolver
    ) -> None:
        f1 = resolver.create_folder(name="外层", parent_node_id=None)
        f2 = resolver.create_folder(name="内层", parent_node_id=f1.node_id)
        with pytest.raises(RuntimeError, match="循环"):
            resolver.move_node(node_id=f1.node_id, parent_node_id=f2.node_id)

    @pytest.mark.asyncio
    async def test_relocate_session_changes_parent_without_disk_move(
        self, resolver: SessionCatalogPathResolver
    ) -> None:
        target_session, _ = await allocated_session(resolver, title="目标会话")
        session_id, session_dir = await allocated_session(resolver)
        fingerprint = directory_fingerprint(session_dir)
        node = resolver.relocate_session(
            session_id=session_id, parent_node_id=target_session
        )
        assert node.parent_node_id == target_session
        assert resolver.resolve_session_node(session_id) == session_dir
        assert directory_fingerprint(session_dir) == fingerprint
        # manifest 的 parent_session_id 由 catalog 派生，不改写文件。
        assert "parent_session_id" not in read_manifest(session_dir)

    def test_relocate_session_rejects_folder(
        self, resolver: SessionCatalogPathResolver
    ) -> None:
        folder = resolver.create_folder(name="目录", parent_node_id=None)
        with pytest.raises(RuntimeError, match="节点不是会话"):
            resolver.relocate_session(session_id=folder.node_id, parent_node_id=None)

    @pytest.mark.asyncio
    async def test_relocate_folder_tree_moves_root_only(
        self, resolver: SessionCatalogPathResolver
    ) -> None:
        target, _ = await allocated_session(resolver, title="挂载会话")
        ids = await build_catalog_tree(resolver)
        f1_node_before = resolver.get_node(ids["f1"])
        # S2 在 F1 子树内，抓 S2 目录指纹验证不动磁盘。
        s2_dir = resolver.get_node(ids["s2"]).path
        assert s2_dir is not None
        s2_fingerprint = directory_fingerprint(s2_dir)
        moved = resolver.relocate_folder_tree(
            folder_id=ids["f1"], parent_node_id=target
        )
        assert moved.parent_node_id == target
        # 根改父，后代父子关系不变；会话物理目录不动。
        assert resolver.get_node(ids["s1"]).parent_node_id == ids["f1"]
        assert resolver.get_node(ids["s2"]).parent_node_id == ids["f2"]
        assert directory_fingerprint(s2_dir) == s2_fingerprint
        assert f1_node_before.path == moved.path

    @pytest.mark.asyncio
    async def test_expected_session_parents_after_folder_move_derivation(
        self, resolver: SessionCatalogPathResolver
    ) -> None:
        mount, _ = await allocated_session(resolver, title="挂载会话")
        root_session, _ = await allocated_session(resolver, title="根会话")
        f1 = resolver.create_folder(name="移动夹", parent_node_id=root_session)
        a, _ = await allocated_session(
            resolver, title="内层A", parent_node_id=f1.node_id
        )
        b, _ = await allocated_session(resolver, title="内层B", parent_node_id=a)
        revision_before = resolver.revision
        expected = resolver.expected_session_parents_after_folder_move(
            folder_id=f1.node_id,
            parent_node_id=mount,
        )
        # 移动后：A 的子树内最近 session 祖先缺失 → 外部挂载会话；B → A。
        assert expected == {a: mount, b: a}
        # 纯派生计算不产生任何写入。
        assert resolver.revision == revision_before

    @pytest.mark.asyncio
    async def test_expected_session_parents_rejects_self_and_cycle(
        self, resolver: SessionCatalogPathResolver
    ) -> None:
        root_session, _ = await allocated_session(resolver)
        f1 = resolver.create_folder(name="夹", parent_node_id=root_session)
        f2 = resolver.create_folder(name="子夹", parent_node_id=f1.node_id)
        with pytest.raises(ValueError, match="自身下"):
            resolver.expected_session_parents_after_folder_move(
                folder_id=f1.node_id, parent_node_id=f1.node_id
            )
        with pytest.raises(ValueError, match="循环"):
            resolver.expected_session_parents_after_folder_move(
                folder_id=f1.node_id, parent_node_id=f2.node_id
            )
        with pytest.raises(ValueError, match="会话文件夹"):
            resolver.expected_session_parents_after_folder_move(
                folder_id=root_session, parent_node_id=None
            )

    @pytest.mark.asyncio
    async def test_delete_folder_empty_ok_and_nonempty_rejected(
        self, resolver: SessionCatalogPathResolver
    ) -> None:
        empty = resolver.create_folder(name="空夹", parent_node_id=None)
        resolver.delete_folder(empty.node_id)
        with pytest.raises(KeyError):
            resolver.get_node(empty.node_id)
        nonempty = resolver.create_folder(name="非空夹", parent_node_id=None)
        await allocated_session(resolver, parent_node_id=nonempty.node_id)
        with pytest.raises(RuntimeError, match="非空"):
            resolver.delete_folder(nonempty.node_id)
        with pytest.raises(KeyError):
            resolver.delete_folder(make_node_id())

    def test_create_folder_no_physical_dir_no_manifest(
        self, resolver: SessionCatalogPathResolver, sessions_root: Path
    ) -> None:
        folder = resolver.create_folder(name="目录", parent_node_id=None)
        assert folder.node_id.startswith("ses_")
        assert len(folder.node_id) == 36
        # 无物理目录、无 manifest：sessions 树中不存在同名路径。
        assert not (sessions_root / folder.node_id).exists()
        assert not any(sessions_root.rglob(f"*{folder.node_id}*"))
        assert folder.path is None


# ----------------------------------------------------------------------
# 删除适配（R14 子树删除协议）
# ----------------------------------------------------------------------


class TestSubtreeDelete:
    @pytest.mark.asyncio
    async def test_begin_marks_whole_tree_deleting(
        self, resolver: SessionCatalogPathResolver, store: SessionCatalogStore
    ) -> None:
        ids = await build_catalog_tree(resolver)
        resolver.begin_subtree_delete(ids["f1"])
        for node_id in (ids["f1"], ids["s1"], ids["f2"], ids["s2"]):
            assert store.get_node(node_id).state == "deleting"
        # 子树外节点不受影响。
        assert store.get_node(ids["s3"]).state == "active"
        # 删除中子树拒绝新的 begin。
        with pytest.raises(RuntimeError):
            resolver.begin_subtree_delete(ids["f1"])

    @pytest.mark.asyncio
    async def test_begin_rejects_non_folder_and_missing(
        self, resolver: SessionCatalogPathResolver
    ) -> None:
        session_id, _ = await allocated_session(resolver)
        with pytest.raises(RuntimeError, match="会话文件夹"):
            resolver.begin_subtree_delete(session_id)
        with pytest.raises(KeyError):
            resolver.begin_subtree_delete(make_node_id())

    @pytest.mark.asyncio
    async def test_finish_drains_and_tombstones(
        self,
        resolver: SessionCatalogPathResolver,
        store: SessionCatalogStore,
        sessions_root: Path,
    ) -> None:
        ids = await build_catalog_tree(resolver)
        s1_dir = resolver.resolve_session_node(ids["s1"])
        s2_dir = resolver.resolve_session_node(ids["s2"])
        resolver.begin_subtree_delete(ids["f1"])
        await resolver.finish_subtree_delete(ids["f1"])
        # tombstone：全部冻结行删除。
        for node_id in (ids["f1"], ids["s1"], ids["f2"], ids["s2"]):
            with pytest.raises(KeyError):
                store.get_node(node_id)
        # 物理隔离：日期桶目录移入 .deleting/<key>/<session_id>/。
        deleting_root = sessions_root / ".deleting"
        isolated = [
            entry
            for entry in deleting_root.rglob("*")
            if entry.is_dir() and entry.name in (ids["s1"], ids["s2"])
        ]
        assert {entry.name for entry in isolated} == {ids["s1"], ids["s2"]}
        assert not s1_dir.exists()
        assert not s2_dir.exists()
        # 无对应 begin 的 finish → RuntimeError。
        with pytest.raises(RuntimeError, match="删除锁不存在"):
            await resolver.finish_subtree_delete(ids["f1"])

    @pytest.mark.asyncio
    async def test_finish_without_begin_rejected(
        self, resolver: SessionCatalogPathResolver
    ) -> None:
        folder = resolver.create_folder(name="目录", parent_node_id=None)
        with pytest.raises(RuntimeError, match="删除锁不存在"):
            await resolver.finish_subtree_delete(folder.node_id)

    @pytest.mark.asyncio
    async def test_delete_session_subtree_single_call(
        self,
        resolver: SessionCatalogPathResolver,
        store: SessionCatalogStore,
        sessions_root: Path,
    ) -> None:
        parent_id, parent_dir = await allocated_session(resolver, title="父会话")
        child_id, child_dir = await allocated_session(
            resolver, title="子会话", parent_node_id=parent_id
        )
        deleted = await resolver.delete_session_subtree(parent_id)
        assert deleted == [child_id]
        with pytest.raises(KeyError):
            store.get_node(parent_id)
        with pytest.raises(KeyError):
            store.get_node(child_id)
        assert not parent_dir.exists()
        assert not child_dir.exists()
        isolated = [
            entry
            for entry in (sessions_root / ".deleting").rglob("*")
            if entry.is_dir() and entry.name in (parent_id, child_id)
        ]
        assert {entry.name for entry in isolated} == {parent_id, child_id}

    @pytest.mark.asyncio
    async def test_delete_session_subtree_rejects_folder_and_missing(
        self, resolver: SessionCatalogPathResolver
    ) -> None:
        folder = resolver.create_folder(name="目录", parent_node_id=None)
        with pytest.raises(RuntimeError, match="节点不是会话"):
            await resolver.delete_session_subtree(folder.node_id)
        with pytest.raises(KeyError):
            await resolver.delete_session_subtree(make_node_id())


# ----------------------------------------------------------------------
# 语义对照抽查（旧 SessionPathResolver 在等价小树上）
# ----------------------------------------------------------------------


def build_equivalent_old_tree(
    sessions_root: Path, ids: dict[str, str]
) -> SessionPathResolver:
    """用与新树相同的稳定 ID 构造旧模型（JSON index + 物理树）小树。"""
    old = SessionPathResolver(sessions_root)
    old.initialize()
    old.create_folder(
        name="团队", parent_node_id=None, folder_id=ids["f1"]
    )
    now = datetime.now(UTC).isoformat()

    def register_old(session_id: str, title: str, parent_node_id: str | None) -> None:
        session_dir = old.allocate_session_dir(
            session_id=session_id,
            title=title,
            parent_node_id=parent_node_id,
        )
        manifest = {
            "session_id": session_id,
            "title": title,
            "parent_session_id": old.nearest_session_ancestor(parent_node_id),
            "created_at": now,
            "updated_at": now,
        }
        (session_dir / "session.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        old.register_session(session_id, session_dir)

    register_old(ids["s1"], "根会话", ids["f1"])
    old.create_folder(
        name="子文件夹", parent_node_id=ids["s1"], folder_id=ids["f2"]
    )
    register_old(ids["s2"], "子会话", ids["f2"])
    register_old(ids["s3"], "旁会话", None)
    return old


class TestSemanticParityWithLegacyResolver:
    @pytest.mark.asyncio
    async def test_list_nodes_node_id_sets_equal(
        self,
        resolver: SessionCatalogPathResolver,
        tmp_path: Path,
    ) -> None:
        ids = await build_catalog_tree(resolver)
        old = build_equivalent_old_tree(tmp_path / "legacy" / ".boxteam" / "sessions", ids)
        assert {node.node_id for node in old.list_nodes()} == {
            node.node_id for node in resolver.list_nodes()
        }

    @pytest.mark.asyncio
    async def test_nearest_session_ancestor_parity(
        self,
        resolver: SessionCatalogPathResolver,
        tmp_path: Path,
    ) -> None:
        ids = await build_catalog_tree(resolver)
        old = build_equivalent_old_tree(tmp_path / "legacy" / ".boxteam" / "sessions", ids)
        for probe in (*ids.values(), None):
            assert old.nearest_session_ancestor(probe) == (
                resolver.nearest_session_ancestor(probe)
            )

    @pytest.mark.asyncio
    async def test_descendants_and_child_summary_parity(
        self,
        resolver: SessionCatalogPathResolver,
        tmp_path: Path,
    ) -> None:
        ids = await build_catalog_tree(resolver)
        old = build_equivalent_old_tree(tmp_path / "legacy" / ".boxteam" / "sessions", ids)
        for root in (ids["f1"], ids["s1"], ids["f2"]):
            assert old.descendant_session_ids(root) == (
                resolver.descendant_session_ids(root)
            )
        old_summary = old.child_session_summary(ids["s1"], limit=10)
        new_summary = resolver.child_session_summary(ids["s1"], limit=10)
        assert old_summary[0] == new_summary[0]
        assert set(old_summary[1]) == {
            item.session_id for item in new_summary[1]
        }


# ----------------------------------------------------------------------
# store.create_or_get_creation_record 的可选 session_id（R17 §2.1-A）
# ----------------------------------------------------------------------


class TestStoreCreationRecordHonorsSessionId:
    def _create_kwargs(self, **overrides: object) -> dict[str, object]:
        kwargs: dict[str, object] = {
            "idempotency_key": f"key-{uuid.uuid4().hex}",
            "workspace_id": WORKSPACE_ID,
            "parent_node_id": None,
            "display_name": "指定ID会话",
            "created_at": datetime.now(UTC),
            "preimage_hash": "preimage-" + uuid.uuid4().hex,
        }
        kwargs.update(overrides)
        return kwargs

    def test_honors_canonical_session_id(
        self, store: SessionCatalogStore
    ) -> None:
        session_id = make_node_id()
        record = store.create_or_get_creation_record(
            **self._create_kwargs(session_id=session_id)
        )
        assert record.session_id == session_id
        # locator 叶名 == 传入 ID（日期桶按 record.created_at 冻结）。
        assert record.storage_relative_locator.endswith(f"/{session_id}")
        assert record.state == "preparing"

    def test_rejects_non_canonical_session_id(
        self, store: SessionCatalogStore
    ) -> None:
        with pytest.raises(ValueError, match="ses_replay"):
            store.create_or_get_creation_record(
                **self._create_kwargs(session_id="ses_replay")
            )

    def test_idempotent_same_key_same_session_id_returns_existing(
        self, store: SessionCatalogStore
    ) -> None:
        session_id = make_node_id()
        kwargs = self._create_kwargs(session_id=session_id)
        first = store.create_or_get_creation_record(**kwargs)
        second = store.create_or_get_creation_record(**kwargs)
        assert second.session_id == first.session_id == session_id
        assert second.session_creation_idempotency_key == (
            first.session_creation_idempotency_key
        )

    def test_conflicting_session_id_on_same_key_rejected(
        self, store: SessionCatalogStore
    ) -> None:
        kwargs = self._create_kwargs(session_id=make_node_id())
        store.create_or_get_creation_record(**kwargs)
        with pytest.raises(RuntimeError, match="session_id 冲突"):
            store.create_or_get_creation_record(
                **{**kwargs, "session_id": make_node_id()}
            )

    def test_software_allocation_unchanged_without_session_id(
        self, store: SessionCatalogStore
    ) -> None:
        record = store.create_or_get_creation_record(**self._create_kwargs())
        assert record.session_id.startswith("ses_")
        assert len(record.session_id) == 4 + 32
        validate_session_id(record.session_id)
