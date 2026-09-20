"""SessionSubtreeDeleteService 子树删除流测试（OpenSpec 8.1-B，R14）。

覆盖：完整流（mark 整树 deleting → 逐 session fence CAS + 物理隔离 →
finish 全树 tombstone）、单 Session 同协议、幂等（同 key 重入）、恢复
（deleting 未 drain / drain 中途 / finish 前崩溃）、冲突（同 key 异
root、子树含 deleting、mark CAS 漂移）、abort、空 folder root、
.deleting/ 目标冲突 fail closed、fence fail closed、并发同 key 收敛。
只使用 tmp_path，不触碰真实工作区。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.session_catalog_store import (
    SessionCatalogNode,
    SessionCatalogStore,
    SubtreeDeleteRecord,
)
from app.core.session_control_store import SessionControlStore
from app.core.session_subtree_delete import (
    SessionSubtreeDeleteService,
    SubtreeDeleteResult,
)

WORKSPACE_ID = "ws-delete"

_MOMENT = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
_DATE_DIR = "2026/06/01"
_DELETING_DIR_NAME = ".deleting"


def make_node_id() -> str:
    """生成满足 UUIDv4 位 profile 的节点 ID（folder 与 session 同形）。"""
    return f"ses_{uuid.uuid4().hex}"


def make_thread_id() -> str:
    return f"thr_{uuid.uuid4().hex}"


def build_session_directory(
    directory: Path,
    main_thread_id: str,
    created_at: datetime,
) -> None:
    """按 R12/R13 相同形态构造物理 session 目录（session.json + control DB）。"""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "session.json").write_text("{}\n", encoding="utf-8")
    control = SessionControlStore(directory / "session-control.sqlite")
    try:
        control.initialize_main_thread(main_thread_id, created_at)
        control.initialize_fence("active", 1)
    finally:
        control.close()


def create_physical_session(
    store: SessionCatalogStore,
    sessions_root: Path,
    *,
    parent_node_id: str | None,
    display_name: str = "会话",
) -> SessionCatalogNode:
    """创建 catalog session 节点并放置物理日期桶目录（fence=(active,1)）。"""
    session_id = make_node_id()
    locator = f"sessions/{_DATE_DIR}/{session_id}"
    node = store.create_session_node(
        session_id,
        WORKSPACE_ID,
        parent_node_id,
        display_name,
        _MOMENT,
        locator,
        make_thread_id(),
    )
    build_session_directory(
        sessions_root / _DATE_DIR / session_id,
        node.main_thread_id,
        _MOMENT,
    )
    return node


def date_bucket_dir(sessions_root: Path, locator: str) -> Path:
    return sessions_root / locator[len("sessions/"):]


def manually_drain_session(
    sessions_root: Path,
    store: SessionCatalogStore,
    *,
    key: str,
    session_id: str,
    locator: str,
) -> None:
    """用公开 API 复现单 session drain（模拟上次运行的已持久化进度）。"""
    source = date_bucket_dir(sessions_root, locator)
    control = SessionControlStore(source / "session-control.sqlite")
    try:
        assert control.cas_fence_transition(1, "deleting") is True
    finally:
        control.close()
    target = sessions_root / _DELETING_DIR_NAME / key / session_id
    target.parent.mkdir(parents=True, exist_ok=True)
    os.rename(source, target)
    store.record_drain_progress(key, session_id)


def prepare_and_mark(
    store: SessionCatalogStore,
    *,
    key: str,
    root_node_id: str,
) -> SubtreeDeleteRecord:
    """测试辅助：直接在 store 建 preparing record 并 mark 整树 deleting。"""
    store.create_or_get_subtree_delete_record(
        idempotency_key=key,
        workspace_id=WORKSPACE_ID,
        root_node_id=root_node_id,
    )
    store.mark_subtree_deleting(key)
    return store.get_subtree_delete_record(key)


@dataclass
class DeleteTree:
    """嵌套删除树 + 子树外对照 session：

    root(folder)
    ├── session_s1（session）
    │   └── session_s2（session）
    ├── folder_inner（folder）
    │   └── session_s3（session）
    └── outsider（session，子树外对照）
    """

    root: str
    session_s1: SessionCatalogNode
    session_s2: SessionCatalogNode
    folder_inner: str
    session_s3: SessionCatalogNode
    outsider: SessionCatalogNode

    @property
    def subtree_session_ids(self) -> list[str]:
        return [
            self.session_s1.node_id,
            self.session_s2.node_id,
            self.session_s3.node_id,
        ]

    @property
    def subtree_node_ids(self) -> list[str]:
        return [
            self.root,
            self.folder_inner,
            *self.subtree_session_ids,
        ]


@pytest.fixture
def sessions_root(tmp_path: Path) -> Path:
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
def service(
    store: SessionCatalogStore, sessions_root: Path
) -> SessionSubtreeDeleteService:
    return SessionSubtreeDeleteService(
        store=store,
        sessions_root=sessions_root,
        workspace_id=WORKSPACE_ID,
    )


@pytest.fixture
def tree(
    store: SessionCatalogStore, sessions_root: Path
) -> DeleteTree:
    root = store.create_folder(make_node_id(), WORKSPACE_ID, None, "删除根")
    session_s1 = create_physical_session(
        store, sessions_root, parent_node_id=root.node_id, display_name="会话1"
    )
    session_s2 = create_physical_session(
        store,
        sessions_root,
        parent_node_id=session_s1.node_id,
        display_name="会话2",
    )
    folder_inner = store.create_folder(
        make_node_id(), WORKSPACE_ID, root.node_id, "内层文件夹"
    )
    session_s3 = create_physical_session(
        store,
        sessions_root,
        parent_node_id=folder_inner.node_id,
        display_name="会话3",
    )
    outsider = create_physical_session(
        store, sessions_root, parent_node_id=None, display_name="子树外"
    )
    return DeleteTree(
        root=root.node_id,
        session_s1=session_s1,
        session_s2=session_s2,
        folder_inner=folder_inner.node_id,
        session_s3=session_s3,
        outsider=outsider,
    )


# ----------------------------------------------------------------------
# 完整流
# ----------------------------------------------------------------------


async def test_delete_full_flow_tombstones_and_isolates(
    service: SessionSubtreeDeleteService,
    store: SessionCatalogStore,
    sessions_root: Path,
    tree: DeleteTree,
) -> None:
    result = await service.delete(
        idempotency_key="del-key-1", root_node_id=tree.root
    )
    assert result.record_state == "completed"
    assert result.root_node_id == tree.root
    assert result.frozen_node_ids == tuple(sorted(tree.subtree_node_ids))
    # drain 按冻结顺序（node_id 排序）进行
    assert result.drained_session_ids == tuple(sorted(tree.subtree_session_ids))
    # finish：整树 tombstone（行删除）
    for node_id in tree.subtree_node_ids:
        with pytest.raises(KeyError):
            store.get_node(node_id)
    # 逐 session fence CAS：隔离目录内 fence == (deleting, 2)
    for session_node in (
        tree.session_s1,
        tree.session_s2,
        tree.session_s3,
    ):
        session_id = session_node.node_id
        isolated = (
            sessions_root / _DELETING_DIR_NAME / "del-key-1" / session_id
        )
        assert isolated.is_dir()
        # 物理内容随目录整体隔离（session.json 保留）
        assert (isolated / "session.json").is_file()
        # 源日期桶目录已不在
        assert not date_bucket_dir(sessions_root, session_node.storage_relative_locator).exists()
        control = SessionControlStore(isolated / "session-control.sqlite")
        try:
            assert control.get_fence() == ("deleting", 2)
        finally:
            control.close()
    record = store.get_subtree_delete_record("del-key-1")
    assert record.state == "completed"
    assert record.abort_reason is None


async def test_runtime_drain_failure_blocks_physical_isolation(
    service: SessionSubtreeDeleteService,
    store: SessionCatalogStore,
    sessions_root: Path,
    tree: DeleteTree,
) -> None:
    """运行时 owner 未收敛时，删除必须停在 deleting 且保留源目录。"""
    calls: list[str] = []

    async def reject_drain(session_id: str) -> None:
        calls.append(session_id)
        raise RuntimeError("Node 调试实例无法核实终态")

    service.set_session_drain_callback(reject_drain)
    record = store.create_or_get_subtree_delete_record(
        idempotency_key="del-key-runtime-blocked",
        workspace_id=WORKSPACE_ID,
        root_node_id=tree.root,
    )
    first_session = min(record.frozen_session_locators)
    source = date_bucket_dir(
        sessions_root, record.frozen_session_locators[first_session]
    )

    with pytest.raises(RuntimeError, match="无法核实终态"):
        await service.delete(
            idempotency_key="del-key-runtime-blocked",
            root_node_id=tree.root,
        )

    assert calls == [first_session]
    assert source.is_dir()
    assert not (
        sessions_root
        / _DELETING_DIR_NAME
        / "del-key-runtime-blocked"
        / first_session
    ).exists()
    assert store.get_subtree_delete_record("del-key-runtime-blocked").state == (
        "deleting"
    )
    assert (
        store.get_subtree_delete_record("del-key-runtime-blocked").drained_session_ids
        == ()
    )


async def test_delete_leaves_outside_subtree_untouched(
    service: SessionSubtreeDeleteService,
    store: SessionCatalogStore,
    sessions_root: Path,
    tree: DeleteTree,
) -> None:
    await service.delete(idempotency_key="del-key-1", root_node_id=tree.root)
    outsider = tree.outsider
    node = store.get_node(outsider.node_id)
    assert node.state == "active"
    assert date_bucket_dir(
        sessions_root, outsider.storage_relative_locator
    ).is_dir()
    control = SessionControlStore(
        date_bucket_dir(sessions_root, outsider.storage_relative_locator)
        / "session-control.sqlite"
    )
    try:
        assert control.get_fence() == ("active", 1)
    finally:
        control.close()


async def test_delete_single_session_uses_same_protocol(
    service: SessionSubtreeDeleteService,
    store: SessionCatalogStore,
    sessions_root: Path,
    tree: DeleteTree,
) -> None:
    result = await service.delete(
        idempotency_key="del-key-1", root_node_id=tree.session_s2.node_id
    )
    assert result.record_state == "completed"
    assert result.frozen_node_ids == (tree.session_s2.node_id,)
    assert result.drained_session_ids == (tree.session_s2.node_id,)
    with pytest.raises(KeyError):
        store.get_node(tree.session_s2.node_id)
    isolated = (
        sessions_root
        / _DELETING_DIR_NAME
        / "del-key-1"
        / tree.session_s2.node_id
    )
    assert isolated.is_dir()
    control = SessionControlStore(isolated / "session-control.sqlite")
    try:
        assert control.get_fence() == ("deleting", 2)
    finally:
        control.close()
    # 父 session 未受影响
    assert store.get_node(tree.session_s1.node_id).state == "active"


async def test_delete_empty_folder_root_completes_without_drain(
    service: SessionSubtreeDeleteService,
    store: SessionCatalogStore,
    sessions_root: Path,
) -> None:
    root_id = store.create_folder(
        make_node_id(), WORKSPACE_ID, None, "空文件夹"
    ).node_id
    result = await service.delete(
        idempotency_key="del-key-1", root_node_id=root_id
    )
    assert result.record_state == "completed"
    assert result.frozen_node_ids == (root_id,)
    assert result.drained_session_ids == ()
    with pytest.raises(KeyError):
        store.get_node(root_id)
    # 无 drain 需要：隔离区未创建
    assert not (sessions_root / _DELETING_DIR_NAME / "del-key-1").exists()


# ----------------------------------------------------------------------
# 幂等与冲突
# ----------------------------------------------------------------------


async def test_delete_idempotent_same_key_returns_same_result(
    service: SessionSubtreeDeleteService,
    store: SessionCatalogStore,
    sessions_root: Path,
    tree: DeleteTree,
) -> None:
    first = await service.delete(
        idempotency_key="del-key-1", root_node_id=tree.root
    )
    second = await service.delete(
        idempotency_key="del-key-1", root_node_id=tree.root
    )
    assert second == first
    assert second.record_state == "completed"
    rows = store.connection.execute(
        "SELECT COUNT(*) FROM subtree_delete_records"
    ).fetchone()
    assert int(rows[0]) == 1
    # tombstone 不变、隔离目录不被触碰重建
    for node_id in tree.subtree_node_ids:
        with pytest.raises(KeyError):
            store.get_node(node_id)
    assert (
        sessions_root / _DELETING_DIR_NAME / "del-key-1" / tree.session_s1.node_id
    ).is_dir()


async def test_delete_same_key_different_root_rejected(
    service: SessionSubtreeDeleteService,
    store: SessionCatalogStore,
    tree: DeleteTree,
) -> None:
    await service.delete(idempotency_key="del-key-1", root_node_id=tree.root)
    with pytest.raises(RuntimeError, match="冲突"):
        await service.delete(
            idempotency_key="del-key-1",
            root_node_id=tree.outsider.node_id,
        )
    # 原 record 与 tombstone 结果不受影响
    assert store.get_subtree_delete_record("del-key-1").state == "completed"


async def test_delete_rejects_subtree_containing_deleting_node(
    service: SessionSubtreeDeleteService,
    store: SessionCatalogStore,
    tree: DeleteTree,
) -> None:
    store.set_node_state(tree.session_s2.node_id, "deleting")
    with pytest.raises(RuntimeError, match="非 active"):
        await service.delete(
            idempotency_key="del-key-1", root_node_id=tree.root
        )
    with pytest.raises(KeyError):
        store.get_subtree_delete_record("del-key-1")
    # 子树未被 mark
    assert store.get_node(tree.root).state == "active"


async def test_delete_mark_cas_failure_keeps_subtree_active(
    service: SessionSubtreeDeleteService,
    store: SessionCatalogStore,
    tree: DeleteTree,
) -> None:
    # 冻结后 rename 子树内节点（revision 漂移），再走 service delete
    store.create_or_get_subtree_delete_record(
        idempotency_key="del-key-1",
        workspace_id=WORKSPACE_ID,
        root_node_id=tree.root,
    )
    store.rename_node(tree.session_s1.node_id, "漂移")
    with pytest.raises(RuntimeError, match="漂移"):
        await service.delete(
            idempotency_key="del-key-1", root_node_id=tree.root
        )
    # 失败回滚：子树仍全部 active、record 保持 preparing
    for node_id in tree.subtree_node_ids:
        assert store.get_node(node_id).state == "active"
    assert store.get_subtree_delete_record("del-key-1").state == "preparing"


async def test_delete_missing_root_raises_keyerror(
    service: SessionSubtreeDeleteService,
    store: SessionCatalogStore,
) -> None:
    with pytest.raises(KeyError):
        await service.delete(
            idempotency_key="del-key-1", root_node_id=make_node_id()
        )
    with pytest.raises(KeyError):
        store.get_subtree_delete_record("del-key-1")


@pytest.mark.parametrize("key", ["", "a/b", "a\\b", "..", ".", "嵌\x00入"])
async def test_delete_unsafe_idempotency_key_rejected(
    service: SessionSubtreeDeleteService,
    store: SessionCatalogStore,
    tree: DeleteTree,
    key: str,
) -> None:
    with pytest.raises(ValueError, match="idempotency_key"):
        await service.delete(idempotency_key=key, root_node_id=tree.root)
    if key:
        with pytest.raises(KeyError):
            store.get_subtree_delete_record(key)


# ----------------------------------------------------------------------
# mark 后的部分可见性（唯一逻辑可见性关闭点）
# ----------------------------------------------------------------------


async def test_failed_drain_keeps_whole_subtree_deleting(
    service: SessionSubtreeDeleteService,
    store: SessionCatalogStore,
    sessions_root: Path,
    tree: DeleteTree,
) -> None:
    # 破坏第一个（冻结序）session 的控制库 → drain fail closed，
    # 但 mark 已提交：整棵子树必须保持 deleting，不出现部分 active
    record = store.create_or_get_subtree_delete_record(
        idempotency_key="del-key-1",
        workspace_id=WORKSPACE_ID,
        root_node_id=tree.root,
    )
    first_session = min(record.frozen_session_locators)
    (date_bucket_dir(sessions_root, record.frozen_session_locators[first_session]) / "session-control.sqlite").unlink()
    with pytest.raises(RuntimeError, match="缺少"):
        await service.delete(
            idempotency_key="del-key-1", root_node_id=tree.root
        )
    for node_id in tree.subtree_node_ids:
        assert store.get_node(node_id).state == "deleting"
    assert store.get_subtree_delete_record("del-key-1").state == "deleting"


# ----------------------------------------------------------------------
# 恢复：三个崩溃窗口
# ----------------------------------------------------------------------


async def test_recovery_deleting_not_drained_resumes_drain(
    service: SessionSubtreeDeleteService,
    store: SessionCatalogStore,
    sessions_root: Path,
    tree: DeleteTree,
) -> None:
    # 崩溃点：mark 已提交、drain 未开始 → 重入从 drain 继续
    prepare_and_mark(store, key="del-key-1", root_node_id=tree.root)
    for node_id in tree.subtree_node_ids:
        assert store.get_node(node_id).state == "deleting"
    result = await service.delete(
        idempotency_key="del-key-1", root_node_id=tree.root
    )
    assert result.record_state == "completed"
    for node_id in tree.subtree_node_ids:
        with pytest.raises(KeyError):
            store.get_node(node_id)


async def test_recovery_partial_drain_resumes_from_progress(
    service: SessionSubtreeDeleteService,
    store: SessionCatalogStore,
    sessions_root: Path,
    tree: DeleteTree,
) -> None:
    record = prepare_and_mark(store, key="del-key-1", root_node_id=tree.root)
    ordered = sorted(record.frozen_session_locators)
    first, second = ordered[0], ordered[1]
    # 上次运行已隔离第一个 session 并持久化进度
    manually_drain_session(
        sessions_root,
        store,
        key="del-key-1",
        session_id=first,
        locator=record.frozen_session_locators[first],
    )
    # 第二个 session 的隔离目标被预置垃圾内容 → 本轮 drain fail closed
    junk = sessions_root / _DELETING_DIR_NAME / "del-key-1" / second
    junk.mkdir(parents=True)
    (junk / "junk.txt").write_text("垃圾", encoding="utf-8")
    with pytest.raises(RuntimeError, match="同时存在"):
        await service.delete(
            idempotency_key="del-key-1", root_node_id=tree.root
        )
    assert store.get_subtree_delete_record("del-key-1").drained_session_ids == (
        first,
    )
    # 清理垃圾后重入：按 drained_session_ids 定点继续，不重做第一个
    shutil.rmtree(junk)
    result = await service.delete(
        idempotency_key="del-key-1", root_node_id=tree.root
    )
    assert result.record_state == "completed"
    assert result.drained_session_ids == tuple(ordered)
    for node_id in tree.subtree_node_ids:
        with pytest.raises(KeyError):
            store.get_node(node_id)


async def test_recovery_finish_crash_completes_on_reentry(
    service: SessionSubtreeDeleteService,
    store: SessionCatalogStore,
    sessions_root: Path,
    tree: DeleteTree,
) -> None:
    # 崩溃点：全部 session 已 drain、finish 未执行 → 重入直接 finish
    record = prepare_and_mark(store, key="del-key-1", root_node_id=tree.root)
    for session_id, locator in record.frozen_session_locators.items():
        manually_drain_session(
            sessions_root,
            store,
            key="del-key-1",
            session_id=session_id,
            locator=locator,
        )
    result = await service.delete(
        idempotency_key="del-key-1", root_node_id=tree.root
    )
    assert result.record_state == "completed"
    for node_id in tree.subtree_node_ids:
        with pytest.raises(KeyError):
            store.get_node(node_id)


# ----------------------------------------------------------------------
# abort 语义
# ----------------------------------------------------------------------


async def test_abort_mid_drain_keeps_nodes_and_isolated_dir(
    service: SessionSubtreeDeleteService,
    store: SessionCatalogStore,
    sessions_root: Path,
    tree: DeleteTree,
) -> None:
    record = prepare_and_mark(store, key="del-key-1", root_node_id=tree.root)
    ordered = sorted(record.frozen_session_locators)
    manually_drain_session(
        sessions_root,
        store,
        key="del-key-1",
        session_id=ordered[0],
        locator=record.frozen_session_locators[ordered[0]],
    )
    store.abort_subtree_delete("del-key-1", "人工中止")
    # 节点保持 deleting（不回滚 active）、隔离目录保留
    for node_id in tree.subtree_node_ids:
        assert store.get_node(node_id).state == "deleting"
    assert (
        sessions_root / _DELETING_DIR_NAME / "del-key-1" / ordered[0]
    ).is_dir()
    # 同 key 重入被拒（含 reason）
    with pytest.raises(RuntimeError, match="已中止"):
        await service.delete(
            idempotency_key="del-key-1", root_node_id=tree.root
        )


async def test_abort_preparing_then_new_key_delete_succeeds(
    service: SessionSubtreeDeleteService,
    store: SessionCatalogStore,
    tree: DeleteTree,
) -> None:
    store.create_or_get_subtree_delete_record(
        idempotency_key="del-key-1",
        workspace_id=WORKSPACE_ID,
        root_node_id=tree.root,
    )
    store.abort_subtree_delete("del-key-1", "预检 blocker")
    with pytest.raises(RuntimeError, match="已中止"):
        await service.delete(
            idempotency_key="del-key-1", root_node_id=tree.root
        )
    # 换新 key 重试：子树仍 active，可完整删除
    result = await service.delete(
        idempotency_key="del-key-2", root_node_id=tree.root
    )
    assert result.record_state == "completed"
    for node_id in tree.subtree_node_ids:
        with pytest.raises(KeyError):
            store.get_node(node_id)


# ----------------------------------------------------------------------
# .deleting/ 目标冲突与 fence fail closed
# ----------------------------------------------------------------------


async def test_delete_target_conflict_fail_closed(
    service: SessionSubtreeDeleteService,
    store: SessionCatalogStore,
    sessions_root: Path,
    tree: DeleteTree,
) -> None:
    record = store.create_or_get_subtree_delete_record(
        idempotency_key="del-key-1",
        workspace_id=WORKSPACE_ID,
        root_node_id=tree.root,
    )
    first_session = min(record.frozen_session_locators)
    junk = (
        sessions_root
        / _DELETING_DIR_NAME
        / "del-key-1"
        / first_session
    )
    junk.mkdir(parents=True)
    (junk / "junk.txt").write_text("垃圾", encoding="utf-8")
    with pytest.raises(RuntimeError, match="同时存在"):
        await service.delete(
            idempotency_key="del-key-1", root_node_id=tree.root
        )
    # fail closed：进度未记、目录未被覆盖
    assert (
        store.get_subtree_delete_record("del-key-1").drained_session_ids == ()
    )
    assert (junk / "junk.txt").is_file()


async def test_delete_fence_generation_mismatch_fail_closed(
    service: SessionSubtreeDeleteService,
    store: SessionCatalogStore,
    sessions_root: Path,
    tree: DeleteTree,
) -> None:
    record = store.create_or_get_subtree_delete_record(
        idempotency_key="del-key-1",
        workspace_id=WORKSPACE_ID,
        root_node_id=tree.root,
    )
    first_session = min(record.frozen_session_locators)
    locator = record.frozen_session_locators[first_session]
    control_path = (
        date_bucket_dir(sessions_root, locator) / "session-control.sqlite"
    )
    connection = sqlite3.connect(control_path)
    try:
        connection.execute("UPDATE lifecycle_fence SET generation = 5")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(RuntimeError, match="generation 不符"):
        await service.delete(
            idempotency_key="del-key-1", root_node_id=tree.root
        )
    # fail closed：源目录仍在原位、进度未记
    assert date_bucket_dir(sessions_root, locator).is_dir()
    assert (
        store.get_subtree_delete_record("del-key-1").drained_session_ids == ()
    )


# ----------------------------------------------------------------------
# 并发同 key 收敛
# ----------------------------------------------------------------------


async def test_concurrent_same_key_serializes_to_same_result(
    service: SessionSubtreeDeleteService,
    store: SessionCatalogStore,
    tree: DeleteTree,
) -> None:
    first, second = await asyncio.gather(
        service.delete(idempotency_key="del-key-1", root_node_id=tree.root),
        service.delete(idempotency_key="del-key-1", root_node_id=tree.root),
    )
    assert first == second
    assert first.record_state == "completed"
    rows = store.connection.execute(
        "SELECT COUNT(*) FROM subtree_delete_records"
    ).fetchone()
    assert int(rows[0]) == 1
    for node_id in tree.subtree_node_ids:
        with pytest.raises(KeyError):
            store.get_node(node_id)


# ----------------------------------------------------------------------
# 服务构造与入参
# ----------------------------------------------------------------------


def test_service_rejects_sessions_root_mismatch(
    store: SessionCatalogStore, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="不一致"):
        SessionSubtreeDeleteService(
            store=store,
            sessions_root=tmp_path / "other" / "sessions",
            workspace_id=WORKSPACE_ID,
        )


def test_service_rejects_empty_workspace(
    store: SessionCatalogStore, sessions_root: Path
) -> None:
    with pytest.raises(ValueError, match="workspace_id"):
        SessionSubtreeDeleteService(
            store=store,
            sessions_root=sessions_root,
            workspace_id="",
        )


def test_service_rejects_non_path_sessions_root(
    store: SessionCatalogStore,
) -> None:
    with pytest.raises(TypeError, match="Path"):
        SessionSubtreeDeleteService(
            store=store,
            sessions_root="not-a-path",  # type: ignore[arg-type]
            workspace_id=WORKSPACE_ID,
        )


async def test_service_default_gate_creates_own_gate(
    store: SessionCatalogStore, sessions_root: Path, tree: DeleteTree
) -> None:
    service = SessionSubtreeDeleteService(
        store=store,
        sessions_root=sessions_root,
        workspace_id=WORKSPACE_ID,
    )
    result: SubtreeDeleteResult = await service.delete(
        idempotency_key="del-key-1", root_node_id=tree.root
    )
    assert result.record_state == "completed"
