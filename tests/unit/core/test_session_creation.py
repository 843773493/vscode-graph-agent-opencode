"""SessionCreationRecord 新模型创建流测试（OpenSpec 8.1-A，R13）。

覆盖：完整流、幂等（同 key 重入）、preimage 冲突、恢复四崩溃点、
CAS 失败定点回收、parent 校验、剥离 manifest 完整性、并发同 key 收敛。
只使用 tmp_path，不触碰真实工作区。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.session_catalog_store import (
    SessionCatalogStore,
    SessionCreationRecord,
)
from app.core.session_control_store import SessionControlStore
from app.core.session_creation import (
    SessionCreationResult,
    SessionCreationService,
    build_session_creation_manifest,
    compute_session_creation_preimage_hash,
    serialize_session_creation_manifest,
)
from app.core.session_lifecycle_gate import NavigationTopologyGate

WORKSPACE_ID = "ws-create"

# 剥离 manifest 的完整字段闭集（调用方六字段 + 服务派生四字段）。
_STRIPPED_MANIFEST_KEYS = {
    "session_id",
    "workspace_id",
    "kind",
    "delegation",
    "generation_origin",
    "created_at",
    "updated_at",
    "current_agent_id",
    "current_provider_id",
    "context_source_session_id",
}


def make_metadata(**overrides: object) -> dict[str, object]:
    """构造合法的调用方 session_metadata（六字段闭集）。"""
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


async def do_create(
    service: SessionCreationService,
    *,
    key: str = "key-1",
    title: str = "测试会话",
    parent_node_id: str | None = None,
    metadata: dict[str, object] | None = None,
) -> SessionCreationResult:
    return await service.create(
        idempotency_key=key,
        title=title,
        parent_node_id=parent_node_id,
        session_metadata=metadata if metadata is not None else make_metadata(),
    )


def create_manual_record(
    store: SessionCatalogStore,
    *,
    key: str = "key-1",
    title: str = "测试会话",
    parent_node_id: str | None = None,
    metadata: dict[str, object] | None = None,
) -> SessionCreationRecord:
    """测试辅助：绕过服务直接在 store 建 preparing record（恢复测试用）。

    preimage_hash 与服务同源（compute_session_creation_preimage_hash），
    保证后续 create() 重入能幂等命中该 record；created_at 固定为
    2026-06-01（重入时服务传入的 now() 不会覆盖 record 冻结值）。
    """
    effective_metadata = metadata if metadata is not None else make_metadata()
    preimage_hash = compute_session_creation_preimage_hash(
        workspace_id=WORKSPACE_ID,
        parent_node_id=parent_node_id,
        title=title,
        session_metadata=effective_metadata,
    )
    return store.create_or_get_creation_record(
        idempotency_key=key,
        workspace_id=WORKSPACE_ID,
        parent_node_id=parent_node_id,
        display_name=title,
        created_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        preimage_hash=preimage_hash,
    )


def build_session_directory(
    directory: Path,
    record: SessionCreationRecord,
    metadata: dict[str, object],
) -> None:
    """按服务相同格式手工构造 session 目录（staging/日期桶恢复测试用）。"""
    manifest = build_session_creation_manifest(record, metadata)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "session.json").write_bytes(
        serialize_session_creation_manifest(manifest)
    )
    control = SessionControlStore(directory / "session-control.sqlite")
    try:
        control.initialize_main_thread(
            record.main_thread_id,
            datetime.fromisoformat(record.created_at),
        )
        control.initialize_fence("active", 1)
        control.verify_matches_catalog_main_thread(record.main_thread_id)
    finally:
        control.close()


def date_bucket_dir(sessions_root: Path, locator: str) -> Path:
    return sessions_root / locator[len("sessions/"):]


def count_rows(store: SessionCatalogStore, table: str) -> int:
    return int(
        store.connection.execute(
            f"SELECT COUNT(*) FROM {table}"
        ).fetchone()[0]
    )


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
def service(store: SessionCatalogStore, sessions_root: Path):
    return SessionCreationService(
        store=store,
        sessions_root=sessions_root,
        workspace_id=WORKSPACE_ID,
        gate=NavigationTopologyGate(sessions_root),
    )


# ----------------------------------------------------------------------
# 完整流
# ----------------------------------------------------------------------


async def test_create_full_flow_publishes_session(
    service: SessionCreationService,
    store: SessionCatalogStore,
) -> None:
    result = await do_create(service)
    assert result.record_state == "published"
    assert result.session_id == result.node.node_id
    assert result.main_thread_id == result.node.main_thread_id
    assert result.storage_relative_locator == (
        result.node.storage_relative_locator
    )
    # nodes 行可见且与 result 一致
    node = store.get_node(result.session_id)
    assert node == result.node
    assert node.kind == "session"
    assert node.state == "active"
    assert node.display_name == "测试会话"
    assert node.parent_node_id is None
    assert node.workspace_id == WORKSPACE_ID
    # record published
    record = store.get_creation_record("key-1")
    assert record.state == "published"
    assert record.abort_reason is None
    assert record.session_id == result.session_id


async def test_create_writes_stripped_session_json(
    service: SessionCreationService, sessions_root: Path
) -> None:
    generation_origin = {"generator_id": "gen-1", "run_id": "run-1"}
    metadata = make_metadata(
        kind="normal",
        generation_origin=generation_origin,
        current_provider_id="provider-x",
    )
    result = await do_create(service, metadata=metadata)
    manifest_path = (
        date_bucket_dir(sessions_root, result.storage_relative_locator)
        / "session.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    # 字段集恰为剥离版十字段；不含可变导航字段
    assert set(manifest) == _STRIPPED_MANIFEST_KEYS
    assert "title" not in manifest
    assert "title_source" not in manifest
    assert "parent_session_id" not in manifest
    assert manifest["session_id"] == result.session_id
    assert manifest["workspace_id"] == WORKSPACE_ID
    assert manifest["created_at"] == result.node.created_at
    assert manifest["updated_at"] == manifest["created_at"]
    assert manifest["kind"] == "normal"
    assert manifest["generation_origin"] == generation_origin
    assert manifest["current_agent_id"] == "default"
    assert manifest["current_provider_id"] == "provider-x"
    assert manifest["context_source_session_id"] is None
    assert manifest["delegation"] is None


async def test_create_initializes_session_control_database(
    service: SessionCreationService, sessions_root: Path
) -> None:
    result = await do_create(service)
    session_dir = date_bucket_dir(sessions_root, result.storage_relative_locator)
    control = SessionControlStore(session_dir / "session-control.sqlite")
    try:
        main_row = control.get_main_thread()
        assert str(main_row["thread_id"]) == result.main_thread_id
        assert str(main_row["kind"]) == "main"
        assert control.get_fence() == ("active", 1)
        control.verify_matches_catalog_main_thread(result.main_thread_id)
    finally:
        control.close()


async def test_create_places_directory_in_date_bucket_and_clears_staging(
    service: SessionCreationService, sessions_root: Path
) -> None:
    result = await do_create(service)
    session_dir = date_bucket_dir(sessions_root, result.storage_relative_locator)
    # 日期桶位置正确（locator 冻结于 record）
    assert session_dir.is_dir()
    assert sorted(p.name for p in session_dir.iterdir()) == [
        "session-control.sqlite",
        "session.json",
    ]
    # staging 区已清空（目录被 rename 走）
    assert not (sessions_root / ".staging" / "key-1").exists()


async def test_create_under_folder_parent(
    service: SessionCreationService, store: SessionCatalogStore
) -> None:
    folder = store.create_folder(
        make_node_id(), WORKSPACE_ID, None, "文件夹"
    )
    result = await do_create(service, parent_node_id=folder.node_id)
    assert result.node.parent_node_id == folder.node_id
    assert result.node.kind == "session"


# ----------------------------------------------------------------------
# 幂等与冲突
# ----------------------------------------------------------------------


async def test_create_idempotent_same_result(
    service: SessionCreationService, store: SessionCatalogStore
) -> None:
    first = await do_create(service)
    second = await do_create(service)
    assert first.session_id == second.session_id
    assert first.main_thread_id == second.main_thread_id
    assert first.storage_relative_locator == second.storage_relative_locator
    assert second.node == first.node
    assert second.record_state == "published"
    assert count_rows(store, "nodes") == 1
    assert count_rows(store, "session_creation_records") == 1


async def test_create_idempotent_three_times(
    service: SessionCreationService,
    store: SessionCatalogStore,
    sessions_root: Path,
) -> None:
    first = await do_create(service)
    second = await do_create(service)
    third = await do_create(service)
    assert second.session_id == first.session_id
    assert third.session_id == first.session_id
    # 日期桶 session.json 字节稳定（确定性序列化，未被重建改写）
    record = store.get_creation_record("key-1")
    session_dir = date_bucket_dir(sessions_root, record.storage_relative_locator)
    manifest_bytes = (session_dir / "session.json").read_bytes()
    assert manifest_bytes == serialize_session_creation_manifest(
        build_session_creation_manifest(record, make_metadata())
    )
    assert count_rows(store, "nodes") == 1


async def test_create_conflicting_preimage_on_published_rejected(
    service: SessionCreationService, store: SessionCatalogStore
) -> None:
    await do_create(service, title="原标题")
    with pytest.raises(RuntimeError, match="冲突"):
        await do_create(service, title="新标题")
    # 原 published record 与 node 不受影响
    assert store.get_creation_record("key-1").state == "published"
    assert count_rows(store, "nodes") == 1


async def test_create_conflicting_preimage_on_preparing_rejected(
    service: SessionCreationService, store: SessionCatalogStore
) -> None:
    create_manual_record(store, key="key-1", title="原标题")
    with pytest.raises(RuntimeError, match="冲突"):
        await do_create(service, title="新标题")
    assert store.get_creation_record("key-1").state == "preparing"


# ----------------------------------------------------------------------
# 入参校验（状态变更前 fail fast）
# ----------------------------------------------------------------------


async def test_create_missing_metadata_key_rejected_before_state_change(
    service: SessionCreationService, store: SessionCatalogStore
) -> None:
    metadata = make_metadata()
    del metadata["kind"]
    with pytest.raises(ValueError, match="字段集"):
        await do_create(service, metadata=metadata)
    with pytest.raises(KeyError):
        store.get_creation_record("key-1")


async def test_create_extra_metadata_key_rejected(
    service: SessionCreationService, store: SessionCatalogStore
) -> None:
    # title 属于可变导航字段，禁止经 metadata 进入剥离 manifest
    metadata = make_metadata(title="混入的标题")
    with pytest.raises(ValueError, match="字段集"):
        await do_create(service, metadata=metadata)
    with pytest.raises(KeyError):
        store.get_creation_record("key-1")


async def test_create_non_dict_metadata_rejected(
    service: SessionCreationService,
) -> None:
    with pytest.raises(TypeError, match="session_metadata"):
        await service.create(
            idempotency_key="key-1",
            title="标题",
            parent_node_id=None,
            session_metadata=["not", "a", "dict"],  # type: ignore[arg-type]
        )


async def test_create_non_serializable_metadata_rejected(
    service: SessionCreationService, store: SessionCatalogStore
) -> None:
    metadata = make_metadata(generation_origin=datetime.now(UTC))
    with pytest.raises(TypeError):
        await do_create(service, metadata=metadata)
    with pytest.raises(KeyError):
        store.get_creation_record("key-1")


async def test_create_empty_title_rejected(
    service: SessionCreationService, store: SessionCatalogStore
) -> None:
    with pytest.raises(ValueError, match="title"):
        await do_create(service, title="")
    with pytest.raises(KeyError):
        store.get_creation_record("key-1")


@pytest.mark.parametrize("key", ["", "a/b", "a\\b", "..", ".", "嵌\x00入"])
async def test_create_unsafe_idempotency_key_rejected(
    service: SessionCreationService, store: SessionCatalogStore, key: str
) -> None:
    with pytest.raises(ValueError, match="idempotency_key"):
        await do_create(service, key=key)
    if key:
        with pytest.raises(KeyError):
            store.get_creation_record(key)


@pytest.mark.parametrize("key_len", [256, 300, 4096])
async def test_create_over_long_idempotency_key_rejected_before_record(
    service: SessionCreationService, store: SessionCatalogStore, key_len: int
) -> None:
    """幂等键超出单段路径预算时，必须在冻结 record 之前就拒绝。

    幂等键是 ``.staging/<key>/`` 目录名。键长超过文件系统单段上限
    （255 bytes）时，旧实现先在 catalog 冻结一个 preparing record，随后
    ``mkdir`` 抛裸 ``OSError``；record 已按输入 preimage 冻结，同 key
    重入必然再次失败，形成永久 fail closed（拒绝服务）。本断言固化
    「路径预算在落盘前校验」的修复：既不落 record，也不留 staging。
    """
    key = "k" * key_len
    with pytest.raises(ValueError, match="超出预算"):
        await do_create(service, key=key)
    with pytest.raises(KeyError):
        store.get_creation_record(key)
    # 超长键的绝对路径本身已无法 stat，改查 `.staging` 目录项集合。
    staging_root = store.sessions_root / ".staging"
    assert not staging_root.exists() or key not in {
        entry.name for entry in staging_root.iterdir()
    }


async def test_create_idempotency_key_at_component_budget_accepted(
    service: SessionCreationService,
) -> None:
    """255 bytes 是单段路径上限本身，边界内必须照常创建成功。"""
    result = await do_create(service, key="k" * 255)
    assert result.record_state == "published"


async def test_create_missing_parent_rejected(
    service: SessionCreationService, store: SessionCatalogStore
) -> None:
    with pytest.raises(KeyError):
        await do_create(service, parent_node_id=make_node_id())
    with pytest.raises(KeyError):
        store.get_creation_record("key-1")


async def test_create_deleting_parent_rejected(
    service: SessionCreationService, store: SessionCatalogStore
) -> None:
    parent = store.create_folder(
        make_node_id(), WORKSPACE_ID, None, "将删除"
    )
    store.set_node_state(parent.node_id, "deleting")
    with pytest.raises(RuntimeError, match="正在删除"):
        await do_create(service, parent_node_id=parent.node_id)
    with pytest.raises(KeyError):
        store.get_creation_record("key-1")


# ----------------------------------------------------------------------
# 恢复：四个崩溃点 + fail closed 变体
# ----------------------------------------------------------------------


async def test_recovery_record_preparing_without_staging(
    service: SessionCreationService,
    store: SessionCatalogStore,
    sessions_root: Path,
) -> None:
    """崩溃点1：record 建立后、staging 前崩溃 → 重入完整重准备并发布。"""
    record = create_manual_record(store)
    result = await do_create(service)
    assert result.record_state == "published"
    assert result.session_id == record.session_id
    assert result.main_thread_id == record.main_thread_id
    # created_at 以冻结 record 为准（重入传入的 now() 不覆盖冻结值）
    assert result.node.created_at == "2026-06-01T12:00:00+00:00"
    assert date_bucket_dir(
        sessions_root, record.storage_relative_locator
    ).is_dir()
    assert not (sessions_root / ".staging" / "key-1").exists()


async def test_recovery_record_preparing_with_staging(
    service: SessionCreationService,
    store: SessionCatalogStore,
    sessions_root: Path,
) -> None:
    """崩溃点2：staging 后、rename 前崩溃 → 重入校验 staging 后继续。"""
    record = create_manual_record(store)
    build_session_directory(
        sessions_root / ".staging" / "key-1", record, make_metadata()
    )
    result = await do_create(service)
    assert result.record_state == "published"
    assert result.session_id == record.session_id
    assert date_bucket_dir(
        sessions_root, record.storage_relative_locator
    ).is_dir()
    assert not (sessions_root / ".staging" / "key-1").exists()


async def test_recovery_target_already_renamed(
    service: SessionCreationService,
    store: SessionCatalogStore,
    sessions_root: Path,
) -> None:
    """崩溃点3：rename 后、publish 前崩溃 → 重入发现目标一致直接 publish。"""
    record = create_manual_record(store)
    target = date_bucket_dir(sessions_root, record.storage_relative_locator)
    build_session_directory(target, record, make_metadata())
    result = await do_create(service)
    assert result.record_state == "published"
    assert result.session_id == record.session_id
    assert target.is_dir()
    assert store.get_node(record.session_id) == result.node
    # 全程未建 staging
    assert not (sessions_root / ".staging" / "key-1").exists()


async def test_recovery_target_inconsistent_fails_closed(
    service: SessionCreationService,
    store: SessionCatalogStore,
    sessions_root: Path,
) -> None:
    """恢复变体：目标已存在但内容被外部改动 → fail closed，record 不动。"""
    record = create_manual_record(store)
    target = date_bucket_dir(sessions_root, record.storage_relative_locator)
    build_session_directory(target, record, make_metadata())
    (target / "session.json").write_text(
        '{"tampered": true}\n', encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="sha256"):
        await do_create(service)
    assert store.get_creation_record("key-1").state == "preparing"
    with pytest.raises(KeyError):
        store.get_node(record.session_id)


async def test_recovery_target_and_staging_both_exist_fails_closed(
    service: SessionCreationService,
    store: SessionCatalogStore,
    sessions_root: Path,
) -> None:
    """恢复变体：目标与 staging 同时存在 → fail closed（无法归一）。"""
    record = create_manual_record(store)
    build_session_directory(
        date_bucket_dir(sessions_root, record.storage_relative_locator),
        record,
        make_metadata(),
    )
    build_session_directory(
        sessions_root / ".staging" / "key-1", record, make_metadata()
    )
    with pytest.raises(RuntimeError, match="同时存在"):
        await do_create(service)
    assert store.get_creation_record("key-1").state == "preparing"


async def test_recovery_record_already_published(
    service: SessionCreationService, store: SessionCatalogStore
) -> None:
    """崩溃点4：publish 后崩溃 → 重入 record published 幂等返回。"""
    record = create_manual_record(store)
    node = store.publish_creation_record("key-1")
    result = await do_create(service)
    assert result.record_state == "published"
    assert result.session_id == record.session_id
    assert result.node == node


async def test_recovery_published_record_missing_node_fails_closed(
    service: SessionCreationService, store: SessionCatalogStore
) -> None:
    """恢复变体：record published 但 nodes 行缺失（外部改动）→ fail closed。"""
    create_manual_record(store)
    store.publish_creation_record("key-1")
    record = store.get_creation_record("key-1")
    connection = sqlite3.connect(store.database_path)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            "DELETE FROM nodes WHERE node_id = ?", (record.session_id,)
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(RuntimeError, match="nodes 行缺失"):
        await do_create(service)


async def test_recovery_aborted_record_rejected(
    service: SessionCreationService, store: SessionCatalogStore
) -> None:
    """record aborted → 同 key 重入拒绝（含 reason），须换新 key。"""
    create_manual_record(store)
    store.abort_creation_record("key-1", "外部中止原因")
    with pytest.raises(RuntimeError, match="外部中止原因"):
        await do_create(service)


# ----------------------------------------------------------------------
# CAS 失败：定点回收 + abort
# ----------------------------------------------------------------------


async def test_cas_failure_parent_drift_quarantines_and_aborts(
    service: SessionCreationService,
    store: SessionCatalogStore,
    sessions_root: Path,
) -> None:
    """CAS 失败：父节点 revision 漂移 → 日期桶隔离到 orphaned + record
    aborted + nodes 无行。"""
    parent = store.create_folder(
        make_node_id(), WORKSPACE_ID, None, "父"
    )
    record = create_manual_record(store, parent_node_id=parent.node_id)
    build_session_directory(
        sessions_root / ".staging" / "key-1", record, make_metadata()
    )
    # 手工制造父节点 revision 漂移
    store.rename_node(parent.node_id, "漂移")
    target = date_bucket_dir(sessions_root, record.storage_relative_locator)
    with pytest.raises(RuntimeError, match="CAS 失败"):
        await do_create(service, parent_node_id=parent.node_id)
    # 日期桶目录被定点回收（隔离到 .boxteam/orphaned/session-creation/<key>/）
    assert not target.exists()
    quarantined = (
        sessions_root.parent / "orphaned" / "session-creation" / "key-1"
    )
    assert quarantined.is_dir()
    assert (quarantined / "session.json").is_file()
    assert (quarantined / "session-control.sqlite").is_file()
    # record aborted（含原因）且 nodes 无行
    aborted = store.get_creation_record("key-1")
    assert aborted.state == "aborted"
    assert aborted.abort_reason is not None
    assert "漂移" in aborted.abort_reason
    with pytest.raises(KeyError):
        store.get_node(record.session_id)
    # staging 区已清空
    assert not (sessions_root / ".staging" / "key-1").exists()


async def test_cas_failure_locator_occupied_quarantines_and_aborts(
    service: SessionCreationService,
    store: SessionCatalogStore,
    sessions_root: Path,
) -> None:
    """CAS 失败：目标 locator 被其它 node 占用 → 同样定点回收 + abort。"""
    record = create_manual_record(store)
    build_session_directory(
        sessions_root / ".staging" / "key-1", record, make_metadata()
    )
    # 直连 SQL 注入同 locator 的既有 node（record 尚未发布，占用合法存在）
    store.connection.execute(
        "INSERT INTO nodes (node_id, kind, parent_node_id, display_name, "
        "state, revision, workspace_id, created_at, "
        "storage_relative_locator, main_thread_id) "
        "VALUES (?, 'session', NULL, '占位', 'active', 1, ?, ?, ?, ?)",
        (
            make_node_id(),
            WORKSPACE_ID,
            record.created_at,
            record.storage_relative_locator,
            f"thr_{uuid.uuid4().hex}",
        ),
    )
    with pytest.raises(RuntimeError, match="CAS 失败"):
        await do_create(service)
    assert not date_bucket_dir(
        sessions_root, record.storage_relative_locator
    ).exists()
    assert (
        sessions_root.parent / "orphaned" / "session-creation" / "key-1"
    ).is_dir()
    assert store.get_creation_record("key-1").state == "aborted"


# ----------------------------------------------------------------------
# 并发
# ----------------------------------------------------------------------


async def test_concurrent_same_key_creates_converge(
    service: SessionCreationService, store: SessionCatalogStore
) -> None:
    first, second = await asyncio.gather(
        do_create(service),
        do_create(service),
    )
    assert first.session_id == second.session_id
    assert first.node == second.node
    assert count_rows(store, "nodes") == 1
    assert count_rows(store, "session_creation_records") == 1


async def test_concurrent_different_keys_both_publish(
    service: SessionCreationService, store: SessionCatalogStore
) -> None:
    first, second = await asyncio.gather(
        do_create(service, key="key-a", title="会话A"),
        do_create(service, key="key-b", title="会话B"),
    )
    assert first.session_id != second.session_id
    assert first.record_state == "published"
    assert second.record_state == "published"
    assert count_rows(store, "nodes") == 2


# ----------------------------------------------------------------------
# 服务构造与 preimage 助手
# ----------------------------------------------------------------------


def test_service_rejects_sessions_root_mismatch(
    store: SessionCatalogStore, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="不一致"):
        SessionCreationService(
            store=store,
            sessions_root=tmp_path / "other" / "sessions",
            workspace_id=WORKSPACE_ID,
        )


async def test_service_default_gate_creates_own_gate(
    store: SessionCatalogStore, sessions_root: Path
) -> None:
    service = SessionCreationService(
        store=store,
        sessions_root=sessions_root,
        workspace_id=WORKSPACE_ID,
    )
    result = await do_create(service)
    assert result.record_state == "published"


def test_service_rejects_empty_workspace(
    store: SessionCatalogStore, sessions_root: Path
) -> None:
    with pytest.raises(ValueError, match="workspace_id"):
        SessionCreationService(
            store=store,
            sessions_root=sessions_root,
            workspace_id="",
        )


def test_preimage_hash_deterministic_and_sensitive() -> None:
    kwargs = {
        "workspace_id": WORKSPACE_ID,
        "parent_node_id": None,
        "title": "标题",
        "session_metadata": make_metadata(),
    }
    assert compute_session_creation_preimage_hash(**kwargs) == (
        compute_session_creation_preimage_hash(**kwargs)
    )
    # 键插入顺序不同不影响 preimage（canonical JSON sort_keys）
    reordered = {
        "current_provider_id": "default_provider",
        "context_source_session_id": None,
        "kind": "normal",
        "delegation": None,
        "generation_origin": None,
        "current_agent_id": "default",
    }
    assert compute_session_creation_preimage_hash(
        **{**kwargs, "session_metadata": reordered}
    ) == compute_session_creation_preimage_hash(**kwargs)
    # title / parent / metadata 任一变化都改变 preimage
    assert compute_session_creation_preimage_hash(
        **{**kwargs, "title": "其他标题"}
    ) != compute_session_creation_preimage_hash(**kwargs)
    assert compute_session_creation_preimage_hash(
        **{**kwargs, "parent_node_id": make_node_id()}
    ) != compute_session_creation_preimage_hash(**kwargs)
    assert compute_session_creation_preimage_hash(
        **{**kwargs, "session_metadata": make_metadata(kind="context_fork")}
    ) != compute_session_creation_preimage_hash(**kwargs)
