"""跨进程生命周期 gate / SessionReadGuard / 通用 operation lease 验收（B2）。

覆盖：锁序（topology → Session gate → SQLite）、锁 inode 不重建、进程
退出自动释放、无双 gate（同进程与跨进程 exclusive 互斥）、shared 读
guard 并发、lease 的 create-or-get 幂等、fencing token CAS 链、恢复
接管、无墙钟过期、非终态索引与 v4→v5 schema 升级数据零丢失。
"""

from __future__ import annotations

import hashlib
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.session_control_store import SessionControlStore
from app.core.session_lifecycle_gate import (
    SESSION_OPERATION_LEASE_KINDS,
    NavigationTopologyGate,
    SessionLifecycleGate,
    SessionOperationLease,
    SessionReadGuard,
)


def make_session_id() -> str:
    return f"ses_{uuid.uuid4().hex}"


def make_thread_id() -> str:
    return f"thr_{uuid.uuid4().hex}"


def make_preimage(content: str = "operation") -> str:
    return hashlib.sha256(content.encode()).hexdigest()


# 子进程锁探测脚本：exit 0 = 取得锁；exit 3 = 非阻塞被拒（他人持有）。
_FLOCK_CHILD = """\
import fcntl, os, sys
path, mode = sys.argv[1], sys.argv[2]
fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
flags = fcntl.LOCK_EX if mode == "ex" else fcntl.LOCK_SH
if len(sys.argv) > 3 and sys.argv[3] == "die":
    fcntl.flock(fd, flags)
    os._exit(0)
code = 0
try:
    fcntl.flock(fd, flags | fcntl.LOCK_NB)
except OSError:
    code = 3
raise SystemExit(code)
"""


def _child_lock_probe(lock_path: Path, mode: str, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", _FLOCK_CHILD, str(lock_path), mode, *extra],
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )


# ----------------------------------------------------------------------
# NavigationTopologyGate / SessionLifecycleGate / SessionReadGuard
# ----------------------------------------------------------------------


