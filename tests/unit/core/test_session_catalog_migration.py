"""session-catalog JSON index → SQLite + 日期桶迁移机器测试(OpenSpec 8.2 切片2)。

全部用例使用 tmp_path 构造旧权威(index + 嵌套物理树),不把项目根注册为
测试工作区;断言迁移机器不切权威、内容 bytes 保持、隔离/删除布局可审计、
失败保留 journal/旧树现场。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core.session_catalog_migration import (
    QuarantinedNode,
    SessionCatalogMigrationError,
    SessionCatalogMigrator,
)
from app.core.session_catalog_store import SessionCatalogStore, validate_thread_id
from app.core.session_control_store import SessionControlStore
from app.core.session_tree.support import (
    FOLDER_MANIFEST_NAME,
    SESSION_CHILDREN_DIR_NAME,
    SESSION_MANIFEST_NAME,
)

WORKSPACE_ID = "ws-primary"

DEFAULT_CREATED_AT = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
DEFAULT_UPDATED_AT = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)


# ----------------------------------------------------------------------
# fixture 与构造 helper
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MigrationWorkspace:
    """tmp_path 下的迁移工作区布局(生产 `.boxteam/` 的等价物)。"""

    root: Path

    @property
    def sessions_root(self) -> Path:
        return self.root / "sessions"

    @property
    def navigation_dir(self) -> Path:
        return self.root / "navigation"

    @property
    def index_path(self) -> Path:
        return self.navigation_dir / "session-catalog-index.json"

    @property
    def database_path(self) -> Path:
        return self.navigation_dir / "session-catalog.sqlite"

    @property
    def maintenance_root(self) -> Path:
        return self.root / "maintenance"

    @property
    def journal_path(self) -> Path:
        return self.maintenance_root / "session-catalog-migration" / "journal.json"


@pytest.fixture
def workspace(tmp_path: Path) -> MigrationWorkspace:
    return MigrationWorkspace(root=tmp_path / "workspace")


def make_migrator(workspace: MigrationWorkspace) -> SessionCatalogMigrator:
    return SessionCatalogMigrator(
        workspace_id=WORKSPACE_ID,
        sessions_root=workspace.sessions_root,
        database_path=workspace.database_path,
        maintenance_root=workspace.maintenance_root,
    )


def make_session_id() -> str:
    return f"ses_{uuid.uuid4().hex}"


def make_thread_id() -> str:
    return f"thr_{uuid.uuid4().hex}"


def _hex_payload_with(index: int, char: str) -> str:
    """把合法 UUIDv4 payload 的指定 hex 位替换成给定字符。"""
    payload = list(uuid.uuid4().hex)
    payload[index] = char
    return "".join(payload)


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _write_folder_dir(
    parent_dir: Path, folder_id: str, *, created_at: datetime = DEFAULT_CREATED_AT
) -> None:
    directory = parent_dir / folder_id
    directory.mkdir(parents=True, exist_ok=True)
    _write_json(
        directory / FOLDER_MANIFEST_NAME,
        {
            "schema_version": 1,
            "folder_id": folder_id,
            "created_at": created_at.isoformat(),
        },
    )


def _write_session_dir(
    parent_dir: Path,
    session_id: str,
    *,
    title: str,
    parent_session_id: str | None,
    created_at: datetime | str = DEFAULT_CREATED_AT,
    updated_at: datetime = DEFAULT_UPDATED_AT,
) -> None:
    directory = parent_dir / session_id
    directory.mkdir(parents=True, exist_ok=True)
    created_at_text = (
        created_at if isinstance(created_at, str) else created_at.isoformat()
    )
    _write_json(
        directory / SESSION_MANIFEST_NAME,
        {
            "session_id": session_id,
            "title": title,
            "created_at": created_at_text,
            "updated_at": updated_at.isoformat(),
            "parent_session_id": parent_session_id,
        },
    )


def _index_record(
    node_id: str,
    kind: str,
    name: str,
    parent_node_id: str | None,
    *,
    created_at: datetime | str = DEFAULT_CREATED_AT,
) -> dict[str, object]:
    created_at_text = (
        created_at if isinstance(created_at, str) else created_at.isoformat()
    )
    return {
        "node_id": node_id,
        "kind": kind,
        "name": name,
        "parent_node_id": parent_node_id,
        "created_at": created_at_text,
        "updated_at": DEFAULT_UPDATED_AT.isoformat(),
    }


def _write_index(index_path: Path, records: list[dict[str, object]]) -> None:
    _write_json(index_path, {"schema_version": 3, "revision": 1, "nodes": records})


@dataclass(frozen=True, slots=True)
class LegacyTree:
    """嵌套旧树:

    root
    ├── folder_a(folder)
    │   ├── session_s1(session)
    │   │   └── children/session_s2(session)
    │   └── session_s3(session)
    └── session_s4(session)
    """

    folder_a: str
    session_s1: str
    session_s2: str
    session_s3: str
    session_s4: str


def build_legacy_tree(workspace: MigrationWorkspace) -> LegacyTree:
    """构造 schema_version=3 的旧权威(index + 嵌套 Session/Folder/children 物理树)。"""
    folder_a = make_session_id()
    s1 = make_session_id()
    s2 = make_session_id()
    s3 = make_session_id()
    s4 = make_session_id()
    _write_folder_dir(workspace.sessions_root, folder_a)
    _write_session_dir(
        workspace.sessions_root / folder_a,
        s1,
        title="会话1",
        parent_session_id=None,
    )
    _write_session_dir(
        workspace.sessions_root / folder_a / s1 / SESSION_CHILDREN_DIR_NAME,
        s2,
        title="会话2",
        parent_session_id=s1,
    )
    _write_session_dir(
        workspace.sessions_root / folder_a,
        s3,
        title="会话3",
        parent_session_id=None,
    )
    _write_session_dir(
        workspace.sessions_root, s4, title="会话4", parent_session_id=None
    )
    _write_index(
        workspace.index_path,
        [
            _index_record(folder_a, "folder", "文件夹A", None),
            _index_record(s1, "session", "会话1", folder_a),
            _index_record(s2, "session", "会话2", s1),
            _index_record(s3, "session", "会话3", folder_a),
            _index_record(s4, "session", "会话4", None),
        ],
    )
    return LegacyTree(
        folder_a=folder_a,
        session_s1=s1,
        session_s2=s2,
        session_s3=s3,
        session_s4=s4,
    )


def open_catalog(workspace: MigrationWorkspace) -> SessionCatalogStore:
    """迁移后重新打开 catalog 做断言(调用方负责 close)。"""
    return SessionCatalogStore(workspace.database_path, workspace.sessions_root)


def collect_store_node_ids(store: SessionCatalogStore) -> set[str]:
    """用 store 读 API 递归收集全部节点 ID(独立于实现的对账投影)。"""
    found: set[str] = set()

    def walk(parent_node_id: str | None) -> None:
        cursor: str | None = None
        while True:
            items, next_cursor, has_more = store.list_children(
                parent_node_id, limit=100, cursor=cursor
            )
            for node in items:
                found.add(node.node_id)
                walk(node.node_id)
            if not has_more:
                return
            cursor = next_cursor

    walk(None)
    return found


def snapshot_old_tree(workspace: MigrationWorkspace) -> dict[Path, tuple[bytes, int]]:
    """快照旧树全部文件(index + manifests)的 bytes 与 mtime_ns。"""
    snapshots: dict[Path, tuple[bytes, int]] = {}
    for path in sorted(workspace.sessions_root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            snapshots[path] = (path.read_bytes(), path.stat().st_mtime_ns)
    snapshots[workspace.index_path] = (
        workspace.index_path.read_bytes(),
        workspace.index_path.stat().st_mtime_ns,
    )
    return snapshots


def read_journal(workspace: MigrationWorkspace) -> dict[str, object]:
    raw = json.loads(workspace.journal_path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def rewrite_journal_state(workspace: MigrationWorkspace, state: str) -> None:
    """把 completed journal 改回指定 state 并去掉 result(模拟崩溃窗口)。"""
    raw = read_journal(workspace)
    raw["state"] = state
    raw.pop("result", None)
    _write_json(workspace.journal_path, raw)


def rewrite_journal_payload(workspace: MigrationWorkspace, raw: dict[str, object]) -> None:
    _write_json(workspace.journal_path, raw)


def reset_journal_to_fresh_preparing(workspace: MigrationWorkspace) -> None:
    """把 completed journal 重置为「preparing 且物理迁移未开始」的崩溃态。

    模拟切片2 机器在 preparing 落盘后、任何物理动作前的崩溃窗口:
    state=preparing,physical 节全部回到 pending 初值,保留冻结映射与
    已分配 main_thread_id。
    """
    raw = read_journal(workspace)
    raw["state"] = "preparing"
    raw.pop("result", None)
    physical = raw["physical"]
    assert isinstance(physical, dict)
    for record in physical["sessions"].values():  # type: ignore[union-attr]
        assert isinstance(record, dict)
        record["state"] = "pending"
        record["original_session_json_sha256"] = None
        record["stripped_session_json_sha256"] = None
        record["content_manifest"] = None
        if "control_state" in record:
            record["control_state"] = "pending"
    for record in physical["folders"].values():  # type: ignore[union-attr]
        assert isinstance(record, dict)
        record["state"] = "pending"
    rewrite_journal_payload(workspace, raw)


def restore_sessions_root(
    workspace: MigrationWorkspace, backup_dir: Path
) -> None:
    """用迁移前快照整体还原 sessions_root(模拟物理迁移未发生的旧树)。"""
    shutil.rmtree(workspace.sessions_root)
    shutil.copytree(backup_dir, workspace.sessions_root)


# ----------------------------------------------------------------------
# 切片2 专用 helper(物理树迁移 / session-control)
# ----------------------------------------------------------------------

SESSION_CONTROL_DB_NAME = "session-control.sqlite"


def write_session_dir_full(
    parent_dir: Path,
    session_id: str,
    *,
    manifest_fields: dict[str, object],
    content_files: dict[str, bytes] | None = None,
) -> Path:
    """构造带完整 manifest 字段与内容文件的 session 目录(切片2 断言用)。

    manifest_fields 必须含 resolver 要求的 session_id/title/created_at/
    updated_at/parent_session_id;content_files 键为目录内相对 posix 路径。
    """
    directory = parent_dir / session_id
    directory.mkdir(parents=True, exist_ok=True)
    _write_json(directory / SESSION_MANIFEST_NAME, manifest_fields)
    for relative, payload in (content_files or {}).items():
        target = directory / Path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    return directory


def full_manifest_fields(
    session_id: str,
    *,
    title: str,
    parent_session_id: str | None,
    title_source: str = "user",
) -> dict[str, object]:
    """任务书 §2.2-B.2 记载的 session.json 全字段(含将被剥离的三个)。"""
    return {
        "session_id": session_id,
        "workspace_id": WORKSPACE_ID,
        "current_agent_id": "agent-default",
        "current_provider_id": "provider-default",
        "context_source_session_id": None,
        "kind": "normal",
        "delegation": None,
        "generation_origin": "user",
        "created_at": DEFAULT_CREATED_AT.isoformat(),
        "updated_at": DEFAULT_UPDATED_AT.isoformat(),
        "title": title,
        "title_source": title_source,
        "parent_session_id": parent_session_id,
    }


def date_bucket_dir(
    workspace: MigrationWorkspace,
    session_id: str,
    created_at: datetime = DEFAULT_CREATED_AT,
) -> Path:
    """会话迁移后的日期桶目录(locator = sessions/YYYY/MM/DD/{session_id})。"""
    utc_date = created_at.astimezone(UTC).date()
    return workspace.sessions_root / f"{utc_date:%Y/%m/%d}" / session_id


def dir_content_digest(directory: Path) -> dict[str, tuple[int, bytes]]:
    """目录内全部内容文件的 {相对 posix 路径: (size, bytes)}(排除
    session.json 与 session-control.sqlite 及其边车,与迁移清单口径一致)。"""
    excluded = {
        SESSION_MANIFEST_NAME,
        SESSION_CONTROL_DB_NAME,
        f"{SESSION_CONTROL_DB_NAME}-wal",
        f"{SESSION_CONTROL_DB_NAME}-shm",
    }
    digests: dict[str, tuple[int, bytes]] = {}
    for path in sorted(directory.rglob("*")):
        if path.is_file() and not path.is_symlink():
            relative = path.relative_to(directory).as_posix()
            if relative in excluded:
                continue
            digests[relative] = (path.stat().st_size, path.read_bytes())
    return digests


def journal_physical(workspace: MigrationWorkspace) -> tuple[dict, dict]:
    """读取 journal physical 节,返回 (sessions, folders) 两张记录表。"""
    raw = read_journal(workspace)
    physical = raw["physical"]
    assert isinstance(physical, dict)
    sessions = physical["sessions"]
    folders = physical["folders"]
    assert isinstance(sessions, dict) and isinstance(folders, dict)
    return sessions, folders  # type: ignore[return-value]


def set_session_staged_crash(
    workspace: MigrationWorkspace, session_id: str
) -> Path:
    """把已完成迁移中某会话改写为 staged 崩溃态:目录搬回 staging 槽位,
    journal state=catalog_rebuilt、该会话 state=staged(清单/sha 保留)。

    返回 staging 槽位路径。"""
    raw = read_journal(workspace)
    migration_id = raw["migration_id"]
    assert isinstance(migration_id, str)
    physical = raw["physical"]
    assert isinstance(physical, dict)
    sessions = physical["sessions"]
    assert isinstance(sessions, dict)
    record = sessions[session_id]
    assert isinstance(record, dict)
    staged_dir = date_bucket_dir(workspace, session_id)
    staging_slot = (
        workspace.sessions_root / ".staging" / migration_id / session_id
    )
    staging_slot.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(staged_dir), str(staging_slot))
    record["state"] = "staged"
    raw["state"] = "catalog_rebuilt"
    raw.pop("result", None)
    rewrite_journal_payload(workspace, raw)
    return staging_slot


def sqlite_execute(
    workspace: MigrationWorkspace, sql: str, params: tuple[object, ...] = ()
) -> None:
    connection = sqlite3.connect(workspace.database_path)
    try:
        connection.execute(sql, params)
        connection.commit()
    finally:
        connection.close()


def _write_journal_raw(workspace: MigrationWorkspace, payload: dict[str, object]) -> None:
    workspace.journal_path.parent.mkdir(parents=True, exist_ok=True)
    workspace.journal_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _base_journal(**overrides: object) -> dict[str, object]:
    """构造一份结构合法的 v2 journal 骨架(校验失败类用例在此之上做变异)。"""
    payload: dict[str, object] = {
        "schema_version": 2,
        "migration_name": SessionCatalogMigrator.MIGRATION_NAME,
        "state": "preparing",
        "workspace_id": WORKSPACE_ID,
        "migration_id": uuid.uuid4().hex,
        "backup": {"index_sha256": "0" * 64, "manifests": {}},
        "frozen_nodes": [],
        "quarantined_nodes": [],
        "physical": {"sessions": {}, "folders": {}},
    }
    payload.update(overrides)
    return payload


# ----------------------------------------------------------------------
# 错误类型
# ----------------------------------------------------------------------


def test_migration_error_is_runtime_error() -> None:
    assert issubclass(SessionCatalogMigrationError, RuntimeError)


# ----------------------------------------------------------------------
# 1. 正常迁移
# ----------------------------------------------------------------------


async def test_migrate_nested_tree_builds_sqlite_nodes(
    workspace: MigrationWorkspace,
) -> None:
    tree = build_legacy_tree(workspace)
    result = await make_migrator(workspace).migrate()

    assert result.migrated_folder_nodes == 1
    assert result.migrated_session_nodes == 4
    assert result.quarantined_nodes == ()
    assert result.journal_path == workspace.journal_path

    store = open_catalog(workspace)
    try:
        assert collect_store_node_ids(store) == {
            tree.folder_a,
            tree.session_s1,
            tree.session_s2,
            tree.session_s3,
            tree.session_s4,
        }
        folder = store.get_node(tree.folder_a)
        assert folder.kind == "folder"
        assert folder.parent_node_id is None
        assert folder.display_name == "文件夹A"
        assert folder.state == "active"
        assert folder.created_at is None
        assert folder.storage_relative_locator is None
        assert folder.main_thread_id is None

        s1 = store.get_node(tree.session_s1)
        assert s1.kind == "session"
        assert s1.parent_node_id == tree.folder_a
        assert s1.display_name == "会话1"
        assert s1.state == "active"
        assert s1.created_at == DEFAULT_CREATED_AT.isoformat()
        utc_date = DEFAULT_CREATED_AT.astimezone(UTC).date()
        assert s1.storage_relative_locator == (
            f"sessions/{utc_date:%Y/%m/%d}/{tree.session_s1}"
        )
        validate_thread_id(s1.main_thread_id)

        s2 = store.get_node(tree.session_s2)
        assert s2.parent_node_id == tree.session_s1
        assert s2.display_name == "会话2"
        assert s2.storage_relative_locator.endswith(tree.session_s2)
        validate_thread_id(s2.main_thread_id)

        assert store.get_node(tree.session_s3).parent_node_id == tree.folder_a
        assert store.get_node(tree.session_s4).parent_node_id is None

        store.verify_workspace_consistency()
    finally:
        store.close()


async def test_migrate_journal_records_frozen_mapping_and_completed(
    workspace: MigrationWorkspace,
) -> None:
    tree = build_legacy_tree(workspace)
    result = await make_migrator(workspace).migrate()

    raw = read_journal(workspace)
    assert raw["schema_version"] == 2
    assert raw["migration_name"] == SessionCatalogMigrator.MIGRATION_NAME
    assert raw["state"] == "completed"
    assert raw["workspace_id"] == WORKSPACE_ID
    # v2 新增:migration_id(uuid4 hex,staging 目录名)与 physical 节。
    migration_id = raw["migration_id"]
    assert isinstance(migration_id, str) and len(migration_id) == 32
    assert all(char in "0123456789abcdef" for char in migration_id)
    physical = raw["physical"]
    assert isinstance(physical, dict)
    assert set(physical["sessions"]) == {  # type: ignore[union-attr]
        tree.session_s1,
        tree.session_s2,
        tree.session_s3,
        tree.session_s4,
    }
    assert set(physical["folders"]) == {tree.folder_a}  # type: ignore[union-attr]
    frozen_by_id = {
        item["node_id"]: item for item in raw["frozen_nodes"]  # type: ignore[union-attr]
    }
    assert set(frozen_by_id) == {
        tree.folder_a,
        tree.session_s1,
        tree.session_s2,
        tree.session_s3,
        tree.session_s4,
    }
    folder_record = frozen_by_id[tree.folder_a]
    assert folder_record["kind"] == "folder"
    assert folder_record["display_name"] == "文件夹A"
    assert folder_record["parent_node_id"] is None
    assert "main_thread_id" not in folder_record
    s1_record = frozen_by_id[tree.session_s1]
    assert s1_record["kind"] == "session"
    assert s1_record["parent_node_id"] == tree.folder_a
    assert s1_record["created_at"] == DEFAULT_CREATED_AT.isoformat()
    assert s1_record["storage_relative_locator"].startswith("sessions/2026/06/01/")
    assert str(s1_record["main_thread_id"]).startswith("thr_")
    assert raw["quarantined_nodes"] == []
    # 备份清单:index sha256 与真实文件一致,manifests 覆盖全部节点
    backup = raw["backup"]
    assert isinstance(backup, dict)
    assert backup["index_sha256"] == hashlib.sha256(
        workspace.index_path.read_bytes()
    ).hexdigest()
    assert set(backup["manifests"]) == set(frozen_by_id)  # type: ignore[union-attr]
    result_node = raw["result"]
    assert isinstance(result_node, dict)
    assert result_node["migrated_session_nodes"] == result.migrated_session_nodes
    assert result_node["migrated_folder_nodes"] == result.migrated_folder_nodes


async def test_migrate_empty_index_builds_empty_tree(
    workspace: MigrationWorkspace,
) -> None:
    _write_index(workspace.index_path, [])
    result = await make_migrator(workspace).migrate()
    assert result.migrated_session_nodes == 0
    assert result.migrated_folder_nodes == 0
    assert result.quarantined_nodes == ()
    store = open_catalog(workspace)
    try:
        assert collect_store_node_ids(store) == set()
        store.verify_workspace_consistency()
    finally:
        store.close()
    raw = read_journal(workspace)
    assert raw["state"] == "completed"
    assert raw["frozen_nodes"] == []
    assert raw["quarantined_nodes"] == []


# ----------------------------------------------------------------------
# 2. quarantine
# ----------------------------------------------------------------------


def _illegal_session_ids() -> list[str]:
    """非法旧 ID 参数集:非 ses_ 前缀/大写/非 v4 version 位/非 variant 位/长度错。"""
    return [
        "job_" + uuid.uuid4().hex,  # 非 ses_ 前缀
        "SES_" + uuid.uuid4().hex,  # 大写前缀
        "ses_" + _hex_payload_with(12, "3"),  # 非 v4 version 位
        "ses_" + _hex_payload_with(16, "c"),  # 非 variant 位
        "ses_" + "a" * 31,  # 长度 31
        "ses_" + "a" * 33,  # 长度 33
    ]


@pytest.mark.parametrize("illegal_id", _illegal_session_ids())
async def test_migrate_quarantines_illegal_folder_id(
    workspace: MigrationWorkspace, illegal_id: str
) -> None:
    legal_id = make_session_id()
    _write_folder_dir(workspace.sessions_root, illegal_id)
    _write_session_dir(
        workspace.sessions_root, legal_id, title="合法", parent_session_id=None
    )
    _write_index(
        workspace.index_path,
        [
            _index_record(illegal_id, "folder", "非法文件夹", None),
            _index_record(legal_id, "session", "合法", None),
        ],
    )
    result = await make_migrator(workspace).migrate()

    assert result.quarantined_nodes == (
        QuarantinedNode(node_id=illegal_id, reason="illegal_id"),
    )
    assert result.migrated_folder_nodes == 0
    assert result.migrated_session_nodes == 1
    store = open_catalog(workspace)
    try:
        assert collect_store_node_ids(store) == {legal_id}
        with pytest.raises(KeyError):
            store.get_node(illegal_id)
    finally:
        store.close()


async def test_migrate_quarantines_naive_created_at_session(
    workspace: MigrationWorkspace,
) -> None:
    naive_id = make_session_id()
    legal_id = make_session_id()
    _write_session_dir(
        workspace.sessions_root,
        naive_id,
        title="无时区",
        parent_session_id=None,
        created_at="2026-06-01T12:00:00",  # naive,无 tzinfo
    )
    _write_session_dir(
        workspace.sessions_root, legal_id, title="合法", parent_session_id=None
    )
    _write_index(
        workspace.index_path,
        [
            _index_record(
                naive_id, "session", "无时区", None, created_at="2026-06-01T12:00:00"
            ),
            _index_record(legal_id, "session", "合法", None),
        ],
    )
    result = await make_migrator(workspace).migrate()

    assert result.quarantined_nodes == (
        QuarantinedNode(node_id=naive_id, reason="illegal_date"),
    )
    assert result.migrated_session_nodes == 1
    store = open_catalog(workspace)
    try:
        assert collect_store_node_ids(store) == {legal_id}
    finally:
        store.close()


async def test_migrate_quarantine_cascades_to_children(
    workspace: MigrationWorkspace,
) -> None:
    legal_id = make_session_id()
    illegal_folder = "job_" + uuid.uuid4().hex
    child_id = make_session_id()
    grandchild_id = make_session_id()
    _write_folder_dir(workspace.sessions_root, illegal_folder)
    _write_session_dir(
        workspace.sessions_root / illegal_folder,
        child_id,
        title="级联子",
        parent_session_id=None,
    )
    _write_session_dir(
        workspace.sessions_root / illegal_folder / child_id / SESSION_CHILDREN_DIR_NAME,
        grandchild_id,
        title="级联孙",
        parent_session_id=child_id,
    )
    _write_session_dir(
        workspace.sessions_root, legal_id, title="合法", parent_session_id=None
    )
    _write_index(
        workspace.index_path,
        [
            _index_record(illegal_folder, "folder", "非法文件夹", None),
            _index_record(child_id, "session", "级联子", illegal_folder),
            _index_record(grandchild_id, "session", "级联孙", child_id),
            _index_record(legal_id, "session", "合法", None),
        ],
    )
    result = await make_migrator(workspace).migrate()

    # 按拓扑序判定:根级非法 folder(illegal_id)先于其级联子(parent_quarantined)。
    assert result.quarantined_nodes == (
        QuarantinedNode(node_id=illegal_folder, reason="illegal_id"),
        QuarantinedNode(node_id=child_id, reason="parent_quarantined"),
        QuarantinedNode(node_id=grandchild_id, reason="parent_quarantined"),
    )
    assert result.migrated_session_nodes == 1
    assert result.migrated_folder_nodes == 0
    store = open_catalog(workspace)
    try:
        assert collect_store_node_ids(store) == {legal_id}
    finally:
        store.close()


async def test_migrate_quarantine_preserves_old_tree(
    workspace: MigrationWorkspace,
) -> None:
    """quarantine 物理目录原样隔离到 orphaned 保留审计;index 审计件不变。

    切片2 语义更新(任务书 §2.2-B.4):切片1「旧树原地不动」升级为
    「quarantine 目录 rename 到 orphaned/session-catalog-migration/,
    内容 bytes 不变;旧 index 不删不改,退役为审计件」。
    """
    illegal_id = "job_" + uuid.uuid4().hex
    legal_id = make_session_id()
    _write_folder_dir(workspace.sessions_root, illegal_id)
    _write_session_dir(
        workspace.sessions_root, legal_id, title="合法", parent_session_id=None
    )
    _write_index(
        workspace.index_path,
        [
            _index_record(illegal_id, "folder", "非法文件夹", None),
            _index_record(legal_id, "session", "合法", None),
        ],
    )
    before_index = (
        workspace.index_path.read_bytes(),
        workspace.index_path.stat().st_mtime_ns,
    )
    illegal_manifest_before = (
        workspace.sessions_root / illegal_id / FOLDER_MANIFEST_NAME
    ).read_bytes()

    await make_migrator(workspace).migrate()

    # index 审计件 bytes+mtime 不变。
    assert (
        workspace.index_path.read_bytes(),
        workspace.index_path.stat().st_mtime_ns,
    ) == before_index
    # 隔离目录保留原 manifest bytes,原位置不再存在。
    isolated = workspace.root / "orphaned" / "session-catalog-migration" / illegal_id
    assert (isolated / FOLDER_MANIFEST_NAME).is_file()
    assert (isolated / FOLDER_MANIFEST_NAME).read_bytes() == illegal_manifest_before
    assert not (workspace.sessions_root / illegal_id).exists()
    # 合法会话进日期桶。
    legal_locator_date = DEFAULT_CREATED_AT.astimezone(UTC).date()
    legal_dir = (
        workspace.sessions_root
        / f"{legal_locator_date:%Y/%m/%d}"
        / legal_id
    )
    assert (legal_dir / SESSION_MANIFEST_NAME).is_file()


async def test_migrate_result_counts_with_mixed_quarantine(
    workspace: MigrationWorkspace,
) -> None:
    legal_folder = make_session_id()
    legal_session = make_session_id()
    legal_root = make_session_id()
    illegal_folder = "job_" + uuid.uuid4().hex
    naive_session = make_session_id()
    cascade_child = make_session_id()
    _write_folder_dir(workspace.sessions_root, legal_folder)
    _write_session_dir(
        workspace.sessions_root / legal_folder,
        legal_session,
        title="合法子会话",
        parent_session_id=None,
    )
    _write_session_dir(
        workspace.sessions_root, legal_root, title="合法根会话", parent_session_id=None
    )
    _write_folder_dir(workspace.sessions_root, illegal_folder)
    _write_session_dir(
        workspace.sessions_root / illegal_folder,
        cascade_child,
        title="级联子",
        parent_session_id=None,
    )
    _write_session_dir(
        workspace.sessions_root,
        naive_session,
        title="无时区",
        parent_session_id=None,
        created_at="2026-06-01T12:00:00",
    )
    _write_index(
        workspace.index_path,
        [
            _index_record(legal_folder, "folder", "合法文件夹", None),
            _index_record(legal_session, "session", "合法子会话", legal_folder),
            _index_record(legal_root, "session", "合法根会话", None),
            _index_record(illegal_folder, "folder", "非法文件夹", None),
            _index_record(
                naive_session, "session", "无时区", None, created_at="2026-06-01T12:00:00"
            ),
            _index_record(cascade_child, "session", "级联子", illegal_folder),
        ],
    )
    result = await make_migrator(workspace).migrate()

    assert result.migrated_folder_nodes == 1
    assert result.migrated_session_nodes == 2
    assert result.quarantined_nodes == (
        QuarantinedNode(node_id=illegal_folder, reason="illegal_id"),
        QuarantinedNode(node_id=naive_session, reason="illegal_date"),
        QuarantinedNode(node_id=cascade_child, reason="parent_quarantined"),
    )


# ----------------------------------------------------------------------
# 3. fail closed:旧权威不一致
# ----------------------------------------------------------------------


async def test_migrate_fails_closed_when_index_missing(
    workspace: MigrationWorkspace,
) -> None:
    workspace.sessions_root.mkdir(parents=True)
    with pytest.raises(SessionCatalogMigrationError, match="缺失"):
        await make_migrator(workspace).migrate()
    assert not workspace.journal_path.exists()
    assert not workspace.database_path.exists()


@pytest.mark.parametrize("version", [2, 4, None, "3"])
async def test_migrate_fails_closed_on_index_schema_version(
    workspace: MigrationWorkspace, version: object
) -> None:
    _write_json(
        workspace.index_path, {"schema_version": version, "revision": 1, "nodes": []}
    )
    with pytest.raises(SessionCatalogMigrationError, match="schema 版本"):
        await make_migrator(workspace).migrate()
    assert not workspace.journal_path.exists()


async def test_migrate_fails_closed_when_node_dir_deleted(
    workspace: MigrationWorkspace,
) -> None:
    tree = build_legacy_tree(workspace)
    shutil.rmtree(workspace.sessions_root / tree.folder_a / tree.session_s3)
    with pytest.raises(SessionCatalogMigrationError):
        await make_migrator(workspace).migrate()
    assert not workspace.journal_path.exists()


async def test_migrate_fails_closed_when_manifest_id_tampered(
    workspace: MigrationWorkspace,
) -> None:
    tree = build_legacy_tree(workspace)
    manifest_path = workspace.sessions_root / tree.session_s4 / SESSION_MANIFEST_NAME
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["session_id"] = make_session_id()
    manifest_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SessionCatalogMigrationError):
        await make_migrator(workspace).migrate()


async def test_migrate_fails_closed_when_unmanaged_dir_added(
    workspace: MigrationWorkspace,
) -> None:
    build_legacy_tree(workspace)
    (workspace.sessions_root / "unmanaged").mkdir()
    with pytest.raises(SessionCatalogMigrationError):
        await make_migrator(workspace).migrate()


async def test_migrate_fails_closed_when_dir_name_drifts(
    workspace: MigrationWorkspace,
) -> None:
    tree = build_legacy_tree(workspace)
    (workspace.sessions_root / tree.session_s4).rename(
        workspace.sessions_root / "renamed"
    )
    with pytest.raises(SessionCatalogMigrationError):
        await make_migrator(workspace).migrate()


# ----------------------------------------------------------------------
# 3. fail closed:journal 冲突/损坏
# ----------------------------------------------------------------------


async def test_migrate_fails_closed_on_corrupt_journal(
    workspace: MigrationWorkspace,
) -> None:
    build_legacy_tree(workspace)
    workspace.journal_path.parent.mkdir(parents=True, exist_ok=True)
    workspace.journal_path.write_text("{corrupt", encoding="utf-8")
    with pytest.raises(SessionCatalogMigrationError, match="journal"):
        await make_migrator(workspace).migrate()
    # 保留原文件供人工恢复,不得重置台账。
    assert workspace.journal_path.read_text(encoding="utf-8") == "{corrupt"


@pytest.mark.parametrize("schema_version", [1, 3, None, "2"])
async def test_migrate_fails_closed_on_journal_schema_version(
    workspace: MigrationWorkspace, schema_version: object
) -> None:
    build_legacy_tree(workspace)
    _write_journal_raw(workspace, _base_journal(schema_version=schema_version))
    with pytest.raises(SessionCatalogMigrationError, match="schema"):
        await make_migrator(workspace).migrate()


async def test_migrate_fails_closed_on_v1_journal(
    workspace: MigrationWorkspace,
) -> None:
    """手写完整 v1 journal(R11 切片1 格式)→ fail closed,不猜测升级。"""
    build_legacy_tree(workspace)
    v1_journal: dict[str, object] = {
        "schema_version": 1,
        "migration_name": SessionCatalogMigrator.MIGRATION_NAME,
        "state": "preparing",
        "workspace_id": WORKSPACE_ID,
        "backup": {"index_sha256": "0" * 64, "manifests": {}},
        "frozen_nodes": [],
        "quarantined_nodes": [],
    }
    _write_journal_raw(workspace, v1_journal)
    before = workspace.journal_path.read_bytes()
    with pytest.raises(SessionCatalogMigrationError, match="v1"):
        await make_migrator(workspace).migrate()
    # 保留原文件供人工恢复,不得重置台账。
    assert workspace.journal_path.read_bytes() == before


async def test_migrate_fails_closed_on_journal_name_mismatch(
    workspace: MigrationWorkspace,
) -> None:
    build_legacy_tree(workspace)
    _write_journal_raw(
        workspace, _base_journal(migration_name="other-migration")
    )
    with pytest.raises(SessionCatalogMigrationError, match="migration_name"):
        await make_migrator(workspace).migrate()


async def test_migrate_fails_closed_on_journal_workspace_mismatch(
    workspace: MigrationWorkspace,
) -> None:
    build_legacy_tree(workspace)
    _write_journal_raw(workspace, _base_journal(workspace_id="ws-other"))
    with pytest.raises(SessionCatalogMigrationError, match="workspace_id"):
        await make_migrator(workspace).migrate()


@pytest.mark.parametrize("state", ["running", "", None])
async def test_migrate_fails_closed_on_journal_unknown_state(
    workspace: MigrationWorkspace, state: object
) -> None:
    build_legacy_tree(workspace)
    _write_journal_raw(workspace, _base_journal(state=state))
    with pytest.raises(SessionCatalogMigrationError, match="state"):
        await make_migrator(workspace).migrate()


@pytest.mark.parametrize(
    "result_payload",
    [
        None,  # 缺失 result 节
        "x",  # 非 object
        [],  # 非 object
        {
            "migrated_session_nodes": "3",  # 计数非 int
            "migrated_folder_nodes": 0,
            "quarantined_nodes": [],
        },
    ],
)
async def test_migrate_fails_closed_on_completed_journal_with_bad_result(
    workspace: MigrationWorkspace, result_payload: object
) -> None:
    build_legacy_tree(workspace)
    await make_migrator(workspace).migrate()
    journal = read_journal(workspace)
    if result_payload is None:
        journal.pop("result", None)
    else:
        journal["result"] = result_payload
    rewrite_journal_payload(workspace, journal)
    with pytest.raises(SessionCatalogMigrationError, match="result"):
        await make_migrator(workspace).migrate()


# ----------------------------------------------------------------------
# 4. 幂等 / 恢复
# ----------------------------------------------------------------------


async def test_migrate_is_idempotent_after_completed(
    workspace: MigrationWorkspace,
) -> None:
    build_legacy_tree(workspace)
    migrator = make_migrator(workspace)
    first = await migrator.migrate()
    journal_before = (
        workspace.journal_path.read_bytes(),
        workspace.journal_path.stat().st_mtime_ns,
    )

    second = await migrator.migrate()

    assert second == first
    # 不重写 journal。
    assert (
        workspace.journal_path.read_bytes(),
        workspace.journal_path.stat().st_mtime_ns,
    ) == journal_before


async def test_migrate_resumes_from_preparing_and_reuses_main_thread_ids(
    workspace: MigrationWorkspace,
) -> None:
    tree = build_legacy_tree(workspace)
    first = await make_migrator(workspace).migrate()

    journal = read_journal(workspace)
    frozen_by_id = {
        item["node_id"]: item for item in journal["frozen_nodes"]  # type: ignore[union-attr]
    }
    rewrite_journal_state(workspace, "preparing")
    # 模拟崩溃:删掉部分已重建行。
    sqlite_execute(
        workspace, "DELETE FROM nodes WHERE node_id = ?", (tree.session_s2,)
    )
    sqlite_execute(
        workspace, "DELETE FROM nodes WHERE node_id = ?", (tree.session_s3,)
    )

    second = await make_migrator(workspace).migrate()

    assert second.migrated_session_nodes == first.migrated_session_nodes
    assert second.migrated_folder_nodes == first.migrated_folder_nodes
    assert second.quarantined_nodes == first.quarantined_nodes
    store = open_catalog(workspace)
    try:
        assert collect_store_node_ids(store) == {
            tree.folder_a,
            tree.session_s1,
            tree.session_s2,
            tree.session_s3,
            tree.session_s4,
        }
        # 复用冻结映射中已分配的 main_thread_id,不重新生成。
        assert (
            store.get_node(tree.session_s2).main_thread_id
            == frozen_by_id[tree.session_s2]["main_thread_id"]
        )
        assert (
            store.get_node(tree.session_s1).main_thread_id
            == frozen_by_id[tree.session_s1]["main_thread_id"]
        )
        store.verify_workspace_consistency()
    finally:
        store.close()
    final = read_journal(workspace)
    assert final["state"] == "completed"


async def test_migrate_resume_requires_backup_match(
    workspace: MigrationWorkspace, tmp_path: Path
) -> None:
    """物理迁移未开始的重入要求旧树备份对账(R11 语义)。

    切片2 机器一次完整迁移会消费旧树,因此用「迁移前快照还原 + journal
    重置为 preparing/物理未开始」构造该崩溃窗口,再篡改旧 manifest 验证
    完整复验 fail closed。
    """
    tree = build_legacy_tree(workspace)
    pre_migration_backup = tmp_path / "pre-migration-sessions"
    shutil.copytree(workspace.sessions_root, pre_migration_backup)
    await make_migrator(workspace).migrate()
    reset_journal_to_fresh_preparing(workspace)
    restore_sessions_root(workspace, pre_migration_backup)
    manifest_path = workspace.sessions_root / tree.session_s4 / SESSION_MANIFEST_NAME
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["title"] = "被改动的标题"
    _write_json(manifest_path, raw)

    with pytest.raises(SessionCatalogMigrationError, match="漂移"):
        await make_migrator(workspace).migrate()


async def test_migrate_resume_fails_when_index_missing(
    workspace: MigrationWorkspace,
) -> None:
    build_legacy_tree(workspace)
    await make_migrator(workspace).migrate()
    rewrite_journal_state(workspace, "preparing")
    workspace.index_path.unlink()
    with pytest.raises(SessionCatalogMigrationError, match="缺失"):
        await make_migrator(workspace).migrate()


@pytest.mark.parametrize(
    "frozen_payload",
    [
        # kind=session 但缺 created_at
        {
            "node_id": "ses_" + "a" * 32,
            "kind": "session",
            "parent_node_id": None,
            "display_name": "损坏",
        },
        # 节点 ID 非法
        {
            "node_id": "not_valid",
            "kind": "folder",
            "parent_node_id": None,
            "display_name": "损坏",
        },
        # created_at 缺时区
        {
            "node_id": make_session_id(),
            "kind": "session",
            "parent_node_id": None,
            "display_name": "损坏",
            "created_at": "2026-06-01T12:00:00",
            "storage_relative_locator": None,
            "main_thread_id": None,
        },
    ],
)
async def test_migrate_resume_fails_on_corrupt_frozen_node(
    workspace: MigrationWorkspace, frozen_payload: object
) -> None:
    build_legacy_tree(workspace)
    await make_migrator(workspace).migrate()
    rewrite_journal_state(workspace, "preparing")
    journal = read_journal(workspace)
    journal["frozen_nodes"] = [frozen_payload]
    rewrite_journal_payload(workspace, journal)
    with pytest.raises(SessionCatalogMigrationError):
        await make_migrator(workspace).migrate()


async def test_migrate_resume_fails_on_unknown_quarantine_reason(
    workspace: MigrationWorkspace,
) -> None:
    build_legacy_tree(workspace)
    await make_migrator(workspace).migrate()
    rewrite_journal_state(workspace, "preparing")
    journal = read_journal(workspace)
    journal["quarantined_nodes"] = [
        {"node_id": make_session_id(), "reason": "mystery"}
    ]
    rewrite_journal_payload(workspace, journal)
    with pytest.raises(SessionCatalogMigrationError, match="reason"):
        await make_migrator(workspace).migrate()


async def test_migrate_fails_closed_when_store_row_conflicts_with_frozen(
    workspace: MigrationWorkspace,
) -> None:
    tree = build_legacy_tree(workspace)
    await make_migrator(workspace).migrate()
    rewrite_journal_state(workspace, "preparing")
    sqlite_execute(
        workspace,
        "UPDATE nodes SET display_name = ? WHERE node_id = ?",
        ("被篡改", tree.session_s1),
    )
    with pytest.raises(SessionCatalogMigrationError, match="不一致"):
        await make_migrator(workspace).migrate()


async def test_migrate_fails_closed_when_store_has_extra_row(
    workspace: MigrationWorkspace,
) -> None:
    build_legacy_tree(workspace)
    await make_migrator(workspace).migrate()
    rewrite_journal_state(workspace, "preparing")
    sqlite_execute(
        workspace,
        "INSERT INTO nodes (node_id, kind, parent_node_id, display_name, state, "
        "revision, workspace_id) VALUES (?, 'folder', NULL, '幽灵', 'active', 1, ?)",
        (make_session_id(), WORKSPACE_ID),
    )
    with pytest.raises(SessionCatalogMigrationError, match="节点集合"):
        await make_migrator(workspace).migrate()


async def test_migrate_fails_closed_when_store_row_state_not_active(
    workspace: MigrationWorkspace,
) -> None:
    """库中行 state 被改成非 active → 逐字段对账 fail closed(R11 审查 M1)。"""
    tree = build_legacy_tree(workspace)
    await make_migrator(workspace).migrate()
    rewrite_journal_state(workspace, "preparing")
    # SQL 注入口径:state 列 CHECK 允许 'deleting',绕过 store API 直接改库。
    sqlite_execute(
        workspace,
        "UPDATE nodes SET state = 'deleting' WHERE node_id = ?",
        (tree.session_s1,),
    )
    with pytest.raises(SessionCatalogMigrationError, match="不一致") as excinfo:
        await make_migrator(workspace).migrate()
    # 错误消息含可定位信息:被篡改节点的 node_id。
    assert tree.session_s1 in str(excinfo.value)


async def test_migrate_fails_closed_when_store_row_workspace_id_conflicts(
    workspace: MigrationWorkspace,
) -> None:
    """库中行 workspace_id 被改成其他工作区 → 逐字段对账 fail closed(R11 审查 M1)。

    篡改根级会话(s4,parent 为 None):``verify_workspace_consistency``
    只校验父子同 workspace 且跳过根节点,根行的 workspace_id 只由
    ``_assert_node_matches`` 的 workspace_id 分支兜底,用例因此精确封住
    该分支。
    """
    tree = build_legacy_tree(workspace)
    await make_migrator(workspace).migrate()
    rewrite_journal_state(workspace, "preparing")
    sqlite_execute(
        workspace,
        "UPDATE nodes SET workspace_id = 'ws-other' WHERE node_id = ?",
        (tree.session_s4,),
    )
    with pytest.raises(SessionCatalogMigrationError, match="不一致") as excinfo:
        await make_migrator(workspace).migrate()
    # 错误消息含可定位信息:被篡改节点的 node_id 与双侧行的 workspace_id。
    assert tree.session_s4 in str(excinfo.value)
    assert "ws-other" in str(excinfo.value)
    assert WORKSPACE_ID in str(excinfo.value)


# ----------------------------------------------------------------------
# 5. 备份复验
# ----------------------------------------------------------------------


async def test_migrate_preserves_old_tree_bytes_and_mtime(
    workspace: MigrationWorkspace,
) -> None:
    """切片2 语义:旧 index 审计件 bytes+mtime 不变;会话内容文件在新位置
    逐 bytes 保持(目录整体 rename,不逐文件复制)。

    切片1 的「整棵旧树原地不动」断言被物理迁移合法消费(任务书
    §2.2-B.3),本用例保留原意图中仍然成立的部分。
    """
    tree = build_legacy_tree(workspace)
    content_payload = b'{"record_type":"item","item_sequence":1}\n'
    content_path = (
        workspace.sessions_root / tree.session_s4 / "rollout" / "sample.jsonl"
    )
    content_path.parent.mkdir(parents=True, exist_ok=True)
    content_path.write_bytes(content_payload)
    before_index = (
        workspace.index_path.read_bytes(),
        workspace.index_path.stat().st_mtime_ns,
    )
    # index + 5 个 manifest + 1 个内容文件。
    assert len(snapshot_old_tree(workspace)) == 7

    await make_migrator(workspace).migrate()

    assert (
        workspace.index_path.read_bytes(),
        workspace.index_path.stat().st_mtime_ns,
    ) == before_index
    utc_date = DEFAULT_CREATED_AT.astimezone(UTC).date()
    new_dir = (
        workspace.sessions_root / f"{utc_date:%Y/%m/%d}" / tree.session_s4
    )
    assert (new_dir / "rollout" / "sample.jsonl").read_bytes() == content_payload


async def test_migrate_fails_closed_when_index_modified_after_completed(
    workspace: MigrationWorkspace,
) -> None:
    build_legacy_tree(workspace)
    await make_migrator(workspace).migrate()
    journal_before = read_journal(workspace)
    index_payload = json.loads(workspace.index_path.read_text(encoding="utf-8"))
    index_payload["revision"] = 99
    _write_json(workspace.index_path, index_payload)

    with pytest.raises(SessionCatalogMigrationError, match="漂移"):
        await make_migrator(workspace).migrate()
    # completed journal 不被重写(短路路径只校验,不落盘)。
    assert read_journal(workspace) == journal_before


# ----------------------------------------------------------------------
# 6. locator 冲突兜底(SQL 注入口径)
# ----------------------------------------------------------------------


async def test_migrate_locator_conflict_fail_closed(
    workspace: MigrationWorkspace,
) -> None:
    tree = build_legacy_tree(workspace)
    # 先建立 catalog 并通过 store connection 注入与冻结映射冲突 locator 的行
    # (store 层 locator 叶名必须等于 node_id,冲突只能由 SQL 注入构造)。
    seeding = SessionCatalogStore(workspace.database_path, workspace.sessions_root)
    try:
        seeding.connection.execute(
            "INSERT INTO nodes (node_id, kind, parent_node_id, display_name, state, "
            "revision, workspace_id, created_at, storage_relative_locator, "
            "main_thread_id) VALUES (?, 'session', NULL, '冲突行', 'active', 1, ?, "
            "?, ?, ?)",
            (
                make_session_id(),
                WORKSPACE_ID,
                DEFAULT_CREATED_AT.isoformat(),
                f"sessions/2026/06/01/{tree.session_s1}",
                make_thread_id(),
            ),
        )
    finally:
        seeding.close()

    with pytest.raises(SessionCatalogMigrationError, match="locator"):
        await make_migrator(workspace).migrate()


# ----------------------------------------------------------------------
# 8. 时区语义
# ----------------------------------------------------------------------


async def test_migrate_timezone_aware_created_at_uses_utc_date_bucket(
    workspace: MigrationWorkspace,
) -> None:
    plus8 = timezone(timedelta(hours=8))
    moment = datetime(2026, 6, 2, 2, 0, tzinfo=plus8)  # UTC 2026-06-01T18:00,跨日
    session_id = make_session_id()
    _write_session_dir(
        workspace.sessions_root,
        session_id,
        title="时区会话",
        parent_session_id=None,
        created_at=moment,
    )
    _write_index(
        workspace.index_path,
        [_index_record(session_id, "session", "时区会话", None, created_at=moment)],
    )

    await make_migrator(workspace).migrate()

    store = open_catalog(workspace)
    try:
        node = store.get_node(session_id)
        # +08:00 的 2026-06-02 凌晨落在 UTC 日期 2026-06-01。
        assert node.storage_relative_locator == f"sessions/2026/06/01/{session_id}"
        assert node.created_at == "2026-06-02T02:00:00+08:00"
    finally:
        store.close()


# ----------------------------------------------------------------------
# 9. gate 互斥
# ----------------------------------------------------------------------


async def test_migrate_concurrent_preparing_resumes_serialize(
    workspace: MigrationWorkspace,
) -> None:
    tree = build_legacy_tree(workspace)
    first = await make_migrator(workspace).migrate()
    rewrite_journal_state(workspace, "preparing")
    sqlite_execute(
        workspace, "DELETE FROM nodes WHERE node_id = ?", (tree.session_s4,)
    )

    results = await asyncio.gather(
        make_migrator(workspace).migrate(),
        make_migrator(workspace).migrate(),
    )

    for result in results:
        assert result.migrated_session_nodes == first.migrated_session_nodes
        assert result.migrated_folder_nodes == first.migrated_folder_nodes
        assert result.quarantined_nodes == first.quarantined_nodes
    store = open_catalog(workspace)
    try:
        assert collect_store_node_ids(store) == {
            tree.folder_a,
            tree.session_s1,
            tree.session_s2,
            tree.session_s3,
            tree.session_s4,
        }
        store.verify_workspace_consistency()
    finally:
        store.close()
    final = read_journal(workspace)
    assert final["state"] == "completed"


async def test_migrate_concurrent_completed_shortcircuit(
    workspace: MigrationWorkspace,
) -> None:
    build_legacy_tree(workspace)
    first = await make_migrator(workspace).migrate()

    results = await asyncio.gather(
        make_migrator(workspace).migrate(),
        make_migrator(workspace).migrate(),
    )

    assert results[0] == first
    assert results[1] == first


# ----------------------------------------------------------------------
# 切片2:完整迁移正常路径(§2.3-1)
# ----------------------------------------------------------------------


def content_payloads() -> dict[str, dict[str, bytes]]:
    """各会话自身内容文件期望表(不含物理嵌套的子会话文件——迁移后子会话独立放置)。"""
    return {
        "s1": {
            "rollout/main.jsonl": b'{"record_type":"item","item_sequence":1}\n',
            "rollout/parts/a.bin": b"\x00\x01\x02r12",
            "runs/exec-1.json": b'{"outcome":"completed"}',
        },
        "s2": {
            "debug/trace.log": b"trace-line-1\ntrace-line-2\n",
        },
        "s4": {
            "changes/patch.diff": b"--- a/x\n+++ b/x\n",
            "logs/agent.log": b"2026-06-01 start\n",
        },
    }


def build_content_tree(workspace: MigrationWorkspace) -> LegacyTree:
    """构造带内容文件的嵌套旧树(rollout/runs/debug 样例,bytes 可复查)。"""
    tree = build_legacy_tree(workspace)
    payloads = {
        tree.session_s1: content_payloads()["s1"],
        tree.session_s2: content_payloads()["s2"],
        tree.session_s4: content_payloads()["s4"],
    }
    roots = {
        tree.session_s1: workspace.sessions_root / tree.folder_a / tree.session_s1,
        tree.session_s2: (
            workspace.sessions_root
            / tree.folder_a
            / tree.session_s1
            / SESSION_CHILDREN_DIR_NAME
            / tree.session_s2
        ),
        tree.session_s4: workspace.sessions_root / tree.session_s4,
    }
    for session_id, files in payloads.items():
        for relative, payload in files.items():
            target = roots[session_id] / Path(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
    return tree


async def test_migrate_full_pipeline_places_sessions_in_date_buckets(
    workspace: MigrationWorkspace,
) -> None:
    """嵌套树全迁移:每会话落日期桶(locator 口径),旧位置消失,旧树瓦解。"""
    tree = build_content_tree(workspace)
    await make_migrator(workspace).migrate()

    for session_id in (
        tree.session_s1,
        tree.session_s2,
        tree.session_s3,
        tree.session_s4,
    ):
        target = date_bucket_dir(workspace, session_id)
        assert (target / SESSION_MANIFEST_NAME).is_file(), session_id
        assert (target / SESSION_CONTROL_DB_NAME).is_file(), session_id
    # 旧位置不再存在(folder/嵌套 children 全部瓦解)。
    assert not (workspace.sessions_root / tree.folder_a).exists()
    sessions, folders = journal_physical(workspace)
    for session_id in (
        tree.session_s1,
        tree.session_s2,
        tree.session_s3,
        tree.session_s4,
    ):
        assert sessions[session_id]["state"] == "placed"
        assert sessions[session_id]["control_state"] == "initialized"
    assert folders[tree.folder_a]["state"] == "deleted"


async def test_migrate_preserves_content_file_bytes_sha256(
    workspace: MigrationWorkspace,
) -> None:
    """rollout/runs/debug 等内容文件逐 bytes 在新位置一致(整体 rename)。"""
    tree = build_content_tree(workspace)
    expected = {
        tree.session_s1: content_payloads()["s1"],
        tree.session_s2: content_payloads()["s2"],
        tree.session_s4: content_payloads()["s4"],
    }

    await make_migrator(workspace).migrate()

    for session_id, files in expected.items():
        digest = dir_content_digest(date_bucket_dir(workspace, session_id))
        assert digest == {
            relative: (len(payload), payload)
            for relative, payload in files.items()
        }


async def test_migrate_strips_session_json_fields(
    workspace: MigrationWorkspace,
) -> None:
    """session.json 剥离 title/title_source/parent_session_id,其余字段值不变。"""
    tree = build_legacy_tree(workspace)
    # 用全字段 manifest 重建 s4(含三个剥离键)。
    shutil.rmtree(workspace.sessions_root / tree.session_s4)
    write_session_dir_full(
        workspace.sessions_root,
        tree.session_s4,
        manifest_fields=full_manifest_fields(
            tree.session_s4, title="会话4", parent_session_id=None
        ),
    )
    original = json.loads(
        (workspace.sessions_root / tree.session_s4 / SESSION_MANIFEST_NAME)
        .read_text(encoding="utf-8")
    )

    await make_migrator(workspace).migrate()

    stripped = json.loads(
        (date_bucket_dir(workspace, tree.session_s4) / SESSION_MANIFEST_NAME)
        .read_text(encoding="utf-8")
    )
    assert set(stripped) == set(original) - {"title", "title_source", "parent_session_id"}
    for key, value in stripped.items():
        assert original[key] == value, key


async def test_migrate_initializes_session_control_store(
    workspace: MigrationWorkspace,
) -> None:
    """session-control.sqlite 初始化:main row == 冻结 main_thread_id,fence (active,1)。"""
    tree = build_legacy_tree(workspace)
    await make_migrator(workspace).migrate()

    raw = read_journal(workspace)
    frozen_by_id = {
        item["node_id"]: item
        for item in raw["frozen_nodes"]  # type: ignore[union-attr]
        if isinstance(item, dict)
    }
    for session_id in (
        tree.session_s1,
        tree.session_s2,
        tree.session_s3,
        tree.session_s4,
    ):
        store = SessionControlStore(
            date_bucket_dir(workspace, session_id) / SESSION_CONTROL_DB_NAME
        )
        try:
            row = store.get_main_thread()
            assert str(row["thread_id"]) == frozen_by_id[session_id]["main_thread_id"]
            assert str(row["kind"]) == "main"
            assert store.get_fence() == ("active", 1)
            store.verify_matches_catalog_main_thread(
                str(frozen_by_id[session_id]["main_thread_id"])
            )
        finally:
            store.close()


async def test_migrate_deletes_folder_physical_dirs(
    workspace: MigrationWorkspace,
) -> None:
    """Folder 变 SQLite-only node:物理目录(含 folder 内嵌 folder)全部删除。"""
    tree = build_legacy_tree(workspace)
    # folder_a 内再嵌套一个 folder,其下挂一个会话(专测深先删除顺序)。
    nested_folder = make_session_id()
    nested_session = make_session_id()
    _write_folder_dir(workspace.sessions_root / tree.folder_a, nested_folder)
    _write_session_dir(
        workspace.sessions_root / tree.folder_a / nested_folder,
        nested_session,
        title="嵌套会话",
        parent_session_id=None,
    )
    records = [
        _index_record(tree.folder_a, "folder", "文件夹A", None),
        _index_record(nested_folder, "folder", "嵌套文件夹", tree.folder_a),
        _index_record(nested_session, "session", "嵌套会话", nested_folder),
        _index_record(tree.session_s1, "session", "会话1", tree.folder_a),
        _index_record(tree.session_s2, "session", "会话2", tree.session_s1),
        _index_record(tree.session_s3, "session", "会话3", tree.folder_a),
        _index_record(tree.session_s4, "session", "会话4", None),
    ]
    _write_index(workspace.index_path, records)

    result = await make_migrator(workspace).migrate()

    assert result.migrated_folder_nodes == 2
    assert not (workspace.sessions_root / tree.folder_a).exists()
    _, folders = journal_physical(workspace)
    assert folders[tree.folder_a]["state"] == "deleted"
    assert folders[nested_folder]["state"] == "deleted"
    assert (
        date_bucket_dir(workspace, nested_session) / SESSION_MANIFEST_NAME
    ).is_file()


async def test_migrate_cleans_staging_area(workspace: MigrationWorkspace) -> None:
    """全部 placed 后 staging 区清理:不再有 .staging/<migration_id>/。"""
    build_legacy_tree(workspace)
    await make_migrator(workspace).migrate()
    raw = read_journal(workspace)
    migration_id = raw["migration_id"]
    assert isinstance(migration_id, str) and len(migration_id) == 32
    assert not (workspace.sessions_root / ".staging" / migration_id).exists()


async def test_migrate_journal_v2_records_physical_section(
    workspace: MigrationWorkspace,
) -> None:
    """journal v2 physical 节:剥离前后 sha256、内容清单、隔离/删除布局。"""
    tree = build_content_tree(workspace)
    illegal_session = "job_" + uuid.uuid4().hex
    _write_session_dir(
        workspace.sessions_root,
        illegal_session,
        title="非法",
        parent_session_id=None,
    )
    _write_index(
        workspace.index_path,
        [
            _index_record(tree.folder_a, "folder", "文件夹A", None),
            _index_record(tree.session_s1, "session", "会话1", tree.folder_a),
            _index_record(
                tree.session_s2, "session", "会话2", tree.session_s1
            ),
            _index_record(tree.session_s3, "session", "会话3", tree.folder_a),
            _index_record(tree.session_s4, "session", "会话4", None),
            _index_record(illegal_session, "session", "非法", None),
        ],
    )
    original_s4_json = (
        workspace.sessions_root / tree.session_s4 / SESSION_MANIFEST_NAME
    ).read_bytes()
    await make_migrator(workspace).migrate()

    sessions, folders = journal_physical(workspace)
    # placed:剥离后 sha256 == 新位置文件;original sha == 迁移前 bytes。
    s4 = sessions[tree.session_s4]
    new_s4_json = (
        date_bucket_dir(workspace, tree.session_s4) / SESSION_MANIFEST_NAME
    ).read_bytes()
    assert new_s4_json != original_s4_json
    assert s4["stripped_session_json_sha256"] == hashlib.sha256(new_s4_json).hexdigest()
    assert (
        s4["original_session_json_sha256"]
        == hashlib.sha256(original_s4_json).hexdigest()
    )
    manifest = s4["content_manifest"]
    assert isinstance(manifest, list)
    paths = {entry["path"] for entry in manifest if isinstance(entry, dict)}
    assert paths == {"changes/patch.diff", "logs/agent.log"}
    # quarantine 隔离布局。
    assert sessions[illegal_session]["state"] == "quarantine_isolated"
    assert (
        workspace.root / "orphaned" / "session-catalog-migration" / illegal_session
    ).is_dir()
    assert folders[tree.folder_a]["state"] == "deleted"


# ----------------------------------------------------------------------
# 切片2:quarantine 隔离(§2.3-2)
# ----------------------------------------------------------------------


async def test_migrate_quarantine_sessions_isolated_to_orphaned(
    workspace: MigrationWorkspace,
) -> None:
    """非法 ID session → orphaned,不进日期桶,journal 记隔离状态。"""
    illegal_session = "job_" + uuid.uuid4().hex
    legal_session = make_session_id()
    _write_session_dir(
        workspace.sessions_root, illegal_session, title="非法", parent_session_id=None
    )
    _write_session_dir(
        workspace.sessions_root, legal_session, title="合法", parent_session_id=None
    )
    _write_index(
        workspace.index_path,
        [
            _index_record(illegal_session, "session", "非法", None),
            _index_record(legal_session, "session", "合法", None),
        ],
    )
    illegal_manifest_before = (
        workspace.sessions_root / illegal_session / SESSION_MANIFEST_NAME
    ).read_bytes()

    result = await make_migrator(workspace).migrate()

    isolated = (
        workspace.root / "orphaned" / "session-catalog-migration" / illegal_session
    )
    assert (isolated / SESSION_MANIFEST_NAME).is_file()
    assert (
        isolated / SESSION_MANIFEST_NAME
    ).read_bytes() == illegal_manifest_before
    assert not (workspace.sessions_root / illegal_session).exists()
    assert not date_bucket_dir(workspace, illegal_session).exists()
    sessions, _ = journal_physical(workspace)
    assert sessions[illegal_session]["state"] == "quarantine_isolated"
    assert sessions[illegal_session]["quarantine_reason"] == "illegal_id"
    assert any(
        node.node_id == illegal_session for node in result.quarantined_nodes
    )


async def test_migrate_quarantine_cascade_isolated_to_orphaned(
    workspace: MigrationWorkspace,
) -> None:
    """隔离级联:非法 folder 及其子/孙(合法 ID,parent_quarantined)逐个隔离。"""
    illegal_folder = "job_" + uuid.uuid4().hex
    child = make_session_id()
    grandchild = make_session_id()
    _write_folder_dir(workspace.sessions_root, illegal_folder)
    _write_session_dir(
        workspace.sessions_root / illegal_folder, child, title="子", parent_session_id=None
    )
    _write_session_dir(
        workspace.sessions_root / illegal_folder / child / SESSION_CHILDREN_DIR_NAME,
        grandchild,
        title="孙",
        parent_session_id=child,
    )
    _write_index(
        workspace.index_path,
        [
            _index_record(illegal_folder, "folder", "非法", None),
            _index_record(child, "session", "子", illegal_folder),
            _index_record(grandchild, "session", "孙", child),
        ],
    )
    child_manifest_before = (
        workspace.sessions_root / illegal_folder / child / SESSION_MANIFEST_NAME
    ).read_bytes()

    await make_migrator(workspace).migrate()

    orphaned = workspace.root / "orphaned" / "session-catalog-migration"
    # 子节点(合法 ID)独立隔离,manifest bytes 不变;孙随其自身隔离。
    assert (orphaned / child / SESSION_MANIFEST_NAME).read_bytes() == child_manifest_before
    assert (orphaned / grandchild / SESSION_MANIFEST_NAME).is_file()
    # 非法 folder 也隔离(B.4「quarantine 节点」),保留 folder manifest 审计。
    assert (orphaned / illegal_folder / FOLDER_MANIFEST_NAME).is_file()
    # 均不进日期桶。
    assert not date_bucket_dir(workspace, child).exists()
    assert not date_bucket_dir(workspace, grandchild).exists()
    sessions, folders = journal_physical(workspace)
    assert sessions[child]["state"] == "quarantine_isolated"
    assert sessions[child]["quarantine_reason"] == "parent_quarantined"
    assert sessions[grandchild]["state"] == "quarantine_isolated"
    assert folders[illegal_folder]["state"] == "quarantine_isolated"


async def test_migrate_quarantine_target_nonempty_fail_closed(
    workspace: MigrationWorkspace,
) -> None:
    """隔离目标已存在且非空 → fail closed,旧目录保持原样。"""
    illegal_session = "job_" + uuid.uuid4().hex
    _write_session_dir(
        workspace.sessions_root, illegal_session, title="非法", parent_session_id=None
    )
    _write_index(
        workspace.index_path,
        [_index_record(illegal_session, "session", "非法", None)],
    )
    target = (
        workspace.root / "orphaned" / "session-catalog-migration" / illegal_session
    )
    target.mkdir(parents=True)
    (target / "preexisting.txt").write_bytes(b"occupied")

    before = snapshot_old_tree(workspace)
    with pytest.raises(SessionCatalogMigrationError, match="非空"):
        await make_migrator(workspace).migrate()
    # 旧目录保留(隔离未执行)。
    assert (workspace.sessions_root / illegal_session / SESSION_MANIFEST_NAME).is_file()
    assert snapshot_old_tree(workspace) == before


# ----------------------------------------------------------------------
# 切片2:恢复语义(§2.3-3)
# ----------------------------------------------------------------------


async def test_migrate_resume_from_staged_crash_completes_placement(
    workspace: MigrationWorkspace,
) -> None:
    """staged 崩溃重入:目录在 staging、journal 记 staged → 继续放置到日期桶。"""
    tree = build_content_tree(workspace)
    await make_migrator(workspace).migrate()
    staging_slot = set_session_staged_crash(workspace, tree.session_s2)
    migration_id = read_journal(workspace)["migration_id"]
    assert isinstance(migration_id, str)

    await make_migrator(workspace).migrate()

    # 复用同一 migration_id;会话回到日期桶,目录内容完整。
    assert not staging_slot.exists()
    assert (date_bucket_dir(workspace, tree.session_s2) / SESSION_MANIFEST_NAME).is_file()
    assert (
        read_journal(workspace)["migration_id"] == migration_id
    )
    assert read_journal(workspace)["state"] == "completed"


async def test_migrate_resume_fails_when_placed_content_tampered(
    workspace: MigrationWorkspace,
) -> None:
    """placed 态重入:内容文件被改 → 内容清单对账 fail closed。"""
    tree = build_content_tree(workspace)
    await make_migrator(workspace).migrate()
    rewrite_journal_state(workspace, "catalog_rebuilt")
    target = date_bucket_dir(workspace, tree.session_s1)
    (target / "rollout" / "main.jsonl").write_bytes(b'{"record_type":"tampered"}\n')

    with pytest.raises(SessionCatalogMigrationError, match="内容清单"):
        await make_migrator(workspace).migrate()


async def test_migrate_resume_fails_when_placed_session_json_tampered(
    workspace: MigrationWorkspace,
) -> None:
    """placed 态重入:session.json 被改 → sha256 对账 fail closed。"""
    tree = build_legacy_tree(workspace)
    await make_migrator(workspace).migrate()
    rewrite_journal_state(workspace, "catalog_rebuilt")
    manifest_path = date_bucket_dir(workspace, tree.session_s4) / SESSION_MANIFEST_NAME
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["kind"] = "tampered"
    _write_json(manifest_path, raw)

    with pytest.raises(SessionCatalogMigrationError, match="sha256"):
        await make_migrator(workspace).migrate()


async def test_migrate_resume_folder_half_deleted_continues(
    workspace: MigrationWorkspace,
) -> None:
    """folder 半删除重入:journal 记 pending 但目录只剩 manifest → 继续删除。"""
    tree = build_legacy_tree(workspace)
    await make_migrator(workspace).migrate()
    raw = read_journal(workspace)
    raw["state"] = "catalog_rebuilt"
    raw.pop("result", None)
    folders = raw["physical"]["folders"]  # type: ignore[union-attr]
    assert isinstance(folders, dict)
    folders[tree.folder_a]["state"] = "pending"
    rewrite_journal_payload(workspace, raw)
    folder_dir = workspace.sessions_root / tree.folder_a
    folder_dir.mkdir(parents=True)
    (folder_dir / FOLDER_MANIFEST_NAME).write_bytes(b'{"schema_version":1}\n')

    await make_migrator(workspace).migrate()

    assert not folder_dir.exists()
    assert read_journal(workspace)["state"] == "completed"


async def test_migrate_resume_fails_on_staging_residue(
    workspace: MigrationWorkspace,
) -> None:
    """staging 残留目录(journal 无对应 staged 记录)→ fail closed,不吸收。"""
    build_legacy_tree(workspace)
    await make_migrator(workspace).migrate()
    rewrite_journal_state(workspace, "catalog_rebuilt")
    migration_id = read_journal(workspace)["migration_id"]
    assert isinstance(migration_id, str)
    residue = workspace.sessions_root / ".staging" / migration_id / "unknown-dir"
    residue.mkdir(parents=True)
    (residue / "orphan.bin").write_bytes(b"?")

    with pytest.raises(SessionCatalogMigrationError, match="残留"):
        await make_migrator(workspace).migrate()


async def test_migrate_resume_fails_when_date_bucket_target_exists(
    workspace: MigrationWorkspace,
) -> None:
    """日期桶目标已存在且 journal 记 staged → fail closed(不覆盖)。"""
    tree = build_legacy_tree(workspace)
    await make_migrator(workspace).migrate()
    set_session_staged_crash(workspace, tree.session_s4)
    date_bucket_dir(workspace, tree.session_s4).mkdir(parents=True)

    with pytest.raises(SessionCatalogMigrationError, match="已存在"):
        await make_migrator(workspace).migrate()


async def test_migrate_resume_fails_when_pending_old_dir_missing(
    workspace: MigrationWorkspace,
) -> None:
    """journal 记 pending 但旧位置目录已不存在 → fail closed(无法证明)。"""
    tree = build_legacy_tree(workspace)
    await make_migrator(workspace).migrate()
    raw = read_journal(workspace)
    raw["state"] = "catalog_rebuilt"
    raw.pop("result", None)
    sessions = raw["physical"]["sessions"]  # type: ignore[union-attr]
    assert isinstance(sessions, dict)
    sessions[tree.session_s4]["state"] = "pending"
    rewrite_journal_payload(workspace, raw)

    with pytest.raises(SessionCatalogMigrationError, match="无法证明"):
        await make_migrator(workspace).migrate()


async def test_migrate_completed_shortcircuit_detects_placed_content_drift(
    workspace: MigrationWorkspace,
) -> None:
    """completed 短路采用分层复验:placed 内容漂移即 fail closed。"""
    tree = build_content_tree(workspace)
    await make_migrator(workspace).migrate()
    target = date_bucket_dir(workspace, tree.session_s4)
    (target / "logs" / "agent.log").write_bytes(b"tampered-bytes\n")

    with pytest.raises(SessionCatalogMigrationError, match="内容清单"):
        await make_migrator(workspace).migrate()


# ----------------------------------------------------------------------
# 切片2:剥离与原子写(§2.3-4)
# ----------------------------------------------------------------------


async def test_migrate_strip_atomic_no_temp_files(
    workspace: MigrationWorkspace,
) -> None:
    """剥离走原子写:新位置无 .session.json.* 临时残留。"""
    tree = build_legacy_tree(workspace)
    await make_migrator(workspace).migrate()
    for session_id in (
        tree.session_s1,
        tree.session_s2,
        tree.session_s3,
        tree.session_s4,
    ):
        leftovers = [
            path.name
            for path in date_bucket_dir(workspace, session_id).iterdir()
            if path.name.startswith(f".{SESSION_MANIFEST_NAME}.")
        ]
        assert leftovers == []


async def test_migrate_journal_records_stripped_and_original_sha(
    workspace: MigrationWorkspace,
) -> None:
    """journal 记录剥离后 sha256 与迁移前 sha256(审计双值)。"""
    tree = build_legacy_tree(workspace)
    original_bytes = (
        workspace.sessions_root / tree.session_s4 / SESSION_MANIFEST_NAME
    ).read_bytes()
    await make_migrator(workspace).migrate()
    sessions, _ = journal_physical(workspace)
    record = sessions[tree.session_s4]
    assert (
        record["original_session_json_sha256"]
        == hashlib.sha256(original_bytes).hexdigest()
    )
    assert (
        record["stripped_session_json_sha256"]
        == hashlib.sha256(
            (date_bucket_dir(workspace, tree.session_s4) / SESSION_MANIFEST_NAME)
            .read_bytes()
        ).hexdigest()
    )
    assert record["original_session_json_sha256"] != record[
        "stripped_session_json_sha256"
    ]


# ----------------------------------------------------------------------
# 切片2:folder 非空与边界(§2.3-6/7)
# ----------------------------------------------------------------------


async def test_migrate_resume_fails_when_folder_has_unexpected_entry(
    workspace: MigrationWorkspace,
) -> None:
    """folder 目录含未预期条目(journal 级恢复场景)→ fail closed 保留审计。"""
    tree = build_legacy_tree(workspace)
    await make_migrator(workspace).migrate()
    raw = read_journal(workspace)
    raw["state"] = "catalog_rebuilt"
    raw.pop("result", None)
    folders = raw["physical"]["folders"]  # type: ignore[union-attr]
    assert isinstance(folders, dict)
    folders[tree.folder_a]["state"] = "pending"
    rewrite_journal_payload(workspace, raw)
    folder_dir = workspace.sessions_root / tree.folder_a
    folder_dir.mkdir(parents=True)
    (folder_dir / FOLDER_MANIFEST_NAME).write_bytes(b'{"schema_version":1}\n')
    (folder_dir / "stray.txt").write_bytes(b"unexpected-entry")

    with pytest.raises(SessionCatalogMigrationError, match="未预期条目"):
        await make_migrator(workspace).migrate()
    # 目录保留(stray 与 manifest 都在)。
    assert (folder_dir / "stray.txt").is_file()


async def test_migrate_empty_tree_physical_noop(
    workspace: MigrationWorkspace,
) -> None:
    """空树:无日期桶、无隔离区、staging 清理,completed。"""
    workspace.sessions_root.mkdir(parents=True, exist_ok=True)
    _write_index(workspace.index_path, [])
    result = await make_migrator(workspace).migrate()
    assert result.migrated_session_nodes == 0
    assert result.migrated_folder_nodes == 0
    assert result.quarantined_nodes == ()
    sessions, folders = journal_physical(workspace)
    assert sessions == {} and folders == {}
    assert not (workspace.sessions_root / ".staging").exists()
    assert read_journal(workspace)["state"] == "completed"


async def test_migrate_all_quarantine_tree_completes(
    workspace: MigrationWorkspace,
) -> None:
    """全隔离树:全部节点隔离、folder 隔离,零迁入,completed。"""
    illegal_folder = "job_" + uuid.uuid4().hex
    child = make_session_id()
    _write_folder_dir(workspace.sessions_root, illegal_folder)
    _write_session_dir(
        workspace.sessions_root / illegal_folder, child, title="子", parent_session_id=None
    )
    _write_index(
        workspace.index_path,
        [
            _index_record(illegal_folder, "folder", "非法", None),
            _index_record(child, "session", "子", illegal_folder),
        ],
    )

    result = await make_migrator(workspace).migrate()

    assert result.migrated_session_nodes == 0
    assert result.migrated_folder_nodes == 0
    assert {node.node_id for node in result.quarantined_nodes} == {
        illegal_folder,
        child,
    }
    orphaned = workspace.root / "orphaned" / "session-catalog-migration"
    assert (orphaned / child / SESSION_MANIFEST_NAME).is_file()
    assert (orphaned / illegal_folder / FOLDER_MANIFEST_NAME).is_file()
    sessions, folders = journal_physical(workspace)
    assert sessions[child]["state"] == "quarantine_isolated"
    assert folders[illegal_folder]["state"] == "quarantine_isolated"


async def test_migrate_nested_legal_sessions_land_independently(
    workspace: MigrationWorkspace,
) -> None:
    """父子会话(staging 深序)分别独立落各自日期桶,children/ 物理嵌套瓦解。"""
    tree = build_content_tree(workspace)
    await make_migrator(workspace).migrate()
    parent_dir = date_bucket_dir(workspace, tree.session_s1)
    child_dir = date_bucket_dir(workspace, tree.session_s2)
    # 父目录内不再有 children/ 下的子会话(子会话已独立放置)。
    assert not (parent_dir / SESSION_CHILDREN_DIR_NAME / tree.session_s2).exists()
    assert (child_dir / SESSION_MANIFEST_NAME).is_file()
    # s1 内容清单不含子会话文件;父目录 rollout 文件保持。
    assert (parent_dir / "rollout" / "main.jsonl").is_file()


async def test_migrate_journal_state_transitions_v2(
    workspace: MigrationWorkspace,
) -> None:
    """v2 状态机:completed journal 已走完 preparing→catalog_rebuilt→
    physical_migrated→completed(重入路径逐状态覆盖见上方恢复用例)。"""
    build_legacy_tree(workspace)
    await make_migrator(workspace).migrate()
    final = read_journal(workspace)
    assert final["state"] == "completed"
    assert final["schema_version"] == 2
    sessions, folders = journal_physical(workspace)
    assert all(
        record["state"] == "placed" and record["control_state"] == "initialized"
        for record in sessions.values()
        if record["classification"] == "migrate"
    )
    assert all(record["state"] == "deleted" for record in folders.values())


# ----------------------------------------------------------------------
# R19：operator 迁移入口（migrate_workspace_session_catalog + runner）
# ----------------------------------------------------------------------


def _build_entry_legacy_tree(workspace_root: Path) -> tuple[str, str, str]:
    """在生产布局（workspace_root/.boxteam/...）构造旧权威树。

    返回 (folder_a, session_s1, session_s2)：folder_a 内嵌套 s1，s1 的
    children/ 下嵌套 s2（嵌套子会话先搬出、物理嵌套瓦解的迁移语义同
    既有用例）。
    """
    sessions_root = workspace_root / ".boxteam" / "sessions"
    folder_a = make_session_id()
    session_s1 = make_session_id()
    session_s2 = make_session_id()
    _write_folder_dir(sessions_root, folder_a)
    _write_session_dir(
        sessions_root / folder_a,
        session_s1,
        title="入口会话1",
        parent_session_id=None,
    )
    _write_session_dir(
        sessions_root / folder_a / session_s1 / SESSION_CHILDREN_DIR_NAME,
        session_s2,
        title="入口会话2",
        parent_session_id=session_s1,
    )
    _write_index(
        workspace_root / ".boxteam" / "navigation" / "session-catalog-index.json",
        [
            _index_record(folder_a, "folder", "入口文件夹", None),
            _index_record(session_s1, "session", "入口会话1", folder_a),
            _index_record(session_s2, "session", "入口会话2", session_s1),
        ],
    )
    return folder_a, session_s1, session_s2


def _entry_date_bucket(workspace_root: Path, session_id: str) -> Path:
    """入口布局的日期桶目录（DEFAULT_CREATED_AT 的 UTC 日期桶）。"""
    return (
        workspace_root
        / ".boxteam"
        / "sessions"
        / f"{DEFAULT_CREATED_AT.astimezone(UTC).date():%Y/%m/%d}"
        / session_id
    )


async def test_migrate_workspace_entry_end_to_end_on_production_layout(
    tmp_path: Path,
) -> None:
    """入口函数在生产布局 tmp_path 工作区端到端可用（路径推导 + 全流程）。"""
    from app.core.session_catalog_migration import migrate_workspace_session_catalog

    workspace_root = tmp_path / "workspace"
    folder_a, session_s1, session_s2 = _build_entry_legacy_tree(workspace_root)

    result = await migrate_workspace_session_catalog(workspace_root=workspace_root)

    # 计数与 journal 落点（生产布局推导）。
    assert result.migrated_session_nodes == 2
    assert result.migrated_folder_nodes == 1
    assert result.quarantined_nodes == ()
    assert result.journal_path == (
        workspace_root
        / ".boxteam"
        / "maintenance"
        / "session-catalog-migration"
        / "journal.json"
    )
    assert result.journal_path.is_file()
    # SQLite catalog 就位且节点齐全（store 独立打开对账）。
    store = SessionCatalogStore(
        workspace_root / ".boxteam" / "navigation" / "session-catalog.sqlite",
        workspace_root / ".boxteam" / "sessions",
    )
    try:
        assert collect_store_node_ids(store) == {folder_a, session_s1, session_s2}
    finally:
        store.close()
    # 物理迁移：嵌套子会话瓦解到各自日期桶，folder 物理目录删除。
    assert _entry_date_bucket(workspace_root, session_s1).is_dir()
    assert _entry_date_bucket(workspace_root, session_s2).is_dir()
    assert not (
        workspace_root / ".boxteam" / "sessions" / folder_a
    ).exists()
    # session-control 初始化（main row + fence 由迁移机器建立）。
    assert (
        _entry_date_bucket(workspace_root, session_s1) / "session-control.sqlite"
    ).is_file()


async def test_migrate_workspace_entry_workspace_id_sources(
    tmp_path: Path,
) -> None:
    """workspace_id 未给定时读 identity API（落 identity 文件），给定时原样使用。"""
    from app.core.session_catalog_migration import migrate_workspace_session_catalog
    from app.core.workspace_identity import workspace_identity_path

    workspace_root = tmp_path / "workspace-identity-default"
    _build_entry_legacy_tree(workspace_root)

    await migrate_workspace_session_catalog(workspace_root=workspace_root)

    # 未给定：entry 经 identity API 创建并使用该 ID（journal 记录同值）。
    identity_path = workspace_identity_path(workspace_root)
    assert identity_path.is_file()
    identity_payload = json.loads(identity_path.read_text(encoding="utf-8"))
    journal = json.loads(
        (
            workspace_root
            / ".boxteam"
            / "maintenance"
            / "session-catalog-migration"
            / "journal.json"
        ).read_text(encoding="utf-8")
    )
    assert journal["workspace_id"] == identity_payload["workspace_id"]

    # 显式给定：原样使用（journal 记录给定值），不创建 identity 文件。
    explicit_root = tmp_path / "workspace-identity-explicit"
    explicit_id = str(uuid.uuid4())
    _build_entry_legacy_tree(explicit_root)

    await migrate_workspace_session_catalog(
        workspace_root=explicit_root,
        workspace_id=explicit_id,
    )

    explicit_journal = json.loads(
        (
            explicit_root
            / ".boxteam"
            / "maintenance"
            / "session-catalog-migration"
            / "journal.json"
        ).read_text(encoding="utf-8")
    )
    assert explicit_journal["workspace_id"] == explicit_id
    assert not workspace_identity_path(explicit_root).exists()


async def test_migrate_workspace_entry_rejects_conflicting_explicit_workspace_id(
    tmp_path: Path,
) -> None:
    """显式 workspace_id 与既有 identity 不一致时入口 fail closed（R19 审查 N3）。"""
    from app.core.session_catalog_migration import migrate_workspace_session_catalog
    from app.core.workspace_identity import load_or_create_workspace_id

    workspace_root = tmp_path / "workspace-identity-conflict"
    _build_entry_legacy_tree(workspace_root)
    # 先落 identity（与生产 resolver 同源），再以冲突 ID 调用入口。
    bound_id = load_or_create_workspace_id(workspace_root)
    conflicting_id = str(uuid.uuid4())
    assert conflicting_id != bound_id

    with pytest.raises(ValueError, match="与 identity 文件不一致"):
        await migrate_workspace_session_catalog(
            workspace_root=workspace_root,
            workspace_id=conflicting_id,
        )

    # fail closed：未产出 catalog、未写 journal（拒绝产出脱节产物）。
    assert not (
        workspace_root / ".boxteam" / "navigation" / "session-catalog.sqlite"
    ).exists()
    assert not (
        workspace_root
        / ".boxteam"
        / "maintenance"
        / "session-catalog-migration"
        / "journal.json"
    ).exists()


def _import_runner_module():
    """以 scripts 命名空间导入 runner 模块（pythonpath=["."] 已含仓库根）。"""
    import scripts.migrate_session_catalog as runner

    return runner


def test_runner_main_json_smoke(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """runner 冒烟：--json 输出计数/quarantine/journal 路径，退出码 0。"""
    from app.core.workspace_identity import load_or_create_workspace_id

    runner = _import_runner_module()
    workspace_root = tmp_path / "workspace"
    folder_a, session_s1, session_s2 = _build_entry_legacy_tree(workspace_root)
    expected_workspace_id = load_or_create_workspace_id(workspace_root)

    exit_code = runner.main(
        ["--workspace-root", str(workspace_root), "--json"]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["migrated_session_nodes"] == 2
    assert payload["migrated_folder_nodes"] == 1
    assert payload["quarantined_nodes"] == []
    assert payload["journal_path"] == str(
        workspace_root
        / ".boxteam"
        / "maintenance"
        / "session-catalog-migration"
        / "journal.json"
    )
    # runner 默认走 identity API（与生产 resolver 同源）。
    assert load_or_create_workspace_id(workspace_root) == expected_workspace_id
    # 迁移真实完成（catalog 就位）。
    assert (
        workspace_root / ".boxteam" / "navigation" / "session-catalog.sqlite"
    ).is_file()
    # runner 出口产物真实包含迁移出的三个节点（R19 审查 N2：原为恒真断言，
    # 改为对 catalog 的真实读取校验；节点级逐字段对账仍由入口端到端用例覆盖）。
    store = SessionCatalogStore(
        workspace_root / ".boxteam" / "navigation" / "session-catalog.sqlite",
        workspace_root / ".boxteam" / "sessions",
    )
    try:
        assert store.get_node(folder_a).kind == "folder"
        assert store.get_node(session_s1).kind == "session"
        assert store.get_node(session_s2).kind == "session"
        # 迁移后父子关系：folder_a > s1 > s2（物理嵌套瓦解、逻辑父链保留）。
        folder_children, _, _ = store.list_children(folder_a, limit=10)
        assert [node.node_id for node in folder_children] == [session_s1]
        parent_children, _, _ = store.list_children(session_s1, limit=10)
        assert [node.node_id for node in parent_children] == [session_s2]
    finally:
        store.close()


def test_runner_main_human_output_and_quarantine_listing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """runner 人类可读输出含计数、quarantine 清单与 journal 路径。"""
    runner = _import_runner_module()
    workspace_root = tmp_path / "workspace"
    _build_entry_legacy_tree(workspace_root)
    # 追加一个非法 ID 会话（index 记录）触发 quarantine（illegal_id）。
    _write_session_dir(
        workspace_root / ".boxteam" / "sessions",
        "ses_illegal_entry",
        title="非法会话",
        parent_session_id=None,
    )
    index_path = (
        workspace_root / ".boxteam" / "navigation" / "session-catalog-index.json"
    )
    index_payload = json.loads(index_path.read_text(encoding="utf-8"))
    index_payload["nodes"].append(
        _index_record("ses_illegal_entry", "session", "非法会话", None)
    )
    _write_json(index_path, index_payload)

    exit_code = runner.main(["--workspace-root", str(workspace_root)])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "迁移 session 节点: 2" in out
    assert "迁移 folder 节点: 1" in out
    assert "quarantine 节点: 1" in out
    assert "ses_illegal_entry" in out
    assert "illegal_id" in out
    assert "journal" in out


def test_runner_main_failure_exits_nonzero_with_clear_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """runner 失败路径：无旧权威 index → 非零退出 + stderr 明确错误（不静默）。"""
    runner = _import_runner_module()
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()

    exit_code = runner.main(
        ["--workspace-root", str(workspace_root), "--json"]
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "会话目录迁移失败" in captured.err
    assert "旧权威 index 缺失" in captured.err
    assert captured.out == ""


def test_runner_main_rejects_workspace_id_mismatching_identity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """runner 快速失败：显式 workspace_id 与 identity 文件不一致 → SystemExit。"""
    runner = _import_runner_module()
    workspace_root = tmp_path / "workspace"
    _build_entry_legacy_tree(workspace_root)
    from app.core.workspace_identity import load_or_create_workspace_id

    identity_id = load_or_create_workspace_id(workspace_root)
    other_id = str(uuid.uuid4())
    assert other_id != identity_id

    with pytest.raises(SystemExit, match="不一致"):
        runner.main(
            [
                "--workspace-root",
                str(workspace_root),
                "--workspace-id",
                other_id,
            ]
        )
    # 拒绝发生在迁移开始前：无 journal、旧 index 原样。
    assert not (
        workspace_root
        / ".boxteam"
        / "maintenance"
        / "session-catalog-migration"
        / "journal.json"
    ).exists()
    assert (
        workspace_root
        / ".boxteam"
        / "navigation"
        / "session-catalog-index.json"
    ).is_file()
