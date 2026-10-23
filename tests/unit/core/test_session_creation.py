"""SessionCreationRecord 新模型创建流测试（OpenSpec 8.1-A，R13）。

覆盖：完整流、幂等（同 key 重入）、preimage 冲突、恢复四崩溃点、
CAS 失败定点回收、parent 校验、剥离 manifest 完整性、并发同 key 收敛。
只使用 tmp_path，不触碰真实工作区。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core import session_creation, sqlite_state
from app.core.identifier import create_uuid_hex
from app.core.session_catalog_store import (
    SessionCatalogStore,
    SessionCreationRecord,
)
from app.core.session_catalog_store.creation_envelope import (
    compute_session_creation_preimage_hash,
)
from app.core.session_catalog_store.queries import _register_session_list_functions
from app.core.session_control_store import SessionControlStore
from app.core.session_creation import (
    ForkCompletionProof,
    SessionCreationResult,
    SessionCreationService,
    build_session_creation_manifest,
    serialize_session_creation_manifest,
)
from app.core.session_lifecycle_gate import NavigationTopologyGate
from app.core.sqlite_state import (
    sqlite_vfs_max_pathname,
    validate_sqlite_path_budget,
)
from tests.support.canonical_id_at import (
    session_id_for_name_at,
    thread_id_for_name_at,
)
from tests.support.graph_binding import TEST_THREAD_GRAPH_BINDING

WORKSPACE_ID = "ws-create"
_SESSION_ID_PATH_PLACEHOLDER = "ses_00000000000070008000000000000000"

# 身份 envelope 仅保留不可变的三键。
_STRIPPED_MANIFEST_KEYS = {"session_id", "workspace_id", "created_at"}


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
    """生成满足 UUIDv7 位 profile 的节点 ID（folder 与 session 同形）。"""
    return f"ses_{create_uuid_hex()}"


async def do_create(
    service: SessionCreationService,
    *,
    key: str = "key-1",
    title: str = "测试会话",
    parent_node_id: str | None = None,
    metadata: dict[str, object] | None = None,
) -> SessionCreationResult:
    record = await service.prepare(
        idempotency_key=key,
        title=title,
        parent_node_id=parent_node_id,
        graph_binding=TEST_THREAD_GRAPH_BINDING,
        session_metadata=metadata if metadata is not None else make_metadata(),
    )
    return await service.publish_prepared(record)


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
        title_source="user",
        purpose="normal",
        graph_binding=TEST_THREAD_GRAPH_BINDING,
        session_metadata=effective_metadata,
    )
    created_at = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    return store.create_or_get_creation_record(
        idempotency_key=key,
        workspace_id=WORKSPACE_ID,
        parent_node_id=parent_node_id,
        display_name=title,
        title_source="user",
        purpose="normal",
        graph_binding=TEST_THREAD_GRAPH_BINDING,
        session_metadata_json=json.dumps(
            effective_metadata,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        preimage_hash=preimage_hash,
        created_at=created_at,
        session_id=session_id_for_name_at(key, created_at),
        main_thread_id=thread_id_for_name_at(key, created_at),
    )


def build_session_directory(
    directory: Path,
    record: SessionCreationRecord,
    metadata: dict[str, object],
) -> None:
    """按服务相同格式手工构造 session 目录（staging/日期桶恢复测试用）。"""
    manifest = build_session_creation_manifest(record, metadata)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "threads" / record.main_thread_id).mkdir(parents=True)
    (directory / "session.json").write_bytes(
        serialize_session_creation_manifest(manifest)
    )
    control = SessionControlStore(directory / "session-control.sqlite")
    try:
        control.initialize_main_thread(
            record.main_thread_id,
            datetime.fromisoformat(record.created_at),
            TEST_THREAD_GRAPH_BINDING,
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


def sessions_root_beyond_vfs_limit(tmp_path: Path) -> Path:
    """构造控制库 sidecar 超过当前 VFS 上限的 sessions 根。"""
    sessions_root = tmp_path.resolve() / "sessions"
    control_path = (
        sessions_root
        / ".staging"
        / _SESSION_ID_PATH_PLACEHOLDER
        / "session-control.sqlite"
    )
    maximum = sqlite_vfs_max_pathname()
    while len(f"{control_path}-journal".encode()) <= maximum:
        sessions_root = (
            sessions_root.parent / ("x" * 200) / sessions_root.name
        )
        control_path = (
            sessions_root
            / ".staging"
            / _SESSION_ID_PATH_PLACEHOLDER
            / "session-control.sqlite"
        )
    return sessions_root


def sessions_root_rollout_beyond_vfs_limit(tmp_path: Path) -> Path:
    """按真实根与 VFS 预算有限填充，让 rollout index sidecar 确实超限。"""
    sessions_root = tmp_path.resolve() / "sessions"
    rollout_sidecar = (
        sessions_root / "1970/01/01" / _SESSION_ID_PATH_PLACEHOLDER
        / "threads" / session_creation._THREAD_ID_PATH_PLACEHOLDER / "rollout/index.sqlite-journal"
    )
    padding_bytes = max(
        0, sqlite_vfs_max_pathname() + 1 - len(str(rollout_sidecar).encode("utf-8"))
    )
    for offset in range(0, padding_bytes, 200):
        sessions_root = (
            sessions_root.parent / ("x" * min(200, padding_bytes - offset))
            / sessions_root.name
        )
    return sessions_root


def database_path_beyond_vfs_limit(tmp_path: Path, database_name: str) -> Path:
    """构造组件合法、但主库绝对路径超过当前 VFS 上限的路径。"""
    path = tmp_path.resolve() / database_name
    maximum = sqlite_vfs_max_pathname()
    while len(str(path).encode("utf-8")) <= maximum:
        path = path.parent / ("x" * 200) / path.name
    return path


def test_sqlite_path_budget_checks_rollback_journal_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path.resolve() / "catalog.sqlite"
    main_path_bytes = len(str(path).encode("utf-8"))
    monkeypatch.setattr(
        sqlite_state,
        "sqlite_vfs_max_pathname",
        lambda: main_path_bytes + len("-journal") - 1,
    )
    with pytest.raises(ValueError, match="-journal"):
        validate_sqlite_path_budget(path)


def test_catalog_constructor_rejects_long_path_before_creating_parent(
    tmp_path: Path,
) -> None:
    database_path = database_path_beyond_vfs_limit(
        tmp_path, "session-catalog.sqlite"
    )
    with pytest.raises(ValueError, match="SQLite 绝对路径"):
        SessionCatalogStore(database_path, tmp_path / "sessions")
    assert not database_path.parent.exists()


def test_control_constructor_rejects_long_path_before_creating_parent(
    tmp_path: Path,
) -> None:
    database_path = database_path_beyond_vfs_limit(
        tmp_path, "session-control.sqlite"
    )
    with pytest.raises(ValueError, match="SQLite 绝对路径"):
        SessionControlStore(database_path)
    assert not database_path.parent.exists()


async def test_deep_sessions_root_rejected_before_record_or_staging(
    tmp_path: Path,
) -> None:
    sessions_root = sessions_root_beyond_vfs_limit(tmp_path)
    store = SessionCatalogStore(
        tmp_path / "catalog" / "session-catalog.sqlite", sessions_root
    )
    service = SessionCreationService(
        store=store,
        sessions_root=sessions_root,
        workspace_id=WORKSPACE_ID,
        gate=NavigationTopologyGate(sessions_root),
    )
    try:
        with pytest.raises(ValueError, match="stage=path_budget") as failure:
            await do_create(service)
        assert str(sessions_root) not in str(failure.value)
        assert isinstance(failure.value.__cause__, ValueError)
        assert "SQLite 绝对路径" in str(failure.value.__cause__)
        assert count_rows(store, "session_creation_records") == 0
        assert not sessions_root.exists()
        assert not (
            sessions_root.parent / "orphaned" / "session-creation"
        ).exists()
    finally:
        store.close()


async def test_rollout_index_path_budget_checked_before_record_or_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_root = sessions_root_rollout_beyond_vfs_limit(tmp_path)
    rollout_database = (
        sessions_root / "1970/01/01" / _SESSION_ID_PATH_PLACEHOLDER
        / "threads" / session_creation._THREAD_ID_PATH_PLACEHOLDER / "rollout/index.sqlite"
    )
    validated_rollouts: list[Path] = []

    def validate_rollout_budget(database_path: Path) -> None:
        # 隔离更长的 quarantine control 路径；control 超限由相邻测试覆盖。
        if database_path.name == "index.sqlite":
            validated_rollouts.append(database_path)
            validate_sqlite_path_budget(database_path)

    monkeypatch.setattr(
        session_creation, "validate_sqlite_path_budget", validate_rollout_budget
    )
    store = SessionCatalogStore(
        tmp_path / "catalog" / "session-catalog.sqlite", sessions_root
    )
    service = SessionCreationService(
        store=store,
        sessions_root=sessions_root,
        workspace_id=WORKSPACE_ID,
        gate=NavigationTopologyGate(sessions_root),
    )
    try:
        with pytest.raises(ValueError, match="stage=path_budget") as failure:
            await do_create(service)
        assert str(sessions_root) not in str(failure.value)
        assert isinstance(failure.value.__cause__, ValueError)
        assert "rollout/index.sqlite" in str(failure.value.__cause__)
        assert validated_rollouts == [rollout_database]
        assert count_rows(store, "session_creation_records") == 0
        assert not sessions_root.exists()
        assert not (
            sessions_root.parent / "orphaned" / "session-creation"
        ).exists()
    finally:
        store.close()


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


async def test_context_fork_request_intent_is_frozen_without_entering_business_manifest(
    service: SessionCreationService,
    store: SessionCatalogStore,
    sessions_root: Path,
) -> None:
    metadata = make_metadata(kind="context_fork", context_source_session_id=make_node_id())
    request_intent = {"request": {"mode": "context_fork"}, "goal_snapshot": None}
    kwargs = {
        "idempotency_key": "fork-intent-key", "title": "冻结请求的分叉",
        "title_source": "auto", "parent_node_id": None, "purpose": "context_fork",
        "session_metadata": metadata, "request_intent": request_intent,
        "graph_binding": TEST_THREAD_GRAPH_BINDING,
        "fork_delivery_request_id": "req-first-fork-delivery",
    }
    record = await service.prepare(**kwargs)
    assert json.loads(record.request_intent_json) == request_intent
    assert await service.prepare(**kwargs) == record
    assert record.fork_delivery_request_id == "req-first-fork-delivery"
    assert await service.prepare(**{**kwargs, "fork_delivery_request_id": "req-retried-fork-delivery"}) == record
    assert service.read_prepared_session_metadata(record) == metadata
    manifest = json.loads((sessions_root / ".staging" / record.session_id / "session.json").read_text())
    assert set(manifest) == _STRIPPED_MANIFEST_KEYS
    assert "request_intent" not in manifest and "request_intent_json" not in manifest
    with pytest.raises(RuntimeError, match="preimage 冲突"):
        await service.prepare(**{**kwargs, "request_intent": {
            **request_intent, "request": {"mode": "full_rollout_copy"},
        }})
    assert store.get_creation_record("fork-intent-key") == record


async def test_context_fork_publish_requires_committed_completion_proof(
    service: SessionCreationService,
    store: SessionCatalogStore,
    sessions_root: Path,
) -> None:
    metadata = make_metadata(
        kind="context_fork", context_source_session_id=make_node_id()
    )
    record = await service.prepare(
        idempotency_key="fork-key",
        title="分叉会话",
        title_source="auto",
        parent_node_id=None,
        purpose="context_fork",
        graph_binding=TEST_THREAD_GRAPH_BINDING,
        session_metadata=metadata,
    )

    assert record.purpose == "context_fork"
    assert record.session_metadata_json == json.dumps(
        metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    assert service.read_prepared_session_metadata(record) == metadata
    assert service.list_preparing_creation_records(purpose="context_fork") == [record]
    staging = sessions_root / ".staging" / record.session_id
    staged_materialization = staging / "threads" / record.main_thread_id / "rollout"
    staged_materialization.mkdir()
    (staged_materialization / "checkpoint.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="completion proof"):
        await service.publish_prepared(record)

    proof = ForkCompletionProof(
        target_session_id=record.session_id,
        fork_id="fork-1",
        materialization_id="materialization-1",
    )
    unexpected_node = staging / "unexpected"
    unexpected_node.mkdir()
    with pytest.raises(RuntimeError, match="条目集与预期不一致"):
        await service.publish_prepared(record, fork_completion=proof)
    unexpected_node.rmdir()

    result = await service.publish_prepared(record, fork_completion=proof)
    assert result.record_state == "published"
    assert store.get_creation_record("fork-key").state == "published"
    published_materialization = (
        date_bucket_dir(sessions_root, record.storage_relative_locator)
        / "threads" / record.main_thread_id / "rollout"
        / "checkpoint.json"
    )
    assert published_materialization.read_text(encoding="utf-8") == "{}"


async def test_context_fork_cas_failure_preserves_prepared_state_until_abort(
    service: SessionCreationService,
    store: SessionCatalogStore,
    sessions_root: Path,
) -> None:
    parent = store.create_folder(make_node_id(), WORKSPACE_ID, None, "父")
    record = await service.prepare(
        idempotency_key="fork-cas-key",
        title="分叉会话",
        title_source="auto",
        parent_node_id=parent.node_id,
        purpose="context_fork",
        graph_binding=TEST_THREAD_GRAPH_BINDING,
        session_metadata=make_metadata(
            kind="context_fork", context_source_session_id=make_node_id()
        ),
    )
    store.rename_node(parent.node_id, "漂移")
    proof = ForkCompletionProof(
        target_session_id=record.session_id,
        fork_id="fork-2",
        materialization_id="materialization-2",
    )
    with pytest.raises(RuntimeError, match="CAS 失败") as error:
        await service.publish_prepared(record, fork_completion=proof)

    target = date_bucket_dir(sessions_root, record.storage_relative_locator)
    assert target.is_dir()
    assert store.get_creation_record("fork-cas-key").state == "preparing"
    assert not (
        sessions_root.parent / "orphaned" / "session-creation" / record.session_id
    ).exists()

    aborted = await service.abort_prepared(record, str(error.value))
    assert aborted.state == "aborted"
    assert not target.exists()
    assert (
        sessions_root.parent / "orphaned" / "session-creation" / record.session_id
    ).is_dir()


async def test_create_writes_stripped_session_json(
    service: SessionCreationService, sessions_root: Path
) -> None:
    generation_origin = {"generator_id": "gen-1", "run_id": "run-1", "idempotency_key": "origin-1", "generator_type_id": "test", "generator_type_version": "1"}
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
    # 业务字段只初始化 catalog，身份 envelope 不承载可变状态。
    assert set(manifest) == _STRIPPED_MANIFEST_KEYS
    assert "title" not in manifest
    assert "title_source" not in manifest
    assert "parent_session_id" not in manifest
    assert manifest["session_id"] == result.session_id
    assert manifest["workspace_id"] == WORKSPACE_ID
    assert manifest["created_at"] == result.node.created_at
    assert result.node.updated_at == result.node.created_at
    assert result.node.session_kind == "normal"
    assert json.loads(result.node.generation_origin_json) == generation_origin
    assert result.node.current_agent_id == "default"
    assert result.node.current_provider_id == "provider-x"
    assert result.node.context_source_session_id is None


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
        "threads",
    ]
    assert tuple(path.name for path in (session_dir / "threads").iterdir()) == (result.main_thread_id,)
    assert not tuple((session_dir / "threads" / result.main_thread_id).iterdir())
    # staging 区已清空（目录被 rename 走）
    assert not (sessions_root / ".staging" / result.session_id).exists()


async def test_recovery_rejects_unregistered_child_thread_in_prepared_session(
    service: SessionCreationService,
    sessions_root: Path,
    store: SessionCatalogStore,
) -> None:
    record = create_manual_record(store)
    staging = sessions_root / ".staging" / record.session_id
    build_session_directory(staging, record, make_metadata())
    (staging / "threads" / "thr_019db1f0-0000-7000-8000-000000000001").mkdir()

    with pytest.raises(RuntimeError, match="prepared Session 必须只含冻结的 main thread"):
        await do_create(service)

    assert staging.is_dir()
    assert not date_bucket_dir(sessions_root, record.storage_relative_locator).exists()
    assert store.get_creation_record("key-1").state == "preparing"


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


async def test_published_replay_preserves_current_catalog_business_fields(
    service: SessionCreationService,
    store: SessionCatalogStore,
    sessions_root: Path,
) -> None:
    """creation journal 只冻结首次发布意图，重放不得恢复旧业务字段。"""
    first = await do_create(service)
    record = store.get_creation_record("key-1")
    manifest_path = date_bucket_dir(sessions_root, record.storage_relative_locator) / "session.json"
    manifest_bytes = manifest_path.read_bytes()
    folder = store.create_folder(make_node_id(), WORKSPACE_ID, None, "发布后归档")
    store.move_node(first.session_id, folder.node_id)
    store.rename_node(first.session_id, "发布后名称")
    updated = store.update_session_configuration(
        first.session_id,
        current_agent_id="coder",
        current_provider_id=None,
        updated_at=datetime(2026, 10, 9, tzinfo=UTC),
    )

    replayed = await do_create(service)

    assert replayed.node == updated
    assert replayed.node.current_agent_id == "coder"
    assert replayed.node.current_provider_id is None
    assert replayed.node.parent_node_id == folder.node_id
    assert replayed.node.display_name == "发布后名称"
    assert store.get_creation_record("key-1") == record
    assert manifest_path.read_bytes() == manifest_bytes


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


async def test_prepare_published_replay_rejects_frozen_title_preimage_conflict(
    service: SessionCreationService,
    store: SessionCatalogStore,
    sessions_root: Path,
) -> None:
    """冻结标题被篡改但 hash 未变时，prepare 重放仍须核验原始前像。"""
    await do_create(service)
    record = store.get_creation_record("key-1")
    directory = date_bucket_dir(sessions_root, record.storage_relative_locator)
    manifest = (directory / "session.json").read_bytes()
    with store.write_transaction() as connection:
        connection.execute(
            "UPDATE session_creation_records SET display_name=? "
            "WHERE session_creation_idempotency_key=?",
            ("被外部改写的创建标题", "key-1"),
        )
    damaged = store.get_creation_record("key-1")
    with pytest.raises(RuntimeError, match="preimage 与 envelope 不一致"):
        await service.prepare(
            idempotency_key="key-1", title="测试会话", parent_node_id=None,
            graph_binding=TEST_THREAD_GRAPH_BINDING, session_metadata=make_metadata(),
        )
    assert store.get_creation_record("key-1") == damaged
    assert (directory / "session.json").read_bytes() == manifest


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


@pytest.mark.parametrize(
    ("purpose", "kind", "has_context_source"),
    [
        ("normal", "context_fork", True),
        ("normal", "normal", True),
        ("context_fork", "normal", False),
        ("context_fork", "context_fork", False),
    ],
)
async def test_create_rejects_purpose_metadata_mismatch_before_state_change(
    service: SessionCreationService,
    store: SessionCatalogStore,
    purpose: str,
    kind: str,
    has_context_source: bool,
) -> None:
    metadata = make_metadata(
        kind=kind,
        context_source_session_id=(make_node_id() if has_context_source else None),
    )
    with pytest.raises(ValueError, match="purpose 与 session_metadata"):
        await service.prepare(
            idempotency_key="mismatched-envelope",
            title="标题",
            parent_node_id=None,
            purpose=purpose,  # type: ignore[arg-type]
            graph_binding=TEST_THREAD_GRAPH_BINDING,
            session_metadata=metadata,
        )
    with pytest.raises(KeyError):
        store.get_creation_record("mismatched-envelope")


async def test_published_record_rejects_purpose_metadata_mismatch(
    service: SessionCreationService,
    store: SessionCatalogStore,
) -> None:
    metadata = make_metadata(
        kind="context_fork", context_source_session_id=make_node_id()
    )
    record = create_manual_record(store, metadata=metadata)
    store.publish_creation_record("key-1", expected_purpose="normal")

    with pytest.raises(RuntimeError, match="purpose 与 session_metadata"):
        await service.publish_prepared(record)


async def test_publish_rejects_durable_purpose_metadata_mismatch(
    service: SessionCreationService,
    store: SessionCatalogStore,
    sessions_root: Path,
) -> None:
    metadata = make_metadata(
        kind="context_fork", context_source_session_id=make_node_id()
    )
    record = create_manual_record(store, metadata=metadata)
    staging = sessions_root / ".staging" / record.session_id
    build_session_directory(staging, record, metadata)

    with pytest.raises(RuntimeError, match="purpose 与 session_metadata"):
        await service.publish_prepared(record)

    assert store.get_creation_record("key-1").state == "preparing"
    assert count_rows(store, "nodes") == 0
    assert staging.is_dir()



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
        await service.prepare(
            idempotency_key="key-1",
            title="标题",
            parent_node_id=None,
            graph_binding=TEST_THREAD_GRAPH_BINDING,
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


async def test_create_empty_idempotency_key_rejected(
    service: SessionCreationService, store: SessionCatalogStore
) -> None:
    key = ""
    with pytest.raises(ValueError, match="idempotency_key"):
        await do_create(service, key=key)
    assert count_rows(store, "session_creation_records") == 0


@pytest.mark.parametrize("key_len", [256, 300, 4096])
async def test_create_long_idempotency_key_is_not_a_path_component(
    service: SessionCreationService,
    store: SessionCatalogStore,
    sessions_root: Path,
    key_len: int,
) -> None:
    """超长幂等键只进入 journal，不再被当作物理路径组件。"""
    key = "k" * key_len
    result = await do_create(service, key=key)
    record = store.get_creation_record(key)
    session_dir = date_bucket_dir(sessions_root, result.storage_relative_locator)
    assert record.session_id == result.session_id
    assert session_dir.name == record.session_id
    assert session_dir.is_dir()
    assert not (sessions_root / ".staging" / record.session_id).exists()


async def test_create_255_byte_idempotency_key_accepted(
    service: SessionCreationService,
    store: SessionCatalogStore,
    sessions_root: Path,
) -> None:
    """既有 255-byte key 成功契约保持，物理目录使用冻结 session ID。"""
    key = "k" * 255
    result = await do_create(service, key=key)
    assert result.record_state == "published"
    record = store.get_creation_record(key)
    session_dir = date_bucket_dir(sessions_root, result.storage_relative_locator)
    assert session_dir.name == record.session_id


async def test_slash_idempotency_key_replays_by_frozen_session_id(
    service: SessionCreationService,
    store: SessionCatalogStore,
    sessions_root: Path,
) -> None:
    key = "opaque/request/key"
    first = await do_create(service, key=key)
    second = await do_create(service, key=key)
    record = store.get_creation_record(key)
    session_dir = date_bucket_dir(sessions_root, first.storage_relative_locator)
    assert first.session_id == second.session_id == record.session_id
    assert session_dir.name == record.session_id
    assert session_dir.is_dir()
    assert not (sessions_root / ".staging" / record.session_id).exists()


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
    assert not (sessions_root / ".staging" / record.session_id).exists()


async def test_recovery_record_preparing_with_staging(
    service: SessionCreationService,
    store: SessionCatalogStore,
    sessions_root: Path,
) -> None:
    """崩溃点2：staging 后、rename 前崩溃 → 重入校验 staging 后继续。"""
    record = create_manual_record(store)
    build_session_directory(
        sessions_root / ".staging" / record.session_id, record, make_metadata()
    )
    result = await do_create(service)
    assert result.record_state == "published"
    assert result.session_id == record.session_id
    assert date_bucket_dir(
        sessions_root, record.storage_relative_locator
    ).is_dir()
    assert not (sessions_root / ".staging" / record.session_id).exists()


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
    assert not (sessions_root / ".staging" / record.session_id).exists()


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
        sessions_root / ".staging" / record.session_id, record, make_metadata()
    )
    with pytest.raises(RuntimeError, match="同时存在"):
        await do_create(service)
    assert store.get_creation_record("key-1").state == "preparing"


async def test_recovery_record_already_published(
    service: SessionCreationService, store: SessionCatalogStore
) -> None:
    """崩溃点4：publish 后崩溃 → 重入 record published 幂等返回。"""
    record = create_manual_record(store)
    node = store.publish_creation_record("key-1", expected_purpose="normal")
    result = await do_create(service)
    assert result.record_state == "published"
    assert result.session_id == record.session_id
    assert result.node == node


async def test_recovery_published_record_missing_node_fails_closed(
    service: SessionCreationService, store: SessionCatalogStore
) -> None:
    """恢复变体：record published 但 nodes 行缺失（外部改动）→ fail closed。"""
    create_manual_record(store)
    store.publish_creation_record("key-1", expected_purpose="normal")
    record = store.get_creation_record("key-1")
    connection = sqlite3.connect(store.database_path)
    try:
        _register_session_list_functions(connection)
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


async def test_cas_failure_parent_drift_preserves_until_explicit_abort(
    service: SessionCreationService,
    store: SessionCatalogStore,
    sessions_root: Path,
) -> None:
    """CAS 失败先保留现场；调用方显式 abort 后再隔离并终结记录。"""
    parent = store.create_folder(
        make_node_id(), WORKSPACE_ID, None, "父"
    )
    record = create_manual_record(store, parent_node_id=parent.node_id)
    build_session_directory(
        sessions_root / ".staging" / record.session_id, record, make_metadata()
    )
    # 手工制造父节点 revision 漂移
    store.rename_node(parent.node_id, "漂移")
    target = date_bucket_dir(sessions_root, record.storage_relative_locator)
    with pytest.raises(RuntimeError, match="CAS 失败") as error:
        await do_create(service, parent_node_id=parent.node_id)
    assert target.is_dir()
    preparing = store.get_creation_record("key-1")
    assert preparing.state == "preparing"
    assert not (
        sessions_root.parent / "orphaned" / "session-creation" / record.session_id
    ).exists()

    # Fork 调用方释放 retention 后才调用 abort，provider 再定点隔离。
    aborted = await service.abort_prepared(preparing, str(error.value))
    assert aborted.state == "aborted"
    assert not target.exists()
    quarantined = (
        sessions_root.parent / "orphaned" / "session-creation" / record.session_id
    )
    assert quarantined.is_dir()
    assert (quarantined / "session.json").is_file()
    assert (quarantined / "session-control.sqlite").is_file()
    # record aborted（含原因）且 nodes 无行
    assert aborted.abort_reason is not None
    assert "漂移" in aborted.abort_reason
    with pytest.raises(KeyError):
        store.get_node(record.session_id)
    # staging 区已清空
    assert not (sessions_root / ".staging" / record.session_id).exists()


async def test_cas_failure_locator_occupied_preserves_until_explicit_abort(
    service: SessionCreationService,
    store: SessionCatalogStore,
    sessions_root: Path,
) -> None:
    """CAS 失败：目标 locator 被其它 node 占用 → 同样定点回收 + abort。"""
    record = create_manual_record(store)
    build_session_directory(
        sessions_root / ".staging" / record.session_id, record, make_metadata()
    )
    # 直连 SQL 注入同 locator 的既有 node（record 尚未发布，占用合法存在）
    store.connection.execute(
        "INSERT INTO nodes (node_id, kind, parent_node_id, display_name, "
        "title_source, state, revision, workspace_id, created_at, "
        "storage_relative_locator, main_thread_id, session_kind, current_agent_id, updated_at) "
        "VALUES (?, 'session', NULL, '占位', 'user', 'active', 1, ?, ?, ?, ?, 'normal', 'default', ?)",
        (
            make_node_id(),
            WORKSPACE_ID,
            record.created_at,
            record.storage_relative_locator,
            f"thr_{create_uuid_hex()}",
            record.created_at,
        ),
    )
    with pytest.raises(RuntimeError, match="CAS 失败") as error:
        await do_create(service)
    target = date_bucket_dir(sessions_root, record.storage_relative_locator)
    assert target.is_dir()
    assert store.get_creation_record("key-1").state == "preparing"
    aborted = await service.abort_prepared(record, str(error.value))
    assert aborted.state == "aborted"
    assert not target.exists()
    assert (
        sessions_root.parent / "orphaned" / "session-creation" / record.session_id
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
        "purpose": "normal",
        "graph_binding": TEST_THREAD_GRAPH_BINDING,
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
        **{**kwargs, "purpose": "context_fork"}
    ) != compute_session_creation_preimage_hash(**kwargs)
    assert compute_session_creation_preimage_hash(
        **{**kwargs, "parent_node_id": make_node_id()}
    ) != compute_session_creation_preimage_hash(**kwargs)
    assert compute_session_creation_preimage_hash(
        **{**kwargs, "session_metadata": make_metadata(kind="context_fork")}
    ) != compute_session_creation_preimage_hash(**kwargs)

async def test_key_lock_pool_is_bounded_and_stable_per_key(service) -> None:
    """session 创建服务的 per-key 锁必须是固定上界且同 key 恒定命中。"""
    for index in range(5000):
        service._key_lock("session-" + str(index))
    assert len(service._key_locks) <= 64
    assert service._key_lock("session-shared") is service._key_lock("session-shared")


async def test_key_lock_serializes_same_key(service) -> None:
    """同 key 的临界区仍严格互斥（并发峰值恒为 1）。"""
    lock = service._key_lock("session-shared")
    concurrent = 0
    peak = 0

    async def worker() -> None:
        nonlocal concurrent, peak
        async with lock:
            concurrent += 1
            peak = max(peak, concurrent)
            await asyncio.sleep(0.01)
            concurrent -= 1

    await asyncio.gather(*(worker() for _ in range(20)))
    assert peak == 1
