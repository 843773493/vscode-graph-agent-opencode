"""per-session session-control.sqlite 基础设施测试(OpenSpec 8.2 切片2)。

全部用例使用 tmp_path 构造独立库文件,不把项目根注册为测试工作区;
覆盖 DDL/约束、幂等初始化、冲突 fail closed、读取/校验 API、
user_version fail-closed 与 WAL 连接约定。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.session_control_store import (
    SessionControlStore,
    ThreadCreationRecord,
    ThreadExecutionIntent,
    compute_initial_execution_binding_preimage_hash,
    derive_initial_execution_identity,
    validate_thread_id,
    validate_thread_relative_locator,
)
from app.core.session_lifecycle_gate import SessionDeletionPendingError

DEFAULT_CREATED_AT = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def make_thread_id() -> str:
    """uuid4 hex 天然满足 v4 位 profile(version 位 4 / variant 位 89ab)。"""
    return f"thr_{uuid.uuid4().hex}"


def make_session_id() -> str:
    """uuid4 hex 天然满足 v4 位 profile(version 位 4 / variant 位 89ab)。"""
    return f"ses_{uuid.uuid4().hex}"


@pytest.fixture
def store(tmp_path: Path) -> SessionControlStore:
    target = tmp_path / "session-control.sqlite"
    created = SessionControlStore(target)
    yield created
    created.close()


def raw_execute(store: SessionControlStore, sql: str, params: tuple[object, ...] = ()) -> None:
    """绕过 store API 直接改库(构造 CHECK 冲突/多行等注入态)。"""
    store.connection.execute(sql, params)


# ----------------------------------------------------------------------
# DDL / schema
# ----------------------------------------------------------------------


def test_initialize_creates_tables_and_sets_user_version(tmp_path: Path) -> None:
    target = tmp_path / "session-control.sqlite"
    created = SessionControlStore(target)
    try:
        version = int(
            created.connection.execute("PRAGMA user_version").fetchone()[0]
        )
        # B1/B2：SCHEMA_VERSION 4→5（session_operation_leases）→6（
        # thread_owner_bindings owner 字段槽）→7（D5 communication
        # outbox/inbox ledger）；thread_execution_intents 稳定 identity
        # 由 R23 落地。
        assert version == SessionControlStore.SCHEMA_VERSION == 7
        tables = {
            str(row[0])
            for row in created.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {"thread_catalog", "lifecycle_fence"} <= tables
        # 8.5-A 新表幂等落库
        assert {
            "thread_creation_records",
            "thread_execution_intents",
            "session_operation_leases",
            "thread_owner_bindings",
        } <= tables
        # D5：communication outbox/inbox ledger 表幂等落库
        assert {"communication_outbox", "communication_inbox"} <= tables
    finally:
        created.close()


def test_sha256_hex_pattern_has_single_neutral_definition() -> None:
    """session-control 各族的 sha256 形态正则只有一处实现（中立原语模块）。

    宿主与三个子包（owner binding / operation lease / communication ledger）
    都必须从 `session_control_primitives` 导入同一对象；任何一处重新定义
    一份拷贝（哪怕是等价正则）都会让本断言失败。
    """
    import app.core.session_control_store as host
    from app.core.session_control_communication_ledger import (
        communication_ledger,
    )
    from app.core.session_control_operation_lease import operation_lease
    from app.core.session_control_primitives import SHA256_HEX_PATTERN
    from app.core.session_control_thread_owner_binding import (
        thread_owner_binding,
    )

    modules = (host, thread_owner_binding, operation_lease, communication_ledger)
    for module in modules:
        assert module.SHA256_HEX_PATTERN is SHA256_HEX_PATTERN, module.__name__
        source = Path(module.__file__).read_text(encoding="utf-8")
        # 只允许出现导入语句，不允许在别处再次 compile 出该形态。
        assert source.count("re.compile(r\"^[0-9a-f]{64}$\")") == 0
        assert (
            "session_control_primitives" in source
            and "SHA256_HEX_PATTERN" in source
        ), module.__name__
    assert SHA256_HEX_PATTERN.pattern == r"^[0-9a-f]{64}$"
    assert SHA256_HEX_PATTERN.fullmatch("0" * 64) is not None
    assert SHA256_HEX_PATTERN.fullmatch("A" * 64) is None


def test_thread_creation_key_has_single_neutral_definition() -> None:
    """幂等键形态校验只有一处实现（中立原语模块），store 与服务共用。

    宿主曾自带一份 ``_validate_thread_creation_key`` 静态方法、thread
    creation service 又逐字复制了同一段，三处口径分叉风险：任一处放宽
    ``.``/``..``/分隔符/版本判定都会让另一处静默失效。本断言固化收敛结果。
    """
    import app.core.session_control_store as host
    import app.core.thread_creation as creation
    from app.core.session_control_primitives import validate_thread_creation_key

    assert host.validate_thread_creation_key is validate_thread_creation_key
    assert creation.validate_thread_creation_key is validate_thread_creation_key
    # 宿主不再保留自带第二份实现（私有名一并下线）。
    assert not hasattr(SessionControlStore, "_validate_thread_creation_key")
    host_source = Path(host.__file__).read_text(encoding="utf-8")
    assert "必须是安全单段路径名（不含分隔符/./..）" not in host_source
    creation_source = Path(creation.__file__).read_text(encoding="utf-8")
    assert "必须是安全单段路径名（不含分隔符/./..）" not in creation_source
    # session 创建流同样只复用中立原语，不得再内联第三份逐字副本。
    from app.core import session_creation

    session_creation_source = Path(session_creation.__file__).read_text(
        encoding="utf-8"
    )
    assert "必须是安全单段路径名（不含分隔符/./..）" not in session_creation_source


def test_initialize_is_idempotent_on_reopen(tmp_path: Path) -> None:
    target = tmp_path / "session-control.sqlite"
    first = SessionControlStore(target)
    try:
        first.initialize_main_thread(make_thread_id(), DEFAULT_CREATED_AT)
    finally:
        first.close()
    second = SessionControlStore(target)
    try:
        # 重复建表不报错、不重置 user_version、不破坏既有行。
        row = second.get_main_thread()
        assert str(row["kind"]) == "main"
        version = int(
            second.connection.execute("PRAGMA user_version").fetchone()[0]
        )
        # B1/B2：user_version 升级后为 7（原断言 4→6→7）。
        assert version == 7
    finally:
        second.close()


def test_wal_mode_enabled(tmp_path: Path) -> None:
    created = SessionControlStore(tmp_path / "session-control.sqlite")
    try:
        mode = str(
            created.connection.execute("PRAGMA journal_mode").fetchone()[0]
        )
        assert mode.lower() == "wal"
    finally:
        created.close()


def test_unknown_user_version_fails_closed(tmp_path: Path) -> None:
    target = tmp_path / "session-control.sqlite"
    created = SessionControlStore(target)
    created.close()
    outside = sqlite3.connect(target)
    try:
        # D5：7 已是受支持版本，未知版本样本顺延为 8。
        outside.execute("PRAGMA user_version = 8")
        outside.commit()
    finally:
        outside.close()
    with pytest.raises(RuntimeError, match="schema 版本未知"):
        SessionControlStore(target)


def test_v6_database_upgrades_additively_to_v7(tmp_path: Path) -> None:
    """v6 库加法升级到 v7：communication 表补建，既有生命周期行零丢失。

    模拟真实 v6 库：按 v7 建库并写入生命周期数据后，DROP 掉 D5 新增的
    communication 表并回拨 user_version=6（v6 库不存在这两张表）；重开
    时 current=6 走加法补建路径，任何失败随 _initialize 事务整体回滚。
    """
    target = tmp_path / "session-control.sqlite"
    main_thread_id = make_thread_id()
    first = SessionControlStore(target)
    try:
        first.initialize_main_thread(main_thread_id, DEFAULT_CREATED_AT)
        first.initialize_fence("active", 1)
    finally:
        first.close()
    outside = sqlite3.connect(target)
    try:
        outside.execute("DROP TABLE communication_outbox")
        outside.execute("DROP TABLE communication_inbox")
        outside.execute(
            "DROP INDEX IF EXISTS idx_communication_inbox_target_accepted"
        )
        outside.execute("PRAGMA user_version = 6")
        outside.commit()
    finally:
        outside.close()
    second = SessionControlStore(target)
    try:
        version = int(
            second.connection.execute("PRAGMA user_version").fetchone()[0]
        )
        assert version == 7
        tables = {
            str(row[0])
            for row in second.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {"communication_outbox", "communication_inbox"} <= tables
        # 生命周期既有行零丢失（main row/fence 逐字段保留）
        row = second.get_main_thread()
        assert str(row["thread_id"]) == main_thread_id
        assert second.get_fence() == ("active", 1)
        # 升级后 communication 表可用：状态索引查询正常返回空集合
        assert second.list_target_accepted_communication_inboxes() == ()
    finally:
        second.close()


# ----------------------------------------------------------------------
# main thread row
# ----------------------------------------------------------------------


def test_initialize_main_thread_inserts_and_get_returns_row(
    store: SessionControlStore,
) -> None:
    thread_id = make_thread_id()
    store.initialize_main_thread(thread_id, DEFAULT_CREATED_AT)
    row = store.get_main_thread()
    assert str(row["thread_id"]) == thread_id
    assert str(row["kind"]) == "main"
    assert str(row["created_at"]) == DEFAULT_CREATED_AT.isoformat()


def test_initialize_main_thread_is_idempotent(store: SessionControlStore) -> None:
    thread_id = make_thread_id()
    store.initialize_main_thread(thread_id, DEFAULT_CREATED_AT)
    store.initialize_main_thread(thread_id, DEFAULT_CREATED_AT)
    rows = store.connection.execute("SELECT thread_id FROM thread_catalog").fetchall()
    assert len(rows) == 1
    assert str(rows[0][0]) == thread_id


def test_initialize_main_thread_conflicts_on_different_thread_id(
    store: SessionControlStore,
) -> None:
    store.initialize_main_thread(make_thread_id(), DEFAULT_CREATED_AT)
    with pytest.raises(RuntimeError, match="不一致"):
        store.initialize_main_thread(make_thread_id(), DEFAULT_CREATED_AT)


def test_initialize_main_thread_rejects_invalid_thread_id(
    store: SessionControlStore,
) -> None:
    with pytest.raises((TypeError, ValueError)):
        store.initialize_main_thread("thr_not_valid", DEFAULT_CREATED_AT)


def test_initialize_main_thread_rejects_non_datetime(
    store: SessionControlStore,
) -> None:
    with pytest.raises(TypeError):
        store.initialize_main_thread(make_thread_id(), "2026-06-01T12:00:00+00:00")


def test_initialize_main_thread_rejects_naive_created_at(
    store: SessionControlStore,
) -> None:
    with pytest.raises(ValueError, match="时区"):
        store.initialize_main_thread(
            make_thread_id(),
            datetime(2026, 6, 1, 12, 0, tzinfo=None),  # noqa: DTZ001 - 故意构造 naive datetime
        )


def test_thread_catalog_kind_check_rejects_non_main(
    store: SessionControlStore,
) -> None:
    # R20 §2.1-A：kind CHECK 扩为 ('main', 'child') 后，'child' 是合法值
    # （原 v1 用例以 'child' 断言 CHECK 拒绝，已按任务书升级适配——child
    # 合法插入改由 test_thread_catalog_child_kind_accepted 覆盖）；此处
    # 改用仍然非法的 'folder' 保持「未知 kind 拒绝」的负向保护。
    with pytest.raises(sqlite3.IntegrityError):
        raw_execute(
            store,
            "INSERT INTO thread_catalog (thread_id, kind, created_at) "
            "VALUES (?, 'folder', ?)",
            (make_thread_id(), DEFAULT_CREATED_AT.isoformat()),
        )


def test_get_main_thread_missing_raises_key_error(
    store: SessionControlStore,
) -> None:
    with pytest.raises(KeyError):
        store.get_main_thread()


def test_multiple_main_rows_fail_closed(store: SessionControlStore) -> None:
    store.initialize_main_thread(make_thread_id(), DEFAULT_CREATED_AT)
    raw_execute(
        store,
        "INSERT INTO thread_catalog (thread_id, kind, created_at) "
        "VALUES (?, 'main', ?)",
        (make_thread_id(), DEFAULT_CREATED_AT.isoformat()),
    )
    with pytest.raises(RuntimeError, match="多个 main row"):
        store.get_main_thread()


def test_initialize_main_thread_rejects_multiple_main_rows(
    store: SessionControlStore,
) -> None:
    """initialize_main_thread 遇多 main row 必须 fail closed，不得静默吸收。

    库被外部改动（出现第二个 main row）时 create-or-get 绝不能返回
    成功——否则 main thread 的唯一性被静默破坏。
    """
    existing = make_thread_id()
    store.initialize_main_thread(existing, DEFAULT_CREATED_AT)
    raw_execute(
        store,
        "INSERT INTO thread_catalog (thread_id, kind, created_at) "
        "VALUES (?, 'main', ?)",
        (make_thread_id(), DEFAULT_CREATED_AT.isoformat()),
    )
    # 即使传入与既有 first row 一致的 thread_id，多行库也必须拒绝。
    with pytest.raises(RuntimeError, match="多个 main row"):
        store.initialize_main_thread(existing, DEFAULT_CREATED_AT)


def test_initialize_main_thread_conflict_does_not_absorb_second_row(
    store: SessionControlStore,
) -> None:
    """冲突路径不得写入第二行：拒绝后 thread_catalog 仍只有 1 行。"""
    store.initialize_main_thread(make_thread_id(), DEFAULT_CREATED_AT)
    with pytest.raises(RuntimeError, match="不一致"):
        store.initialize_main_thread(make_thread_id(), DEFAULT_CREATED_AT)
    rows = store.connection.execute(
        "SELECT COUNT(*) FROM thread_catalog WHERE kind = 'main'"
    ).fetchone()
    assert int(rows[0]) == 1


# ----------------------------------------------------------------------
# lifecycle fence
# ----------------------------------------------------------------------


def test_initialize_fence_default_active_generation_1(
    store: SessionControlStore,
) -> None:
    store.initialize_fence()
    assert store.get_fence() == ("active", 1)


def test_initialize_fence_is_idempotent(store: SessionControlStore) -> None:
    store.initialize_fence("active", 1)
    store.initialize_fence("active", 1)
    rows = store.connection.execute(
        "SELECT id, state, generation FROM lifecycle_fence"
    ).fetchall()
    assert len(rows) == 1
    assert (int(rows[0][0]), str(rows[0][1]), int(rows[0][2])) == (1, "active", 1)


def test_initialize_fence_conflict_fails_closed(store: SessionControlStore) -> None:
    store.initialize_fence("active", 1)
    with pytest.raises(RuntimeError, match="不一致"):
        store.initialize_fence("active", 2)
    with pytest.raises(RuntimeError, match="不一致"):
        store.initialize_fence("deleting", 1)


def test_initialize_fence_rejects_unknown_state(store: SessionControlStore) -> None:
    with pytest.raises(ValueError, match="状态非法"):
        store.initialize_fence("paused", 1)


def test_initialize_fence_rejects_bad_generation(store: SessionControlStore) -> None:
    with pytest.raises(TypeError):
        store.initialize_fence("active", "1")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="不能为负"):
        store.initialize_fence("active", -1)


def test_fence_id_check_rejects_second_row(store: SessionControlStore) -> None:
    store.initialize_fence()
    with pytest.raises(sqlite3.IntegrityError):
        raw_execute(
            store,
            "INSERT INTO lifecycle_fence (id, state, generation) "
            "VALUES (2, 'active', 1)",
        )


def test_fence_state_check_rejects_unknown_state_via_update(
    store: SessionControlStore,
) -> None:
    store.initialize_fence()
    with pytest.raises(sqlite3.IntegrityError):
        raw_execute(
            store, "UPDATE lifecycle_fence SET state = 'paused' WHERE id = 1"
        )


def test_get_fence_missing_raises_key_error(store: SessionControlStore) -> None:
    with pytest.raises(KeyError, match="缺少 lifecycle fence row"):
        store.get_fence()


# ----------------------------------------------------------------------
# cas_fence_transition（8.1-B，R14：对齐 R10 SessionLifecycleFence 语义）
# ----------------------------------------------------------------------


def test_cas_fence_transition_success_advances_generation(
    store: SessionControlStore,
) -> None:
    store.initialize_fence()
    assert store.cas_fence_transition(1, "deleting") is True
    assert store.get_fence() == ("deleting", 2)


def test_cas_fence_transition_wrong_generation_returns_false(
    store: SessionControlStore,
) -> None:
    store.initialize_fence()
    assert store.cas_fence_transition(2, "deleting") is False
    # fence 保持不变
    assert store.get_fence() == ("active", 1)


def test_cas_fence_transition_deleting_to_active_returns_false(
    store: SessionControlStore,
) -> None:
    store.initialize_fence()
    assert store.cas_fence_transition(1, "deleting") is True
    assert store.cas_fence_transition(2, "active") is False
    # 不可复活：保持 deleting
    assert store.get_fence() == ("deleting", 2)


def test_cas_fence_transition_idempotent_when_already_deleting(
    store: SessionControlStore,
) -> None:
    store.initialize_fence()
    assert store.cas_fence_transition(1, "deleting") is True
    # 已 deleting：再次 CAS 返回 False 且状态/generation 不变
    assert store.cas_fence_transition(2, "deleting") is False
    assert store.get_fence() == ("deleting", 2)


def test_cas_fence_transition_rejects_unknown_state(
    store: SessionControlStore,
) -> None:
    store.initialize_fence()
    with pytest.raises(ValueError, match="状态非法"):
        store.cas_fence_transition(1, "paused")
    assert store.get_fence() == ("active", 1)


def test_cas_fence_transition_rejects_non_int_generation(
    store: SessionControlStore,
) -> None:
    store.initialize_fence()
    with pytest.raises(TypeError, match="整数"):
        store.cas_fence_transition("1", "deleting")  # type: ignore[arg-type]


def test_cas_fence_transition_missing_fence_row_raises_key_error(
    store: SessionControlStore,
) -> None:
    # 未 initialize_fence 的库：缺失的 fence 不是可 CAS 的 active fence
    with pytest.raises(KeyError):
        store.cas_fence_transition(1, "deleting")


def test_cas_fence_transition_persists_across_reopen(tmp_path: Path) -> None:
    target = tmp_path / "session-control.sqlite"
    first = SessionControlStore(target)
    try:
        first.initialize_fence()
        assert first.cas_fence_transition(1, "deleting") is True
    finally:
        first.close()
    second = SessionControlStore(target)
    try:
        assert second.get_fence() == ("deleting", 2)
    finally:
        second.close()


def test_cas_fence_transition_wrong_generation_false_then_same_instance_retry(
    store: SessionControlStore,
) -> None:
    """M1 回归：generation 不符返回 False 后不遗留事务，同实例可重试 CAS。

    R14 审查 M1 实证：修复前 False 路径把 BEGIN IMMEDIATE 事务留在打开
    状态，同实例第二次 CAS 抛 ``sqlite3.OperationalError: cannot start a
    transaction within a transaction``。
    """
    store.initialize_fence()
    # 第一次：generation 不符 → False，且事务已结束（不遗留打开事务）
    assert store.cas_fence_transition(999, "deleting") is False
    assert store.connection.in_transaction is False
    # 第二次：同实例先取实际 generation 再 CAS（装配轮的自然重试序列），
    # 修复前此处抛 OperationalError，修复后行为正确。
    state, generation = store.get_fence()
    assert (state, generation) == ("active", 1)
    assert store.cas_fence_transition(generation, "deleting") is True
    assert store.get_fence() == ("deleting", 2)
    assert store.connection.in_transaction is False


def test_cas_fence_transition_deleting_to_active_false_then_same_instance_calls(
    store: SessionControlStore,
) -> None:
    """M1 回归：deleting→active 拒绝路径不遗留事务，同实例可连续调用。"""
    store.initialize_fence()
    assert store.cas_fence_transition(1, "deleting") is True
    assert store.connection.in_transaction is False
    # 第一次拒绝：deleting→active 恒 False，事务已结束
    assert store.cas_fence_transition(2, "active") is False
    assert store.connection.in_transaction is False
    # 同实例连续调用：已 deleting 幂等 False，不抛 OperationalError
    assert store.cas_fence_transition(2, "deleting") is False
    assert store.connection.in_transaction is False
    # fence 全程保持 (deleting, 2)，不可复活
    assert store.get_fence() == ("deleting", 2)


# ----------------------------------------------------------------------
# verify_matches_catalog_main_thread
# ----------------------------------------------------------------------


def test_verify_matches_catalog_main_thread_ok(
    store: SessionControlStore,
) -> None:
    thread_id = make_thread_id()
    store.initialize_main_thread(thread_id, DEFAULT_CREATED_AT)
    store.verify_matches_catalog_main_thread(thread_id)


def test_verify_mismatch_fails_closed(store: SessionControlStore) -> None:
    store.initialize_main_thread(make_thread_id(), DEFAULT_CREATED_AT)
    with pytest.raises(RuntimeError, match="不一致"):
        store.verify_matches_catalog_main_thread(make_thread_id())


def test_verify_rejects_invalid_thread_id_format(
    store: SessionControlStore,
) -> None:
    store.initialize_main_thread(make_thread_id(), DEFAULT_CREATED_AT)
    with pytest.raises((TypeError, ValueError)):
        store.verify_matches_catalog_main_thread("thr_0022")


# ----------------------------------------------------------------------
# 连接生命周期
# ----------------------------------------------------------------------


def test_close_is_idempotent_and_operations_fail_after_close(
    tmp_path: Path,
) -> None:
    created = SessionControlStore(tmp_path / "session-control.sqlite")
    created.close()
    created.close()  # 幂等 no-op
    with pytest.raises(RuntimeError, match="已关闭"):
        created.get_main_thread()
    with pytest.raises(RuntimeError, match="已关闭"):
        created.initialize_fence()


# ----------------------------------------------------------------------
# 8.5-A（R20）：schema v1→v2 升级 + kind CHECK child
# ----------------------------------------------------------------------


def build_v1_database(path: Path, thread_id: str) -> None:
    """按 R12/R13 v1 DDL 手工构造 v1 库（含一条 main row + active fence）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE thread_catalog ("
            "thread_id TEXT PRIMARY KEY, "
            "kind TEXT NOT NULL CHECK (kind IN ('main')), "
            "created_at TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE lifecycle_fence ("
            "id INTEGER PRIMARY KEY CHECK (id = 1), "
            "state TEXT NOT NULL CHECK (state IN ('active', 'deleting')), "
            "generation INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO thread_catalog (thread_id, kind, created_at) "
            "VALUES (?, 'main', ?)",
            (thread_id, DEFAULT_CREATED_AT.isoformat()),
        )
        connection.execute(
            "INSERT INTO lifecycle_fence (id, state, generation) "
            "VALUES (1, 'active', 1)"
        )
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
    finally:
        connection.close()