def test_topology_gate_lock_path_under_navigation(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    gate = NavigationTopologyGate(sessions_root)
    assert gate.lock_path == tmp_path / "navigation" / "topology.lock"


def test_topology_gate_rejects_non_path_root() -> None:
    with pytest.raises(TypeError):
        NavigationTopologyGate("not-a-path")  # type: ignore[arg-type]


async def test_topology_gate_exclusive_and_shared_modes(
    tmp_path: Path,
) -> None:
    gate = NavigationTopologyGate(tmp_path / "sessions")
    async with gate.exclusive():
        assert gate.lock_path.is_file()
    async with gate.shared():
        assert gate.lock_path.is_file()


async def test_cross_process_exclusive_mutual_exclusion(tmp_path: Path) -> None:
    """跨进程无双 gate：持有者存在时另一进程非阻塞获取必须被拒。"""
    gate = NavigationTopologyGate(tmp_path / "sessions")
    async with gate.exclusive():
        blocked = _child_lock_probe(gate.lock_path, "ex")
        assert blocked.returncode == 3
    released = _child_lock_probe(gate.lock_path, "ex")
    assert released.returncode == 0


async def test_cross_process_exclusive_blocks_shared(tmp_path: Path) -> None:
    gate = NavigationTopologyGate(tmp_path / "sessions")
    async with gate.exclusive():
        blocked = _child_lock_probe(gate.lock_path, "sh")
        assert blocked.returncode == 3


async def test_shared_guards_admit_concurrent_readers(tmp_path: Path) -> None:
    gate = NavigationTopologyGate(tmp_path / "sessions")
    async with gate.shared(), gate.shared():
        pass


def test_process_exit_auto_releases_lock(tmp_path: Path) -> None:
    """子进程取锁后 os._exit，OS 必须自动释放，父进程随后可立即获取。"""
    gate = NavigationTopologyGate(tmp_path / "sessions")
    gate.lock_path.parent.mkdir(parents=True, exist_ok=True)
    victim = _child_lock_probe(gate.lock_path, "ex", "die")
    assert victim.returncode == 0
    deadline = time.monotonic() + 5.0
    acquired = False
    while time.monotonic() < deadline:
        probe = _child_lock_probe(gate.lock_path, "ex")
        if probe.returncode == 0:
            acquired = True
            break
        time.sleep(0.05)
    assert acquired, "进程退出后锁未被 OS 自动释放"


async def test_lock_inode_is_never_rebuilt(tmp_path: Path) -> None:
    """锁文件只创建不删除：多次获取前后 inode/dev 保持一致。"""
    gate = NavigationTopologyGate(tmp_path / "sessions")
    async with gate.exclusive():
        first = gate.lock_path.stat()
    async with gate.exclusive():
        second = gate.lock_path.stat()
    assert (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)
    assert gate.lock_path.is_file()


def test_session_gate_rejects_non_canonical_session_id(tmp_path: Path) -> None:
    gate = SessionLifecycleGate(tmp_path / "sessions")
    with pytest.raises(ValueError):
        gate.exclusive("非法ID")
    with pytest.raises(ValueError):
        gate.shared("ses_" + uuid.uuid4().hex.upper())


async def test_session_gate_lock_path_and_read_guard_type(
    tmp_path: Path,
) -> None:
    session_id = make_session_id()
    gate = SessionLifecycleGate(tmp_path / "sessions")
    guard = gate.shared(session_id)
    assert isinstance(guard, SessionReadGuard)
    async with guard:
        expected = (
            tmp_path / "navigation" / "session-lifecycle-gates"
            / f"{session_id}.lock"
        )
        assert guard.path == expected
        assert expected.is_file()


async def test_read_guard_allows_concurrent_and_blocks_exclusive(
    tmp_path: Path,
) -> None:
    session_id = make_session_id()
    gate = SessionLifecycleGate(tmp_path / "sessions")
    async with gate.shared(session_id):
        async with gate.shared(session_id):
            pass
        blocked = _child_lock_probe(
            gate.gates_root / f"{session_id}.lock", "ex"
        )
        assert blocked.returncode == 3


async def test_lock_order_topology_then_session_gate(tmp_path: Path) -> None:
    """固定锁序：topology → Session gate 可嵌套获取；不反向。"""
    topology = NavigationTopologyGate(tmp_path / "sessions")
    session_gate = SessionLifecycleGate(tmp_path / "sessions")
    session_id = make_session_id()
    async with (
        topology.exclusive(),
        session_gate.exclusive(session_id),
        session_gate.shared(make_session_id()),
    ):
        pass


# ----------------------------------------------------------------------
# 通用 operation lease（SessionControlStore 持久层）
# ----------------------------------------------------------------------


@pytest.fixture()
def control(tmp_path: Path):
    store = SessionControlStore(tmp_path / "session-control.sqlite")
    store.initialize_fence()
    yield store
    store.close()


def test_lease_create_or_get_idempotent(control: SessionControlStore) -> None:
    identity = "op-" + uuid.uuid4().hex
    first = control.create_or_get_lease(
        operation_kind="thread_creation",
        operation_identity=identity,
        preimage_hash=make_preimage(identity),
    )
    second = control.create_or_get_lease(
        operation_kind="thread_creation",
        operation_identity=identity,
        preimage_hash=make_preimage(identity),
    )
    assert first.lease_id == second.lease_id
    assert first.fencing_token == second.fencing_token == 1
    assert first.state == "active"
    assert first.captured_lifecycle_generation == 1
    assert first.holder_generation == 1


def test_lease_preimage_conflict_fails_closed(
    control: SessionControlStore,
) -> None:
    identity = "op-" + uuid.uuid4().hex
    control.create_or_get_lease(
        operation_kind="execution",
        operation_identity=identity,
        preimage_hash=make_preimage("a"),
    )
    with pytest.raises(RuntimeError, match="preimage 冲突"):
        control.create_or_get_lease(
            operation_kind="execution",
            operation_identity=identity,
            preimage_hash=make_preimage("b"),
        )


def test_lease_input_validation(control: SessionControlStore) -> None:
    identity = "op-" + uuid.uuid4().hex
    with pytest.raises(ValueError, match="operation_kind 非法"):
        control.create_or_get_lease(
            operation_kind="made_up_kind",
            operation_identity=identity,
            preimage_hash=make_preimage(),
        )
    with pytest.raises(ValueError, match="operation_identity"):
        control.create_or_get_lease(
            operation_kind="execution",
            operation_identity="",
            preimage_hash=make_preimage(),
        )
    with pytest.raises(ValueError, match="preimage_hash"):
        control.create_or_get_lease(
            operation_kind="execution",
            operation_identity=identity,
            preimage_hash="not-a-hash",
        )
    assert len(SESSION_OPERATION_LEASE_KINDS) == 13


def test_lease_expected_generation_fresh_check(
    control: SessionControlStore,
) -> None:
    with pytest.raises(ValueError, match="expected_generation"):
        control.create_or_get_lease(
            operation_kind="execution",
            operation_identity="op-" + uuid.uuid4().hex,
            preimage_hash=make_preimage(),
            expected_generation=99,
        )
    lease = control.create_or_get_lease(
        operation_kind="execution",
        operation_identity="op-" + uuid.uuid4().hex,
        preimage_hash=make_preimage(),
        expected_generation=1,
    )
    assert lease.captured_lifecycle_generation == 1


def test_lease_rejected_when_fence_deleting(
    control: SessionControlStore,
) -> None:
    assert control.cas_fence_transition(1, "deleting") is True
    with pytest.raises(RuntimeError, match="拒绝建立新 operation lease"):
        control.create_or_get_lease(
            operation_kind="execution",
            operation_identity="op-" + uuid.uuid4().hex,
            preimage_hash=make_preimage(),
        )


def test_lease_missing_fence_row_fails_closed(tmp_path: Path) -> None:
    store = SessionControlStore(tmp_path / "no-fence.sqlite")
    try:
        with pytest.raises(KeyError, match="缺少 lifecycle fence row"):
            store.create_or_get_lease(
                operation_kind="execution",
                operation_identity="op-" + uuid.uuid4().hex,
                preimage_hash=make_preimage(),
            )
    finally:
        store.close()


def test_lease_settling_and_terminal_cas_chain(
    control: SessionControlStore,
) -> None:
    lease = control.create_or_get_lease(
        operation_kind="session_catalog_mutation",
        operation_identity="op-" + uuid.uuid4().hex,
        preimage_hash=make_preimage(),
    )
    # 错 token 不能推进 settling
    with pytest.raises(RuntimeError, match="settling CAS 失败"):
        control.mark_lease_settling(
            lease_id=lease.lease_id, expected_fencing_token=999
        )
    settling = control.mark_lease_settling(
        lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token
    )
    assert settling.state == "settling"
    # 未 durable commit 主体就 settle：从 active 路径不可达；settling
    # 是 terminal 的唯一入口（跨库先 commit 后 terminal 的顺序合同）。
    with pytest.raises(RuntimeError, match="settle CAS 失败"):
        control.settle_lease(
            lease_id=lease.lease_id,
            expected_fencing_token=999,
            outcome="completed",
        )
    done = control.settle_lease(
        lease_id=lease.lease_id,
        expected_fencing_token=lease.fencing_token,
        outcome="completed",
    )
    assert done.state == "completed"


def test_lease_terminal_only_from_settling(
    control: SessionControlStore,
) -> None:
    lease = control.create_or_get_lease(
        operation_kind="execution",
        operation_identity="op-" + uuid.uuid4().hex,
        preimage_hash=make_preimage(),
    )
    with pytest.raises(RuntimeError, match="settle CAS 失败"):
        control.settle_lease(
            lease_id=lease.lease_id,
            expected_fencing_token=lease.fencing_token,
            outcome="completed",
        )


def test_lease_invalid_outcome_rejected(control: SessionControlStore) -> None:
    lease = control.create_or_get_lease(
        operation_kind="execution",
        operation_identity="op-" + uuid.uuid4().hex,
        preimage_hash=make_preimage(),
    )
    control.mark_lease_settling(
        lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token
    )
    with pytest.raises(ValueError, match="终态非法"):
        control.settle_lease(
            lease_id=lease.lease_id,
            expected_fencing_token=lease.fencing_token,
            outcome="expired",
        )


def test_lease_takeover_increments_token_and_holder(
    control: SessionControlStore,
) -> None:
    lease = control.create_or_get_lease(
        operation_kind="communication_source",
        operation_identity="op-" + uuid.uuid4().hex,
        preimage_hash=make_preimage(),
    )
    control.mark_lease_settling(
        lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token
    )
    taken = control.takeover_lease(
        lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token
    )
    assert taken.state == "active"
    assert taken.fencing_token == lease.fencing_token + 1
    assert taken.holder_generation == lease.holder_generation + 1
    # 旧 token 的 callback 一律失败
    assert not control.verify_lease_token(
        lease_id=lease.lease_id, fencing_token=lease.fencing_token
    )
    assert control.verify_lease_token(
        lease_id=lease.lease_id, fencing_token=taken.fencing_token
    )
    with pytest.raises(RuntimeError):
        control.mark_lease_settling(
            lease_id=lease.lease_id,
            expected_fencing_token=lease.fencing_token,
        )


def test_lease_takeover_rejected_on_terminal(
    control: SessionControlStore,
) -> None:
    lease = control.create_or_get_lease(
        operation_kind="execution",
        operation_identity="op-" + uuid.uuid4().hex,
        preimage_hash=make_preimage(),
    )
    control.mark_lease_settling(
        lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token
    )
    control.settle_lease(
        lease_id=lease.lease_id,
        expected_fencing_token=lease.fencing_token,
        outcome="cancelled",
    )
    with pytest.raises(RuntimeError, match="takeover CAS 失败"):
        control.takeover_lease(
            lease_id=lease.lease_id,
            expected_fencing_token=lease.fencing_token,
        )


def test_lease_takeover_from_active_directly(
    control: SessionControlStore,
) -> None:
    """active 而非 settling 的 lease 也允许接管（源状态闭集含两者）。

    恢复 owner 可能面对尚未提交任何主体、仍停在 active 的旧 holder；
    takeover 的源状态闭集必须是 active|settling，不能只剩 settling。
    """
    lease = control.create_or_get_lease(
        operation_kind="runtime_owner",
        operation_identity="op-" + uuid.uuid4().hex,
        preimage_hash=make_preimage(),
    )
    assert lease.state == "active"
    taken = control.takeover_lease(
        lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token
    )
    assert taken.state == "active"
    assert taken.fencing_token == lease.fencing_token + 1
    assert taken.holder_generation == lease.holder_generation + 1
    assert control.verify_lease_token(
        lease_id=lease.lease_id, fencing_token=taken.fencing_token
    )


def test_lease_verify_token_missing_or_terminal(
    control: SessionControlStore,
) -> None:
    assert not control.verify_lease_token(
        lease_id="lease_" + "0" * 32, fencing_token=1
    )
    lease = control.create_or_get_lease(
        operation_kind="execution",
        operation_identity="op-" + uuid.uuid4().hex,
        preimage_hash=make_preimage(),
    )
    control.mark_lease_settling(
        lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token
    )
    control.settle_lease(
        lease_id=lease.lease_id,
        expected_fencing_token=lease.fencing_token,
        outcome="failed",
    )
    assert not control.verify_lease_token(
        lease_id=lease.lease_id, fencing_token=lease.fencing_token
    )


def test_lease_has_no_wall_clock_expiry(control: SessionControlStore) -> None:
    """lease 无墙钟自动到期：创建后不随时间失效，恢复按 token CAS。"""
    """lease 无墙钟自动到期：创建后不随时间失效，恢复按 token CAS。"""
    lease = control.create_or_get_lease(
        operation_kind="remote_observation",
        operation_identity="op-" + uuid.uuid4().hex,
        preimage_hash=make_preimage(),
        recovery_ref="rollout:checkpoint-42",
    )
    current = control.get_lease(lease.lease_id)
    assert current.state == "active"
    assert current.recovery_ref == "rollout:checkpoint-42"
    assert current.created_at
    assert current.updated_at


def test_lease_non_terminal_listing(control: SessionControlStore) -> None:
    identities = ["op-" + uuid.uuid4().hex for _ in range(3)]
    leases = [
        control.create_or_get_lease(
            operation_kind="execution",
            operation_identity=identity,
            preimage_hash=make_preimage(identity),
        )
        for identity in identities
    ]
    control.mark_lease_settling(
        lease_id=leases[0].lease_id,
        expected_fencing_token=leases[0].fencing_token,
    )
    control.settle_lease(
        lease_id=leases[0].lease_id,
        expected_fencing_token=leases[0].fencing_token,
        outcome="completed",
    )
    pending = control.list_non_terminal_leases()
    assert {item.lease_id for item in pending} == {
        leases[1].lease_id,
        leases[2].lease_id,
    }


def test_find_lease_by_operation_latest(
    control: SessionControlStore,
) -> None:
    identity = "op-" + uuid.uuid4().hex
    created = control.create_or_get_lease(
        operation_kind="execution",
        operation_identity=identity,
        preimage_hash=make_preimage(identity),
    )
    found = control.find_lease_by_operation(identity)
    assert found is not None
    assert isinstance(found, SessionOperationLease)
    assert found.lease_id == created.lease_id
    assert control.find_lease_by_operation("missing-op") is None


def test_lease_missing_row_read_and_cas_diagnostics(
    control: SessionControlStore,
) -> None:
    """缺失行的只读与 CAS 诊断路径都走同一取行实现并抛 KeyError。

    覆盖 get_lease 的缺失分支，以及 settling/settle/takeover CAS 未命中
    后按 lease_id 诊断取行时该行也不存在（行被外部删除）的分支。
    """
    missing = "lease_" + "0" * 32
    with pytest.raises(KeyError, match="operation lease 不存在"):
        control.get_lease(missing)
    with pytest.raises(KeyError, match="operation lease 不存在"):
        control.mark_lease_settling(
            lease_id=missing, expected_fencing_token=1
        )
    with pytest.raises(KeyError, match="operation lease 不存在"):
        control.settle_lease(
            lease_id=missing,
            expected_fencing_token=1,
            outcome="completed",
        )
    with pytest.raises(KeyError, match="operation lease 不存在"):
        control.takeover_lease(lease_id=missing, expected_fencing_token=1)


def test_schema_v4_upgrades_to_current_schema_zero_loss(
    tmp_path: Path,
) -> None:
    """v4 库打开时加法补建 lease 表；既有 main/fence 数据零丢失。"""
    db_path = tmp_path / "upgrade.sqlite"
    store = SessionControlStore(db_path)
    main_thread_id = make_thread_id()
    store.initialize_fence()
    store.initialize_main_thread(main_thread_id, datetime.now(UTC))
    store.close()
    # 模拟 v4 库：删除 v5 新表并回退 user_version。
    raw = sqlite3.connect(db_path)
    raw.execute("DROP TABLE session_operation_leases")
    raw.execute(
        "DROP INDEX IF EXISTS idx_session_operation_leases_non_terminal"
    )
    raw.execute("PRAGMA user_version = 4")
    raw.commit()
    raw.close()
    reopened = SessionControlStore(db_path)
    try:
        version = int(
            reopened.connection.execute("PRAGMA user_version").fetchone()[0]
        )
        assert version == reopened.SCHEMA_VERSION == 7
        main_row = reopened.get_main_thread()
        assert str(main_row["thread_id"]) == main_thread_id
        lease = reopened.create_or_get_lease(
            operation_kind="thread_creation",
            operation_identity="op-" + uuid.uuid4().hex,
            preimage_hash=make_preimage(),
        )
        assert lease.state == "active"
    finally:
        reopened.close()
