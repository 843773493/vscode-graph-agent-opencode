"""session-catalog.sqlite 基础设施测试。

对应 OpenSpec add-itemized-rollout-context 任务 8.1 第一片：只验证
基础设施（nodes 表、验证器、gate/fence 原语、CTE 查询），不切换权威。
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core.session_catalog_store import (
    CatalogBackupManifest,
    CatalogMaintenanceRequiredError,
    ForkRetentionClaim,
    SessionCatalogNode,
    SessionCatalogStore,
    SessionCreationRecord,
    SessionLifecycleFence,
    SourceRetainedByForkError,
    SourceRetentionOperationPendingError,
    SubtreeDeleteRecord,
    validate_path_budget,
    validate_session_id,
    validate_storage_relative_locator,
    validate_thread_id,
)
from app.core.session_lifecycle_gate import NavigationTopologyGate, SessionLifecycleGate

WORKSPACE_ID = "ws-primary"
OTHER_WORKSPACE_ID = "ws-other"


def make_session_id() -> str:
    """生成满足 UUIDv4 位 profile 的 session_id。"""
    return f"ses_{uuid.uuid4().hex}"


def make_thread_id() -> str:
    """生成满足 UUIDv4 位 profile 的 thread_id。"""
    return f"thr_{uuid.uuid4().hex}"


def make_locator(session_id: str, moment: datetime) -> str:
    """按 created_at 的 UTC 日期生成 locator。"""
    utc_date = moment.astimezone(UTC).date()
    return f"sessions/{utc_date:%Y/%m/%d}/{session_id}"


def create_session(
    store: SessionCatalogStore,
    *,
    workspace_id: str = WORKSPACE_ID,
    parent_node_id: str | None = None,
    display_name: str = "会话",
    created_at: datetime | None = None,
) -> SessionCatalogNode:
    """测试辅助：创建一个合法 session 节点并返回投影。"""
    session_id = make_session_id()
    moment = created_at or datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    return store.create_session_node(
        session_id,
        workspace_id,
        parent_node_id,
        display_name,
        moment,
        make_locator(session_id, moment),
        make_thread_id(),
    )


def _hex_payload_with(index: int, char: str) -> str:
    """把合法 UUIDv4 payload 的指定 hex 位替换成给定字符。"""
    payload = list(uuid.uuid4().hex)
    payload[index] = char
    return "".join(payload)


@pytest.fixture
def sessions_root(tmp_path: Path) -> Path:
    """sessions 根目录路径；本切片不创建物理目录。"""
    return tmp_path / "sessions"


@pytest.fixture
def store(tmp_path: Path, sessions_root: Path):
    catalog = SessionCatalogStore(
        tmp_path / "navigation" / "session-catalog.sqlite",
        sessions_root,
    )
    yield catalog
    catalog.close()


@dataclass
class CatalogTree:
    """多层嵌套测试树：

    root
    ├── folder_a（folder）
    │   ├── session_s1（session）
    │   │   ├── folder_b（folder）
    │   │   │   └── session_s3（session）
    │   │   └── session_s2（session）
    │   └── session_s4（session）
    └── session_s5（session）
    """

    folder_a: str
    session_s1: str
    folder_b: str
    session_s2: str
    session_s3: str
    session_s4: str
    session_s5: str


@pytest.fixture
def tree(store: SessionCatalogStore) -> CatalogTree:
    folder_a = store.create_folder(
        make_session_id(), WORKSPACE_ID, None, "文件夹A"
    ).node_id
    session_s1 = create_session(
        store, parent_node_id=folder_a, display_name="会话1"
    ).node_id
    folder_b = store.create_folder(
        make_session_id(), WORKSPACE_ID, session_s1, "文件夹B"
    ).node_id
    session_s3 = create_session(
        store, parent_node_id=folder_b, display_name="会话3"
    ).node_id
    session_s2 = create_session(
        store, parent_node_id=session_s1, display_name="会话2"
    ).node_id
    session_s4 = create_session(
        store, parent_node_id=folder_a, display_name="会话4"
    ).node_id
    session_s5 = create_session(store, parent_node_id=None, display_name="会话5").node_id
    return CatalogTree(
        folder_a=folder_a,
        session_s1=session_s1,
        folder_b=folder_b,
        session_s2=session_s2,
        session_s3=session_s3,
        session_s4=session_s4,
        session_s5=session_s5,
    )


# ----------------------------------------------------------------------
# 验证器：session_id / thread_id
# ----------------------------------------------------------------------


def test_validate_session_id_accepts_uuid_v4_profile() -> None:
    validate_session_id(make_session_id())


def test_validate_thread_id_accepts_uuid_v4_profile() -> None:
    validate_thread_id(make_thread_id())


@pytest.mark.parametrize(
    "value",
    [
        "thr_" + uuid.uuid4().hex,  # 错误前缀
        "job_" + uuid.uuid4().hex,  # 错误前缀
        uuid.uuid4().hex,  # 缺前缀
        "ses_" + "0" * 31,  # 长度 31
        "ses_" + "0" * 33,  # 长度 33
        "SES_" + uuid.uuid4().hex,  # 大写前缀
        "ses_" + uuid.uuid4().hex.upper(),  # 大写 hex
        "ses_" + "g" + uuid.uuid4().hex[1:],  # 非 hex
        "ses_" + _hex_payload_with(12, "3"),  # 坏 v4 version 位
        "ses_" + _hex_payload_with(12, "5"),  # 坏 v4 version 位
        "ses_" + _hex_payload_with(16, "c"),  # 坏 variant 位
        "ses_" + _hex_payload_with(16, "7"),  # 坏 variant 位
        "ses_/" + uuid.uuid4().hex[:31],  # 斜杠
        "ses_\\",  # 反斜杠
        "ses_..",
        "ses_.",
        "ses_" + "测" * 32,  # Unicode
        "ses_" + uuid.uuid4().hex + "/",  # 尾部斜杠
        " ses_" + uuid.uuid4().hex,  # 前导空白
        "",
    ],
)
def test_validate_session_id_rejects_invalid(value: str) -> None:
    with pytest.raises(ValueError):
        validate_session_id(value)


@pytest.mark.parametrize("value", [None, 123])
def test_validate_session_id_rejects_non_string(value: object) -> None:
    with pytest.raises(TypeError):
        validate_session_id(value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "value",
    [
        "ses_" + uuid.uuid4().hex,  # 错误前缀
        "thr_" + "0" * 31,  # 长度 31
        "THR_" + uuid.uuid4().hex,  # 大写前缀
        "thr_" + _hex_payload_with(12, "3"),  # 坏 v4 version 位
        "thr_" + _hex_payload_with(16, "c"),  # 坏 variant 位
        "thr_..",
    ],
)
def test_validate_thread_id_rejects_invalid(value: str) -> None:
    with pytest.raises(ValueError):
        validate_thread_id(value)


def test_validate_thread_id_rejects_non_string() -> None:
    with pytest.raises(TypeError):
        validate_thread_id(None)  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# 验证器：storage locator
# ----------------------------------------------------------------------


def test_validate_storage_relative_locator_accepts_valid() -> None:
    validate_storage_relative_locator(f"sessions/2026/06/01/{make_session_id()}")


def test_validate_storage_relative_locator_accepts_leap_day() -> None:
    # 2024 是闰年，2 月 29 日合法
    validate_storage_relative_locator(f"sessions/2024/02/29/{make_session_id()}")


def _invalid_locators() -> list[str]:
    """构造逐类非法 locator；session_id 部分用真实合法 ID，确保失败归因于被测形态。"""
    sid = make_session_id()
    return [
        f"session/2026/06/01/{sid}",  # 坏前缀
        f"2026/06/01/{sid}",  # 缺前缀
        f"sessions/26/06/01/{sid}",  # 年非 4 位
        f"sessions/2026/6/01/{sid}",  # 月非两位
        f"sessions/2026/06/1/{sid}",  # 日非两位
        f"sessions/2026/13/01/{sid}",  # 月 13
        f"sessions/2026/00/01/{sid}",  # 月 00
        f"sessions/2026/06/00/{sid}",  # 日 00
        f"sessions/2026/06/32/{sid}",  # 日 32
        f"sessions/2026/02/30/{sid}",  # 2 月 30 日
        f"sessions/2026/02/29/{sid}",  # 2026 非闰年
        f"sessions/2026/04/31/{sid}",  # 4 月 31 日
        f"sessions/2026/06/01/{sid}/",  # 尾部斜杠
        f"sessions/2026/06/01/{sid}/extra",  # 多余组件
        f"sessions/2026/06/01/thr_{uuid.uuid4().hex}",  # 叶名是 thread 前缀
        f"sessions/2026/06/01/ses_{_hex_payload_with(12, '3')}",  # session_id 非法
        f"sessions/2026/06/01/ses_{_hex_payload_with(16, 'c')}",  # session_id 非法
        f"sessions/2026/06/01/ses_{'0' * 31}",  # session_id 长度非法
        f"sessions\\2026\\06\\01\\{sid}",  # 反斜杠
        f"sessions/2026/../01/{sid}",  # ..
        "sessions/2026/06/01/..",  # ..
        f"Sessions/2026/06/01/{sid}",  # 大写前缀
        "",
    ]


@pytest.mark.parametrize("locator", _invalid_locators())
def test_validate_storage_relative_locator_rejects_invalid(locator: str) -> None:
    with pytest.raises(ValueError):
        validate_storage_relative_locator(locator)


def test_validate_storage_relative_locator_rejects_non_string() -> None:
    with pytest.raises(TypeError):
        validate_storage_relative_locator(None)  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# 验证器：路径预算
# ----------------------------------------------------------------------


def test_validate_path_budget_accepts_normal_path(tmp_path: Path) -> None:
    validate_path_budget(tmp_path / "sessions", "2026/06/01/" + make_session_id())


def test_validate_path_budget_rejects_long_component(tmp_path: Path) -> None:
    base = tmp_path / ("a" * 300)
    with pytest.raises(ValueError, match="组件"):
        validate_path_budget(base, "2026/06/01/" + make_session_id())


def test_validate_path_budget_rejects_long_total(tmp_path: Path) -> None:
    # 18 个 250 字符组件：每个组件 ≤255 bytes，但总长超过 4096 bytes
    base = tmp_path.joinpath(*(["b" * 250] * 18))
    with pytest.raises(ValueError, match="总长"):
        validate_path_budget(base, "2026/06/01/" + make_session_id())


# ----------------------------------------------------------------------
# store 基础：schema / 连接约定 / user_version
# ----------------------------------------------------------------------


def test_store_initializes_wal_and_pragmas(store: SessionCatalogStore) -> None:
    assert (
        str(store.connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        == "wal"
    )
    assert int(store.connection.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
    assert int(store.connection.execute("PRAGMA busy_timeout").fetchone()[0]) == 5000
    assert int(store.connection.execute("PRAGMA user_version").fetchone()[0]) == 3


def test_store_does_not_create_sessions_root(
    store: SessionCatalogStore, sessions_root: Path
) -> None:
    assert not sessions_root.exists()


def test_store_rejects_unknown_user_version(
    tmp_path: Path, sessions_root: Path
) -> None:
    database_path = tmp_path / "navigation" / "session-catalog.sqlite"
    first = SessionCatalogStore(database_path, sessions_root)
    first.close()
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA user_version = 99")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(RuntimeError, match="user_version"):
        SessionCatalogStore(database_path, sessions_root)


def test_store_reopens_existing_database(
    tmp_path: Path, sessions_root: Path
) -> None:
    database_path = tmp_path / "navigation" / "session-catalog.sqlite"
    first = SessionCatalogStore(database_path, sessions_root)
    folder_id = make_session_id()
    first.create_folder(folder_id, WORKSPACE_ID, None, "持久文件夹")
    first.close()
    second = SessionCatalogStore(database_path, sessions_root)
    try:
        assert second.get_node(folder_id).display_name == "持久文件夹"
        assert int(second.connection.execute("PRAGMA user_version").fetchone()[0]) == 3
    finally:
        second.close()


def test_closed_store_rejects_operations(store: SessionCatalogStore) -> None:
    store.close()
    with pytest.raises(RuntimeError, match="已关闭"):
        store.get_node(make_session_id())


# ----------------------------------------------------------------------
# CRUD 全路径
# ----------------------------------------------------------------------


def test_create_folder_roundtrip(store: SessionCatalogStore) -> None:
    folder_id = make_session_id()
    node = store.create_folder(folder_id, WORKSPACE_ID, None, "根文件夹")
    assert node.node_id == folder_id
    assert node.kind == "folder"
    assert node.parent_node_id is None
    assert node.display_name == "根文件夹"
    assert node.state == "active"
    assert node.revision == 1
    assert node.workspace_id == WORKSPACE_ID
    assert node.created_at is None
    assert node.storage_relative_locator is None
    assert node.main_thread_id is None
    assert store.get_node(folder_id) == node


def test_create_session_node_roundtrip(store: SessionCatalogStore) -> None:
    node = create_session(store, display_name="子会话")
    assert node.kind == "session"
    assert node.state == "active"
    assert node.revision == 1
    assert node.created_at == "2026-06-01T12:00:00+00:00"
    assert node.storage_relative_locator == f"sessions/2026/06/01/{node.node_id}"
    assert node.main_thread_id.startswith("thr_")
    assert len(node.main_thread_id) == 36
    assert store.get_node(node.node_id) == node


def test_rename_node_bumps_revision(store: SessionCatalogStore) -> None:
    folder = store.create_folder(make_session_id(), WORKSPACE_ID, None, "旧名")
    renamed = store.rename_node(folder.node_id, "新名")
    assert renamed.display_name == "新名"
    assert renamed.revision == 2
    assert store.get_node(folder.node_id).revision == 2


def test_rename_node_rejects_empty_name(store: SessionCatalogStore) -> None:
    folder = store.create_folder(make_session_id(), WORKSPACE_ID, None, "名字")
    with pytest.raises(ValueError, match="显示名"):
        store.rename_node(folder.node_id, "")


def test_rename_node_missing_node_raises_keyerror(store: SessionCatalogStore) -> None:
    with pytest.raises(KeyError):
        store.rename_node(make_session_id(), "新名")


def test_move_node_bumps_revision(store: SessionCatalogStore, tree: CatalogTree) -> None:
    moved = store.move_node(tree.session_s2, tree.folder_a)
    assert moved.parent_node_id == tree.folder_a
    assert moved.revision == 2
    assert tree.session_s2 in store.descendant_session_ids(tree.folder_a)


def test_move_node_to_root(store: SessionCatalogStore, tree: CatalogTree) -> None:
    moved = store.move_node(tree.session_s2, None)
    assert moved.parent_node_id is None
    assert moved.revision == 2
    assert store.descendant_session_ids(tree.session_s1) == [tree.session_s3]


def test_set_node_state_active_to_deleting(store: SessionCatalogStore) -> None:
    folder = store.create_folder(make_session_id(), WORKSPACE_ID, None, "待删除")
    updated = store.set_node_state(folder.node_id, "deleting")
    assert updated.state == "deleting"
    assert updated.revision == 2


def test_set_node_state_rejects_revival(store: SessionCatalogStore) -> None:
    folder = store.create_folder(make_session_id(), WORKSPACE_ID, None, "已删除")
    store.set_node_state(folder.node_id, "deleting")
    with pytest.raises(RuntimeError, match="不可复活"):
        store.set_node_state(folder.node_id, "active")


def test_set_node_state_rejects_same_state(store: SessionCatalogStore) -> None:
    folder = store.create_folder(make_session_id(), WORKSPACE_ID, None, "活跃")
    with pytest.raises(RuntimeError, match="无变化"):
        store.set_node_state(folder.node_id, "active")


def test_set_node_state_rejects_unknown_state(store: SessionCatalogStore) -> None:
    folder = store.create_folder(make_session_id(), WORKSPACE_ID, None, "任意")
    with pytest.raises(ValueError, match="状态"):
        store.set_node_state(folder.node_id, "paused")


def test_count_children(store: SessionCatalogStore, tree: CatalogTree) -> None:
    assert store.count_children(tree.folder_a) == 2
    assert store.count_children(tree.session_s1) == 2
    assert store.count_children(tree.folder_b) == 1
    assert store.count_children(tree.session_s5) == 0


def test_count_children_missing_node_raises_keyerror(
    store: SessionCatalogStore,
) -> None:
    with pytest.raises(KeyError):
        store.count_children(make_session_id())


def test_get_node_missing_raises_keyerror(store: SessionCatalogStore) -> None:
    with pytest.raises(KeyError, match="不存在"):
        store.get_node(make_session_id())


# ----------------------------------------------------------------------
# 约束：CHECK / UNIQUE（直接 SQL 注入验证 DDL 约束）
# ----------------------------------------------------------------------


def test_folder_with_locator_rejected_by_check(store: SessionCatalogStore) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute(
            "INSERT INTO nodes (node_id, kind, parent_node_id, display_name, state, "
            "revision, workspace_id, storage_relative_locator) "
            "VALUES (?, 'folder', NULL, 'f', 'active', 1, ?, ?)",
            (make_session_id(), WORKSPACE_ID, "sessions/2026/06/01/x"),
        )


def test_folder_with_main_thread_rejected_by_check(
    store: SessionCatalogStore,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute(
            "INSERT INTO nodes (node_id, kind, parent_node_id, display_name, state, "
            "revision, workspace_id, main_thread_id) "
            "VALUES (?, 'folder', NULL, 'f', 'active', 1, ?, ?)",
            (make_session_id(), WORKSPACE_ID, make_thread_id()),
        )


def test_session_missing_created_at_rejected_by_check(
    store: SessionCatalogStore,
) -> None:
    session_id = make_session_id()
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute(
            "INSERT INTO nodes (node_id, kind, parent_node_id, display_name, state, "
            "revision, workspace_id, storage_relative_locator, main_thread_id) "
            "VALUES (?, 'session', NULL, 's', 'active', 1, ?, ?, ?)",
            (
                session_id,
                WORKSPACE_ID,
                f"sessions/2026/06/01/{session_id}",
                make_thread_id(),
            ),
        )


def test_session_missing_locator_rejected_by_check(
    store: SessionCatalogStore,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute(
            "INSERT INTO nodes (node_id, kind, parent_node_id, display_name, state, "
            "revision, workspace_id, created_at, main_thread_id) "
            "VALUES (?, 'session', NULL, 's', 'active', 1, ?, ?, ?)",
            (
                make_session_id(),
                WORKSPACE_ID,
                "2026-06-01T12:00:00+00:00",
                make_thread_id(),
            ),
        )


def test_session_missing_main_thread_rejected_by_check(
    store: SessionCatalogStore,
) -> None:
    session_id = make_session_id()
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute(
            "INSERT INTO nodes (node_id, kind, parent_node_id, display_name, state, "
            "revision, workspace_id, created_at, storage_relative_locator) "
            "VALUES (?, 'session', NULL, 's', 'active', 1, ?, ?, ?)",
            (
                session_id,
                WORKSPACE_ID,
                "2026-06-01T12:00:00+00:00",
                f"sessions/2026/06/01/{session_id}",
            ),
        )


def test_invalid_kind_rejected_by_check(store: SessionCatalogStore) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute(
            "INSERT INTO nodes (node_id, kind, parent_node_id, display_name, state, "
            "revision, workspace_id) VALUES (?, 'workspace', NULL, 'w', 'active', 1, ?)",
            (make_session_id(), WORKSPACE_ID),
        )


def test_invalid_state_rejected_by_check(store: SessionCatalogStore) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute(
            "INSERT INTO nodes (node_id, kind, parent_node_id, display_name, state, "
            "revision, workspace_id) VALUES (?, 'folder', NULL, 'f', 'paused', 1, ?)",
            (make_session_id(), WORKSPACE_ID),
        )


def test_duplicate_node_id_rejected(store: SessionCatalogStore) -> None:
    folder_id = make_session_id()
    store.create_folder(folder_id, WORKSPACE_ID, None, "第一个")
    with pytest.raises(RuntimeError, match="已存在"):
        store.create_folder(folder_id, WORKSPACE_ID, None, "第二个")
    # 同一 node_id 换 kind 也拒绝
    with pytest.raises(RuntimeError, match="已存在"):
        store.create_session_node(
            folder_id,
            WORKSPACE_ID,
            None,
            "同名 session",
            datetime(2026, 6, 1, tzinfo=UTC),
            f"sessions/2026/06/01/{folder_id}",
            make_thread_id(),
        )


def test_duplicate_main_thread_rejected(store: SessionCatalogStore) -> None:
    thread_id = make_thread_id()
    first_id = make_session_id()
    store.create_session_node(
        first_id,
        WORKSPACE_ID,
        None,
        "第一个",
        datetime(2026, 6, 1, tzinfo=UTC),
        f"sessions/2026/06/01/{first_id}",
        thread_id,
    )
    second_id = make_session_id()
    with pytest.raises(RuntimeError, match="main_thread_id"):
        store.create_session_node(
            second_id,
            WORKSPACE_ID,
            None,
            "第二个",
            datetime(2026, 6, 1, tzinfo=UTC),
            f"sessions/2026/06/01/{second_id}",
            thread_id,
        )


def test_same_main_thread_across_workspaces_allowed(
    store: SessionCatalogStore,
) -> None:
    thread_id = make_thread_id()
    first_id = make_session_id()
    store.create_session_node(
        first_id,
        WORKSPACE_ID,
        None,
        "ws1 会话",
        datetime(2026, 6, 1, tzinfo=UTC),
        f"sessions/2026/06/01/{first_id}",
        thread_id,
    )
    second_id = make_session_id()
    node = store.create_session_node(
        second_id,
        OTHER_WORKSPACE_ID,
        None,
        "ws2 会话",
        datetime(2026, 6, 1, tzinfo=UTC),
        f"sessions/2026/06/01/{second_id}",
        thread_id,
    )
    assert node.workspace_id == OTHER_WORKSPACE_ID


def test_duplicate_locator_rejected_by_unique_constraint(
    store: SessionCatalogStore,
) -> None:
    # store 层 locator 叶名必须等于 node_id，因此同 locator 重复只能通过
    # 直接 SQL 注入触发 UNIQUE (workspace_id, storage_relative_locator)。
    session_id = make_session_id()
    locator = f"sessions/2026/06/01/{session_id}"
    store.connection.execute(
        "INSERT INTO nodes (node_id, kind, parent_node_id, display_name, state, "
        "revision, workspace_id, created_at, storage_relative_locator, main_thread_id) "
        "VALUES (?, 'session', NULL, 'a', 'active', 1, ?, ?, ?, ?)",
        (
            session_id,
            WORKSPACE_ID,
            "2026-06-01T12:00:00+00:00",
            locator,
            make_thread_id(),
        ),
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute(
            "INSERT INTO nodes (node_id, kind, parent_node_id, display_name, state, "
            "revision, workspace_id, created_at, storage_relative_locator, main_thread_id) "
            "VALUES (?, 'session', NULL, 'b', 'active', 1, ?, ?, ?, ?)",
            (
                make_session_id(),
                WORKSPACE_ID,
                "2026-06-01T12:00:00+00:00",
                locator,
                make_thread_id(),
            ),
        )


# ----------------------------------------------------------------------
# CTE：descendant_session_ids / breadcrumb / nearest_session_ancestor
# ----------------------------------------------------------------------


def test_descendant_session_ids_nested_tree(
    store: SessionCatalogStore, tree: CatalogTree
) -> None:
    assert store.descendant_session_ids(tree.folder_a) == sorted(
        [
            tree.session_s1,
            tree.session_s2,
            tree.session_s3,
            tree.session_s4,
        ]
    )
    assert store.descendant_session_ids(tree.session_s1) == sorted(
        [tree.session_s2, tree.session_s3]
    )
    assert store.descendant_session_ids(tree.folder_b) == [tree.session_s3]
    assert store.descendant_session_ids(tree.session_s5) == []


def test_descendant_session_ids_excludes_self(
    store: SessionCatalogStore, tree: CatalogTree
) -> None:
    # 自身是 session 也不包含自身（include_self=False 语义）
    assert tree.session_s1 not in store.descendant_session_ids(tree.session_s1)
    assert tree.folder_a not in store.descendant_session_ids(tree.folder_a)


def test_descendant_session_ids_missing_node_raises_keyerror(
    store: SessionCatalogStore,
) -> None:
    with pytest.raises(KeyError):
        store.descendant_session_ids(make_session_id())


def test_breadcrumb(store: SessionCatalogStore, tree: CatalogTree) -> None:
    chain = store.breadcrumb(tree.session_s3)
    assert [node.node_id for node in chain] == [
        tree.folder_a,
        tree.session_s1,
        tree.folder_b,
        tree.session_s3,
    ]


def test_breadcrumb_root_level_node(
    store: SessionCatalogStore, tree: CatalogTree
) -> None:
    assert [node.node_id for node in store.breadcrumb(tree.folder_a)] == [tree.folder_a]
    assert [node.node_id for node in store.breadcrumb(tree.session_s5)] == [
        tree.session_s5
    ]


def test_breadcrumb_missing_node_raises_keyerror(
    store: SessionCatalogStore,
) -> None:
    with pytest.raises(KeyError):
        store.breadcrumb(make_session_id())


def test_nearest_session_ancestor(store: SessionCatalogStore, tree: CatalogTree) -> None:
    # s3 的父是 folder_b，folder_b 的父是 s1（session）
    assert store.nearest_session_ancestor(tree.session_s3) == tree.session_s1
    assert store.nearest_session_ancestor(tree.folder_b) == tree.session_s1
    assert store.nearest_session_ancestor(tree.session_s2) == tree.session_s1


def test_nearest_session_ancestor_none_for_root_chain(
    store: SessionCatalogStore, tree: CatalogTree
) -> None:
    # s1 的父是 folder_a（folder），folder_a 的父是 None → 无 session 祖先
    assert store.nearest_session_ancestor(tree.session_s1) is None
    assert store.nearest_session_ancestor(tree.session_s4) is None
    assert store.nearest_session_ancestor(tree.folder_a) is None
    assert store.nearest_session_ancestor(tree.session_s5) is None


def test_nearest_session_ancestor_excludes_self(
    store: SessionCatalogStore, tree: CatalogTree
) -> None:
    # s1 自身是 session，但结果不含自身（传 parent 语义）
    assert store.nearest_session_ancestor(tree.session_s1) is None


def test_nearest_session_ancestor_missing_node_raises_keyerror(
    store: SessionCatalogStore,
) -> None:
    with pytest.raises(KeyError):
        store.nearest_session_ancestor(make_session_id())


def test_get_session_by_main_thread(store: SessionCatalogStore) -> None:
    session_id = make_session_id()
    thread_id = make_thread_id()
    store.create_session_node(
        session_id,
        WORKSPACE_ID,
        None,
        "主线程会话",
        datetime(2026, 6, 1, tzinfo=UTC),
        f"sessions/2026/06/01/{session_id}",
        thread_id,
    )
    node = store.get_session_by_main_thread(WORKSPACE_ID, thread_id)
    assert node.node_id == session_id
    assert node.main_thread_id == thread_id


def test_get_session_by_main_thread_missing_raises_keyerror(
    store: SessionCatalogStore,
) -> None:
    with pytest.raises(KeyError):
        store.get_session_by_main_thread(WORKSPACE_ID, make_thread_id())


# ----------------------------------------------------------------------
# 环拒绝
# ----------------------------------------------------------------------


def test_move_node_to_self_rejected(
    store: SessionCatalogStore, tree: CatalogTree
) -> None:
    with pytest.raises(RuntimeError, match="自身"):
        store.move_node(tree.session_s1, tree.session_s1)


def test_move_node_to_child_rejected(
    store: SessionCatalogStore, tree: CatalogTree
) -> None:
    with pytest.raises(RuntimeError, match="循环"):
        store.move_node(tree.session_s1, tree.session_s2)


def test_move_node_to_deep_descendant_rejected(
    store: SessionCatalogStore, tree: CatalogTree
) -> None:
    with pytest.raises(RuntimeError, match="循环"):
        store.move_node(tree.folder_a, tree.session_s3)


def test_move_node_to_descendant_folder_rejected(
    store: SessionCatalogStore, tree: CatalogTree
) -> None:
    with pytest.raises(RuntimeError, match="循环"):
        store.move_node(tree.session_s1, tree.folder_b)


# ----------------------------------------------------------------------
# 跨 workspace
# ----------------------------------------------------------------------


def test_move_node_across_workspace_rejected(
    store: SessionCatalogStore, tree: CatalogTree
) -> None:
    other_root = store.create_folder(
        make_session_id(), OTHER_WORKSPACE_ID, None, "其他工作区根"
    )
    with pytest.raises(RuntimeError, match="其他 workspace"):
        store.move_node(tree.session_s1, other_root.node_id)


def test_create_folder_under_cross_workspace_parent_rejected(
    store: SessionCatalogStore,
) -> None:
    other_root = store.create_folder(
        make_session_id(), OTHER_WORKSPACE_ID, None, "其他工作区根"
    )
    with pytest.raises(RuntimeError, match="其他 workspace"):
        store.create_folder(
            make_session_id(), WORKSPACE_ID, other_root.node_id, "跨工作区子节点"
        )


def test_create_session_under_cross_workspace_parent_rejected(
    store: SessionCatalogStore,
) -> None:
    other_root = store.create_folder(
        make_session_id(), OTHER_WORKSPACE_ID, None, "其他工作区根"
    )
    with pytest.raises(RuntimeError, match="其他 workspace"):
        create_session(store, parent_node_id=other_root.node_id)


def test_verify_consistency_detects_cross_workspace(
    store: SessionCatalogStore, tree: CatalogTree
) -> None:
    # 直接 SQL 注入：把 s2 的 workspace 改成独立值，违反父子同 workspace
    store.connection.execute(
        "UPDATE nodes SET workspace_id = 'ws-injected' WHERE node_id = ?",
        (tree.session_s2,),
    )
    with pytest.raises(RuntimeError, match="workspace 不一致"):
        store.verify_workspace_consistency()


def test_verify_consistency_detects_cycle(
    store: SessionCatalogStore, tree: CatalogTree
) -> None:
    # 直接 SQL 注入：把根 folder_a 的父指回其深层后代，制造环
    store.connection.execute(
        "UPDATE nodes SET parent_node_id = ? WHERE node_id = ?",
        (tree.session_s3, tree.folder_a),
    )
    with pytest.raises(RuntimeError, match="循环"):
        store.verify_workspace_consistency()


def test_verify_consistency_detects_missing_parent(
    store: SessionCatalogStore,
) -> None:
    # 关闭外键的直连写入孤儿节点（模拟绕过软件的写入）
    connection = sqlite3.connect(store.database_path)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            "INSERT INTO nodes (node_id, kind, parent_node_id, display_name, state, "
            "revision, workspace_id) VALUES (?, 'folder', ?, '孤儿', 'active', 1, ?)",
            (make_session_id(), make_session_id(), WORKSPACE_ID),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(RuntimeError, match="父节点缺失"):
        store.verify_workspace_consistency()


def test_verify_consistency_passes_healthy_tree(
    store: SessionCatalogStore, tree: CatalogTree
) -> None:
    store.verify_workspace_consistency()


# ----------------------------------------------------------------------
# gate / fence 原语
# ----------------------------------------------------------------------


async def test_navigation_topology_gate_mutual_exclusion(tmp_path: Path) -> None:
    gate = NavigationTopologyGate(tmp_path / "sessions")
    events: list[str] = []

    async def worker(name: str) -> None:
        async with gate.exclusive():
            events.append(f"{name}:enter")
            await asyncio.sleep(0.01)
            events.append(f"{name}:exit")

    await asyncio.gather(worker("a"), worker("b"))
    # 互斥：先进入者必须先退出，另一协程的 enter 只能排在其后
    assert [event.split(":")[1] for event in events] == [
        "enter",
        "exit",
        "enter",
        "exit",
    ]
    assert events[0].split(":")[0] == events[1].split(":")[0]
    assert events[2].split(":")[0] == events[3].split(":")[0]
    assert events[0].split(":")[0] != events[2].split(":")[0]


def test_session_lifecycle_gate_validates_and_paths(tmp_path: Path) -> None:
    gate = SessionLifecycleGate(tmp_path / "sessions")
    assert gate.gates_root == tmp_path / "navigation" / "session-lifecycle-gates"
    with pytest.raises(ValueError):
        gate.exclusive("非法ID")
    with pytest.raises(ValueError):
        gate.shared("非法ID")


def test_fence_cas_success_advances_generation() -> None:
    fence = SessionLifecycleFence()
    assert fence.state == "active"
    assert fence.generation == 0
    assert fence.cas_transition(0, "deleting") is True
    assert fence.state == "deleting"
    assert fence.generation == 1


def test_fence_cas_wrong_generation_rejected() -> None:
    fence = SessionLifecycleFence()
    assert fence.cas_transition(1, "deleting") is False
    assert fence.state == "active"
    assert fence.generation == 0


def test_fence_rejects_revival() -> None:
    fence = SessionLifecycleFence()
    assert fence.cas_transition(0, "deleting") is True
    assert fence.cas_transition(1, "active") is False
    assert fence.state == "deleting"
    assert fence.generation == 1


def test_fence_rejects_same_state_transition() -> None:
    fence = SessionLifecycleFence()
    assert fence.cas_transition(0, "active") is False
    assert fence.state == "active"
    assert fence.generation == 0


def test_fence_rejects_unknown_state() -> None:
    fence = SessionLifecycleFence()
    with pytest.raises(ValueError, match="状态"):
        fence.cas_transition(0, "paused")


def test_fence_rejects_invalid_initial_state() -> None:
    with pytest.raises(ValueError, match="状态"):
        SessionLifecycleFence(state="paused")


# ----------------------------------------------------------------------
# 分页：list_children
# ----------------------------------------------------------------------


def test_list_children_pagination(store: SessionCatalogStore) -> None:
    parent = store.create_folder(make_session_id(), WORKSPACE_ID, None, "分页父")
    child_ids = sorted(make_session_id() for _ in range(5))
    for child_id in child_ids:
        store.create_folder(child_id, WORKSPACE_ID, parent.node_id, "子节点")

    page1, cursor1, has_more1 = store.list_children(parent.node_id, limit=2)
    assert [node.node_id for node in page1] == child_ids[:2]
    assert has_more1 is True
    assert cursor1 == child_ids[1]

    page2, cursor2, has_more2 = store.list_children(
        parent.node_id, limit=2, cursor=cursor1
    )
    assert [node.node_id for node in page2] == child_ids[2:4]
    assert has_more2 is True
    assert cursor2 == child_ids[3]

    page3, cursor3, has_more3 = store.list_children(
        parent.node_id, limit=2, cursor=cursor2
    )
    assert [node.node_id for node in page3] == child_ids[4:]
    assert has_more3 is False
    assert cursor3 is None


def test_list_children_cursor_stable_across_pages(
    store: SessionCatalogStore,
) -> None:
    parent = store.create_folder(make_session_id(), WORKSPACE_ID, None, "分页父")
    child_ids = sorted(make_session_id() for _ in range(7))
    for child_id in child_ids:
        store.create_folder(child_id, WORKSPACE_ID, parent.node_id, "子节点")

    collected: list[str] = []
    cursor: str | None = None
    for _ in range(10):
        items, next_cursor, has_more = store.list_children(
            parent.node_id, limit=3, cursor=cursor
        )
        collected.extend(node.node_id for node in items)
        cursor = next_cursor
        if not has_more:
            break
    assert collected == child_ids


def test_list_children_exact_page_has_more_false(
    store: SessionCatalogStore,
) -> None:
    parent = store.create_folder(make_session_id(), WORKSPACE_ID, None, "分页父")
    for _ in range(3):
        store.create_folder(
            make_session_id(), WORKSPACE_ID, parent.node_id, "子节点"
        )
    items, cursor, has_more = store.list_children(parent.node_id, limit=3)
    assert len(items) == 3
    assert has_more is False
    assert cursor is None


def test_list_children_root_level(
    store: SessionCatalogStore, tree: CatalogTree
) -> None:
    items, cursor, has_more = store.list_children(None, limit=10)
    assert [node.node_id for node in items] == sorted(
        [tree.folder_a, tree.session_s5]
    )
    assert has_more is False
    assert cursor is None


def test_list_children_rejects_invalid_limit(
    store: SessionCatalogStore, tree: CatalogTree
) -> None:
    with pytest.raises(ValueError, match="limit"):
        store.list_children(tree.folder_a, limit=0)
    with pytest.raises(ValueError, match="limit"):
        store.list_children(tree.folder_a, limit=-1)


def test_list_children_missing_parent_raises_keyerror(
    store: SessionCatalogStore,
) -> None:
    with pytest.raises(KeyError):
        store.list_children(make_session_id(), limit=5)


# ----------------------------------------------------------------------
# parent 校验
# ----------------------------------------------------------------------


def test_create_folder_missing_parent_rejected(store: SessionCatalogStore) -> None:
    with pytest.raises(KeyError):
        store.create_folder(
            make_session_id(), WORKSPACE_ID, make_session_id(), "孤儿"
        )


def test_create_folder_deleting_parent_rejected(store: SessionCatalogStore) -> None:
    parent = store.create_folder(make_session_id(), WORKSPACE_ID, None, "将删除")
    store.set_node_state(parent.node_id, "deleting")
    with pytest.raises(RuntimeError, match="正在删除"):
        store.create_folder(make_session_id(), WORKSPACE_ID, parent.node_id, "子")


def test_create_session_missing_parent_rejected(store: SessionCatalogStore) -> None:
    with pytest.raises(KeyError):
        create_session(store, parent_node_id=make_session_id())


def test_create_session_deleting_parent_rejected(store: SessionCatalogStore) -> None:
    parent = store.create_folder(make_session_id(), WORKSPACE_ID, None, "将删除")
    store.set_node_state(parent.node_id, "deleting")
    with pytest.raises(RuntimeError, match="正在删除"):
        create_session(store, parent_node_id=parent.node_id)


def test_move_node_to_deleting_parent_rejected(
    store: SessionCatalogStore, tree: CatalogTree
) -> None:
    target = store.create_folder(make_session_id(), WORKSPACE_ID, None, "将删除")
    store.set_node_state(target.node_id, "deleting")
    with pytest.raises(RuntimeError, match="正在删除"):
        store.move_node(tree.session_s2, target.node_id)


# ----------------------------------------------------------------------
# locator 日期 / created_at 一致性与路径预算
# ----------------------------------------------------------------------


def test_create_session_locator_date_mismatch_rejected(
    store: SessionCatalogStore,
) -> None:
    session_id = make_session_id()
    with pytest.raises(ValueError, match="UTC 日期"):
        store.create_session_node(
            session_id,
            WORKSPACE_ID,
            None,
            "日期不一致",
            datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
            f"sessions/2026/06/02/{session_id}",
            make_thread_id(),
        )


def test_create_session_locator_utc_date_semantics(
    store: SessionCatalogStore,
) -> None:
    # +08:00 时区的 20:00 是 UTC 12:00，locator 必须用 UTC 日期
    plus8 = timezone(timedelta(hours=8))
    session_id = make_session_id()
    node = store.create_session_node(
        session_id,
        WORKSPACE_ID,
        None,
        "时区会话",
        datetime(2026, 6, 1, 20, 0, tzinfo=plus8),
        f"sessions/2026/06/01/{session_id}",
        make_thread_id(),
    )
    assert node.storage_relative_locator == f"sessions/2026/06/01/{session_id}"
    # UTC 日期是 06-01，用 06-02 的 locator 被拒
    other_id = make_session_id()
    with pytest.raises(ValueError, match="UTC 日期"):
        store.create_session_node(
            other_id,
            WORKSPACE_ID,
            None,
            "时区会话2",
            datetime(2026, 6, 1, 20, 0, tzinfo=plus8),
            f"sessions/2026/06/02/{other_id}",
            make_thread_id(),
        )


def test_create_session_naive_created_at_rejected(
    store: SessionCatalogStore,
) -> None:
    session_id = make_session_id()
    with pytest.raises(ValueError, match="时区"):
        store.create_session_node(
            session_id,
            WORKSPACE_ID,
            None,
            "无时区会话",
            datetime(2026, 6, 1, 12, 0),  # type: ignore[arg-type]  # noqa: DTZ001 - 故意构造 naive datetime 验证拒绝
            f"sessions/2026/06/01/{session_id}",
            make_thread_id(),
        )


def test_create_session_non_datetime_created_at_rejected(
    store: SessionCatalogStore,
) -> None:
    session_id = make_session_id()
    with pytest.raises(TypeError, match="created_at"):
        store.create_session_node(
            session_id,
            WORKSPACE_ID,
            None,
            "非时间会话",
            "2026-06-01",  # type: ignore[arg-type]
            f"sessions/2026/06/01/{session_id}",
            make_thread_id(),
        )


def test_create_session_none_created_at_rejected(
    store: SessionCatalogStore,
) -> None:
    session_id = make_session_id()
    with pytest.raises(TypeError, match="created_at"):
        store.create_session_node(
            session_id,
            WORKSPACE_ID,
            None,
            "空时间会话",
            None,  # type: ignore[arg-type]
            f"sessions/2026/06/01/{session_id}",
            make_thread_id(),
        )


def test_create_session_none_main_thread_rejected(
    store: SessionCatalogStore,
) -> None:
    session_id = make_session_id()
    with pytest.raises(TypeError):
        store.create_session_node(
            session_id,
            WORKSPACE_ID,
            None,
            "空主线程会话",
            datetime(2026, 6, 1, tzinfo=UTC),
            f"sessions/2026/06/01/{session_id}",
            None,  # type: ignore[arg-type]
        )


def test_create_session_none_locator_rejected(store: SessionCatalogStore) -> None:
    session_id = make_session_id()
    with pytest.raises(TypeError):
        store.create_session_node(
            session_id,
            WORKSPACE_ID,
            None,
            "空定位会话",
            datetime(2026, 6, 1, tzinfo=UTC),
            None,  # type: ignore[arg-type]
            make_thread_id(),
        )


def test_create_session_locator_leaf_mismatch_rejected(
    store: SessionCatalogStore,
) -> None:
    session_id = make_session_id()
    with pytest.raises(ValueError, match="叶名"):
        store.create_session_node(
            session_id,
            WORKSPACE_ID,
            None,
            "叶名不一致会话",
            datetime(2026, 6, 1, tzinfo=UTC),
            f"sessions/2026/06/01/{make_session_id()}",
            make_thread_id(),
        )


def test_create_session_path_budget_component_rejected(
    tmp_path: Path,
) -> None:
    # 用超长 sessions_root 前缀构造组件超限
    sessions_root = tmp_path / ("a" * 300)
    catalog = SessionCatalogStore(
        tmp_path / "navigation" / "session-catalog.sqlite",
        sessions_root,
    )
    try:
        session_id = make_session_id()
        with pytest.raises(ValueError, match="组件"):
            catalog.create_session_node(
                session_id,
                WORKSPACE_ID,
                None,
                "超预算会话",
                datetime(2026, 6, 1, tzinfo=UTC),
                f"sessions/2026/06/01/{session_id}",
                make_thread_id(),
            )
    finally:
        catalog.close()


def test_create_session_path_budget_total_rejected(tmp_path: Path) -> None:
    # 用超长 sessions_root 前缀构造总长超限（每个组件 ≤255 bytes）
    sessions_root = tmp_path.joinpath(*(["b" * 250] * 18))
    catalog = SessionCatalogStore(
        tmp_path / "navigation" / "session-catalog.sqlite",
        sessions_root,
    )
    try:
        session_id = make_session_id()
        with pytest.raises(ValueError, match="总长"):
            catalog.create_session_node(
                session_id,
                WORKSPACE_ID,
                None,
                "超预算会话",
                datetime(2026, 6, 1, tzinfo=UTC),
                f"sessions/2026/06/01/{session_id}",
                make_thread_id(),
            )
    finally:
        catalog.close()


def test_resolve_session_locator(store: SessionCatalogStore) -> None:
    session_id = make_session_id()
    locator = f"sessions/2026/06/01/{session_id}"
    resolved = store.resolve_session_locator(locator)
    assert resolved == store.sessions_root / "2026" / "06" / "01" / session_id
    # 只做路径解析，不创建物理目录
    assert not resolved.exists()
    with pytest.raises(ValueError):
        store.resolve_session_locator(f"sessions/2026/13/01/{session_id}")


# ----------------------------------------------------------------------
# SessionCreationRecord journal（8.1-A，R13）
# ----------------------------------------------------------------------


def make_preimage(seed: str) -> str:
    """生成确定性的 preimage_hash 测试值。"""
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def create_record(
    store: SessionCatalogStore,
    *,
    key: str = "key-1",
    parent_node_id: str | None = None,
    display_name: str = "记录会话",
    created_at: datetime | None = None,
    preimage_hash: str = "preimage-A",
) -> SessionCreationRecord:
    """测试辅助：create-or-get 一个 creation record。"""
    return store.create_or_get_creation_record(
        idempotency_key=key,
        workspace_id=WORKSPACE_ID,
        parent_node_id=parent_node_id,
        display_name=display_name,
        created_at=created_at or datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        preimage_hash=preimage_hash,
    )


def test_create_or_get_creates_preparing_record(
    store: SessionCatalogStore,
) -> None:
    record = create_record(store, key="key-1")
    assert record.state == "preparing"
    assert record.abort_reason is None
    assert record.session_creation_idempotency_key == "key-1"
    assert record.workspace_id == WORKSPACE_ID
    assert record.display_name == "记录会话"
    assert record.created_at == "2026-06-01T12:00:00+00:00"
    assert record.preimage_hash == "preimage-A"
    assert record.parent_node_id is None
    assert record.parent_revision is None
    validate_session_id(record.session_id)
    validate_thread_id(record.main_thread_id)
    assert record.storage_relative_locator == (
        f"sessions/2026/06/01/{record.session_id}"
    )
    assert record.record_created_at
    assert record.record_updated_at == record.record_created_at
    # record 不发布可见 node
    with pytest.raises(KeyError):
        store.get_node(record.session_id)


def test_create_or_get_idempotent_same_key_same_preimage(
    store: SessionCatalogStore,
) -> None:
    first = create_record(store, key="key-1", preimage_hash=make_preimage("a"))
    second = create_record(store, key="key-1", preimage_hash=make_preimage("a"))
    assert second == first
    rows = store.connection.execute(
        "SELECT COUNT(*) FROM session_creation_records"
    ).fetchone()
    assert int(rows[0]) == 1


def test_create_or_get_conflicting_preimage_rejected(
    store: SessionCatalogStore,
) -> None:
    create_record(store, key="key-1", preimage_hash=make_preimage("a"))
    with pytest.raises(RuntimeError, match="冲突"):
        create_record(store, key="key-1", preimage_hash=make_preimage("b"))


def test_create_or_get_freezes_parent_revision(
    store: SessionCatalogStore,
) -> None:
    parent = store.create_folder(make_session_id(), WORKSPACE_ID, None, "父")
    record = create_record(store, key="key-1", parent_node_id=parent.node_id)
    assert record.parent_node_id == parent.node_id
    assert record.parent_revision == 1
    # 父节点 revision 漂移后，既有 record 冻结值不变（幂等重入不刷新）
    store.rename_node(parent.node_id, "改名")
    again = create_record(store, key="key-1", preimage_hash="preimage-A")
    assert again.parent_revision == 1


def test_create_or_get_rejects_deleting_parent(
    store: SessionCatalogStore,
) -> None:
    parent = store.create_folder(make_session_id(), WORKSPACE_ID, None, "将删除")
    store.set_node_state(parent.node_id, "deleting")
    with pytest.raises(RuntimeError, match="正在删除"):
        create_record(store, key="key-1", parent_node_id=parent.node_id)


def test_create_or_get_rejects_missing_parent(
    store: SessionCatalogStore,
) -> None:
    with pytest.raises(KeyError):
        create_record(store, key="key-1", parent_node_id=make_session_id())


def test_create_or_get_rejects_cross_workspace_parent(
    store: SessionCatalogStore,
) -> None:
    other_root = store.create_folder(
        make_session_id(), OTHER_WORKSPACE_ID, None, "其他工作区根"
    )
    with pytest.raises(RuntimeError, match="其他 workspace"):
        create_record(store, key="key-1", parent_node_id=other_root.node_id)


def test_create_or_get_rejects_invalid_created_at(
    store: SessionCatalogStore,
) -> None:
    with pytest.raises(TypeError, match="created_at"):
        store.create_or_get_creation_record(
            idempotency_key="key-1",
            workspace_id=WORKSPACE_ID,
            parent_node_id=None,
            display_name="会话",
            created_at="2026-06-01",  # type: ignore[arg-type]
            preimage_hash="preimage-A",
        )
    with pytest.raises(ValueError, match="时区"):
        store.create_or_get_creation_record(
            idempotency_key="key-1",
            workspace_id=WORKSPACE_ID,
            parent_node_id=None,
            display_name="会话",
            created_at=datetime(2026, 6, 1, 12, 0),  # type: ignore[arg-type]  # noqa: DTZ001 - 故意构造 naive datetime 验证拒绝
            preimage_hash="preimage-A",
        )


def test_get_creation_record_missing_raises_keyerror(
    store: SessionCatalogStore,
) -> None:
    with pytest.raises(KeyError, match="不存在"):
        store.get_creation_record("missing-key")


def test_creation_record_session_id_unique_rejected_by_constraint(
    store: SessionCatalogStore,
) -> None:
    record = create_record(store, key="key-1")
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute(
            "INSERT INTO session_creation_records ("
            "session_creation_idempotency_key, session_id, main_thread_id, "
            "workspace_id, parent_node_id, display_name, created_at, "
            "storage_relative_locator, preimage_hash, parent_revision, "
            "state, abort_reason, record_created_at, record_updated_at) "
            "VALUES ('key-2', ?, 'thr_" + "0" * 32 + "', ?, NULL, 'x', ?, "
            "?, 'p', NULL, 'preparing', NULL, ?, ?)",
            (
                record.session_id,
                WORKSPACE_ID,
                record.created_at,
                record.storage_relative_locator,
                record.record_created_at,
                record.record_created_at,
            ),
        )


def test_creation_record_locator_unique_rejected_by_constraint(
    store: SessionCatalogStore,
) -> None:
    record = create_record(store, key="key-1")
    other_session_id = make_session_id()
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute(
            "INSERT INTO session_creation_records ("
            "session_creation_idempotency_key, session_id, main_thread_id, "
            "workspace_id, parent_node_id, display_name, created_at, "
            "storage_relative_locator, preimage_hash, parent_revision, "
            "state, abort_reason, record_created_at, record_updated_at) "
            "VALUES ('key-2', ?, ?, ?, NULL, 'x', ?, ?, 'p', NULL, "
            "'preparing', NULL, ?, ?)",
            (
                other_session_id,
                record.main_thread_id,
                WORKSPACE_ID,
                record.created_at,
                record.storage_relative_locator,
                record.record_created_at,
                record.record_created_at,
            ),
        )


def test_creation_record_state_check_rejected_by_constraint(
    store: SessionCatalogStore,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute(
            "INSERT INTO session_creation_records ("
            "session_creation_idempotency_key, session_id, main_thread_id, "
            "workspace_id, parent_node_id, display_name, created_at, "
            "storage_relative_locator, preimage_hash, parent_revision, "
            "state, abort_reason, record_created_at, record_updated_at) "
            "VALUES ('key-x', ?, ?, ?, NULL, 'x', ?, ?, 'p', NULL, "
            "'paused', NULL, ?, ?)",
            (
                make_session_id(),
                make_thread_id(),
                WORKSPACE_ID,
                "2026-06-01T12:00:00+00:00",
                f"sessions/2026/06/01/{make_session_id()}",
                "2026-06-01T00:00:00+00:00",
                "2026-06-01T00:00:00+00:00",
            ),
        )


def test_publish_creation_record_publishes_node_and_record(
    store: SessionCatalogStore,
) -> None:
    parent = store.create_folder(make_session_id(), WORKSPACE_ID, None, "父")
    record = create_record(
        store,
        key="key-1",
        parent_node_id=parent.node_id,
        display_name="发布会话",
    )
    node = store.publish_creation_record("key-1")
    assert node.node_id == record.session_id
    assert node.kind == "session"
    assert node.state == "active"
    assert node.revision == 1
    assert node.parent_node_id == parent.node_id
    assert node.display_name == "发布会话"
    assert node.workspace_id == WORKSPACE_ID
    assert node.created_at == record.created_at
    assert node.storage_relative_locator == record.storage_relative_locator
    assert node.main_thread_id == record.main_thread_id
    assert store.get_node(record.session_id) == node
    published = store.get_creation_record("key-1")
    assert published.state == "published"
    assert published.abort_reason is None


def test_publish_creation_record_parent_revision_drift_rejected(
    store: SessionCatalogStore,
) -> None:
    parent = store.create_folder(make_session_id(), WORKSPACE_ID, None, "父")
    create_record(store, key="key-1", parent_node_id=parent.node_id)
    store.rename_node(parent.node_id, "漂移")
    with pytest.raises(RuntimeError, match="漂移"):
        store.publish_creation_record("key-1")
    # 失败回滚：node 未发布、record 仍 preparing
    record = store.get_creation_record("key-1")
    assert record.state == "preparing"
    with pytest.raises(KeyError):
        store.get_node(record.session_id)


def test_publish_creation_record_parent_missing_rejected(
    store: SessionCatalogStore,
) -> None:
    parent = store.create_folder(make_session_id(), WORKSPACE_ID, None, "父")
    create_record(store, key="key-1", parent_node_id=parent.node_id)
    # 直连 SQL 删除父节点行（模拟绕过软件的外部改动）
    connection = sqlite3.connect(store.database_path)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            "DELETE FROM nodes WHERE node_id = ?", (parent.node_id,)
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(RuntimeError, match="父节点已不存在"):
        store.publish_creation_record("key-1")
    assert store.get_creation_record("key-1").state == "preparing"


def test_publish_creation_record_deleting_parent_rejected(
    store: SessionCatalogStore,
) -> None:
    parent = store.create_folder(make_session_id(), WORKSPACE_ID, None, "父")
    create_record(store, key="key-1", parent_node_id=parent.node_id)
    store.set_node_state(parent.node_id, "deleting")
    with pytest.raises(RuntimeError, match="非 active"):
        store.publish_creation_record("key-1")
    assert store.get_creation_record("key-1").state == "preparing"


def test_publish_creation_record_locator_occupied_rejected(
    store: SessionCatalogStore,
) -> None:
    record = create_record(store, key="key-1")
    # 直连 SQL 注入同 locator 的既有 node（store 层叶名约束绕过，
    # 触发 UNIQUE (workspace_id, storage_relative_locator) 场景）
    injected_node_id = make_session_id()
    store.connection.execute(
        "INSERT INTO nodes (node_id, kind, parent_node_id, display_name, "
        "state, revision, workspace_id, created_at, "
        "storage_relative_locator, main_thread_id) "
        "VALUES (?, 'session', NULL, '占位', 'active', 1, ?, ?, ?, ?)",
        (
            injected_node_id,
            WORKSPACE_ID,
            record.created_at,
            record.storage_relative_locator,
            make_thread_id(),
        ),
    )
    with pytest.raises(RuntimeError, match="storage_relative_locator"):
        store.publish_creation_record("key-1")
    # 失败回滚：record 仍 preparing；record 的 session node 未发布；
    # 注入的占位 node 未受影响
    assert store.get_creation_record("key-1").state == "preparing"
    assert (
        store.get_node(injected_node_id).storage_relative_locator
        == record.storage_relative_locator
    )
    with pytest.raises(KeyError):
        store.get_node(record.session_id)


def test_publish_creation_record_missing_record_raises_keyerror(
    store: SessionCatalogStore,
) -> None:
    with pytest.raises(KeyError):
        store.publish_creation_record("missing-key")


def test_publish_creation_record_twice_rejected(
    store: SessionCatalogStore,
) -> None:
    create_record(store, key="key-1")
    store.publish_creation_record("key-1")
    with pytest.raises(RuntimeError, match="已发布"):
        store.publish_creation_record("key-1")


def test_publish_creation_record_aborted_rejected(
    store: SessionCatalogStore,
) -> None:
    create_record(store, key="key-1")
    store.abort_creation_record("key-1", "外部原因")
    with pytest.raises(RuntimeError, match="已中止"):
        store.publish_creation_record("key-1")


def test_abort_creation_record_records_reason(
    store: SessionCatalogStore,
) -> None:
    create_record(store, key="key-1")
    aborted = store.abort_creation_record("key-1", "定点回收")
    assert aborted.state == "aborted"
    assert aborted.abort_reason == "定点回收"
    fetched = store.get_creation_record("key-1")
    assert fetched.state == "aborted"
    assert fetched.abort_reason == "定点回收"


def test_abort_creation_record_idempotent_for_aborted(
    store: SessionCatalogStore,
) -> None:
    create_record(store, key="key-1")
    first = store.abort_creation_record("key-1", "第一次")
    second = store.abort_creation_record("key-1", "第二次")
    assert second.state == "aborted"
    # 幂等：不覆盖原 abort_reason
    assert second.abort_reason == first.abort_reason == "第一次"


def test_abort_creation_record_published_rejected(
    store: SessionCatalogStore,
) -> None:
    create_record(store, key="key-1")
    store.publish_creation_record("key-1")
    with pytest.raises(RuntimeError, match="不可撤销"):
        store.abort_creation_record("key-1", "迟到的回收")


def test_abort_creation_record_missing_raises_keyerror(
    store: SessionCatalogStore,
) -> None:
    with pytest.raises(KeyError):
        store.abort_creation_record("missing-key", "原因")


def test_schema_upgrade_v1_database_adds_records_table(
    tmp_path: Path, sessions_root: Path
) -> None:
    """v1 库（手工建 nodes 表 + user_version=1）打开后自动升 v3。"""
    database_path = tmp_path / "navigation" / "session-catalog.sqlite"
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    try:
        # 手工构建 R10 v1 形态（nodes 表 DDL 本轮冻结不变）
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS nodes (
                node_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL CHECK (kind IN ('folder', 'session')),
                parent_node_id TEXT REFERENCES nodes(node_id),
                display_name TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('active', 'deleting')),
                revision INTEGER NOT NULL DEFAULT 1,
                workspace_id TEXT NOT NULL,
                created_at TEXT,
                storage_relative_locator TEXT,
                main_thread_id TEXT,
                CHECK (
                    (kind = 'session'
                        AND created_at IS NOT NULL
                        AND storage_relative_locator IS NOT NULL
                        AND main_thread_id IS NOT NULL)
                    OR (kind = 'folder'
                        AND created_at IS NULL
                        AND storage_relative_locator IS NULL
                        AND main_thread_id IS NULL)
                ),
                UNIQUE (workspace_id, main_thread_id),
                UNIQUE (workspace_id, storage_relative_locator)
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_nodes_parent ON nodes(parent_node_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_nodes_workspace "
            "ON nodes(workspace_id)"
        )
        folder_id = make_session_id()
        connection.execute(
            "INSERT INTO nodes (node_id, kind, parent_node_id, display_name, "
            "state, revision, workspace_id) "
            "VALUES (?, 'folder', NULL, '升级前文件夹', 'active', 1, ?)",
            (folder_id, WORKSPACE_ID),
        )
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
    finally:
        connection.close()
    store = SessionCatalogStore(database_path, sessions_root)
    try:
        assert (
            int(store.connection.execute("PRAGMA user_version").fetchone()[0])
            == 3
        )
        # 加法升级：records 表存在且可用
        record = create_record(store, key="key-upgraded")
        assert record.state == "preparing"
        # v1 数据完整保留
        assert store.get_node(folder_id).display_name == "升级前文件夹"
    finally:
        store.close()


def test_schema_v0_fresh_creates_v3(tmp_path: Path, sessions_root: Path) -> None:
    store = SessionCatalogStore(
        tmp_path / "navigation" / "session-catalog.sqlite", sessions_root
    )
    try:
        assert (
            int(store.connection.execute("PRAGMA user_version").fetchone()[0])
            == 3
        )
        tables = {
            str(row[0])
            for row in store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert "session_creation_records" in tables
        assert "fork_retention_claims" in tables
        assert "catalog_metadata" in tables
    finally:
        store.close()


@pytest.mark.parametrize("table", ["nodes", "session_creation_records"])
def test_store_rejects_lost_authoritative_table_on_reopen(
    tmp_path: Path, sessions_root: Path, table: str
) -> None:
    """已登记的权威表被外部删掉时重开必须响亮失败，不得静默重建空表。

    ``nodes`` 是会话位置与父子组织的唯一权威。若外部进程把已登记版本的
    权威表删除而 ``user_version`` 仍停在当前版本，此前
    ``CREATE TABLE IF NOT EXISTS`` 会悄悄把表重建为**空表**，等于把全部
    会话静默丢失——违反 AGENTS.md “绝不返回虚假默认值”。重开必须
    fail closed 并指明缺表。
    """
    database_path = tmp_path / "navigation" / "session-catalog.sqlite"
    first = SessionCatalogStore(database_path, sessions_root)
    folder_id = make_session_id()
    first.create_folder(folder_id, WORKSPACE_ID, None, "外部改动前")
    first.close()

    connection = sqlite3.connect(database_path)
    try:
        connection.execute(f"DROP TABLE {table}")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RuntimeError, match=table):
        SessionCatalogStore(database_path, sessions_root)


# ----------------------------------------------------------------------
# NavigationSubtreeDeleteRecord journal（8.1-B，R14）
# ----------------------------------------------------------------------


def build_delete_tree(store: SessionCatalogStore) -> dict[str, str]:
    """测试辅助：嵌套删除树（全部 active）并返回 id→kind 映射。

    root(folder)
    ├── session_s1（session）
    │   └── session_s2（session）
    └── folder_inner（folder）
        └── session_s3（session）
    """
    ids: dict[str, str] = {}
    ids["root"] = store.create_folder(
        make_session_id(), WORKSPACE_ID, None, "删除根"
    ).node_id
    ids["s1"] = create_session(
        store, parent_node_id=ids["root"], display_name="会话1"
    ).node_id
    ids["s2"] = create_session(
        store, parent_node_id=ids["s1"], display_name="会话2"
    ).node_id
    ids["folder_inner"] = store.create_folder(
        make_session_id(), WORKSPACE_ID, ids["root"], "内层文件夹"
    ).node_id
    ids["s3"] = create_session(
        store, parent_node_id=ids["folder_inner"], display_name="会话3"
    ).node_id
    return ids


def create_subtree_record(
    store: SessionCatalogStore,
    *,
    key: str = "del-key-1",
    workspace_id: str = WORKSPACE_ID,
    root_node_id: str,
) -> SubtreeDeleteRecord:
    """测试辅助：create-or-get 一个 subtree delete record。"""
    return store.create_or_get_subtree_delete_record(
        idempotency_key=key,
        workspace_id=workspace_id,
        root_node_id=root_node_id,
    )


def test_create_or_get_subtree_delete_record_freezes_subtree(
    store: SessionCatalogStore,
) -> None:
    ids = build_delete_tree(store)
    record = create_subtree_record(store, root_node_id=ids["root"])
    assert record.state == "preparing"
    assert record.abort_reason is None
    assert record.subtree_delete_idempotency_key == "del-key-1"
    assert record.workspace_id == WORKSPACE_ID
    assert record.root_node_id == ids["root"]
    # 冻结集合含 root 全部后代（含 root 自身），按 node_id 排序
    expected_ids = sorted(ids.values())
    assert [item.node_id for item in record.frozen_node_ids] == expected_ids
    assert all(item.revision == 1 for item in record.frozen_node_ids)
    # session locator 只含 session（folder 无物理 locator）
    assert set(record.frozen_session_locators) == {
        ids["s1"],
        ids["s2"],
        ids["s3"],
    }
    for session_id, locator in record.frozen_session_locators.items():
        assert locator == f"sessions/2026/06/01/{session_id}"
    assert record.drained_session_ids == ()
    assert record.record_created_at
    assert record.record_updated_at == record.record_created_at
    # record 建立不改变任何节点状态
    assert store.get_node(ids["root"]).state == "active"
    assert store.get_node(ids["s3"]).state == "active"


def test_create_or_get_subtree_delete_idempotent_same_key(
    store: SessionCatalogStore,
) -> None:
    ids = build_delete_tree(store)
    first = create_subtree_record(store, root_node_id=ids["root"])
    second = create_subtree_record(store, root_node_id=ids["root"])
    assert second == first
    rows = store.connection.execute(
        "SELECT COUNT(*) FROM subtree_delete_records"
    ).fetchone()
    assert int(rows[0]) == 1


def test_create_or_get_subtree_delete_conflicting_root_rejected(
    store: SessionCatalogStore,
) -> None:
    ids = build_delete_tree(store)
    create_subtree_record(store, root_node_id=ids["root"])
    with pytest.raises(RuntimeError, match="冲突"):
        create_subtree_record(store, root_node_id=ids["s1"])


def test_create_or_get_subtree_delete_conflicting_workspace_rejected(
    store: SessionCatalogStore,
) -> None:
    ids = build_delete_tree(store)
    create_subtree_record(store, root_node_id=ids["root"])
    with pytest.raises(RuntimeError, match="冲突"):
        create_subtree_record(
            store, workspace_id=OTHER_WORKSPACE_ID, root_node_id=ids["root"]
        )


def test_create_or_get_subtree_delete_missing_root_raises_keyerror(
    store: SessionCatalogStore,
) -> None:
    with pytest.raises(KeyError):
        create_subtree_record(store, root_node_id=make_session_id())


def test_create_or_get_subtree_delete_cross_workspace_root_rejected(
    store: SessionCatalogStore,
) -> None:
    other_root = store.create_folder(
        make_session_id(), OTHER_WORKSPACE_ID, None, "其他工作区根"
    )
    with pytest.raises(RuntimeError, match="其他 workspace"):
        create_subtree_record(store, root_node_id=other_root.node_id)


def test_create_or_get_subtree_delete_non_active_root_rejected(
    store: SessionCatalogStore,
) -> None:
    ids = build_delete_tree(store)
    store.set_node_state(ids["root"], "deleting")
    with pytest.raises(RuntimeError, match="非 active"):
        create_subtree_record(store, root_node_id=ids["root"])


def test_create_or_get_subtree_delete_deleting_node_in_subtree_rejected(
    store: SessionCatalogStore,
) -> None:
    ids = build_delete_tree(store)
    # 子树内任一节点非 active → 拒绝新删除（防部分重叠删除）
    store.set_node_state(ids["s2"], "deleting")
    with pytest.raises(RuntimeError, match="非 active"):
        create_subtree_record(store, root_node_id=ids["root"])


def test_mark_subtree_deleting_marks_whole_subtree(
    store: SessionCatalogStore,
) -> None:
    ids = build_delete_tree(store)
    create_subtree_record(store, root_node_id=ids["root"])
    store.mark_subtree_deleting("del-key-1")
    for node_id in ids.values():
        node = store.get_node(node_id)
        assert node.state == "deleting"
        assert node.revision == 2
    record = store.get_subtree_delete_record("del-key-1")
    assert record.state == "deleting"


def test_mark_subtree_deleting_revision_drift_rejected(
    store: SessionCatalogStore,
) -> None:
    ids = build_delete_tree(store)
    create_subtree_record(store, root_node_id=ids["root"])
    # 冻结后 rename 子树内节点（revision 漂移）
    store.rename_node(ids["s1"], "漂移")
    with pytest.raises(RuntimeError, match="漂移"):
        store.mark_subtree_deleting("del-key-1")
    # 失败整体回滚：子树仍全部 active，record 保持 preparing
    for node_id in ids.values():
        assert store.get_node(node_id).state == "active"
    record = store.get_subtree_delete_record("del-key-1")
    assert record.state == "preparing"
    assert record.abort_reason is None


def test_mark_subtree_deleting_partial_active_rejected(
    store: SessionCatalogStore,
) -> None:
    ids = build_delete_tree(store)
    create_subtree_record(store, root_node_id=ids["root"])
    # 直连 SQL 注入：子树内一个节点已被标记 deleting（模拟并发删除已 mark）
    store.connection.execute(
        "UPDATE nodes SET state = 'deleting' WHERE node_id = ?",
        (ids["s3"],),
    )
    with pytest.raises(RuntimeError, match="非 active"):
        store.mark_subtree_deleting("del-key-1")
    # 失败回滚：其余节点未被 mark 改动
    assert store.get_node(ids["root"]).state == "active"
    assert store.get_node(ids["s1"]).state == "active"
    assert store.get_node(ids["s3"]).state == "deleting"
    assert store.get_subtree_delete_record("del-key-1").state == "preparing"


def test_mark_subtree_deleting_missing_node_rejected(
    store: SessionCatalogStore,
) -> None:
    ids = build_delete_tree(store)
    create_subtree_record(store, root_node_id=ids["root"])
    # 直连 SQL 删除冻结节点行（模拟绕过软件的外部改动）
    connection = sqlite3.connect(store.database_path)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DELETE FROM nodes WHERE node_id = ?", (ids["s2"],))
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(RuntimeError, match="已不存在"):
        store.mark_subtree_deleting("del-key-1")
    assert store.get_subtree_delete_record("del-key-1").state == "preparing"


def test_mark_subtree_deleting_idempotent_on_deleting(
    store: SessionCatalogStore,
) -> None:
    ids = build_delete_tree(store)
    create_subtree_record(store, root_node_id=ids["root"])
    store.mark_subtree_deleting("del-key-1")
    store.mark_subtree_deleting("del-key-1")  # 幂等 no-op
    assert store.get_node(ids["root"]).revision == 2
    assert store.get_subtree_delete_record("del-key-1").state == "deleting"


def test_mark_subtree_deleting_wrong_state_rejected(
    store: SessionCatalogStore,
) -> None:
    ids = build_delete_tree(store)
    create_subtree_record(store, root_node_id=ids["root"])
    store.abort_subtree_delete("del-key-1", "外部原因")
    with pytest.raises(RuntimeError, match="不允许 mark"):
        store.mark_subtree_deleting("del-key-1")


def test_record_drain_progress_transitions_to_draining_and_appends(
    store: SessionCatalogStore,
) -> None:
    ids = build_delete_tree(store)
    create_subtree_record(store, root_node_id=ids["root"])
    store.mark_subtree_deleting("del-key-1")
    store.record_drain_progress("del-key-1", ids["s1"])
    record = store.get_subtree_delete_record("del-key-1")
    assert record.state == "draining"
    assert record.drained_session_ids == (ids["s1"],)
    store.record_drain_progress("del-key-1", ids["s2"])
    store.record_drain_progress("del-key-1", ids["s3"])
    record = store.get_subtree_delete_record("del-key-1")
    assert record.state == "draining"
    assert record.drained_session_ids == (ids["s1"], ids["s2"], ids["s3"])


def test_record_drain_progress_idempotent_for_already_drained(
    store: SessionCatalogStore,
) -> None:
    ids = build_delete_tree(store)
    create_subtree_record(store, root_node_id=ids["root"])
    store.mark_subtree_deleting("del-key-1")
    store.record_drain_progress("del-key-1", ids["s1"])
    store.record_drain_progress("del-key-1", ids["s1"])  # 幂等 no-op
    record = store.get_subtree_delete_record("del-key-1")
    assert record.drained_session_ids == (ids["s1"],)


def test_record_drain_progress_rejects_unknown_session(
    store: SessionCatalogStore,
) -> None:
    ids = build_delete_tree(store)
    create_subtree_record(store, root_node_id=ids["root"])
    store.mark_subtree_deleting("del-key-1")
    outsider = create_session(store, parent_node_id=None).node_id
    with pytest.raises(RuntimeError, match="冻结集合"):
        store.record_drain_progress("del-key-1", outsider)
    assert store.get_subtree_delete_record("del-key-1").state == "deleting"


def test_record_drain_progress_rejects_preparing(
    store: SessionCatalogStore,
) -> None:
    ids = build_delete_tree(store)
    create_subtree_record(store, root_node_id=ids["root"])
    with pytest.raises(RuntimeError, match="drain 进度"):
        store.record_drain_progress("del-key-1", ids["s1"])


def test_finish_subtree_delete_requires_complete_drain(
    store: SessionCatalogStore,
) -> None:
    ids = build_delete_tree(store)
    create_subtree_record(store, root_node_id=ids["root"])
    store.mark_subtree_deleting("del-key-1")
    store.record_drain_progress("del-key-1", ids["s1"])
    with pytest.raises(RuntimeError, match="未完成"):
        store.finish_subtree_delete("del-key-1")
    # 失败回滚：节点行未删除、record 保持 draining
    for node_id in ids.values():
        store.get_node(node_id)
    assert store.get_subtree_delete_record("del-key-1").state == "draining"


def test_finish_subtree_delete_tombstones_rows_and_completes(
    store: SessionCatalogStore,
) -> None:
    ids = build_delete_tree(store)
    create_subtree_record(store, root_node_id=ids["root"])
    store.mark_subtree_deleting("del-key-1")
    for session_id in (ids["s1"], ids["s2"], ids["s3"]):
        store.record_drain_progress("del-key-1", session_id)
    store.finish_subtree_delete("del-key-1")
    # tombstone=行删除：全部冻结行消失
    for node_id in ids.values():
        with pytest.raises(KeyError):
            store.get_node(node_id)
    rows = store.connection.execute("SELECT COUNT(*) FROM nodes").fetchone()
    assert int(rows[0]) == 0
    record = store.get_subtree_delete_record("del-key-1")
    assert record.state == "completed"
    assert record.abort_reason is None


def test_finish_subtree_delete_rejects_deleting_with_sessions(
    store: SessionCatalogStore,
) -> None:
    ids = build_delete_tree(store)
    create_subtree_record(store, root_node_id=ids["root"])
    store.mark_subtree_deleting("del-key-1")
    # 有 session 未 drain：deleting 状态直接 finish 被完整性校验拒绝
    with pytest.raises(RuntimeError, match="未完成"):
        store.finish_subtree_delete("del-key-1")


def test_finish_subtree_delete_allows_deleting_without_sessions(
    store: SessionCatalogStore,
) -> None:
    # 空子树（纯 folder，无 session）：deleting → finish（无 drain 需要）
    root_id = store.create_folder(
        make_session_id(), WORKSPACE_ID, None, "空文件夹"
    ).node_id
    create_subtree_record(store, root_node_id=root_id)
    store.mark_subtree_deleting("del-key-1")
    store.finish_subtree_delete("del-key-1")
    with pytest.raises(KeyError):
        store.get_node(root_id)
    assert store.get_subtree_delete_record("del-key-1").state == "completed"


def test_finish_subtree_delete_wrong_state_rejected(
    store: SessionCatalogStore,
) -> None:
    ids = build_delete_tree(store)
    create_subtree_record(store, root_node_id=ids["root"])
    with pytest.raises(RuntimeError, match="不允许 finish"):
        store.finish_subtree_delete("del-key-1")  # preparing
    store.abort_subtree_delete("del-key-1", "外部原因")
    with pytest.raises(RuntimeError, match="不允许 finish"):
        store.finish_subtree_delete("del-key-1")  # aborted


def test_finish_subtree_delete_missing_record_raises_keyerror(
    store: SessionCatalogStore,
) -> None:
    with pytest.raises(KeyError):
        store.finish_subtree_delete("missing-key")


def test_abort_subtree_delete_records_reason(
    store: SessionCatalogStore,
) -> None:
    ids = build_delete_tree(store)
    create_subtree_record(store, root_node_id=ids["root"])
    aborted = store.abort_subtree_delete("del-key-1", "预检 blocker")
    assert aborted.state == "aborted"
    assert aborted.abort_reason == "预检 blocker"
    fetched = store.get_subtree_delete_record("del-key-1")
    assert fetched.state == "aborted"
    assert fetched.abort_reason == "预检 blocker"


def test_abort_subtree_delete_does_not_rollback_nodes(
    store: SessionCatalogStore,
) -> None:
    ids = build_delete_tree(store)
    create_subtree_record(store, root_node_id=ids["root"])
    store.mark_subtree_deleting("del-key-1")
    store.record_drain_progress("del-key-1", ids["s1"])
    store.abort_subtree_delete("del-key-1", "中途人工中止")
    # abort 只终结 record，不回滚节点状态（design.md「不回滚 active」）
    for node_id in ids.values():
        assert store.get_node(node_id).state == "deleting"
    record = store.get_subtree_delete_record("del-key-1")
    assert record.state == "aborted"
    assert record.drained_session_ids == (ids["s1"],)


def test_abort_subtree_delete_completed_rejected(
    store: SessionCatalogStore,
) -> None:
    root_id = store.create_folder(
        make_session_id(), WORKSPACE_ID, None, "空文件夹"
    ).node_id
    create_subtree_record(store, root_node_id=root_id)
    store.mark_subtree_deleting("del-key-1")
    store.finish_subtree_delete("del-key-1")
    with pytest.raises(RuntimeError, match="不可撤销"):
        store.abort_subtree_delete("del-key-1", "迟到的中止")


def test_abort_subtree_delete_idempotent_for_aborted(
    store: SessionCatalogStore,
) -> None:
    ids = build_delete_tree(store)
    create_subtree_record(store, root_node_id=ids["root"])
    first = store.abort_subtree_delete("del-key-1", "第一次")
    second = store.abort_subtree_delete("del-key-1", "第二次")
    assert second.state == "aborted"
    # 幂等：不覆盖原 abort_reason
    assert second.abort_reason == first.abort_reason == "第一次"


def test_abort_subtree_delete_missing_raises_keyerror(
    store: SessionCatalogStore,
) -> None:
    with pytest.raises(KeyError):
        store.abort_subtree_delete("missing-key", "原因")


def test_get_subtree_delete_record_missing_raises_keyerror(
    store: SessionCatalogStore,
) -> None:
    with pytest.raises(KeyError, match="不存在"):
        store.get_subtree_delete_record("missing-key")


def test_subtree_delete_state_check_rejected_by_constraint(
    store: SessionCatalogStore,
) -> None:
    ids = build_delete_tree(store)
    create_subtree_record(store, root_node_id=ids["root"])
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute(
            "UPDATE subtree_delete_records SET state = 'paused' "
            "WHERE subtree_delete_idempotency_key = 'del-key-1'"
        )


def test_subtree_delete_drained_session_ids_defaults_to_empty_array(
    store: SessionCatalogStore,
) -> None:
    ids = build_delete_tree(store)
    create_subtree_record(store, root_node_id=ids["root"])
    row = store.connection.execute(
        "SELECT drained_session_ids FROM subtree_delete_records "
        "WHERE subtree_delete_idempotency_key = 'del-key-1'"
    ).fetchone()
    assert str(row[0]) == "[]"


def test_delete_empty_folder_deletes_row(store: SessionCatalogStore) -> None:
    folder_id = make_session_id()
    store.create_folder(folder_id, WORKSPACE_ID, None, "空文件夹")
    store.delete_empty_folder(folder_id)
    with pytest.raises(KeyError):
        store.get_node(folder_id)


def test_delete_empty_folder_with_children_rejected(
    store: SessionCatalogStore,
) -> None:
    ids = build_delete_tree(store)
    with pytest.raises(RuntimeError, match="明确拒绝"):
        store.delete_empty_folder(ids["root"])
    # 子节点不受影响
    assert store.get_node(ids["s1"]).state == "active"


def test_delete_empty_folder_rejects_session_node(
    store: SessionCatalogStore,
) -> None:
    ids = build_delete_tree(store)
    with pytest.raises(RuntimeError, match="非 folder"):
        store.delete_empty_folder(ids["s3"])


def test_delete_empty_folder_rejects_deleting_folder(
    store: SessionCatalogStore,
) -> None:
    folder_id = make_session_id()
    store.create_folder(folder_id, WORKSPACE_ID, None, "将删除")
    store.set_node_state(folder_id, "deleting")
    with pytest.raises(RuntimeError, match="非 active"):
        store.delete_empty_folder(folder_id)


def test_delete_empty_folder_missing_raises_keyerror(
    store: SessionCatalogStore,
) -> None:
    with pytest.raises(KeyError):
        store.delete_empty_folder(make_session_id())


def test_schema_upgrade_v2_database_adds_subtree_table(
    tmp_path: Path, sessions_root: Path
) -> None:
    """库缺 subtree_delete_records 表（R14 加法补表）重开后幂等补建，不动版本。"""
    database_path = tmp_path / "navigation" / "session-catalog.sqlite"
    # 用真实 store 建立当前形态，再仅删除 R14 加法补建的 subtree_delete_records
    # 表，精确还原缺表现场；不在此复制第二份 DDL。
    first = SessionCatalogStore(database_path, sessions_root)
    folder_id = make_session_id()
    first.create_folder(folder_id, WORKSPACE_ID, None, "升级前文件夹")
    first.close()
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("DROP TABLE subtree_delete_records")
        connection.commit()
    finally:
        connection.close()
    store = SessionCatalogStore(database_path, sessions_root)
    try:
        # 加法补表：user_version 不动（保持 3），新表可用
        assert (
            int(store.connection.execute("PRAGMA user_version").fetchone()[0])
            == 3
        )
        record = create_subtree_record(store, root_node_id=folder_id)
        assert record.state == "preparing"
        # v2 数据完整保留
        assert store.get_node(folder_id).display_name == "升级前文件夹"
    finally:
        store.close()


# ----------------------------------------------------------------------
# R18-3：单连接跨线程事务串行化（确定性回归，Event 强制交叠窗口）
# ----------------------------------------------------------------------


def _park_inside_transaction(
    store: SessionCatalogStore,
    *,
    kind: str,
    entered: threading.Event,
    settled: threading.Event,
    release: threading.Event,
    holder_errors: list[BaseException],
) -> None:
    """在共享连接上持有读/写事务并停住（确定性交叠窗口的持有侧）。

    ``settled`` 由并发方线程在调用返回后置位——修复前这是持有方退出事务
    的正常路径，保证并发方的 BEGIN 必然落在持有方事务打开的窗口内（交叠
    确定性成立，不靠概率）；``release`` 是主线程超时兜底（修复后并发方
    阻塞在连接串行锁上无法及时置位 settled，由主线程放行持有方解除阻塞）。
    """
    try:
        if kind == "read":
            with store.read_transaction() as connection:
                connection.execute("SELECT COUNT(*) FROM nodes")
                entered.set()
                while not (settled.wait(0.01) or release.is_set()):
                    pass
        else:
            with store.write_transaction() as connection:
                connection.execute("SELECT COUNT(*) FROM nodes")
                entered.set()
                while not (settled.wait(0.01) or release.is_set()):
                    pass
    except BaseException as exc:  # noqa: BLE001 —— 线程内收集后交主线程断言（fail loud）
        holder_errors.append(exc)
        entered.set()


def _run_overlap_scenario(
    store: SessionCatalogStore,
    *,
    holder_kind: str,
    concurrent_op: Callable[[], object],
    settled_timeout: float = 0.5,
) -> tuple[object | None, BaseException | None, list[BaseException]]:
    """确定性交叠一次：持有方在事务内停住，并发方在另一线程发起调用。

    返回 (并发方结果, 并发方异常, 持有方异常列表)。修复前并发方的 BEGIN
    必然撞上持有方打开的事务并抛 OperationalError；修复后并发方阻塞在
    连接串行锁上，持有方释放后正常完成。
    """
    entered = threading.Event()
    settled = threading.Event()
    release = threading.Event()
    holder_errors: list[BaseException] = []
    worker: dict[str, object] = {}

    def run_concurrent() -> None:
        try:
            worker["result"] = concurrent_op()
        except BaseException as exc:  # noqa: BLE001 —— 线程内收集后交主线程断言
            worker["error"] = exc
        finally:
            settled.set()

    holder = threading.Thread(
        target=_park_inside_transaction,
        kwargs={
            "store": store,
            "kind": holder_kind,
            "entered": entered,
            "settled": settled,
            "release": release,
            "holder_errors": holder_errors,
        },
    )
    worker_thread = threading.Thread(target=run_concurrent)
    holder.start()
    assert entered.wait(5), "持有方未能在 5s 内进入事务"
    worker_thread.start()
    # 修复前：并发方立即失败并置位 settled；修复后：并发方阻塞在串行锁上，
    # settled 超时 → 兜底放行持有方。
    settled.wait(settled_timeout)
    release.set()
    worker_thread.join(10)
    holder.join(10)
    assert not worker_thread.is_alive(), "并发方调用未能在 10s 内返回"
    assert not holder.is_alive(), "持有方事务未能在 10s 内退出"
    return worker.get("result"), worker.get("error"), holder_errors


def test_read_transaction_hold_does_not_break_concurrent_writer(
    store: SessionCatalogStore,
) -> None:
    """R18-3 回归：读事务 hold 窗口内的并发写不抛 OperationalError。

    修复前：并发写 BEGIN IMMEDIATE 撞上持有中的读事务 →
    OperationalError("cannot start a transaction within a transaction")。
    修复后：并发写阻塞至读事务释放后正常完成，断言最终状态一致。
    """
    result, error, holder_errors = _run_overlap_scenario(
        store,
        holder_kind="read",
        concurrent_op=lambda: store.create_folder(
            make_session_id(), WORKSPACE_ID, None, "并发写（读持有窗口）"
        ),
    )
    assert error is None
    assert holder_errors == []
    assert isinstance(result, SessionCatalogNode)
    # 最终状态一致：并发写真正落库可见
    assert store.get_node(result.node_id).display_name == "并发写（读持有窗口）"


def test_write_transaction_hold_does_not_break_concurrent_reader(
    store: SessionCatalogStore,
) -> None:
    """R18-3 回归：写事务 hold 窗口内的并发读不抛 OperationalError。

    修复前：并发读 BEGIN DEFERRED 撞上持有中的写事务 → OperationalError。
    修复后：并发读阻塞至写事务提交后正常返回节点投影。
    """
    node = create_session(store)
    result, error, holder_errors = _run_overlap_scenario(
        store,
        holder_kind="write",
        concurrent_op=lambda: store.get_node(node.node_id),
    )
    assert error is None
    assert holder_errors == []
    assert isinstance(result, SessionCatalogNode)
    assert result.node_id == node.node_id


def test_interleaved_read_write_rounds_without_operational_error(
    store: SessionCatalogStore,
) -> None:
    """R18-3 回归：20 轮确定性交错全成功、无 OperationalError。

    每轮用 Event 强制交叠（持有方在事务内停住 + 并发方后发），四种组合
    轮转覆盖：读hold+写 / 写hold+读 / 读hold+读 / 写hold+写。修复前首轮
    即失败；修复后全部成功且最终状态一致。
    """
    reader_target = create_session(store)
    created_folders: list[str] = []
    for round_index in range(20):
        combo = round_index % 4
        holder_kind = "read" if combo in (0, 2) else "write"
        folder_name = f"交错轮{round_index}"
        if combo in (0, 3):

            def concurrent_write(name: str = folder_name) -> object:
                return store.create_folder(
                    make_session_id(), WORKSPACE_ID, None, name
                )

            op: Callable[[], object] = concurrent_write
        else:

            def concurrent_read(target: str = reader_target.node_id) -> object:
                return store.get_node(target)

            op = concurrent_read
        result, error, holder_errors = _run_overlap_scenario(
            store,
            holder_kind=holder_kind,
            concurrent_op=op,
            settled_timeout=0.05,
        )
        assert error is None, f"第 {round_index} 轮并发方异常: {error!r}"
        assert holder_errors == [], f"第 {round_index} 轮持有方异常: {holder_errors!r}"
        if combo in (0, 3):
            assert isinstance(result, SessionCatalogNode)
            created_folders.append(result.node_id)
        else:
            assert isinstance(result, SessionCatalogNode)
            assert result.node_id == reader_target.node_id
    # 全部落库可见 + 读目标完好
    children, _, _ = store.list_children(None, limit=64)
    child_ids = {child.node_id for child in children}
    assert set(created_folders) == child_ids - {reader_target.node_id}
    assert store.get_node(reader_target.node_id).node_id == reader_target.node_id


def test_same_thread_reentrant_lock_within_write_transaction(
    store: SessionCatalogStore,
) -> None:
    """R19（R18 审查非阻断 3）：同线程写事务体内嵌套重入连接串行锁不死锁。

    R18-3 选型 RLock 的可重入性是刻意选择（同线程事务体内再取
    ``connection`` 属性等嵌套获锁路径必须不死锁），但 R18 的 3 个跨线程
    回归用例对 ``RLock→Lock`` 变异无检出力（R18 审查 M2 实证变异下仍
    3 passed）——本用例补上该盲区：

    - 探针 1：事务体内 ``acquire(blocking=False)``——RLock 同线程重入
      立即成功；变异为不可重入 Lock 时立即返回 False（无死锁、无悬挂，
      用例确定性失败且不阻塞同套件其余用例）；
    - 探针 2：功能路径——事务体内经 ``connection`` 属性再次获锁并裸
      SELECT（R18 审查 M2 构造探针的功能等价物）。
    """
    node = create_session(store)
    with store.write_transaction() as connection:
        # 探针 1：非阻塞重入（同线程持锁期间的第二次获取）。
        assert store._connection_lock.acquire(blocking=False), (
            "同线程写事务内无法重入连接串行锁——_connection_lock 已不是"
            "可重入 RLock（RLock→Lock 变异）"
        )
        store._connection_lock.release()
        # 探针 2：功能重入——事务体内经 connection 属性获锁后裸查询，
        # 且能读到本事务内可见的既有数据。
        nested_connection = store.connection
        assert nested_connection is connection
        row = nested_connection.execute("SELECT COUNT(*) FROM nodes").fetchone()
        assert int(row[0]) >= 1
    # 事务正常提交后状态一致（重入未破坏提交语义）。
    assert store.get_node(node.node_id).node_id == node.node_id


# ----------------------------------------------------------------------
# 8.1-D：pinned ForkRetentionClaim 与整树删除竞争同一 CAS 事务
# ----------------------------------------------------------------------


def _create_claim(
    store: SessionCatalogStore,
    *,
    source_session_id: str,
    target_session_id: str,
    claim_id: str = "fork-claim-1",
    generation: int = 1,
    workspace_id: str = WORKSPACE_ID,
) -> ForkRetentionClaim:
    return store.create_or_get_fork_retention_claim(
        claim_id=claim_id,
        workspace_id=workspace_id,
        source_session_id=source_session_id,
        target_session_id=target_session_id,
        source_lifecycle_generation=generation,
    )


def test_fork_retention_claim_create_is_preparing_and_idempotent(
    store: SessionCatalogStore,
) -> None:
    ids = build_delete_tree(store)
    claim = _create_claim(
        store, source_session_id=ids["s1"], target_session_id=ids["s2"]
    )
    assert claim.state == "preparing"
    assert claim.source_session_id == ids["s1"]
    assert claim.target_session_id == ids["s2"]
    assert claim.source_lifecycle_generation == 1
    assert claim.release_reason is None
    # 同 preimage 幂等返回（崩溃恢复不重复建 claim）。
    again = _create_claim(
        store, source_session_id=ids["s1"], target_session_id=ids["s2"]
    )
    assert again == claim
    assert store.get_fork_retention_claim("fork-claim-1") == claim


def test_fork_retention_claim_preimage_conflict_rejected(
    store: SessionCatalogStore,
) -> None:
    ids = build_delete_tree(store)
    _create_claim(
        store, source_session_id=ids["s1"], target_session_id=ids["s2"]
    )
    with pytest.raises(RuntimeError, match="preimage 冲突"):
        _create_claim(
            store,
            source_session_id=ids["s1"],
            target_session_id=ids["s3"],
        )


def test_fork_retention_claim_rejects_deleting_source(
    store: SessionCatalogStore,
) -> None:
    """删除先行（source 已 deleting）时 claim 零副作用失败。"""
    ids = build_delete_tree(store)
    store.set_node_state(ids["s1"], "deleting")
    with pytest.raises(RuntimeError, match="零副作用失败"):
        _create_claim(
            store, source_session_id=ids["s1"], target_session_id=ids["s2"]
        )
    # 零副作用：未产生任何 claim 行。
    assert store.list_pinned_claims_for_source(ids["s1"]) == []


def test_fork_retention_claim_activate_then_release(
    store: SessionCatalogStore,
) -> None:
    ids = build_delete_tree(store)
    _create_claim(
        store,
        source_session_id=ids["s1"],
        target_session_id=ids["s2"],
        generation=7,
    )
    active = store.activate_fork_retention_claim(
        "fork-claim-1", expected_generation=7
    )
    assert active.state == "active"
    # 幂等重激活。
    assert store.activate_fork_retention_claim(
        "fork-claim-1", expected_generation=7
    ).state == "active"
    released = store.release_fork_retention_claim("fork-claim-1", "target 删除")
    assert released.state == "released"
    assert released.release_reason == "target 删除"
    # 已 released 不从 preparing/active 复活。
    with pytest.raises(RuntimeError, match="状态不允许激活"):
        store.activate_fork_retention_claim(
            "fork-claim-1", expected_generation=7
        )
    # released 不再计入 blocker。
    assert store.list_pinned_claims_for_source(ids["s1"]) == []


def test_fork_retention_claim_activate_generation_drift_fails_closed(
    store: SessionCatalogStore,
) -> None:
    ids = build_delete_tree(store)
    _create_claim(
        store,
        source_session_id=ids["s1"],
        target_session_id=ids["s2"],
        generation=3,
    )
    with pytest.raises(RuntimeError, match="generation 已漂移"):
        store.activate_fork_retention_claim(
            "fork-claim-1", expected_generation=4
        )


def test_release_fork_retention_claim_missing_raises_keyerror(
    store: SessionCatalogStore,
) -> None:
    with pytest.raises(KeyError):
        store.release_fork_retention_claim("missing", "原因")


def test_mark_subtree_deleting_reports_active_claim_blocker(
    store: SessionCatalogStore,
) -> None:
    """claim 先行：删除在整树 catalog deleting 前返回具体 blocker 且全树 active。"""
    ids = build_delete_tree(store)
    _create_claim(
        store, source_session_id=ids["s1"], target_session_id=ids["s2"]
    )
    store.activate_fork_retention_claim(
        "fork-claim-1", expected_generation=1
    )
    record = create_subtree_record(store, root_node_id=ids["root"])
    with pytest.raises(SourceRetainedByForkError) as excinfo:
        store.mark_subtree_deleting("del-key-1")
    # blocker 具体到 Session 与 claim。
    assert excinfo.value.source_session_id == ids["s1"]
    assert excinfo.value.claim_id == "fork-claim-1"
    assert excinfo.value.target_session_id == ids["s2"]
    # 整棵子树保持 active，record 保持 preparing。
    for node_id in (ids["root"], ids["s1"], ids["s2"], ids["s3"]):
        assert store.get_node(node_id).state == "active"
    assert store.get_subtree_delete_record("del-key-1").state == "preparing"
    assert record.frozen_node_ids


def test_mark_subtree_deleting_reports_preparing_claim_blocker(
    store: SessionCatalogStore,
) -> None:
    """preparing claim 无墙钟过期，删除须 fail closed 要求 recovery。"""
    ids = build_delete_tree(store)
    _create_claim(
        store, source_session_id=ids["s2"], target_session_id=ids["s3"]
    )
    create_subtree_record(store, root_node_id=ids["root"])
    with pytest.raises(SourceRetentionOperationPendingError) as excinfo:
        store.mark_subtree_deleting("del-key-1")
    assert excinfo.value.source_session_id == ids["s2"]
    assert excinfo.value.claim_id == "fork-claim-1"
    for node_id in (ids["root"], ids["s1"], ids["s2"], ids["s3"]):
        assert store.get_node(node_id).state == "active"


def test_mark_subtree_deleting_blocker_in_nested_child_session(
    store: SessionCatalogStore,
) -> None:
    """blocker 位于不同子 Session 时同样被整树预检捕获。"""
    ids = build_delete_tree(store)
    # s3 是 s1→folder_b→s3 的深层后代。
    _create_claim(
        store, source_session_id=ids["s3"], target_session_id=ids["s2"]
    )
    create_subtree_record(store, root_node_id=ids["root"])
    with pytest.raises(SourceRetentionOperationPendingError) as excinfo:
        store.mark_subtree_deleting("del-key-1")
    assert excinfo.value.source_session_id == ids["s3"]


def test_mark_subtree_deleting_succeeds_without_claims(
    store: SessionCatalogStore,
) -> None:
    """无 claim 时既有删除语义不变（零回归）。"""
    ids = build_delete_tree(store)
    create_subtree_record(store, root_node_id=ids["root"])
    store.mark_subtree_deleting("del-key-1")
    for node_id in (ids["root"], ids["s1"], ids["s2"], ids["s3"]):
        assert store.get_node(node_id).state == "deleting"


def test_claim_after_delete_committed_catalog_deleting_fails(
    store: SessionCatalogStore,
) -> None:
    """删除先行：整树 deleting 提交后 fork 建 claim 零副作用失败。"""
    ids = build_delete_tree(store)
    create_subtree_record(store, root_node_id=ids["root"])
    store.mark_subtree_deleting("del-key-1")
    with pytest.raises(RuntimeError, match="非 active"):
        _create_claim(
            store, source_session_id=ids["s1"], target_session_id=ids["s2"]
        )
    assert store.list_pinned_claims_for_source(ids["s1"]) == []


def test_released_claim_then_delete_succeeds(
    store: SessionCatalogStore,
) -> None:
    """claim 释放后删除可继续（active/preparing/released 三态覆盖）。"""
    ids = build_delete_tree(store)
    _create_claim(
        store, source_session_id=ids["s1"], target_session_id=ids["s2"]
    )
    store.activate_fork_retention_claim("fork-claim-1", expected_generation=1)
    store.release_fork_retention_claim("fork-claim-1", "target 删除")
    create_subtree_record(store, root_node_id=ids["root"])
    store.mark_subtree_deleting("del-key-1")
    assert store.get_node(ids["s1"]).state == "deleting"


def test_claim_and_delete_share_write_transaction_generation(
    store: SessionCatalogStore,
) -> None:
    """claim 与删除提交都推进 generation（同一写事务序列竞争的证据）。"""
    ids = build_delete_tree(store)
    before = store.current_generation()
    _create_claim(
        store, source_session_id=ids["s1"], target_session_id=ids["s2"]
    )
    assert store.current_generation() == before + 1


def test_create_or_get_claim_in_caller_transaction_rolls_back_together(
    store: SessionCatalogStore,
) -> None:
    """caller 事务内建 claim 与旁挂写入可整体回滚（原子性 seam）。"""
    ids = build_delete_tree(store)
    with (
        pytest.raises(RuntimeError, match="注入失败"),
        store.write_transaction() as connection,
    ):
        store.create_or_get_fork_retention_claim(
            claim_id="fork-claim-atomic",
            workspace_id=WORKSPACE_ID,
            source_session_id=ids["s1"],
            target_session_id=ids["s2"],
            source_lifecycle_generation=1,
            connection=connection,
        )
        raise RuntimeError("注入失败")
    with pytest.raises(KeyError):
        store.get_fork_retention_claim("fork-claim-atomic")


# ----------------------------------------------------------------------
# 8.1-F：SQLite online backup / generation+checksum / 维护模式 fail-closed
# ----------------------------------------------------------------------


def test_online_backup_records_generation_and_checksum(
    store: SessionCatalogStore, tmp_path: Path
) -> None:
    create_session(store, display_name="备份会话")
    backup_path = tmp_path / "backup" / "session-catalog.sqlite"
    manifest = store.create_consistent_backup(backup_path)
    assert isinstance(manifest, CatalogBackupManifest)
    assert manifest.generation == store.current_generation()
    assert backup_path.is_file()
    assert manifest.checksum == hashlib.sha256(backup_path.read_bytes()).hexdigest()
    # 备份是可打开的完整库（online backup 而非 WAL 复制）。
    restored = SessionCatalogStore(backup_path, store.sessions_root)
    try:
        assert restored.current_generation() == manifest.generation
    finally:
        restored.close()


def test_online_backup_rejects_existing_target(
    store: SessionCatalogStore, tmp_path: Path
) -> None:
    backup_path = tmp_path / "session-catalog.sqlite"
    store.create_consistent_backup(backup_path)
    with pytest.raises(RuntimeError, match="拒绝覆盖"):
        store.create_consistent_backup(backup_path)


def test_verify_backup_rejects_checksum_mismatch(
    store: SessionCatalogStore, tmp_path: Path
) -> None:
    backup_path = tmp_path / "session-catalog.sqlite"
    manifest = store.create_consistent_backup(backup_path)
    backup_path.write_bytes(b"corrupted")
    with pytest.raises(CatalogMaintenanceRequiredError, match="checksum 不符"):
        store.verify_consistent_backup(
            backup_path,
            expected_checksum=manifest.checksum,
            backup_generation=manifest.generation,
        )


def test_verify_backup_rejects_stale_generation(
    store: SessionCatalogStore, tmp_path: Path
) -> None:
    """备份落后于已提交操作 → 维护模式，不扫盘补齐。"""
    backup_path = tmp_path / "session-catalog.sqlite"
    manifest = store.create_consistent_backup(backup_path)
    # 备份之后又提交了新的操作：backup_generation < current。
    create_session(store, display_name="备份后会话")
    with pytest.raises(CatalogMaintenanceRequiredError, match="落后于已提交操作"):
        store.verify_consistent_backup(
            backup_path,
            expected_checksum=manifest.checksum,
            backup_generation=manifest.generation,
        )
    # 一致备份则通过。
    fresh = store.create_consistent_backup(tmp_path / "fresh.sqlite")
    report = store.verify_consistent_backup(
        tmp_path / "fresh.sqlite",
        expected_checksum=fresh.checksum,
        backup_generation=fresh.generation,
    )
    assert report.generation == fresh.generation
    assert report.quick_check == "ok"


def test_verify_backup_missing_enters_maintenance(
    store: SessionCatalogStore, tmp_path: Path
) -> None:
    with pytest.raises(CatalogMaintenanceRequiredError, match="备份缺失"):
        store.verify_consistent_backup(
            tmp_path / "nope.sqlite",
            expected_checksum="deadbeef",
            backup_generation=0,
        )


def test_startup_detects_unregistered_date_directory(
    store: SessionCatalogStore, sessions_root: Path, tmp_path: Path
) -> None:
    """未登记日期目录 → 维护模式，保留原数据且不补 active node。"""
    node = create_session(store, display_name="已登记会话")
    # 手工放一个 catalog 未登记的日期桶目录（绕过软件写入）。
    rogue = sessions_root / "2026" / "07" / "15" / make_session_id()
    rogue.mkdir(parents=True, exist_ok=True)
    with pytest.raises(CatalogMaintenanceRequiredError, match="未登记日期目录"):
        store.verify_registered_date_directories()
    # 原数据保留，未吸收为 active node。
    assert rogue.is_dir()
    assert store.get_node(node.node_id).state == "active"
    assert store.get_node(node.node_id).display_name == "已登记会话"


def test_registered_date_directories_pass(
    store: SessionCatalogStore,
) -> None:
    """catalog locator 与日期桶一一对应时校验通过。"""
    node = create_session(store, display_name="正常会话")
    sessions_root = store.sessions_root
    locator = store.get_node(node.node_id).storage_relative_locator
    assert locator is not None
    target = sessions_root / locator[len("sessions/"):]
    target.mkdir(parents=True, exist_ok=True)
    store.verify_registered_date_directories()


def test_store_rejects_corrupt_catalog_on_open(
    tmp_path: Path, sessions_root: Path
) -> None:
    """catalog 文件被截断/损坏时打开 fail closed（quick_check）。"""
    database_path = tmp_path / "navigation" / "session-catalog.sqlite"
    first = SessionCatalogStore(database_path, sessions_root)
    create_session(first, display_name="将被损坏")
    first.close()
    # 覆写文件头，破坏页结构。
    database_path.write_bytes(b"\x00" * 4096)
    with pytest.raises(CatalogMaintenanceRequiredError, match="无法打开"):
        SessionCatalogStore(database_path, sessions_root)


def test_schema_upgrade_v2_database_adds_claim_and_metadata_tables(
    tmp_path: Path, sessions_root: Path
) -> None:
    """v2 库（无 claim/metadata 表）打开后显式一次性迁移升 v3。"""
    database_path = tmp_path / "navigation" / "session-catalog.sqlite"
    first = SessionCatalogStore(database_path, sessions_root)
    folder_id = make_session_id()
    first.create_folder(folder_id, WORKSPACE_ID, None, "升级前文件夹")
    first.close()
    connection = sqlite3.connect(database_path)
    try:
        # 精确还原 v2 缺表现场：删两新表并回退 user_version。
        connection.execute("DROP TABLE fork_retention_claims")
        connection.execute("DROP TABLE catalog_metadata")
        connection.execute("PRAGMA user_version = 2")
        connection.commit()
    finally:
        connection.close()
    store = SessionCatalogStore(database_path, sessions_root)
    try:
        assert int(store.connection.execute("PRAGMA user_version").fetchone()[0]) == 3
        # 迁移后新表可用，generation 从 0 起。
        assert store.current_generation() == 0
        # v2 数据完整保留。
        assert store.get_node(folder_id).display_name == "升级前文件夹"
    finally:
        store.close()


# ----------------------------------------------------------------------
# 8.1-G 复用面：write/read 事务公开别名与 CAS 导航写
# ----------------------------------------------------------------------


def test_apply_navigation_mutation_rename_cas(store: SessionCatalogStore) -> None:
    node = store.create_folder(make_session_id(), WORKSPACE_ID, None, "旧名")
    updated = store.apply_navigation_mutation(
        node.node_id, expected_revision=1, new_display_name="新名"
    )
    assert updated.display_name == "新名"
    assert updated.revision == 2


def test_apply_navigation_mutation_revision_drift_rejected(
    store: SessionCatalogStore,
) -> None:
    node = store.create_folder(make_session_id(), WORKSPACE_ID, None, "名")
    store.rename_node(node.node_id, "先改名")
    with pytest.raises(RuntimeError, match="CAS 失败"):
        store.apply_navigation_mutation(
            node.node_id, expected_revision=1, new_display_name="再加名"
        )


def test_apply_navigation_mutation_move_and_sentinel(
    store: SessionCatalogStore, tree: CatalogTree
) -> None:
    # 只改名（不改父）：哨兵让父保持不变。
    renamed = store.apply_navigation_mutation(
        tree.session_s5, expected_revision=1, new_display_name="仅改名"
    )
    assert renamed.parent_node_id is None
    # 移到根：显式传 None。
    moved = store.apply_navigation_mutation(
        tree.session_s2, expected_revision=1, new_parent_node_id=None
    )
    assert moved.parent_node_id is None
    assert moved.revision == 2


def test_apply_navigation_mutation_rejects_duplicate_sibling_name(
    store: SessionCatalogStore,
) -> None:
    parent = store.create_folder(make_session_id(), WORKSPACE_ID, None, "父")
    store.create_folder(make_session_id(), WORKSPACE_ID, parent.node_id, "同名")
    other = store.create_folder(
        make_session_id(), WORKSPACE_ID, parent.node_id, "另一个"
    )
    with pytest.raises(RuntimeError, match="同名兄弟冲突"):
        store.apply_navigation_mutation(
            other.node_id, expected_revision=1, new_display_name="同名"
        )


def test_apply_navigation_mutation_cycle_rejected(
    store: SessionCatalogStore, tree: CatalogTree
) -> None:
    with pytest.raises(RuntimeError, match="循环"):
        store.apply_navigation_mutation(
            tree.folder_a, expected_revision=1, new_parent_node_id=tree.session_s3
        )


def test_apply_navigation_mutation_in_caller_transaction(
    store: SessionCatalogStore,
) -> None:
    """同一 caller 事务内做 node CAS + 旁挂写入（原子性 seam）。"""
    node = store.create_folder(make_session_id(), WORKSPACE_ID, None, "原子")
    with store.write_transaction() as connection:
        updated = store.apply_navigation_mutation(
            node.node_id,
            expected_revision=1,
            new_display_name="原子改",
            connection=connection,
        )
        assert updated.revision == 2
    assert store.get_node(node.node_id).display_name == "原子改"


def test_read_transaction_shared_snapshot(
    store: SessionCatalogStore,
) -> None:
    node = create_session(store, display_name="读事务")
    with store.read_transaction() as connection:
        row = connection.execute(
            "SELECT display_name FROM nodes WHERE node_id = ?", (node.node_id,)
        ).fetchone()
        assert str(row[0]) == "读事务"


def test_create_folder_in_caller_transaction(
    store: SessionCatalogStore,
) -> None:
    with store.write_transaction() as connection:
        created = store.create_folder(
            make_session_id(),
            WORKSPACE_ID,
            None,
            "事务内 folder",
            connection=connection,
        )
    assert store.get_node(created.node_id).display_name == "事务内 folder"