def test_v1_database_upgrades_preserving_main_row(tmp_path: Path) -> None:
    """v1 库原地升级：main row 数据零丢失、user_version 1→7、child 可插。"""
    target = tmp_path / "nested" / "session-control.sqlite"
    main_thread_id = make_thread_id()
    build_v1_database(target, main_thread_id)
    store = SessionControlStore(target)
    try:
        version = int(
            store.connection.execute("PRAGMA user_version").fetchone()[0]
        )
        assert version == 7
        # main row 数据零丢失（thread_id/created_at 逐字段保留）
        row = store.get_main_thread()
        assert str(row["thread_id"]) == main_thread_id
        assert str(row["kind"]) == "main"
        assert str(row["created_at"]) == DEFAULT_CREATED_AT.isoformat()
        assert store.get_fence() == ("active", 1)
        # 升级后 kind='child' 可插入（CHECK 扩为 ('main', 'child')）
        child_id = make_thread_id()
        raw_execute(
            store,
            "INSERT INTO thread_catalog (thread_id, kind, created_at) "
            "VALUES (?, 'child', ?)",
            (child_id, DEFAULT_CREATED_AT.isoformat()),
        )
        # main row 读取不受 child row 影响
        assert str(store.get_main_thread()["thread_id"]) == main_thread_id
    finally:
        store.close()


def test_v1_upgrade_reopens_idempotently(tmp_path: Path) -> None:
    """升级后的库重开是幂等 no-op（DDL IF NOT EXISTS，user_version 保持 2）。"""
    target = tmp_path / "session-control.sqlite"
    main_thread_id = make_thread_id()
    build_v1_database(target, main_thread_id)
    first = SessionControlStore(target)
    first.initialize_main_thread(main_thread_id, DEFAULT_CREATED_AT)
    first.close()
    second = SessionControlStore(target)
    try:
        assert str(second.get_main_thread()["thread_id"]) == main_thread_id
        version = int(
            second.connection.execute("PRAGMA user_version").fetchone()[0]
        )
        assert version == 7
        rows = second.connection.execute(
            "SELECT COUNT(*) FROM thread_creation_records"
        ).fetchone()
        assert int(rows[0]) == 0
    finally:
        second.close()


def test_thread_catalog_child_kind_accepted(store: SessionControlStore) -> None:
    """kind='child' 在 v2 CHECK 下合法；child row 不影响 main row 语义。"""
    child_id = make_thread_id()
    raw_execute(
        store,
        "INSERT INTO thread_catalog (thread_id, kind, created_at) "
        "VALUES (?, 'child', ?)",
        (child_id, DEFAULT_CREATED_AT.isoformat()),
    )
    rows = store.connection.execute(
        "SELECT thread_id, kind FROM thread_catalog WHERE kind = 'child'"
    ).fetchall()
    assert len(rows) == 1
    assert str(rows[0]["thread_id"]) == child_id
    # 无 main row 时 main 读取仍 fail closed
    with pytest.raises(KeyError):
        store.get_main_thread()


def test_initialize_main_thread_ignores_child_rows(
    store: SessionControlStore,
) -> None:
    """既有 child row 不影响 main row 的 create-or-get（kind='main' 过滤）。"""
    store.initialize_main_thread(make_thread_id(), DEFAULT_CREATED_AT)
    raw_execute(
        store,
        "INSERT INTO thread_catalog (thread_id, kind, created_at) "
        "VALUES (?, 'child', ?)",
        (make_thread_id(), DEFAULT_CREATED_AT.isoformat()),
    )
    store.initialize_main_thread(store.get_main_thread()["thread_id"], DEFAULT_CREATED_AT)
    rows = store.connection.execute(
        "SELECT COUNT(*) FROM thread_catalog WHERE kind = 'main'"
    ).fetchall()
    assert int(rows[0][0]) == 1


def test_get_main_thread_returns_main_with_children(
    store: SessionControlStore,
) -> None:
    store.initialize_main_thread(make_thread_id(), DEFAULT_CREATED_AT)
    main_id = str(store.get_main_thread()["thread_id"])
    raw_execute(
        store,
        "INSERT INTO thread_catalog (thread_id, kind, created_at) "
        "VALUES (?, 'child', ?)",
        (make_thread_id(), DEFAULT_CREATED_AT.isoformat()),
    )
    raw_execute(
        store,
        "INSERT INTO thread_catalog (thread_id, kind, created_at) "
        "VALUES (?, 'child', ?)",
        (make_thread_id(), DEFAULT_CREATED_AT.isoformat()),
    )
    assert str(store.get_main_thread()["thread_id"]) == main_id


def test_v1_upgrade_temp_table_conflict_fails_closed(tmp_path: Path) -> None:
    """v1 库被外部塞入同名临时表时升级 fail closed（不静默覆盖）。"""
    target = tmp_path / "session-control.sqlite"
    build_v1_database(target, make_thread_id())
    outside = sqlite3.connect(target)
    try:
        outside.execute(
            "CREATE TABLE thread_catalog_kind_upgrade ("
            "thread_id TEXT PRIMARY KEY, kind TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        outside.commit()
    finally:
        outside.close()
    with pytest.raises(sqlite3.OperationalError):
        SessionControlStore(target)


# ----------------------------------------------------------------------
# 8.5-A（R20）：ThreadCreationRecord create-or-get / freeze / publish /
# abort / mark / initial execution intent
# ----------------------------------------------------------------------


def make_thread_metadata_json() -> tuple[str, str]:
    """构造合法的 graph_binding / capability_profile canonical JSON 文本。"""
    graph_binding = json.dumps(
        {
            "graph_id": "deep-agent",
            "graph_revision": "rev-1",
            "graph_schema_hash": "sha256:aa",
            "capability_profile_hash": "sha256:bb",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    capability_profile = json.dumps(
        {"tools": ["read_file"]}, sort_keys=True, separators=(",", ":")
    )
    return graph_binding, capability_profile


def valid_thread_creation_kwargs(store: SessionControlStore) -> dict[str, object]:
    """构造 create_or_get_thread_creation_record 的最小合法参数。"""
    graph_binding, capability_profile = make_thread_metadata_json()
    return {
        "idempotency_key": "key-1",
        "initial_state": "running",
        "preimage_hash": "a" * 64,
        "graph_binding": graph_binding,
        "capability_profile": capability_profile,
        "created_at": DEFAULT_CREATED_AT,
        # ensure_ascii=False：与生产 canonical JSON 同口径（中文不转义）。
        "task_seed": json.dumps(
            {"task": "做一件事"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "task_reference": None,
    }


def prepare_record(
    store: SessionControlStore,
    **overrides: object,
) -> ThreadCreationRecord:
    """测试辅助：初始化 main/fence 后 create-or-get 一条 preparing record。"""
    store.initialize_main_thread(make_thread_id(), DEFAULT_CREATED_AT)
    store.initialize_fence("active", 1)
    kwargs = valid_thread_creation_kwargs(store)
    kwargs.update(overrides)
    return store.create_or_get_thread_creation_record(**kwargs)  # type: ignore[arg-type]


def test_create_or_get_record_freezes_identity(
    store: SessionControlStore,
) -> None:
    store.initialize_main_thread(make_thread_id(), DEFAULT_CREATED_AT)
    store.initialize_fence("active", 1)
    record = store.create_or_get_thread_creation_record(
        **valid_thread_creation_kwargs(store)
    )
    assert record.state == "preparing"
    assert record.abort_reason is None
    # child ID 软件分配 thr_ 且过完整验证器
    validate_thread_id(record.child_thread_id)
    # final/staging locator 冻结：日期 == child created_at UTC 日期
    expected_final = (
        f"threads/{DEFAULT_CREATED_AT.astimezone(UTC).date():%Y/%m/%d}/"
        f"{record.child_thread_id}"
    )
    assert record.final_relative_locator == expected_final
    validate_thread_relative_locator(record.final_relative_locator)
    assert record.staging_locator == ".staging/key-1"
    assert record.child_created_at == DEFAULT_CREATED_AT.isoformat()
    # owner fence generation 与 catalog precondition revision 冻结
    assert record.owner_session_lifecycle_generation == 1
    assert record.catalog_precondition_revision == 1  # 仅 main row
    assert record.collaboration_precondition_revision is None
    # GraphBinding/capability/seed/reference 冻结为传入 canonical JSON
    graph_binding, capability_profile = make_thread_metadata_json()
    assert record.graph_binding == graph_binding
    assert record.capability_profile == capability_profile
    assert record.task_seed is not None and "做一件事" in record.task_seed
    assert record.task_reference is None
    # artifact manifest 未冻结（freeze 方法负责）
    assert record.artifact_manifest is None
    assert record.artifact_manifest_hash is None
    # admission identity 冻结（幂等键 + thread ref + 初始 state）
    intent = json.loads(record.admission_intent)
    assert intent == {
        "admission_idempotency_key": "key-1",
        "thread_id": record.child_thread_id,
        "initial_state": "running",
    }
    # record 不进入 thread_catalog（preparing 不构成可见性）
    catalog_rows = store.connection.execute(
        "SELECT COUNT(*) FROM thread_catalog"
    ).fetchone()
    assert int(catalog_rows[0]) == 1  # 仅 main


def test_create_or_get_record_idempotent_same_preimage(
    store: SessionControlStore,
) -> None:
    first = prepare_record(store)
    kwargs = valid_thread_creation_kwargs(store)
    second = store.create_or_get_thread_creation_record(**kwargs)  # type: ignore[arg-type]
    assert second == first
    rows = store.connection.execute(
        "SELECT COUNT(*) FROM thread_creation_records"
    ).fetchone()
    assert int(rows[0]) == 1


def test_create_or_get_record_preimage_conflict(
    store: SessionControlStore,
) -> None:
    prepare_record(store)
    kwargs = valid_thread_creation_kwargs(store)
    kwargs["preimage_hash"] = "b" * 64
    with pytest.raises(RuntimeError, match="preimage 冲突"):
        store.create_or_get_thread_creation_record(**kwargs)  # type: ignore[arg-type]


def test_create_or_get_record_thread_id_conflict(
    store: SessionControlStore,
) -> None:
    first = prepare_record(store)
    kwargs = valid_thread_creation_kwargs(store)
    kwargs["thread_id"] = make_thread_id()
    with pytest.raises(RuntimeError, match="拒绝改绑"):
        store.create_or_get_thread_creation_record(**kwargs)  # type: ignore[arg-type]
    # 原 record 未受影响
    assert store.get_thread_creation_record("key-1") == first


def test_create_or_get_record_honors_provided_thread_id(
    store: SessionControlStore,
) -> None:
    provided = make_thread_id()
    record = prepare_record(store, thread_id=provided)
    assert record.child_thread_id == provided


def test_create_or_get_record_rejects_bad_inputs(
    store: SessionControlStore,
) -> None:
    store.initialize_main_thread(make_thread_id(), DEFAULT_CREATED_AT)
    store.initialize_fence("active", 1)
    base = valid_thread_creation_kwargs(store)
    # 幂等键形态
    for bad_key in ("", "a/b", "a\\b", "..", ".", "a\x00b"):
        kwargs = dict(base)
        kwargs["idempotency_key"] = bad_key
        with pytest.raises(ValueError, match="idempotency_key"):
            store.create_or_get_thread_creation_record(**kwargs)  # type: ignore[arg-type]
    # initial_state 闭集
    kwargs = dict(base)
    kwargs["initial_state"] = "paused"
    with pytest.raises(ValueError, match="initial_state"):
        store.create_or_get_thread_creation_record(**kwargs)  # type: ignore[arg-type]
    # thread_id 形态
    kwargs = dict(base)
    kwargs["thread_id"] = "thr_not_valid"
    with pytest.raises(ValueError, match="形态非法"):
        store.create_or_get_thread_creation_record(**kwargs)  # type: ignore[arg-type]
    # graph_binding 非法 JSON
    kwargs = dict(base)
    kwargs["graph_binding"] = "not-json"
    with pytest.raises(ValueError, match="graph_binding"):
        store.create_or_get_thread_creation_record(**kwargs)  # type: ignore[arg-type]
    # naive created_at
    kwargs = dict(base)
    kwargs["created_at"] = datetime(2026, 6, 1, tzinfo=None)  # noqa: DTZ001
    with pytest.raises(ValueError, match="时区"):
        store.create_or_get_thread_creation_record(**kwargs)  # type: ignore[arg-type]
    # delegated 必须携带非空 delegation_id
    kwargs = dict(base)
    kwargs["delegation_id"] = ""
    with pytest.raises(ValueError, match="delegation_id"):
        store.create_or_get_thread_creation_record(**kwargs)  # type: ignore[arg-type]


def test_create_or_get_record_requires_active_fence(
    store: SessionControlStore,
) -> None:
    store.initialize_main_thread(make_thread_id(), DEFAULT_CREATED_AT)
    store.initialize_fence("active", 1)
    store.cas_fence_transition(1, "deleting")
    with pytest.raises(RuntimeError, match="非 active"):
        store.create_or_get_thread_creation_record(
            **valid_thread_creation_kwargs(store)
        )


def test_create_or_get_record_requires_main_row(
    store: SessionControlStore,
) -> None:
    store.initialize_fence("active", 1)
    with pytest.raises(RuntimeError, match="main row"):
        store.create_or_get_thread_creation_record(
            **valid_thread_creation_kwargs(store)
        )


def test_create_or_get_record_missing_fence_raises_key_error(
    store: SessionControlStore,
) -> None:
    store.initialize_main_thread(make_thread_id(), DEFAULT_CREATED_AT)
    with pytest.raises(KeyError):
        store.create_or_get_thread_creation_record(
            **valid_thread_creation_kwargs(store)
        )


def test_create_or_get_record_delegation_unique(
    store: SessionControlStore,
) -> None:
    prepare_record(store, delegation_id="dlg-1")
    # 同 delegation 不同 key → 部分唯一约束拒绝
    kwargs = valid_thread_creation_kwargs(store)
    kwargs["idempotency_key"] = "key-2"
    kwargs["delegation_id"] = "dlg-1"
    with pytest.raises(RuntimeError, match="delegation_id"):
        store.create_or_get_thread_creation_record(**kwargs)  # type: ignore[arg-type]
    # 不同 delegation / None delegation 不受影响
    kwargs2 = valid_thread_creation_kwargs(store)
    kwargs2["idempotency_key"] = "key-3"
    kwargs2["delegation_id"] = "dlg-2"
    store.create_or_get_thread_creation_record(**kwargs2)  # type: ignore[arg-type]
    kwargs3 = valid_thread_creation_kwargs(store)
    kwargs3["idempotency_key"] = "key-4"
    kwargs3["delegation_id"] = None
    store.create_or_get_thread_creation_record(**kwargs3)  # type: ignore[arg-type]


def test_create_or_get_record_rejects_rebind_aborted_delegation(
    store: SessionControlStore,
) -> None:
    """aborted 的 delegation 不换绑：同 delegation_id、新 key 重新申请
    → RuntimeError，且不产生第二条 record（失败换新 delegation）。"""
    prepare_record(store, delegation_id="dlg-1")
    aborted = store.abort_thread_creation_record("key-1", "定点清理完成")
    assert aborted.state == "aborted"
    kwargs = valid_thread_creation_kwargs(store)
    kwargs["idempotency_key"] = "key-2"
    kwargs["delegation_id"] = "dlg-1"
    with pytest.raises(RuntimeError, match="delegation_id"):
        store.create_or_get_thread_creation_record(**kwargs)  # type: ignore[arg-type]
    # 不产生第二条 record（既有 aborted record 保持原状）
    rows = store.connection.execute(
        "SELECT COUNT(*) FROM thread_creation_records"
    ).fetchone()
    assert int(rows[0]) == 1
    assert store.get_thread_creation_record("key-1").state == "aborted"


def test_freeze_artifact_manifest_idempotent_then_conflict(
    store: SessionControlStore,
) -> None:
    prepare_record(store)
    manifest = json.dumps(
        {"thread.json": "a" * 64}, sort_keys=True, separators=(",", ":")
    )
    frozen = store.freeze_thread_creation_artifact_manifest(
        "key-1",
        artifact_manifest=manifest,
        artifact_manifest_hash="c" * 64,
    )
    assert frozen.artifact_manifest == manifest
    assert frozen.artifact_manifest_hash == "c" * 64
    # 幂等重入（同 manifest 同 hash）
    refrozen = store.freeze_thread_creation_artifact_manifest(
        "key-1",
        artifact_manifest=manifest,
        artifact_manifest_hash="c" * 64,
    )
    assert refrozen == frozen
    # 已冻结且不一致 → fail closed
    with pytest.raises(RuntimeError, match="已冻结"):
        store.freeze_thread_creation_artifact_manifest(
            "key-1",
            artifact_manifest=manifest,
            artifact_manifest_hash="d" * 64,
        )


def test_freeze_artifact_manifest_rejects_bad_inputs(
    store: SessionControlStore,
) -> None:
    prepare_record(store)
    with pytest.raises(ValueError, match="artifact_manifest"):
        store.freeze_thread_creation_artifact_manifest(
            "key-1", artifact_manifest="not-json", artifact_manifest_hash="c" * 64
        )
    bad_mapping = json.dumps({"a": 1}, sort_keys=True, separators=(",", ":"))
    with pytest.raises(ValueError, match="artifact_manifest"):
        store.freeze_thread_creation_artifact_manifest(
            "key-1", artifact_manifest=bad_mapping, artifact_manifest_hash="c" * 64
        )
    with pytest.raises(ValueError, match="artifact_manifest_hash"):
        store.freeze_thread_creation_artifact_manifest(
            "key-1",
            artifact_manifest=json.dumps(
                {"a": "b" * 64}, sort_keys=True, separators=(",", ":")
            ),
            artifact_manifest_hash="NOT-HEX",
        )
    with pytest.raises(KeyError):
        store.freeze_thread_creation_artifact_manifest(
            "key-missing", artifact_manifest="{}", artifact_manifest_hash="c" * 64
        )


def test_freeze_artifact_manifest_rejects_non_preparing(
    store: SessionControlStore,
) -> None:
    record = prepare_record(store)
    manifest = json.dumps({}, sort_keys=True, separators=(",", ":"))
    store.freeze_thread_creation_artifact_manifest(
        "key-1", artifact_manifest=manifest, artifact_manifest_hash="c" * 64
    )
    # 先正常发布，再对 published record 冻结 → 拒绝
    store.publish_thread_creation_record("key-1")
    with pytest.raises(RuntimeError, match="非 preparing"):
        store.freeze_thread_creation_artifact_manifest(
            "key-1", artifact_manifest=manifest, artifact_manifest_hash="c" * 64
        )
    assert record.child_thread_id


def test_publish_record_inserts_child_row_single_transaction(
    store: SessionControlStore,
) -> None:
    record = prepare_record(store)
    manifest = json.dumps({}, sort_keys=True, separators=(",", ":"))
    store.freeze_thread_creation_artifact_manifest(
        "key-1", artifact_manifest=manifest, artifact_manifest_hash="c" * 64
    )
    published = store.publish_thread_creation_record("key-1")
    assert published.state == "published"
    assert published.abort_reason is None
    # thread_catalog child row 可见（唯一可见性提交点）
    rows = store.connection.execute(
        "SELECT thread_id, kind, created_at FROM thread_catalog "
        "WHERE kind = 'child'"
    ).fetchall()
    assert len(rows) == 1
    assert str(rows[0]["thread_id"]) == record.child_thread_id
    assert str(rows[0]["created_at"]) == record.child_created_at
    # catalog revision 随 child 插入推进（1 → 2）
    assert store.get_thread_catalog_revision() == 2


def test_publish_child_row_and_record_state_single_transaction(
    store: SessionControlStore,
) -> None:
    """publish 单事务原子性：child row 与 record→published **同隐同现**。

    在 publish 事务内对 `UPDATE thread_creation_records` 语句挂 trace
    回调，用第二个 sqlite3 连接读取（WAL：读到最近一次已提交快照）。
    事务进行中二者必须同时不可见（child=0, state='preparing'）；COMMIT
    之后二者同时可见（child=1, state='published'）。把事务拆裂为
    「child row 先 COMMIT、record 后更新」或反之，都会被本用例检出。
    """
    record = prepare_record(store)
    manifest = json.dumps({}, sort_keys=True, separators=(",", ":"))
    store.freeze_thread_creation_artifact_manifest(
        "key-1", artifact_manifest=manifest, artifact_manifest_hash="c" * 64
    )
    observations: list[tuple[int, str]] = []
    reader = sqlite3.connect(store.database_path, isolation_level=None)
    try:
        def observe(statement: str) -> None:
            if "UPDATE thread_creation_records" not in statement:
                return
            child_count = int(
                reader.execute(
                    "SELECT COUNT(*) FROM thread_catalog WHERE kind = 'child'"
                ).fetchone()[0]
            )
            state_row = reader.execute(
                "SELECT state FROM thread_creation_records WHERE "
                "thread_creation_idempotency_key = 'key-1'"
            ).fetchone()
            observations.append((child_count, str(state_row[0])))

        store.connection.set_trace_callback(observe)
        try:
            published = store.publish_thread_creation_record("key-1")
        finally:
            store.connection.set_trace_callback(None)
    finally:
        reader.close()
    assert published.state == "published"
    # 观测列表非空（否则 trace 未命中，用例形同虚设）
    assert observations, "trace 未观测到 publish 的 record 更新语句"
    # 事务中：child row 与 record published 同时不可见
    assert set(observations) == {(0, "preparing")}
    # 提交后：同时可见
    child_rows = store.connection.execute(
        "SELECT COUNT(*) FROM thread_catalog WHERE kind = 'child'"
    ).fetchone()
    assert int(child_rows[0]) == 1
    assert store.get_thread_creation_record("key-1").state == "published"
    assert record.child_thread_id is not None


def test_publish_record_cas_fails_when_owner_fence_drifted(
    store: SessionControlStore,
) -> None:
    """CAS 1 漂移：fence 仍 active 但 generation 前进（本库 API 只在
    active→deleting 前进 generation，故该态只可能来自外部直改）→ publish
    拒绝、事务回滚，record 保持 preparing。"""
    prepare_record(store)
    manifest = json.dumps({}, sort_keys=True, separators=(",", ":"))
    store.freeze_thread_creation_artifact_manifest(
        "key-1", artifact_manifest=manifest, artifact_manifest_hash="c" * 64
    )
    # 只推进 generation、保持 active：record 冻结的是 generation=1。
    store.connection.execute(
        "UPDATE lifecycle_fence SET generation = 2 WHERE id = 1"
    )
    with pytest.raises(RuntimeError, match="owner fence 已漂移"):
        store.publish_thread_creation_record("key-1")
    # 失败回滚：无 child row、record 保持 preparing
    child_rows = store.connection.execute(
        "SELECT COUNT(*) FROM thread_catalog WHERE kind = 'child'"
    ).fetchone()
    assert int(child_rows[0]) == 0
    record = store.get_thread_creation_record("key-1")
    assert record.state == "preparing"
    # fence 未被 publish 改动
    assert store.get_fence() == ("active", 2)


def test_publish_record_rejected_when_owner_fence_deleting(
    store: SessionControlStore,
) -> None:
    """CAS 1 删除先行：fence 已 deleting → 统一 session_deletion_pending
    错误合同（不得降级为模糊 CAS 文案），事务回滚且 record 保持 preparing。"""
    prepare_record(store)
    manifest = json.dumps({}, sort_keys=True, separators=(",", ":"))
    store.freeze_thread_creation_artifact_manifest(
        "key-1", artifact_manifest=manifest, artifact_manifest_hash="c" * 64
    )
    assert store.cas_fence_transition(1, "deleting") is True
    with pytest.raises(
        SessionDeletionPendingError, match="session_deletion_pending"
    ):
        store.publish_thread_creation_record("key-1")
    child_rows = store.connection.execute(
        "SELECT COUNT(*) FROM thread_catalog WHERE kind = 'child'"
    ).fetchone()
    assert int(child_rows[0]) == 0
    assert store.get_thread_creation_record("key-1").state == "preparing"
    assert store.get_fence() == ("deleting", 2)


def test_publish_record_tolerates_sibling_growth_and_rejects_shrink(
    store: SessionControlStore,
) -> None:
    """CAS 2 收缩检测合同（2.3-A）：并发 sibling publish 的行数增长是
    合法交错，发布必须成功；行数收缩（外部直改/删除流残留）fail closed。
    """
    prepare_record(store)
    manifest = json.dumps({}, sort_keys=True, separators=(",", ":"))
    store.freeze_thread_creation_artifact_manifest(
        "key-1", artifact_manifest=manifest, artifact_manifest_hash="c" * 64
    )
    # 合法 sibling 增长（并发其它创建已发布的最小模拟）→ 发布成功
    sibling_id = make_thread_id()
    raw_execute(
        store,
        "INSERT INTO thread_catalog (thread_id, kind, created_at) "
        "VALUES (?, 'child', ?)",
        (sibling_id, DEFAULT_CREATED_AT.isoformat()),
    )
    published = store.publish_thread_creation_record("key-1")
    assert published.state == "published"
    # owner binding 字段槽随发布原子建立（2.1）
    binding = store.get_thread_owner_binding(published.child_thread_id)
    assert binding.final_relative_locator == published.final_relative_locator
    assert (binding.prefix_epoch, binding.prefix_epoch_reason) == (1, "initial")
    # 行数收缩（外部直改）→ 新 record 冻结当前更高 revision 后收缩，
    # CAS 2 先于 occupied 预检触发（收缩=外部改动，fail closed）。
    kwargs2 = valid_thread_creation_kwargs(store)
    kwargs2["idempotency_key"] = "key-2"
    record2 = store.create_or_get_thread_creation_record(**kwargs2)  # type: ignore[arg-type]
    assert record2.catalog_precondition_revision == 3  # main+published+sibling
    store.freeze_thread_creation_artifact_manifest(
        "key-2", artifact_manifest=manifest, artifact_manifest_hash="d" * 64
    )
    raw_execute(store, "DELETE FROM thread_catalog WHERE thread_id = ?", (sibling_id,))
    with pytest.raises(RuntimeError, match="precondition revision 已回退"):
        store.publish_thread_creation_record("key-2")
    assert store.get_thread_creation_record("key-2").state == "preparing"


def test_publish_record_requires_frozen_artifact_manifest(
    store: SessionControlStore,
) -> None:
    prepare_record(store)
    with pytest.raises(RuntimeError, match="尚未冻结"):
        store.publish_thread_creation_record("key-1")
    assert store.get_thread_creation_record("key-1").state == "preparing"


def test_publish_record_rejects_frozen_collaboration_drift(
    store: SessionControlStore,
) -> None:
    """R25 起 ledger 已落地：冻结 revision 与当前 revision 漂移即拒绝。"""
    prepare_record(store, collaboration_precondition_revision=5)
    manifest = json.dumps({}, sort_keys=True, separators=(",", ":"))
    store.freeze_thread_creation_artifact_manifest(
        "key-1", artifact_manifest=manifest, artifact_manifest_hash="c" * 64
    )
    with pytest.raises(RuntimeError, match="collaboration ledger revision"):
        store.publish_thread_creation_record("key-1")
    assert store.get_thread_creation_record("key-1").state == "preparing"


def test_publish_record_rejects_published_and_aborted(
    store: SessionControlStore,
) -> None:
    prepare_record(store)
    manifest = json.dumps({}, sort_keys=True, separators=(",", ":"))
    store.freeze_thread_creation_artifact_manifest(
        "key-1", artifact_manifest=manifest, artifact_manifest_hash="c" * 64
    )
    store.publish_thread_creation_record("key-1")
    # 已 published → 拒绝重复发布（重复 Job/重复发布防线）
    with pytest.raises(RuntimeError, match="拒绝重复发布"):
        store.publish_thread_creation_record("key-1")
    # 已 published 的 record 不可经 abort 回退为终态 aborted
    with pytest.raises(RuntimeError, match="不可撤销"):
        store.abort_thread_creation_record("key-1", "迟到取消")


def test_abort_record_preparing_to_aborted_then_idempotent(
    store: SessionControlStore,
) -> None:
    prepare_record(store)
    aborted = store.abort_thread_creation_record("key-1", "定点清理完成")
    assert aborted.state == "aborted"
    assert aborted.abort_reason == "定点清理完成"
    # 已 aborted 幂等返回（不覆盖原 reason）
    again = store.abort_thread_creation_record("key-1", "另一个原因")
    assert again.state == "aborted"
    assert again.abort_reason == "定点清理完成"
    # aborted 拒绝发布
    with pytest.raises(RuntimeError, match="已中止"):
        store.publish_thread_creation_record("key-1")


def test_abort_record_rejects_published(store: SessionControlStore) -> None:
    prepare_record(store)
    manifest = json.dumps({}, sort_keys=True, separators=(",", ":"))
    store.freeze_thread_creation_artifact_manifest(
        "key-1", artifact_manifest=manifest, artifact_manifest_hash="c" * 64
    )
    store.publish_thread_creation_record("key-1")
    with pytest.raises(RuntimeError, match="不可撤销"):
        store.abort_thread_creation_record("key-1", "迟到取消")
    assert store.get_thread_creation_record("key-1").state == "published"


# ----------------------------------------------------------------------
# get_published_child_thread_locator（已发布 child 的冻结 locator 解析）
# ----------------------------------------------------------------------


def _publish_child(store: SessionControlStore) -> ThreadCreationRecord:
    """初始化 main/fence 后发布一条 child record，返回 published record。"""
    prepare_record(store)
    store.freeze_thread_creation_artifact_manifest(
        "key-1", artifact_manifest="{}", artifact_manifest_hash="c" * 64
    )
    return store.publish_thread_creation_record("key-1")


def test_get_published_child_locator_returns_frozen_locator(
    store: SessionControlStore,
) -> None:
    """发布后按 thread_id 返回 record 冻结的 locator，叶名严格等于 thread_id。"""
    record = _publish_child(store)
    locator = store.get_published_child_thread_locator(record.child_thread_id)
    assert locator == record.final_relative_locator
    assert locator.endswith(f"/{record.child_thread_id}")


def test_get_published_child_locator_missing_raises_key_error(
    store: SessionControlStore,
) -> None:
    store.initialize_main_thread(make_thread_id(), DEFAULT_CREATED_AT)
    with pytest.raises(KeyError):
        store.get_published_child_thread_locator(make_thread_id())


def test_get_published_child_locator_rejects_main_row(
    store: SessionControlStore,
) -> None:
    """main row 不是 child，解析必须 fail closed。"""
    main_thread_id = make_thread_id()
    store.initialize_main_thread(main_thread_id, DEFAULT_CREATED_AT)
    with pytest.raises(RuntimeError, match="不是 child"):
        store.get_published_child_thread_locator(main_thread_id)


def test_get_published_child_locator_detects_created_at_drift(
    store: SessionControlStore,
) -> None:
    """child row 与 creation record 的 created_at 漂移（库被外部改动）→ 报错。

    物理定位必须同时受 thread_catalog 的可见性提交点与同一发布事务冻结
    的 thread_creation_records 约束；二者创建时间不一致即 fail closed。
    """
    record = _publish_child(store)
    raw_execute(
        store,
        "UPDATE thread_catalog SET created_at = ? WHERE thread_id = ?",
        ("2099-01-01T00:00:00+00:00", record.child_thread_id),
    )
    with pytest.raises(RuntimeError, match="与 creation record 不一致"):
        store.get_published_child_thread_locator(record.child_thread_id)


def test_get_published_child_locator_detects_locator_leaf_drift(
    store: SessionControlStore,
) -> None:
    """record 冻结 locator 的叶名与 child_thread_id 不一致 → 报错。"""
    record = _publish_child(store)
    drifted = record.final_relative_locator.rsplit("/", 1)[0] + "/" + make_thread_id()
    raw_execute(
        store,
        "UPDATE thread_creation_records SET final_relative_locator = ? "
        "WHERE thread_creation_idempotency_key = ?",
        (drifted, "key-1"),
    )
    with pytest.raises(RuntimeError, match="与 creation record 不一致"):
        store.get_published_child_thread_locator(record.child_thread_id)


def test_get_published_child_locator_rejects_unpublished_record(
    store: SessionControlStore,
) -> None:
    """preparing record 不出现在 thread_catalog，解析必须 KeyError。"""
    record = prepare_record(store)
    with pytest.raises(KeyError):
        store.get_published_child_thread_locator(record.child_thread_id)
    assert store.get_thread_creation_record("key-1").state == "preparing"


def test_mark_published_idempotent_verifies_catalog_row(
    store: SessionControlStore,
) -> None:
    record = prepare_record(store)
    manifest = json.dumps({}, sort_keys=True, separators=(",", ":"))
    store.freeze_thread_creation_artifact_manifest(
        "key-1", artifact_manifest=manifest, artifact_manifest_hash="c" * 64
    )
    # preparing 时拒绝（须经 publish 推进）
    with pytest.raises(RuntimeError, match="仍 preparing"):
        store.mark_thread_creation_published("key-1")
    store.publish_thread_creation_record("key-1")
    marked = store.mark_thread_creation_published("key-1")
    assert marked.state == "published"
    assert marked.child_thread_id == record.child_thread_id
    # catalog child row 被外部删除 → fail closed
    raw_execute(
        store,
        "DELETE FROM thread_catalog WHERE thread_id = ?",
        (record.child_thread_id,),
    )
    with pytest.raises(RuntimeError, match="child row 缺失"):
        store.mark_thread_creation_published("key-1")


def test_mark_published_rejects_aborted_and_missing(
    store: SessionControlStore,
) -> None:
    prepare_record(store)
    store.abort_thread_creation_record("key-1", "取消")
    with pytest.raises(RuntimeError, match="已中止"):
        store.mark_thread_creation_published("key-1")
    with pytest.raises(KeyError):
        store.mark_thread_creation_published("key-missing")


def test_initial_execution_intent_roundtrip_and_guards(
    store: SessionControlStore,
) -> None:
    record = prepare_record(store)
    manifest = json.dumps({}, sort_keys=True, separators=(",", ":"))
    store.freeze_thread_creation_artifact_manifest(
        "key-1", artifact_manifest=manifest, artifact_manifest_hash="c" * 64
    )
    owner_session_id = make_session_id()
    # record 未 published → 拒绝
    with pytest.raises(RuntimeError, match="尚未 published"):
        store.create_or_get_initial_execution_intent(
            admission_idempotency_key="key-1",
            session_id=owner_session_id,
            thread_id=record.child_thread_id,
            initial_state="running",
            creation_idempotency_key="key-1",
        )
    store.publish_thread_creation_record("key-1")
    intent = store.create_or_get_initial_execution_intent(
        admission_idempotency_key="key-1",
        session_id=owner_session_id,
        thread_id=record.child_thread_id,
        initial_state="running",
        creation_idempotency_key="key-1",
    )
    assert intent.state == "pending"
    assert intent.thread_id == record.child_thread_id
    assert intent.initial_state == "running"
    assert intent.creation_idempotency_key == "key-1"
    # 幂等重入（同身份）
    again = store.create_or_get_initial_execution_intent(
        admission_idempotency_key="key-1",
        session_id=owner_session_id,
        thread_id=record.child_thread_id,
        initial_state="running",
        creation_idempotency_key="key-1",
    )
    assert again == intent
    # 同 thread 不同 admission key → 唯一索引拒绝
    with pytest.raises(RuntimeError, match="唯一索引"):
        store.create_or_get_initial_execution_intent(
            admission_idempotency_key="key-2",
            session_id=owner_session_id,
            thread_id=record.child_thread_id,
            initial_state="running",
            creation_idempotency_key="key-1",
        )
    # initial_state 与 creation record 冻结值不一致 → record 守卫先拒绝
    with pytest.raises(RuntimeError, match="冻结值不一致"):
        store.create_or_get_initial_execution_intent(
            admission_idempotency_key="key-1",
            session_id=owner_session_id,
            thread_id=record.child_thread_id,
            initial_state="idle",
            creation_idempotency_key="key-1",
        )
    # 同 admission key 不同身份（session_id 漂移；record 守卫已过）→ 冲突拒绝
    with pytest.raises(RuntimeError, match="幂等冲突"):
        store.create_or_get_initial_execution_intent(
            admission_idempotency_key="key-1",
            session_id=make_session_id(),
            thread_id=record.child_thread_id,
            initial_state="running",
            creation_idempotency_key="key-1",
        )
    # thread_id 与 record child 不一致 → 拒绝
    with pytest.raises(RuntimeError, match="不一致"):
        store.create_or_get_initial_execution_intent(
            admission_idempotency_key="key-3",
            session_id=make_session_id(),
            thread_id=make_thread_id(),
            initial_state="running",
            creation_idempotency_key="key-1",
        )
    # 读取与缺失
    fetched = store.get_initial_execution_intent("key-1")
    assert fetched == intent
    with pytest.raises(KeyError):
        store.get_initial_execution_intent("key-missing")


def test_get_thread_catalog_revision(store: SessionControlStore) -> None:
    store.initialize_main_thread(make_thread_id(), DEFAULT_CREATED_AT)
    assert store.get_thread_catalog_revision() == 1
    raw_execute(
        store,
        "INSERT INTO thread_catalog (thread_id, kind, created_at) "
        "VALUES (?, 'child', ?)",
        (make_thread_id(), DEFAULT_CREATED_AT.isoformat()),
    )
    assert store.get_thread_catalog_revision() == 2


# ----------------------------------------------------------------------
# v2→v3 升级与 8.5-B intent 消费状态机
# ----------------------------------------------------------------------

_V2_THREAD_EXECUTION_INTENTS_DDL = """
CREATE TABLE thread_execution_intents (
    admission_idempotency_key TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    creation_idempotency_key TEXT NOT NULL,
    initial_state TEXT NOT NULL CHECK (initial_state IN ('running', 'idle')),
    state TEXT NOT NULL CHECK (state IN ('pending', 'bound')),
    intent_created_at TEXT NOT NULL,
    intent_updated_at TEXT NOT NULL
)
"""

_V2_INTENT_CREATED_AT = "2026-06-01T12:00:00+00:00"
_V2_INTENT_UPDATED_AT = "2026-06-01T12:00:01+00:00"


def build_v2_database(target: Path, main_thread_id: str) -> None:
    """构造 R20 v2 形态库（main row + fence + v2 intents 表 + 索引）。

    thread_creation_records 表由 ``_initialize`` 的 ``CREATE ... IF
    NOT EXISTS`` 幂等补建，不影响 intents 升级路径，测试不重复其 DDL。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(target)
    try:
        connection.execute(
            "CREATE TABLE thread_catalog ("
            "thread_id TEXT PRIMARY KEY, "
            "kind TEXT NOT NULL CHECK (kind IN ('main', 'child')), "
            "created_at TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE lifecycle_fence ("
            "id INTEGER PRIMARY KEY CHECK (id = 1), "
            "state TEXT NOT NULL CHECK (state IN ('active', 'deleting')), "
            "generation INTEGER NOT NULL)"
        )
        connection.execute(_V2_THREAD_EXECUTION_INTENTS_DDL)
        connection.execute(
            "CREATE UNIQUE INDEX idx_thread_execution_intent_thread "
            "ON thread_execution_intents(thread_id)"
        )
        connection.execute(
            "INSERT INTO thread_catalog (thread_id, kind, created_at) "
            "VALUES (?, 'main', ?)",
            (main_thread_id, DEFAULT_CREATED_AT.isoformat()),
        )
        connection.execute(
            "INSERT INTO lifecycle_fence (id, state, generation) "
            "VALUES (1, 'active', 1)"
        )
        connection.execute("PRAGMA user_version = 2")
        connection.commit()
    finally:
        connection.close()


def insert_v2_intent(
    target: Path,
    *,
    admission_key: str = "key-1",
    session_id: str | None = None,
    thread_id: str | None = None,
    state: str = "pending",
) -> tuple[str, str]:
    """向 v2 库插入一条 intent 行，返回 (session_id, thread_id)。"""
    resolved_session = session_id or make_session_id()
    resolved_thread = thread_id or make_thread_id()
    connection = sqlite3.connect(target)
    try:
        connection.execute(
            "INSERT INTO thread_execution_intents "
            "(admission_idempotency_key, session_id, thread_id, "
            "creation_idempotency_key, initial_state, state, "
            "intent_created_at, intent_updated_at) "
            "VALUES (?, ?, ?, ?, 'running', ?, ?, ?)",
            (
                admission_key,
                resolved_session,
                resolved_thread,
                admission_key,
                state,
                _V2_INTENT_CREATED_AT,
                _V2_INTENT_UPDATED_AT,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return resolved_session, resolved_thread


def test_v2_database_upgrades_to_v3_with_stable_identity(
    tmp_path: Path,
) -> None:
    """v2 pending 行升级：确定性补齐 identity/preimage，时间戳零丢失。"""
    target = tmp_path / "session-control.sqlite"
    build_v2_database(target, make_thread_id())
    session_id, thread_id = insert_v2_intent(target)
    store = SessionControlStore(target)
    try:
        version = int(
            store.connection.execute("PRAGMA user_version").fetchone()[0]
        )
        assert version == 7
        intent = store.get_initial_execution_intent("key-1")
        binding_id, job_id = derive_initial_execution_identity("key-1")
        assert intent.state == "pending"
        assert intent.execution_binding_id == binding_id
        assert intent.job_id == job_id
        assert intent.binding_preimage_hash == (
            compute_initial_execution_binding_preimage_hash(
                admission_idempotency_key="key-1",
                session_id=session_id,
                thread_id=thread_id,
                creation_idempotency_key="key-1",
                initial_state="running",
                execution_binding_id=binding_id,
                job_id=job_id,
            )
        )
        # v2 无 claim/错误语义：迁移为 NULL（等待 worker 领取）。
        assert intent.claim_owner is None
        assert intent.claim_generation is None
        assert intent.last_error is None
        # 迁移零丢失：原时间戳逐字段保留。
        assert intent.intent_created_at == _V2_INTENT_CREATED_AT
        assert intent.intent_updated_at == _V2_INTENT_UPDATED_AT
    finally:
        store.close()


def test_v3_migration_identity_is_deterministic_across_databases(
    tmp_path: Path,
) -> None:
    """同 identity 的 v2 库在不同路径升级得到同一稳定 identity；重开幂等。"""
    session_id = make_session_id()
    thread_id = make_thread_id()
    identities: list[tuple[str, str]] = []
    for index in range(2):
        target = tmp_path / f"db-{index}" / "session-control.sqlite"
        build_v2_database(target, make_thread_id())
        insert_v2_intent(
            target, session_id=session_id, thread_id=thread_id
        )
        store = SessionControlStore(target)
        try:
            intent = store.get_initial_execution_intent("key-1")
            identities.append(
                (intent.execution_binding_id, intent.job_id)
            )
        finally:
            store.close()
    assert identities[0] == identities[1]
    # 重开幂等：identity 不再变化。
    target = tmp_path / "db-0" / "session-control.sqlite"
    reopened = SessionControlStore(target)
    try:
        intent = reopened.get_initial_execution_intent("key-1")
        assert (intent.execution_binding_id, intent.job_id) == identities[0]
    finally:
        reopened.close()


def test_v2_migration_rejects_bound_row_fail_closed(tmp_path: Path) -> None:
    """v2 库存在 state='bound' 行（从未被合法写入）→ 迁移 fail closed。"""
    target = tmp_path / "session-control.sqlite"
    build_v2_database(target, make_thread_id())
    insert_v2_intent(target, state="bound")
    with pytest.raises(RuntimeError, match="非 pending 行"):
        SessionControlStore(target)


def prepare_published_intent(
    store: SessionControlStore,
    admission_key: str = "key-1",
) -> ThreadExecutionIntent:
    """测试辅助：published record + 冻结身份的初始 execution intent。"""
    # main/fence 只初始化一次（同库可复用，支持同测试多条 record）。
    try:
        store.get_main_thread()
    except KeyError:
        store.initialize_main_thread(make_thread_id(), DEFAULT_CREATED_AT)
        store.initialize_fence("active", 1)
    kwargs = valid_thread_creation_kwargs(store)
    kwargs["idempotency_key"] = admission_key
    record = store.create_or_get_thread_creation_record(**kwargs)  # type: ignore[arg-type]
    store.freeze_thread_creation_artifact_manifest(
        admission_key,
        artifact_manifest="{}",
        artifact_manifest_hash="c" * 64,
    )
    store.publish_thread_creation_record(admission_key)
    return store.create_or_get_initial_execution_intent(
        admission_idempotency_key=admission_key,
        session_id=make_session_id(),
        thread_id=record.child_thread_id,
        initial_state="running",
        creation_idempotency_key=admission_key,
    )


def test_list_pending_intents_reads_state_index_only(
    store: SessionControlStore,
) -> None:
    """pending 列表：空 → 含新 intent → bound 后消失（纯状态索引）。"""
    assert store.list_pending_initial_execution_intents() == ()
    intent = prepare_published_intent(store)
    pending = store.list_pending_initial_execution_intents()
    assert [row.admission_idempotency_key for row in pending] == ["key-1"]
    # claim 不改变 pending（claim 是消费租约，不是终态）。
    store.claim_initial_execution_intent(
        "key-1", claim_owner="worker-a", claim_generation=1
    )
    assert len(store.list_pending_initial_execution_intents()) == 1
    store.mark_initial_execution_bound(
        "key-1",
        execution_binding_id=intent.execution_binding_id,
        job_id=intent.job_id,
        claim_owner="worker-a",
        claim_generation=1,
    )
    assert store.list_pending_initial_execution_intents() == ()


def test_claim_semantics_idempotent_generation_and_conflict(
    store: SessionControlStore,
) -> None:
    """claim：幂等重入、generation CAS 只增、异 owner 冲突 fail closed。"""
    prepare_published_intent(store)
    claimed = store.claim_initial_execution_intent(
        "key-1", claim_owner="worker-a", claim_generation=1
    )
    assert claimed.claim_owner == "worker-a"
    assert claimed.claim_generation == 1
    assert claimed.state == "pending"
    # 相同 claim 幂等（字段完全不变）。
    again = store.claim_initial_execution_intent(
        "key-1", claim_owner="worker-a", claim_generation=1
    )
    assert again == claimed
    # 不同 owner → 冲突 fail closed。
    with pytest.raises(RuntimeError, match="其他 claim"):
        store.claim_initial_execution_intent(
            "key-1", claim_owner="worker-b", claim_generation=1
        )
    # 同 owner 更高 generation → CAS 接管（恢复 owner 语义）。
    advanced = store.claim_initial_execution_intent(
        "key-1", claim_owner="worker-a", claim_generation=3
    )
    assert advanced.claim_generation == 3
    # 同 owner 更低 generation → 过期拒绝。
    with pytest.raises(RuntimeError, match="过期"):
        store.claim_initial_execution_intent(
            "key-1", claim_owner="worker-a", claim_generation=1
        )
    # 缺失 intent → KeyError；非法字段 → ValueError。
    with pytest.raises(KeyError):
        store.claim_initial_execution_intent(
            "key-missing", claim_owner="worker-a", claim_generation=1
        )
    with pytest.raises(ValueError):
        store.claim_initial_execution_intent(
            "key-1", claim_owner="", claim_generation=1
        )
    with pytest.raises(ValueError):
        store.claim_initial_execution_intent(
            "key-1", claim_owner="worker-a", claim_generation=True
        )


def test_claim_rejects_non_pending_intent(store: SessionControlStore) -> None:
    """intent 已 bound 后不可再领取（claim 只对 pending 有效）。"""
    intent = prepare_published_intent(store)
    store.claim_initial_execution_intent(
        "key-1", claim_owner="worker-a", claim_generation=1
    )
    store.mark_initial_execution_bound(
        "key-1",
        execution_binding_id=intent.execution_binding_id,
        job_id=intent.job_id,
        claim_owner="worker-a",
        claim_generation=1,
    )
    with pytest.raises(RuntimeError, match="非 pending"):
        store.claim_initial_execution_intent(
            "key-1", claim_owner="worker-b", claim_generation=1
        )


def test_mark_bound_cas_and_drift_rejection(store: SessionControlStore) -> None:
    """mark bound：需持 claim、identity 漂移拒绝、重复相同提交幂等。"""
    intent = prepare_published_intent(store)
    binding_id = intent.execution_binding_id
    job_id = intent.job_id
    # 未领取 → 拒绝。
    with pytest.raises(RuntimeError, match="未被领取"):
        store.mark_initial_execution_bound(
            "key-1",
            execution_binding_id=binding_id,
            job_id=job_id,
            claim_owner="worker-a",
            claim_generation=1,
        )
    store.claim_initial_execution_intent(
        "key-1", claim_owner="worker-a", claim_generation=1
    )
    # claim 不符 → 拒绝。
    with pytest.raises(RuntimeError, match="claim 不一致"):
        store.mark_initial_execution_bound(
            "key-1",
            execution_binding_id=binding_id,
            job_id=job_id,
            claim_owner="worker-b",
            claim_generation=1,
        )
    # binding_id 漂移 → 拒绝（pending 不被推进）。
    with pytest.raises(RuntimeError, match="漂移"):
        store.mark_initial_execution_bound(
            "key-1",
            execution_binding_id="tbind_" + "0" * 32,
            job_id=job_id,
            claim_owner="worker-a",
            claim_generation=1,
        )
    assert store.get_initial_execution_intent("key-1").state == "pending"
    # job_id 漂移 → 拒绝。
    with pytest.raises(RuntimeError, match="漂移"):
        store.mark_initial_execution_bound(
            "key-1",
            execution_binding_id=binding_id,
            job_id="job_" + "0" * 32,
            claim_owner="worker-a",
            claim_generation=1,
        )
    # 正确提交 → CAS bound。
    bound = store.mark_initial_execution_bound(
        "key-1",
        execution_binding_id=binding_id,
        job_id=job_id,
        claim_owner="worker-a",
        claim_generation=1,
    )
    assert bound.state == "bound"
    assert bound.execution_binding_id == binding_id
    assert bound.job_id == job_id
    assert bound.claim_owner == "worker-a"
    # 重复相同提交幂等。
    assert (
        store.mark_initial_execution_bound(
            "key-1",
            execution_binding_id=binding_id,
            job_id=job_id,
            claim_owner="worker-a",
            claim_generation=1,
        )
        == bound
    )
    # bound 后 identity 漂移 → 明确报错。
    with pytest.raises(RuntimeError, match="漂移"):
        store.mark_initial_execution_bound(
            "key-1",
            execution_binding_id=binding_id,
            job_id="job_" + "0" * 32,
            claim_owner="worker-a",
            claim_generation=1,
        )


def test_mark_bound_rejects_preimage_tamper(store: SessionControlStore) -> None:
    """binding_preimage_hash 被外部改动 → mark bound 复算失败 fail closed。"""
    intent = prepare_published_intent(store)
    store.claim_initial_execution_intent(
        "key-1", claim_owner="worker-a", claim_generation=1
    )
    raw_execute(
        store,
        "UPDATE thread_execution_intents SET binding_preimage_hash = ? "
        "WHERE admission_idempotency_key = ?",
        ("d" * 64, "key-1"),
    )
    with pytest.raises(RuntimeError, match="preimage 复算失败"):
        store.mark_initial_execution_bound(
            "key-1",
            execution_binding_id=intent.execution_binding_id,
            job_id=intent.job_id,
            claim_owner="worker-a",
            claim_generation=1,
        )
    assert store.get_initial_execution_intent("key-1").state == "pending"


def test_record_failure_keeps_pending_recoverable(
    store: SessionControlStore,
) -> None:
    """record failure：只记 last_error，保持 pending + claim 可恢复。"""
    intent = prepare_published_intent(store)
    store.claim_initial_execution_intent(
        "key-1", claim_owner="worker-a", claim_generation=1
    )
    failed = store.record_initial_execution_failure(
        "key-1",
        claim_owner="worker-a",
        claim_generation=1,
        last_error="binder boom: graph unavailable",
    )
    assert failed.state == "pending"
    assert failed.last_error == "binder boom: graph unavailable"
    assert failed.claim_owner == "worker-a"
    assert failed.claim_generation == 1
    # pending 索引仍包含该 intent。
    assert [
        row.admission_idempotency_key
        for row in store.list_pending_initial_execution_intents()
    ] == ["key-1"]
    # 同 claim 幂等重入可恢复：直接 mark bound 成功，last_error 清空。
    bound = store.mark_initial_execution_bound(
        "key-1",
        execution_binding_id=intent.execution_binding_id,
        job_id=intent.job_id,
        claim_owner="worker-a",
        claim_generation=1,
    )
    assert bound.state == "bound"
    assert bound.last_error is None
    # bound 后无失败可记录；claim 不符拒绝；空错误拒绝；缺失 KeyError。
    with pytest.raises(RuntimeError, match="非 pending"):
        store.record_initial_execution_failure(
            "key-1",
            claim_owner="worker-a",
            claim_generation=1,
            last_error="late",
        )
    prepare_published_intent(store, "key-2")
    with pytest.raises(RuntimeError, match="claim 不一致"):
        store.record_initial_execution_failure(
            "key-2",
            claim_owner="worker-b",
            claim_generation=1,
            last_error="wrong owner",
        )
    with pytest.raises(ValueError):
        store.record_initial_execution_failure(
            "key-2",
            claim_owner="worker-b",
            claim_generation=1,
            last_error="",
        )
    with pytest.raises(KeyError):
        store.record_initial_execution_failure(
            "key-missing",
            claim_owner="worker-a",
            claim_generation=1,
            last_error="missing",
        )

def test_collaboration_member_register_idempotent_and_conflict(
    store: SessionControlStore,
) -> None:
    """member 登记：revision 只在新登记推进；同 delegation 漂移/取消换绑拒绝。"""
    store.initialize_main_thread(make_thread_id(), DEFAULT_CREATED_AT)
    store.initialize_fence("active", 1)
    assert store.get_collaboration_ledger_revision() == 0
    first = store.register_collaboration_member(
        delegation_id="del-1",
        coordinator_session_id=make_session_id(),
        coordinator_thread_id=make_thread_id(),
        role="delegated_subagent",
        subagent_type="general-purpose",
        title="委派：做一件事",
        task_seed=json.dumps(
            {"description": "做一件事"},
            sort_keys=True,
            separators=(",", ":"),
        ),
    )
    assert first == 1
    member = store.get_collaboration_member("del-1")
    assert member.state == "registering"
    assert member.child_thread_id is None
    # 同 delegation 同内容重入 → 幂等（revision 不变）。
    again = store.register_collaboration_member(
        delegation_id="del-1",
        coordinator_session_id=member.coordinator_session_id,
        coordinator_thread_id=member.coordinator_thread_id,
        role=member.role,
        subagent_type=member.subagent_type,
        title=member.title,
        task_seed=member.task_seed,
    )
    assert again == 1
    # 新 delegation → revision +1。
    second = store.register_collaboration_member(
        delegation_id="del-2",
        coordinator_session_id=member.coordinator_session_id,
        coordinator_thread_id=member.coordinator_thread_id,
        role=member.role,
        subagent_type=member.subagent_type,
        title="委派：另一件事",
        task_seed=json.dumps(
            {"description": "另一件事"},
            sort_keys=True,
            separators=(",", ":"),
        ),
    )
    assert second == 2
    # 同 delegation 内容漂移 → 冲突拒绝。
    with pytest.raises(RuntimeError, match="拒绝换绑"):
        store.register_collaboration_member(
            delegation_id="del-1",
            coordinator_session_id=member.coordinator_session_id,
            coordinator_thread_id=member.coordinator_thread_id,
            role=member.role,
            subagent_type="other-type",
            title=member.title,
            task_seed=member.task_seed,
        )
    # 列表按 coordinator 过滤。
    members = store.list_collaboration_members(
        coordinator_session_id=member.coordinator_session_id
    )
    assert [item.delegation_id for item in members] == ["del-1", "del-2"]
    assert store.list_collaboration_members() == members
    with pytest.raises(KeyError):
        store.get_collaboration_member("del-missing")


def test_delegated_record_publish_cas_and_member_atomic_visibility(
    store: SessionControlStore,
) -> None:
    """delegated record：ledger CAS + member 随 publish 原子转正回填。"""
    store.initialize_main_thread(make_thread_id(), DEFAULT_CREATED_AT)
    store.initialize_fence("active", 1)
    coordinator_session_id = make_session_id()
    coordinator_thread_id = make_thread_id()
    revision = store.register_collaboration_member(
        delegation_id="del-1",
        coordinator_session_id=coordinator_session_id,
        coordinator_thread_id=coordinator_thread_id,
        role="delegated_subagent",
        subagent_type="general-purpose",
        title="委派：做一件事",
        task_seed=json.dumps(
            {"description": "做一件事"}, sort_keys=True, separators=(",", ":")
        ),
    )
    kwargs = valid_thread_creation_kwargs(store)
    record = store.create_or_get_thread_creation_record(
        **kwargs,
        delegation_id="del-1",
        collaboration_precondition_revision=revision,
    )
    store.freeze_thread_creation_artifact_manifest(
        "key-1", artifact_manifest="{}", artifact_manifest_hash="c" * 64
    )
    # publish 前 API 不可见：member 仍 registering、无 child_thread_id。
    member = store.get_collaboration_member("del-1")
    assert member.state == "registering"
    assert member.child_thread_id is None
    # ledger 漂移（其他 delegation 登记）→ publish CAS fail closed，
    # record 保持 preparing、无 child row 可见性。
    store.register_collaboration_member(
        delegation_id="del-2",
        coordinator_session_id=coordinator_session_id,
        coordinator_thread_id=coordinator_thread_id,
        role="delegated_subagent",
        subagent_type="general-purpose",
        title="委派：另一件事",
        task_seed=json.dumps(
            {"description": "另一件事"}, sort_keys=True, separators=(",", ":")
        ),
    )
    with pytest.raises(RuntimeError, match="collaboration ledger revision"):
        store.publish_thread_creation_record("key-1")
    assert store.get_thread_creation_record("key-1").state == "preparing"
    visible = store.connection.execute(
        "SELECT 1 FROM thread_catalog WHERE thread_id = ?",
        (record.child_thread_id,),
    ).fetchone()
    assert visible is None
    # 成功路径：新 operation（新 delegation 先登记，再以登记后 revision
    # 冻结 record）正常发布，member 与 catalog child row 原子可见。
    revision_now = store.register_collaboration_member(
        delegation_id="del-3",
        coordinator_session_id=coordinator_session_id,
        coordinator_thread_id=coordinator_thread_id,
        role="delegated_subagent",
        subagent_type="general-purpose",
        title="委派：第三件事",
        task_seed=json.dumps(
            {"description": "第三件事"}, sort_keys=True, separators=(",", ":")
        ),
    )
    kwargs2 = valid_thread_creation_kwargs(store)
    kwargs2["idempotency_key"] = "key-2"
    record2 = store.create_or_get_thread_creation_record(
        **kwargs2,
        delegation_id="del-3",
        collaboration_precondition_revision=revision_now,
    )
    store.freeze_thread_creation_artifact_manifest(
        "key-2", artifact_manifest="{}", artifact_manifest_hash="c" * 64
    )
    store.publish_thread_creation_record("key-2")
    member3 = store.get_collaboration_member("del-3")
    assert member3.state == "published"
    assert member3.child_thread_id == record2.child_thread_id


def test_abort_record_cancels_collaboration_member_no_rebind(
    store: SessionControlStore,
) -> None:
    """record abort 定点取消 member；同 delegation 重登记拒绝（不换绑）。"""
    store.initialize_main_thread(make_thread_id(), DEFAULT_CREATED_AT)
    store.initialize_fence("active", 1)
    coordinator_session_id = make_session_id()
    revision = store.register_collaboration_member(
        delegation_id="del-1",
        coordinator_session_id=coordinator_session_id,
        coordinator_thread_id=make_thread_id(),
        role="delegated_subagent",
        subagent_type="general-purpose",
        title="委派：做一件事",
        task_seed=json.dumps(
            {"description": "做一件事"}, sort_keys=True, separators=(",", ":")
        ),
    )
    kwargs = valid_thread_creation_kwargs(store)
    store.create_or_get_thread_creation_record(
        **kwargs,
        delegation_id="del-1",
        collaboration_precondition_revision=revision,
    )
    store.abort_thread_creation_record("key-1", "publish CAS 失败")
    member = store.get_collaboration_member("del-1")
    assert member.state == "cancelled"
    # 同 delegation 重登记 → 换绑拒绝（重试须换新 delegation）。
    with pytest.raises(RuntimeError, match="不换绑"):
        store.register_collaboration_member(
            delegation_id="del-1",
            coordinator_session_id=coordinator_session_id,
            coordinator_thread_id=member.coordinator_thread_id,
            role=member.role,
            subagent_type=member.subagent_type,
            title=member.title,
            task_seed=member.task_seed,
        )

# ----------------------------------------------------------------------
# thread owner binding 字段槽（2.1，B1/B3）
# ----------------------------------------------------------------------

def _sha(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode()).hexdigest()

def test_owner_binding_ensure_get_roundtrip(tmp_path: Path) -> None:
    store = SessionControlStore(tmp_path / "c.sqlite")
    try:
        thread_id = make_thread_id()
        locator = "threads/2026/06/01/" + thread_id
        first = store.ensure_thread_owner_binding(
            thread_id=thread_id, final_relative_locator=locator
        )
        assert first.final_relative_locator == locator
        assert (first.prefix_epoch, first.prefix_epoch_reason) == (1, "initial")
        assert first.attachment_refs == ()
        # 幂等：同 locator 返回同一行
        second = store.ensure_thread_owner_binding(
            thread_id=thread_id, final_relative_locator=locator
        )
        assert second == first
        # locator 漂移 fail closed
        with pytest.raises(RuntimeError, match="locator 不一致"):
            store.ensure_thread_owner_binding(
                thread_id=thread_id,
                final_relative_locator="threads/2026/06/02/" + make_thread_id(),
            )
        # 非法 thread_id 拒绝
        with pytest.raises(ValueError):
            store.ensure_thread_owner_binding(
                thread_id="thr_" + "g" * 32, final_relative_locator=""
            )
    finally:
        store.close()

def test_owner_binding_update_scalar_and_append(tmp_path: Path) -> None:
    store = SessionControlStore(tmp_path / "c.sqlite")
    try:
        thread_id = make_thread_id()
        store.ensure_thread_owner_binding(thread_id=thread_id)
        updated = store.update_thread_owner_binding(
            thread_id,
            prefix_epoch=2,
            prefix_epoch_reason="compaction",
            stable_prefix_hash=_sha("prefix"),
            stable_prefix_length=128,
            desired_toolset_revision=3,
            applied_toolset_revision=2,
            toolset_compatibility_key="toolset-compat:v7",
            policy_compatibility_key="policy-compat:v1",
            resource_activation_snapshot_ref="activation:rev-11",
            assembly_parent_ref="assembly:parent-rev-4",
            mutation_provenance={
                "actor_kind": "execution",
                "actor_ref": "job_" + uuid.uuid4().hex,
                "mutated_at": "2026-06-01T12:00:00+00:00",
            },
            append_attachment_ref={"attachment_id": "att-1", "revision": 2},
            append_resource_ref={
                "resource_id": "res-1",
                "display_uri": "boxteam://memory/session/notes",
                "revision": 5,
            },
        )
        assert (updated.prefix_epoch, updated.prefix_epoch_reason) == (2, "compaction")
        assert updated.stable_prefix_length == 128
        assert updated.desired_toolset_revision == 3
        assert updated.applied_toolset_revision == 2
        assert updated.resource_activation_snapshot_ref == "activation:rev-11"
        assert updated.mutation_provenance is not None
        assert updated.mutation_provenance["actor_kind"] == "execution"
        assert len(updated.attachment_refs) == 1
        assert len(updated.resource_refs) == 1
        assert updated.revision == 2
        # CAS：旧 revision 拒绝
        with pytest.raises(RuntimeError, match="revision CAS 失败"):
            store.update_thread_owner_binding(
                thread_id, expected_revision=1, applied_toolset_revision=9
            )
        # 成对约束
        with pytest.raises(ValueError, match="成对提供"):
            store.update_thread_owner_binding(thread_id, prefix_epoch=3)
        with pytest.raises(ValueError, match="成对提供"):
            store.update_thread_owner_binding(
                thread_id, stable_prefix_hash=_sha("x"), stable_prefix_length=None
            )
        # epoch reason 闭集
        with pytest.raises(ValueError, match="prefix_epoch_reason 非法"):
            store.update_thread_owner_binding(
                thread_id, prefix_epoch=3, prefix_epoch_reason="hack"
            )
    finally:
        store.close()

def test_owner_binding_negative_contract(tmp_path: Path) -> None:
    """2.1 负面合同：物理路径/相对路径片段/凭据形态键拒绝落盘。"""
    store = SessionControlStore(tmp_path / "c.sqlite")
    try:
        thread_id = make_thread_id()
        store.ensure_thread_owner_binding(thread_id=thread_id)
        with pytest.raises(ValueError, match="绝对路径形态"):
            store.update_thread_owner_binding(
                thread_id, resource_activation_snapshot_ref="/workspace/.boxteam/x"
            )
        with pytest.raises(ValueError, match="相对路径片段"):
            store.update_thread_owner_binding(
                thread_id, toolset_compatibility_key="a/../b"
            )
        with pytest.raises(ValueError, match="凭据形态"):
            store.update_thread_owner_binding(
                thread_id,
                append_resource_ref={"api_key": "sk-xxx", "revision": 1},
            )
        with pytest.raises(TypeError, match="必须是标量"):
            store.update_thread_owner_binding(
                thread_id, append_variant_ref={"nested": {"a": 1}},
            )
        with pytest.raises(ValueError, match="键闭集"):
            store.update_thread_owner_binding(
                thread_id,
                mutation_provenance={"actor_kind": "execution", "extra": 1},
            )
        # sha256 形态
        with pytest.raises(ValueError, match="sha256"):
            store.update_thread_owner_binding(
                thread_id,
                stable_prefix_hash="nothex",
                stable_prefix_length=1,
            )
    finally:
        store.close()


def test_owner_binding_missing_thread_raises_key_error(tmp_path: Path) -> None:
    """get/update 对不存在的 thread_id 抛 KeyError（fail closed）。"""
    store = SessionControlStore(tmp_path / "c.sqlite")
    try:
        missing = make_thread_id()
        with pytest.raises(KeyError, match="不存在"):
            store.get_thread_owner_binding(missing)
        with pytest.raises(KeyError, match="不存在"):
            store.update_thread_owner_binding(
                missing, prefix_epoch=2, prefix_epoch_reason="compaction"
            )
    finally:
        store.close()


def test_owner_binding_ref_and_parameter_guards(tmp_path: Path) -> None:
    """引用槽/参数下界：空串、NUL、空条目键、epoch/length 下界、空更新。"""
    store = SessionControlStore(tmp_path / "c.sqlite")
    try:
        thread_id = make_thread_id()
        store.ensure_thread_owner_binding(thread_id=thread_id)
        with pytest.raises(ValueError, match="必须是非空字符串"):
            store.update_thread_owner_binding(
                thread_id, toolset_compatibility_key=""
            )
        with pytest.raises(ValueError, match="含 NUL"):
            store.update_thread_owner_binding(
                thread_id, assembly_parent_ref="assembly\x00parent"
            )
        with pytest.raises(ValueError, match="条目键必须是非空字符串"):
            store.update_thread_owner_binding(
                thread_id, append_attachment_ref={"": 1}
            )
        with pytest.raises(ValueError, match="prefix_epoch 必须是"):
            store.update_thread_owner_binding(
                thread_id, prefix_epoch=0, prefix_epoch_reason="initial"
            )
        with pytest.raises(ValueError, match="stable_prefix_length 必须是"):
            store.update_thread_owner_binding(
                thread_id,
                stable_prefix_hash=_sha("prefix"),
                stable_prefix_length=-1,
            )
        with pytest.raises(ValueError, match="desired_toolset_revision 必须是"):
            store.update_thread_owner_binding(
                thread_id, desired_toolset_revision=0
            )
        with pytest.raises(ValueError, match="需要至少一个字段"):
            store.update_thread_owner_binding(thread_id)
    finally:
        store.close()


def test_owner_binding_row_json_corruption_fail_closed(tmp_path: Path) -> None:
    """库内 JSON 槽损坏（绕过 API 直改）在读取时 fail closed。"""
    store = SessionControlStore(tmp_path / "c.sqlite")
    try:
        thread_id = make_thread_id()
        store.ensure_thread_owner_binding(thread_id=thread_id)
        # 列表槽不是 JSON 数组
        raw_execute(
            store,
            "UPDATE thread_owner_bindings SET attachment_refs = ? "
            "WHERE thread_id = ?",
            ('{"a": 1}', thread_id),
        )
        with pytest.raises(RuntimeError, match="必须是 JSON 数组"):
            store.get_thread_owner_binding(thread_id)
        # 列表槽 JSON 损坏
        raw_execute(
            store,
            "UPDATE thread_owner_bindings SET attachment_refs = ? "
            "WHERE thread_id = ?",
            ("not-json", thread_id),
        )
        with pytest.raises(RuntimeError, match="JSON 损坏"):
            store.get_thread_owner_binding(thread_id)
        # provenance JSON 损坏
        raw_execute(
            store,
            "UPDATE thread_owner_bindings SET mutation_provenance = ? "
            "WHERE thread_id = ?",
            ("not-json", thread_id),
        )
        with pytest.raises(RuntimeError, match="mutation_provenance JSON 损坏"):
            store.get_thread_owner_binding(thread_id)
        # provenance 不是 JSON object
        raw_execute(
            store,
            "UPDATE thread_owner_bindings SET mutation_provenance = ? "
            "WHERE thread_id = ?",
            ("[1]", thread_id),
        )
        with pytest.raises(RuntimeError, match="必须是 JSON object"):
            store.get_thread_owner_binding(thread_id)
    finally:
        store.close()


def test_owner_binding_append_requires_list_slot(tmp_path: Path) -> None:
    """append 路径同样对非数组列表槽 fail closed。"""
    store = SessionControlStore(tmp_path / "c.sqlite")
    try:
        thread_id = make_thread_id()
        store.ensure_thread_owner_binding(thread_id=thread_id)
        raw_execute(
            store,
            "UPDATE thread_owner_bindings SET variant_refs = ? "
            "WHERE thread_id = ?",
            ("{}", thread_id),
        )
        with pytest.raises(RuntimeError, match="必须是 JSON 数组"):
            store.update_thread_owner_binding(
                thread_id, append_variant_ref={"revision": 1}
            )
    finally:
        store.close()

def test_owner_binding_scalar_tamper_layers_are_frozen(
    tmp_path: Path,
) -> None:
    """D7 取证固化：owner binding 外部篡改的三层防线边界。

    结构层由 SQLite 约束兜底，语义层（JSON 槽）读时 fail closed，
    未设读时校验的标量槽是有意为之（详见 thread_owner_binding.py 模块
    docstring 的「外部篡改边界」）。本用例把三层现状固定下来，防止后人
    误以为标量槽漏检而无条件加固。
    """
    store = SessionControlStore(tmp_path / "binding-tamper.sqlite")
    try:
        thread_id = make_thread_id()
        store.ensure_thread_owner_binding(thread_id=thread_id)

        # 结构层：非法直改在写入时即被 SQLite 约束拒绝。
        with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
            raw_execute(
                store,
                "UPDATE thread_owner_bindings SET prefix_epoch = NULL "
                "WHERE thread_id = ?",
                (thread_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            raw_execute(
                store,
                "UPDATE thread_owner_bindings SET prefix_epoch = -5 "
                "WHERE thread_id = ?",
                (thread_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            raw_execute(
                store,
                "UPDATE thread_owner_bindings SET prefix_epoch_reason = 'bogus' "
                "WHERE thread_id = ?",
                (thread_id,),
            )

        # 语义层：JSON 列表槽读时 fail closed。
        raw_execute(
            store,
            "UPDATE thread_owner_bindings SET variant_refs = 'oops' "
            "WHERE thread_id = ?",
            (thread_id,),
        )
        with pytest.raises(RuntimeError, match="JSON 损坏"):
            store.get_thread_owner_binding(thread_id)

        # 标量层：非整数文本在投影 int() 时自然抛错（不是静默返回假值）。
        raw_execute(
            store,
            "UPDATE thread_owner_bindings SET variant_refs = '[]' "
            "WHERE thread_id = ?",
            (thread_id,),
        )
        raw_execute(
            store,
            "UPDATE thread_owner_bindings SET prefix_epoch = 'abc' "
            "WHERE thread_id = ?",
            (thread_id,),
        )
        with pytest.raises(ValueError, match="invalid literal"):
            store.get_thread_owner_binding(thread_id)

        # 未设读时校验的标量槽：直改后读回不报错（有意为之，见模块 docstring）。
        raw_execute(
            store,
            "UPDATE thread_owner_bindings SET prefix_epoch = 1, "
            "stable_prefix_hash = 'not-a-hash', revision = 999 "
            "WHERE thread_id = ?",
            (thread_id,),
        )
        tampered = store.get_thread_owner_binding(thread_id)
        assert tampered.stable_prefix_hash == "not-a-hash"
        assert tampered.revision == 999
        # update 路径仍按写入口径校验：hash/length 必须成对且合法。
        with pytest.raises(ValueError, match="成对提供"):
            store.update_thread_owner_binding(
                thread_id, stable_prefix_hash="a" * 64
            )
    finally:
        store.close()


# ----------------------------------------------------------------------
# D5 通信 ledger：outbox/inbox 幂等与 fail-closed 分支
# ----------------------------------------------------------------------


def make_comm_id() -> str:
    return f"comm_{uuid.uuid4().hex}"


def make_payload_hash(seed: str = "payload") -> str:
    import hashlib

    return hashlib.sha256(seed.encode()).hexdigest()


def outbox_kwargs(communication_id: str, **over: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "session_id": make_session_id(),
        "send_operation_id": f"send-op-{uuid.uuid4().hex}",
        "communication_id": communication_id,
        "source_gateway_id": "gw_a",
        "source_workspace_id": "ws_a",
        "source_thread_id": make_thread_id(),
        "target_gateway_id": "gw_b",
        "target_workspace_id": "ws_b",
        "target_session_id": make_session_id(),
        "target_thread_id": make_thread_id(),
        "kind": "progress",
        "reply_to_communication_id": None,
        "payload_hash": make_payload_hash(),
    }
    kwargs.update(over)
    return kwargs


def test_outbox_create_or_get_is_idempotent(tmp_path: Path) -> None:
    """同 operation 完全复现身份字段 → 返回既有行且不新增行。"""
    store = SessionControlStore(tmp_path / "outbox.sqlite")
    try:
        kwargs = outbox_kwargs(make_comm_id())
        first, created_first = store.create_or_get_communication_outbox(**kwargs)
        second, created_second = store.create_or_get_communication_outbox(**kwargs)
        assert created_first is True
        assert created_second is False
        assert first.send_operation_id == second.send_operation_id
        assert first.state == second.state == "accepted"
        assert first.payload_hash == second.payload_hash
        rows = store.connection.execute(
            "SELECT COUNT(*) FROM communication_outbox"
        ).fetchone()[0]
        assert rows == 1
    finally:
        store.close()


def test_outbox_dedupes_by_communication_id_across_operations(
    tmp_path: Path,
) -> None:
    """同 communication_id 绑定不同 operation 且 preimage 一致 → dedupe。"""
    store = SessionControlStore(tmp_path / "outbox-dedupe.sqlite")
    try:
        base = outbox_kwargs(make_comm_id())
        first, _ = store.create_or_get_communication_outbox(**base)
        second, created = store.create_or_get_communication_outbox(
            **dict(base, send_operation_id=f"send-op-{uuid.uuid4().hex}")
        )
        assert created is False
        assert second.send_operation_id == first.send_operation_id
        rows = store.connection.execute(
            "SELECT COUNT(*) FROM communication_outbox"
        ).fetchone()[0]
        assert rows == 1
    finally:
        store.close()


def test_outbox_operation_retry_with_different_communication_id_fails_closed(
    tmp_path: Path,
) -> None:
    """覆盖 operation 重试的 communication_id 漂移守卫。

    覆盖 create_or_get_communication_outbox 中\n    ``mismatches or record.communication_id != communication_id`` 的
    右侧分支：身份字段全一致、仅 communication_id 不同，也必须 fail
    closed，而不仅是 preimage 漂移才报错。
    """
    store = SessionControlStore(tmp_path / "outbox-comm-drift.sqlite")
    try:
        base = outbox_kwargs(make_comm_id())
        store.create_or_get_communication_outbox(**base)
        with pytest.raises(RuntimeError, match="preimage 漂移"):
            store.create_or_get_communication_outbox(
                **dict(base, communication_id=make_comm_id())
            )
    finally:
        store.close()


def test_outbox_operation_retry_with_drifted_preimage_fails_closed(
    tmp_path: Path,
) -> None:
    """同 operation 但 preimage 字段漂移 → fail closed。"""
    store = SessionControlStore(tmp_path / "outbox-preimage-drift.sqlite")
    try:
        base = outbox_kwargs(make_comm_id())
        store.create_or_get_communication_outbox(**base)
        with pytest.raises(RuntimeError, match="preimage 漂移"):
            store.create_or_get_communication_outbox(
                **dict(base, payload_hash=make_payload_hash("drift"))
            )
    finally:
        store.close()


def test_outbox_communication_rebind_with_other_preimage_fails_closed(
    tmp_path: Path,
) -> None:
    """同 communication_id 绑定不同 preimage → fail closed（不重基）。"""
    store = SessionControlStore(tmp_path / "outbox-rebind.sqlite")
    try:
        base = outbox_kwargs(make_comm_id())
        store.create_or_get_communication_outbox(**base)
        with pytest.raises(RuntimeError, match="已绑定不同 preimage"):
            store.create_or_get_communication_outbox(
                **dict(
                    base,
                    send_operation_id=f"send-op-{uuid.uuid4().hex}",
                    payload_hash=make_payload_hash("other"),
                )
            )
    finally:
        store.close()


def test_outbox_advance_state_chain_and_guards(tmp_path: Path) -> None:
    """前向 CAS 闭集：跳过中间态、终态回退、缺 receipt 均 fail closed。"""
    store = SessionControlStore(tmp_path / "outbox-advance.sqlite")
    try:
        record, _ = store.create_or_get_communication_outbox(
            **outbox_kwargs(make_comm_id())
        )
        operation_id = record.send_operation_id
        with pytest.raises(ValueError, match="新状态非法"):
            store.advance_communication_outbox_state(
                operation_id, new_state="made_up"
            )
        with pytest.raises(ValueError, match="必须携带 receipt JSON"):
            store.advance_communication_outbox_state(
                operation_id, new_state="terminal"
            )
        with pytest.raises(ValueError, match="必须携带 abort_reason"):
            store.advance_communication_outbox_state(
                operation_id, new_state="failed"
            )
        with pytest.raises(RuntimeError, match="状态迁移非法"):
            store.advance_communication_outbox_state(
                operation_id, new_state="target_accepted", receipt_json="[]"
            )
        routed = store.advance_communication_outbox_state(
            operation_id, new_state="routing"
        )
        assert routed.state == "routing"
        accepted = store.advance_communication_outbox_state(
            operation_id, new_state="target_accepted", receipt_json="[]"
        )
        assert accepted.state == "target_accepted"
        # 重复相同 (state, receipt) 幂等；receipt 漂移 fail closed
        assert store.advance_communication_outbox_state(
            operation_id, new_state="target_accepted", receipt_json="[]"
        ).state == "target_accepted"
        with pytest.raises(RuntimeError, match="载荷漂移"):
            store.advance_communication_outbox_state(
                operation_id, new_state="target_accepted", receipt_json="[1]"
            )
        with pytest.raises(RuntimeError, match="状态迁移非法"):
            store.advance_communication_outbox_state(
                operation_id, new_state="routing"
            )
        assert store.advance_communication_outbox_state(
            operation_id, new_state="execution_bound", receipt_json="[]"
        ).state == "execution_bound"
        assert store.advance_communication_outbox_state(
            operation_id, new_state="terminal", receipt_json="[]"
        ).state == "terminal"
        with pytest.raises(RuntimeError, match="状态迁移非法"):
            store.advance_communication_outbox_state(
                operation_id, new_state="routing"
            )
        with pytest.raises(KeyError, match="无法推进状态"):
            store.advance_communication_outbox_state(
                f"send-op-{uuid.uuid4().hex}", new_state="routing"
            )
    finally:
        store.close()


def test_outbox_idempotent_reentry_compares_both_payload_columns(
    tmp_path: Path,
) -> None:
    """同状态重入逐字复现两个载荷列：receipt/abort_reason 任一漂移都抛错。

    `advance_communication_outbox_state` 的 CAS SET 子句写 `latest_receipt`
    与 `abort_reason` 两列，幂等分支必须对两列都比对；只比 receipt 会让
    abort_reason 漂移被静默吞掉（真实缺口，本用例固化修复）。
    """
    store = SessionControlStore(tmp_path / "outbox-payload-drift.sqlite")
    try:
        record, _ = store.create_or_get_communication_outbox(
            **outbox_kwargs(make_comm_id())
        )
        operation_id = record.send_operation_id
        # 先落 failed（带原因），再以三种方式重入同一状态。
        failed = store.advance_communication_outbox_state(
            operation_id, new_state="failed", abort_reason="reason-A"
        )
        assert failed.state == "failed"
        assert failed.abort_reason == "reason-A"
        # 1) 完全一致 → 幂等返回原行。
        assert store.advance_communication_outbox_state(
            operation_id, new_state="failed", abort_reason="reason-A"
        ).abort_reason == "reason-A"
        # 2) abort_reason 漂移 → fail closed（修复前被静默接受）。
        with pytest.raises(RuntimeError, match="载荷漂移"):
            store.advance_communication_outbox_state(
                operation_id, new_state="failed", abort_reason="reason-B"
            )
        # 3) abort_reason 缺失 → 同样 fail closed（不得当成 None 收下）。
        with pytest.raises(ValueError, match="必须携带 abort_reason"):
            store.advance_communication_outbox_state(
                operation_id, new_state="failed"
            )
        # 漂移被拒后原行未被改动。
        assert store.advance_communication_outbox_state(
            operation_id, new_state="failed", abort_reason="reason-A"
        ).abort_reason == "reason-A"
    finally:
        store.close()


def test_outbox_idempotent_reentry_compares_receipt_column(
    tmp_path: Path,
) -> None:
    """同状态重入的 receipt 漂移对照用例（终止态路径）。"""
    store = SessionControlStore(tmp_path / "outbox-receipt-drift.sqlite")
    try:
        record, _ = store.create_or_get_communication_outbox(
            **outbox_kwargs(make_comm_id())
        )
        operation_id = record.send_operation_id
        store.advance_communication_outbox_state(
            operation_id, new_state="routing"
        )
        store.advance_communication_outbox_state(
            operation_id, new_state="target_accepted", receipt_json="[]"
        )
        # 完全一致 → 幂等；receipt 漂移 → fail closed。
        assert store.advance_communication_outbox_state(
            operation_id, new_state="target_accepted", receipt_json="[]"
        ).state == "target_accepted"
        with pytest.raises(RuntimeError, match="载荷漂移"):
            store.advance_communication_outbox_state(
                operation_id, new_state="target_accepted", receipt_json="[1]"
            )
        # 同状态但夹带 abort_reason 也是漂移（该列只允许 failed|cancelled）。
        with pytest.raises(RuntimeError, match="载荷漂移"):
            store.advance_communication_outbox_state(
                operation_id,
                new_state="target_accepted",
                receipt_json="[]",
                abort_reason="sneaky",
            )
    finally:
        store.close()


def test_outbox_failure_state_preserves_authenticated_receipt(
    tmp_path: Path,
) -> None:
    """失败/取消只写 abort_reason，既有受认证 receipt 不得被抹掉。

    修复前：CAS 的 SET 子句把 ``latest_receipt`` 写成传入的 ``None``，
    ``target_accepted → failed`` 会静默抹掉已认证 receipt；行不变量
    “latest_receipt 记录最近一次受认证 target receipt，target_accepted
    起非空”被破坏，迟到重试再也拿不到 receipt 验证字段。
    """
    store = SessionControlStore(tmp_path / "outbox-receipt-preserve.sqlite")
    try:
        record, _ = store.create_or_get_communication_outbox(
            **outbox_kwargs(make_comm_id())
        )
        operation_id = record.send_operation_id
        store.advance_communication_outbox_state(operation_id, new_state="routing")
        accepted = store.advance_communication_outbox_state(
            operation_id, new_state="target_accepted", receipt_json='{"r":1}'
        )
        assert accepted.latest_receipt == '{"r":1}'
        # 失败态：receipt 保留、abort_reason 落库。
        failed = store.advance_communication_outbox_state(
            operation_id, new_state="failed", abort_reason="remote-unreachable"
        )
        assert failed.latest_receipt == '{"r":1}'
        assert failed.abort_reason == "remote-unreachable"
        # 失败态重入幂等（只比 abort_reason，receipt 由既有行保留）。
        assert store.advance_communication_outbox_state(
            operation_id, new_state="failed", abort_reason="remote-unreachable"
        ).latest_receipt == '{"r":1}'
        # 失败态夹带 receipt → 明确拒绝（不接受覆盖受认证 receipt）。
        with pytest.raises(ValueError, match="不得携带 receipt JSON"):
            store.advance_communication_outbox_state(
                operation_id,
                new_state="failed",
                abort_reason="remote-unreachable",
                receipt_json="{\"spoofed\":1}",
            )
    finally:
        store.close()


def test_outbox_success_state_rejects_abort_reason(
    tmp_path: Path,
) -> None:
    """成功态不得夹带 abort_reason（非失败态不接受该列）。"""
    store = SessionControlStore(tmp_path / "outbox-abort-reject.sqlite")
    try:
        record, _ = store.create_or_get_communication_outbox(
            **outbox_kwargs(make_comm_id())
        )
        operation_id = record.send_operation_id
        store.advance_communication_outbox_state(operation_id, new_state="routing")
        with pytest.raises(ValueError, match="不得携带 abort_reason"):
            store.advance_communication_outbox_state(
                operation_id,
                new_state="target_accepted",
                receipt_json="[]",
                abort_reason="sneaky",
            )
        # 未写入：仍停在 routing（拒绝不推进状态）。
        assert store.connection.execute(
            "SELECT state FROM communication_outbox WHERE send_operation_id = ?",
            (operation_id,),
        ).fetchone()[0] == "routing"
        # 非载荷中间态不接受任一载荷列（新 operation，accepted → routing）。
        fresh, _ = store.create_or_get_communication_outbox(
            **outbox_kwargs(make_comm_id())
        )
        with pytest.raises(ValueError, match="不接受 receipt/abort_reason"):
            store.advance_communication_outbox_state(
                fresh.send_operation_id, new_state="routing", abort_reason="x"
            )
    finally:
        store.close()


def inbox_kwargs(
    communication_id: str, target_thread_id: str, **over: object
) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "session_id": make_session_id(),
        "communication_id": communication_id,
        "source_gateway_id": "gw_a",
        "source_workspace_id": "ws_a",
        "source_session_id": make_session_id(),
        "source_thread_id": make_thread_id(),
        "target_thread_id": target_thread_id,
        "kind": "progress",
        "reply_to_communication_id": None,
        "payload_hash": make_payload_hash(),
    }
    kwargs.update(over)
    return kwargs


def test_inbox_create_or_get_binds_main_and_dedupes(tmp_path: Path) -> None:
    """target main binding fresh 校验通过并幂等；身份漂移 fail closed。"""
    store = SessionControlStore(tmp_path / "inbox.sqlite")
    store.initialize_main_thread(make_thread_id(), DEFAULT_CREATED_AT)
    try:
        main_thread_id = str(store.get_main_thread()["thread_id"])
        kwargs = inbox_kwargs(make_comm_id(), main_thread_id)
        first, created_first = store.create_or_get_communication_inbox(**kwargs)
        second, created_second = store.create_or_get_communication_inbox(**kwargs)
        assert created_first is True
        assert created_second is False
        assert first.state == second.state == "target_accepted"
        assert first.admission_id.startswith("cadm_")
        assert first.wakeup_key.startswith("cwake_")
        with pytest.raises(RuntimeError, match="main binding 漂移"):
            store.create_or_get_communication_inbox(
                **dict(kwargs, target_thread_id=make_thread_id())
            )
        with pytest.raises(RuntimeError, match="身份漂移"):
            store.create_or_get_communication_inbox(
                **dict(kwargs, payload_hash=make_payload_hash("drift"))
            )
    finally:
        store.close()


def test_inbox_requires_unique_main_row(tmp_path: Path) -> None:
    """无 main row 时拒绝建立 inbox（fail closed，不伪装成功）。"""
    store = SessionControlStore(tmp_path / "inbox-nomain.sqlite")
    try:
        with pytest.raises(RuntimeError, match="main row 缺失或不唯一"):
            store.create_or_get_communication_inbox(
                **inbox_kwargs(make_comm_id(), make_thread_id())
            )
    finally:
        store.close()


def test_inbox_claim_chain_guards(tmp_path: Path) -> None:
    """admission 领取：未领取写入、同 claim 幂等、更高 generation 接管、
    更低 generation 与不同 owner fail closed。"""
    store = SessionControlStore(tmp_path / "inbox-claim.sqlite")
    store.initialize_main_thread(make_thread_id(), DEFAULT_CREATED_AT)
    try:
        main_thread_id = str(store.get_main_thread()["thread_id"])
        record, _ = store.create_or_get_communication_inbox(
            **inbox_kwargs(make_comm_id(), main_thread_id)
        )
        communication_id = record.communication_id
        with pytest.raises(ValueError, match="claim_owner 不能为空"):
            store.claim_communication_inbox_admission(
                communication_id, claim_owner="", claim_generation=1
            )
        with pytest.raises(ValueError, match="claim_generation 必须是"):
            store.claim_communication_inbox_admission(
                communication_id, claim_owner="w1", claim_generation=0
            )
        claimed = store.claim_communication_inbox_admission(
            communication_id, claim_owner="w1", claim_generation=1
        )
        assert claimed.admission_claim_owner == "w1"
        assert claimed.admission_claim_generation == 1
        assert store.claim_communication_inbox_admission(
            communication_id, claim_owner="w1", claim_generation=1
        ).admission_claim_generation == 1
        bumped = store.claim_communication_inbox_admission(
            communication_id, claim_owner="w1", claim_generation=2
        )
        assert bumped.admission_claim_generation == 2
        with pytest.raises(RuntimeError, match="generation 过期"):
            store.claim_communication_inbox_admission(
                communication_id, claim_owner="w1", claim_generation=1
            )
        with pytest.raises(RuntimeError, match="已被其他 claim 持有"):
            store.claim_communication_inbox_admission(
                communication_id, claim_owner="w2", claim_generation=3
            )
        with pytest.raises(KeyError, match="无法领取 admission"):
            store.claim_communication_inbox_admission(
                make_comm_id(), claim_owner="w1", claim_generation=1
            )
    finally:
        store.close()


def test_inbox_mark_bound_and_record_failure(tmp_path: Path) -> None:
    """execution_bound CAS、重复提交幂等、identity 漂移与失败记录。"""
    store = SessionControlStore(tmp_path / "inbox-bound.sqlite")
    store.initialize_main_thread(make_thread_id(), DEFAULT_CREATED_AT)
    try:
        main_thread_id = str(store.get_main_thread()["thread_id"])
        record, _ = store.create_or_get_communication_inbox(
            **inbox_kwargs(make_comm_id(), main_thread_id)
        )
        communication_id = record.communication_id
        job_id = f"job_{uuid.uuid4().hex}"
        with pytest.raises(RuntimeError, match="未被领取"):
            store.mark_communication_inbox_execution_bound(
                communication_id, job_id=job_id, turn_id=None,
                claim_owner="w1", claim_generation=1,
            )
        store.claim_communication_inbox_admission(
            communication_id, claim_owner="w1", claim_generation=1
        )
        with pytest.raises(ValueError, match="job_id 形态非法"):
            store.mark_communication_inbox_execution_bound(
                communication_id, job_id="nope", turn_id=None,
                claim_owner="w1", claim_generation=1,
            )
        with pytest.raises(RuntimeError, match="claim 与当前持有 claim 不一致"):
            store.mark_communication_inbox_execution_bound(
                communication_id, job_id=job_id, turn_id=None,
                claim_owner="w9", claim_generation=1,
            )
        bound = store.mark_communication_inbox_execution_bound(
            communication_id, job_id=job_id, turn_id="turn-1",
            claim_owner="w1", claim_generation=1,
        )
        assert bound.state == "execution_bound"
        assert bound.job_id == job_id
        assert bound.turn_id == "turn-1"
        repeated = store.mark_communication_inbox_execution_bound(
            communication_id, job_id=job_id, turn_id="turn-1",
            claim_owner="w1", claim_generation=1,
        )
        assert repeated.turn_id == "turn-1"
        with pytest.raises(RuntimeError, match="identity 漂移"):
            store.mark_communication_inbox_execution_bound(
                communication_id, job_id=job_id, turn_id="turn-2",
                claim_owner="w1", claim_generation=1,
            )
        # 失败记录：state 保持 target_accepted、untouched 的 claim 校验
        fresh, _ = store.create_or_get_communication_inbox(
            **inbox_kwargs(make_comm_id(), main_thread_id)
        )
        fresh_id = fresh.communication_id
        with pytest.raises(ValueError, match="last_error 不能为空"):
            store.record_communication_inbox_admission_failure(
                fresh_id, claim_owner="w1", claim_generation=1, last_error=""
            )
        with pytest.raises(RuntimeError, match="claim 与当前持有 claim 不一致"):
            store.record_communication_inbox_admission_failure(
                fresh_id, claim_owner="w1", claim_generation=1,
                last_error="boom",
            )
        store.claim_communication_inbox_admission(
            fresh_id, claim_owner="w1", claim_generation=1
        )
        recorded = store.record_communication_inbox_admission_failure(
            fresh_id, claim_owner="w1", claim_generation=1, last_error="boom"
        )
        assert recorded.state == "target_accepted"
        assert recorded.last_error == "boom"
        with pytest.raises(RuntimeError, match="无失败可记录"):
            store.record_communication_inbox_admission_failure(
                communication_id, claim_owner="w1", claim_generation=1,
                last_error="boom",
            )
    finally:
        store.close()


def test_inbox_listing_and_get_fail_closed(tmp_path: Path) -> None:
    """状态索引只列 target_accepted；缺失行读取抛 KeyError。"""
    store = SessionControlStore(tmp_path / "inbox-listing.sqlite")
    store.initialize_main_thread(make_thread_id(), DEFAULT_CREATED_AT)
    try:
        main_thread_id = str(store.get_main_thread()["thread_id"])
        records = [
            store.create_or_get_communication_inbox(
                **inbox_kwargs(make_comm_id(), main_thread_id)
            )[0]
            for _ in range(3)
        ]
        bound = records[1]
        store.claim_communication_inbox_admission(
            bound.communication_id, claim_owner="w1", claim_generation=1
        )
        store.mark_communication_inbox_execution_bound(
            bound.communication_id, job_id=f"job_{uuid.uuid4().hex}",
            turn_id=None, claim_owner="w1", claim_generation=1,
        )
        listed = store.list_target_accepted_communication_inboxes()
        assert {item.communication_id for item in listed} == {
            records[0].communication_id,
            records[2].communication_id,
        }
        assert store.get_communication_inbox(
            records[0].communication_id
        ).communication_id == records[0].communication_id
        with pytest.raises(KeyError, match="communication inbox 不存在"):
            store.get_communication_inbox(make_comm_id())
    finally:
        store.close()

def test_inbox_last_error_has_no_write_time_length_cap(tmp_path: Path) -> None:
    """固化 D2 判定：last_error 是诊断真值，store 层写入不截断。

    全库既有口径是在展示/传输边界截断（session_information_service.
    _truncate_text 对 last_error 走 _DIAGNOSTIC_TEXT_LIMIT=2048 并带
    _truncated 标志；bounded_json 在边界加截断标记）。store 层若截断会
    丢失可诊断信息，故只校验非空。本用例固定该边界，防止后人误加限长。
    """
    store = SessionControlStore(tmp_path / "inbox-long-error.sqlite")
    try:
        main_thread_id = make_thread_id()
        store.initialize_main_thread(main_thread_id, DEFAULT_CREATED_AT)
        record, _ = store.create_or_get_communication_inbox(
            **inbox_kwargs(make_comm_id(), main_thread_id)
        )
        store.claim_communication_inbox_admission(
            record.communication_id, claim_owner="w1", claim_generation=1
        )
        long_error = "x" * 200_000
        updated = store.record_communication_inbox_admission_failure(
            record.communication_id,
            claim_owner="w1",
            claim_generation=1,
            last_error=long_error,
        )
        assert updated.last_error == long_error
        # 空串仍然 fail closed（唯一保留的写入校验）。
        with pytest.raises(ValueError, match="不能为空"):
            store.record_communication_inbox_admission_failure(
                record.communication_id,
                claim_owner="w1",
                claim_generation=1,
                last_error="",
            )
    finally:
        store.close()


def test_outbox_reply_causal_direction_is_enforced_by_endpoints(
    tmp_path: Path,
) -> None:
    """source 侧 reply 因果证明：方向相反放行，方向相同 fail closed。"""
    store = SessionControlStore(tmp_path / "outbox-reply.sqlite")
    try:
        session_id = make_session_id()
        # 本 session 作为 target 收到过 forward：inbox 行把 main 记为 target。
        main_thread_id = make_thread_id()
        store.initialize_main_thread(main_thread_id, DEFAULT_CREATED_AT)
        peer_thread_id = make_thread_id()
        forward_id = make_comm_id()
        store.create_or_get_communication_inbox(
            session_id=session_id,
            communication_id=forward_id,
            source_gateway_id="gw_peer",
            source_workspace_id="ws_peer",
            source_session_id=make_session_id(),
            source_thread_id=peer_thread_id,
            target_thread_id=main_thread_id,
            kind="progress",
            reply_to_communication_id=None,
            payload_hash=make_payload_hash("forward"),
        )
        reply_base = outbox_kwargs(make_comm_id())
        reply_base.update(
            session_id=session_id,
            source_gateway_id="gw_peer",
            source_workspace_id="ws_peer",
            source_thread_id=main_thread_id,
            target_gateway_id="gw_peer",
            target_workspace_id="ws_peer",
            target_thread_id=peer_thread_id,
            kind="reply",
            reply_to_communication_id=forward_id,
        )
        # 方向相反（source 端点回到 forward 的 source，target 端点落在 main）。
        record, created = store.create_or_get_communication_outbox(**reply_base)
        assert created is True
        assert record.kind == "reply"
        # 方向与 forward 的 inbox 行相同 → 必须 fail closed。
        same_direction = dict(
            reply_base,
            communication_id=make_comm_id(),
            send_operation_id=f"send-op-{uuid.uuid4().hex}",
            source_gateway_id="gw_peer",
            source_workspace_id="ws_peer",
            source_thread_id=peer_thread_id,
            target_gateway_id="gw_peer",
            target_workspace_id="ws_peer",
            target_thread_id=main_thread_id,
        )
        with pytest.raises(RuntimeError, match="方向与本次 send 相同"):
            store.create_or_get_communication_outbox(**same_direction)
        # 本库没有该 communication 的 inbox 行 → 也 fail closed。
        unknown = dict(
            reply_base,
            communication_id=make_comm_id(),
            send_operation_id=f"send-op-{uuid.uuid4().hex}",
            reply_to_communication_id=make_comm_id(),
        )
        with pytest.raises(RuntimeError, match="无法在本 session inbox 中证明"):
            store.create_or_get_communication_outbox(**unknown)
    finally:
        store.close()


def test_inbox_reply_causal_direction_is_enforced_by_endpoints(
    tmp_path: Path,
) -> None:
    """target 侧 reply 因果证明：方向相反放行，方向相同 fail closed。"""
    store = SessionControlStore(tmp_path / "inbox-reply.sqlite")
    try:
        main_thread_id = make_thread_id()
        store.initialize_main_thread(main_thread_id, DEFAULT_CREATED_AT)
        peer_thread_id = make_thread_id()
        session_id = make_session_id()
        source_session_id = make_session_id()
        forward_id = make_comm_id()
        # 本 session 作为 source 发出过 forward：outbox 行 source=本库 main。
        store.create_or_get_communication_outbox(
            session_id=session_id,
            send_operation_id=f"send-op-{uuid.uuid4().hex}",
            communication_id=forward_id,
            source_gateway_id="gw_a",
            source_workspace_id="ws_a",
            source_thread_id=main_thread_id,
            target_gateway_id="gw_b",
            target_workspace_id="ws_b",
            target_session_id=source_session_id,
            target_thread_id=peer_thread_id,
            kind="progress",
            reply_to_communication_id=None,
            payload_hash=make_payload_hash("forward"),
        )
        reply_base = inbox_kwargs(make_comm_id(), main_thread_id)
        reply_base.update(
            session_id=session_id,
            source_gateway_id="gw_b",
            source_workspace_id="ws_b",
            source_session_id=source_session_id,
            source_thread_id=peer_thread_id,
            target_thread_id=main_thread_id,
            kind="reply",
            reply_to_communication_id=forward_id,
        )
        # 方向相反（source 端点回到 forward 的 target，target 端点落在 main）。
        record, created = store.create_or_get_communication_inbox(**reply_base)
        assert created is True
        assert record.kind == "reply"
        # 方向与 forward 的 outbox 行相同 → 必须 fail closed。
        same_direction = dict(
            reply_base,
            communication_id=make_comm_id(),
            source_gateway_id="gw_a",
            source_workspace_id="ws_a",
            source_session_id=session_id,
            source_thread_id=main_thread_id,
            target_thread_id=main_thread_id,
        )
        with pytest.raises(RuntimeError, match="方向与本次方向不一致"):
            store.create_or_get_communication_inbox(**same_direction)
        # 本库没有该 communication 的 outbox 行 → 也 fail closed。
        unknown = dict(
            reply_base,
            communication_id=make_comm_id(),
            reply_to_communication_id=make_comm_id(),
        )
        with pytest.raises(RuntimeError, match="无法在本 session outbox 中证明"):
            store.create_or_get_communication_inbox(**unknown)
    finally:
        store.close()


# ----------------------------------------------------------------------
# 写事务不变量：_write_transaction 不可重入
# ----------------------------------------------------------------------


def test_write_transaction_rejects_nesting_with_domain_error(
    tmp_path: Path,
) -> None:
    """嵌套写事务必须抛本库领域错误（含「嵌套」），而不是 sqlite3 原始异常。

    固化 `_write_transaction` 的不可重入不变量：内层 BEGIN IMMEDIATE 若
    在外层事务内执行，sqlite3 会报 "cannot start a transaction within a
    transaction"，该文案不含本库上下文；本库改在进入前 fail loud。
    """
    store = SessionControlStore(tmp_path / "nested-tx.sqlite")

    def enter_nested_transaction() -> None:
        with store._write_transaction():
            pass

    try:
        with store._write_transaction(), pytest.raises(RuntimeError, match="嵌套"):
            enter_nested_transaction()
        # 外层事务已正常提交；再次单独开事务不受影响（不变量只拒嵌套）。
        with store._write_transaction() as connection:
            assert connection.execute("SELECT 1").fetchone()[0] == 1
        assert store.connection.in_transaction is False
    finally:
        store.close()


def test_write_transaction_nesting_error_does_not_corrupt_outer_transaction(
    tmp_path: Path,
) -> None:
    """嵌套被拒后外层事务仍可正常提交，且异常不被静默吞掉。"""
    store = SessionControlStore(tmp_path / "nested-tx-outer.sqlite")
    try:
        store.initialize_main_thread(make_thread_id(), DEFAULT_CREATED_AT)
        with store._write_transaction() as connection:
            connection.execute(
                "INSERT INTO lifecycle_fence (id, state, generation) "
                "VALUES (1, 'active', 1) "
                "ON CONFLICT(id) DO NOTHING"
            )
            nested_error: RuntimeError | None = None
            try:
                with store._write_transaction():
                    pass
            except RuntimeError as error:
                nested_error = error
            assert nested_error is not None
            assert "嵌套" in str(nested_error)
        # 外层事务提交成功，插入可见。
        assert store.get_fence() is not None
    finally:
        store.close()


# ----------------------------------------------------------------------
# 准入门禁边界：通信 ledger 不做 fence 门禁（由 SessionLifecycleGate 单点负责）
# ----------------------------------------------------------------------


def test_communication_ledger_does_not_gate_on_lifecycle_fence(
    tmp_path: Path,
) -> None:
    """固化现状：store 层通信 ledger 不看 lifecycle fence。

    准入红线由 `SessionLifecycleGate` 单点负责——唯一两个生产调用方
    （`CommunicationLedgerService.admit_outgoing_send` /
    `accept_incoming_send`）都在 `gate.exclusive(session_id)` 内；绕过
    gate 直接调用 store 即视为绕过。本用例把这条分工固定下来，防止后人
    误以为漏检而在 store 层重复加 fence 检查（那会制造第二套准入判断）。
    """
    store = SessionControlStore(tmp_path / "comm-fence.sqlite")
    try:
        main_thread_id = make_thread_id()
        store.initialize_main_thread(main_thread_id, DEFAULT_CREATED_AT)
        store.initialize_fence("active", 1)
        assert store.cas_fence_transition(1, "deleting") is True
        assert store.get_fence()[0] == "deleting"
        # fence 已 deleting，store 层通信 ledger 仍照常写入（不设门禁）。
        inbox, created = store.create_or_get_communication_inbox(
            **inbox_kwargs(make_comm_id(), main_thread_id)
        )
        assert created is True
        assert inbox.state == "target_accepted"
        outbox, outbox_created = store.create_or_get_communication_outbox(
            **outbox_kwargs(make_comm_id())
        )
        assert outbox_created is True
        assert outbox.state == "accepted"
    finally:
        store.close()


# ----------------------------------------------------------------------
# 写锁冲突：跨连接竞争超时转领域错误（不是裸 sqlite3.OperationalError）
# ----------------------------------------------------------------------


def test_begin_immediate_lock_contention_raises_domain_error(
    tmp_path: Path,
) -> None:
    """另一连接持写锁时，本库抛含阶段与路径的领域错误。

    同一 session 库可能被多个进程同时打开；跨进程写竞争超过
    ``SQLITE_BUSY_TIMEOUT_MS`` 时不得让裸 ``OperationalError: database
    is locked`` 冒泡（无库路径、无等待时长，无法定位）。
    """
    from app.core.sqlite_state import SQLITE_BUSY_TIMEOUT_MS

    database = tmp_path / "lock-contention.sqlite"
    store = SessionControlStore(database)
    holder = sqlite3.connect(
        store.database_path,
        timeout=SQLITE_BUSY_TIMEOUT_MS / 1000,
        isolation_level=None,
    )
    try:
        # 另一连接先占写锁；holder 连接写完后 store 侧事务会超时。
        holder.execute("PRAGMA busy_timeout = 50")
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("INSERT INTO lifecycle_fence (id, state, generation) "
                       "VALUES (1, 'active', 1) ON CONFLICT(id) DO NOTHING")
        # 缩短本 store 连接的等待，避免用例耗时 5s。
        store.connection.execute("PRAGMA busy_timeout = 50")
        with (
            pytest.raises(RuntimeError, match="获取写事务失败") as excinfo,
            store._write_transaction(),
        ):
            pass
        message = str(excinfo.value)
        assert "stage=BEGIN IMMEDIATE" in message
        assert str(store.database_path) in message
        # 错误里报告的是实际生效的 busy_timeout（此处被本用例缩短为 50ms）。
        assert "等待 50ms 后仍被占用" in message
        # 底层 sqlite3 异常保留为 cause，便于诊断。
        assert isinstance(excinfo.value.__cause__, sqlite3.OperationalError)
        # 失败后连接未卡在事务里，仍可正常工作。
        holder.execute("ROLLBACK")
        store.connection.execute("PRAGMA busy_timeout = 5000")
        with store._write_transaction():
            pass
        assert store.connection.in_transaction is False
    finally:
        holder.close()
        store.close()
