"""per-session session-control.sqlite 的通用 operation lease owner。

本模块承载单 session 内持久准入/操作 lease 这条垂直链路的唯一实现：

- session_operation_leases 表 DDL、非终态部分索引与行投影；
- create-or-get（按 captured lifecycle generation + operation identity 幂等，
  preimage 不同 fail closed）；
- fencing token CAS 链（mark settling → settle terminal、takeover 换 token）；
- 读取（get/find-by-operation/list 非终态/verify token）。

OperationLeaseMixin 由 app.core.session_control_store.SessionControlStore 继承
装配；本模块只依赖宿主类提供的 database_path、_connection、_ensure_open() 与
_write_transaction()，以及 thread_catalog 的 read_fence_row()，不感知其余
控制库职责。错误分类沿用 session_control_store 约定：KeyError 目标行缺失、
RuntimeError 库被外部改动或 CAS 冲突、ValueError 输入形态非法。

lease 无墙钟自动到期；删除 drain 与恢复路径只消费 active|settling 非终态。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from app.core.identifier import create_prefixed_id
from app.core.session_control_primitives import SHA256_HEX_PATTERN
from app.core.session_control_thread_catalog.thread_catalog import (
    read_fence_row,
)
from app.core.session_lifecycle_gate import (
    SESSION_OPERATION_LEASE_KINDS,
    SESSION_OPERATION_LEASE_TERMINAL_STATES,
    SessionOperationLease,
)

__all__ = [
    "IDX_SESSION_OPERATION_LEASES_NON_TERMINAL_DDL",
    "SESSION_OPERATION_LEASES_TABLE_DDL",
    "OperationLeaseMixin",
]


# 通用 operation lease（2.3-E，B2）：单 session 内持久准入/操作 lease。
# 字段集按 design.md「通用lease」冻结：lease_id 软件生成（重试不变）；
# (captured_lifecycle_generation, operation_identity) 唯一（同代同操作
# 幂等 create-or-get）；holder_generation/fencing_token 是行内单调 CAS
# 令牌（恢复接管时 +1，旧 token callback 一律失败）；状态闭集
# active|settling|completed|cancelled|failed，无墙钟自动到期；
# recovery_ref 是可选稳定恢复引用（不存物理路径或凭据）。
SESSION_OPERATION_LEASES_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS session_operation_leases (
    lease_id TEXT PRIMARY KEY,
    operation_kind TEXT NOT NULL CHECK (operation_kind IN (
        'thread_creation', 'board_migration', 'collaboration_fanout',
        'runtime_owner', 'execution', 'context_control',
        'communication_source', 'communication_target', 'federated_call',
        'remote_observation', 'attachment', 'fork_retention',
        'session_catalog_mutation')),
    operation_identity TEXT NOT NULL,
    preimage_hash TEXT NOT NULL,
    captured_lifecycle_generation INTEGER NOT NULL,
    holder_generation INTEGER NOT NULL CHECK (holder_generation >= 1),
    fencing_token INTEGER NOT NULL CHECK (fencing_token >= 1),
    state TEXT NOT NULL CHECK (state IN (
        'active', 'settling', 'completed', 'cancelled', 'failed')),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    recovery_ref TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (captured_lifecycle_generation, operation_identity)
)
"""

# 非终态索引（2.3-E）：删除 drain/恢复路径只消费 active|settling。
IDX_SESSION_OPERATION_LEASES_NON_TERMINAL_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_session_operation_leases_non_terminal "
    "ON session_operation_leases(state) "
    "WHERE state IN ('active', 'settling')"
)


def _lease_from_row(row: sqlite3.Row) -> SessionOperationLease:
    """session_operation_leases 行 → 不可变投影（结构不符 fail closed）。"""
    return SessionOperationLease(
        lease_id=str(row["lease_id"]),
        operation_kind=str(row["operation_kind"]),
        operation_identity=str(row["operation_identity"]),
        preimage_hash=str(row["preimage_hash"]),
        captured_lifecycle_generation=int(row["captured_lifecycle_generation"]),
        holder_generation=int(row["holder_generation"]),
        fencing_token=int(row["fencing_token"]),
        state=str(row["state"]),
        revision=int(row["revision"]),
        recovery_ref=(
            str(row["recovery_ref"]) if row["recovery_ref"] is not None else None
        ),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )



class OperationLeaseMixin:
    """SessionControlStore 的通用 operation lease 方法族。

    依赖宿主类提供 database_path、_connection、_ensure_open() 与
    _write_transaction()。
    """

    def create_or_get_lease(
        self,
        *,
        operation_kind: str,
        operation_identity: str,
        preimage_hash: str,
        expected_generation: int | None = None,
        recovery_ref: str | None = None,
    ) -> SessionOperationLease:
        """create-or-get 一条 operation lease（gate 内短事务调用）。

        fence 为 active 时按 ``(captured_lifecycle_generation,
        operation_identity)`` 幂等：同代同操作返回既有行（含终态，恢复方
        按稳定 identity 自行核对后续路径），preimage 不同即冲突 fail
        closed；fence 为 deleting 时拒绝新建（新准入立即失败）；
        expected_generation 提供时必须等于当前 fence generation（fresh
        校验由调用方在 gate 内完成，本参数是最后一道防线）。
        """
        if operation_kind not in SESSION_OPERATION_LEASE_KINDS:
            raise ValueError(
                f"operation_kind 非法: {operation_kind!r}"
                f"（闭集 {SESSION_OPERATION_LEASE_KINDS!r}）"
            )
        if not isinstance(operation_identity, str) or not operation_identity:
            raise ValueError(
                f"operation_identity 必须是非空字符串: {operation_identity!r}"
            )
        if SHA256_HEX_PATTERN.fullmatch(preimage_hash) is None:
            raise ValueError(
                f"preimage_hash 必须是 sha256 小写 hex: {preimage_hash!r}"
            )
        if recovery_ref is not None and (
            not isinstance(recovery_ref, str) or not recovery_ref
        ):
            raise ValueError(
                f"recovery_ref 必须是非空字符串或 None: {recovery_ref!r}"
            )
        now_text = datetime.now(UTC).isoformat()
        with self._write_transaction() as connection:
            row = read_fence_row(
                connection, database_path=self.database_path
            )
            fence_state = str(row["state"])
            fence_generation = int(row["generation"])
            if fence_state != "active":
                raise RuntimeError(
                    "fence 非 active，拒绝建立新 operation lease（新准入立即"
                    "失败）: "
                    f"path={self.database_path}, fence_state={fence_state}, "
                    f"fence_generation={fence_generation}, "
                    f"operation_kind={operation_kind!r}, "
                    f"operation_identity={operation_identity!r}"
                )
            if expected_generation is not None and (
                expected_generation != fence_generation
            ):
                raise ValueError(
                    "expected_generation 与当前 fence generation 不一致: "
                    f"expected={expected_generation}, "
                    f"actual={fence_generation}"
                )
            existing = connection.execute(
                "SELECT * FROM session_operation_leases "
                "WHERE captured_lifecycle_generation = ? "
                "AND operation_identity = ?",
                (fence_generation, operation_identity),
            ).fetchone()
            if existing is not None:
                lease = _lease_from_row(existing)
                if lease.preimage_hash != preimage_hash:
                    raise RuntimeError(
                        "同 operation identity 的 preimage 冲突（fail closed）: "
                        f"lease_id={lease.lease_id}, "
                        f"operation_identity={operation_identity!r}"
                    )
                return lease
            connection.execute(
                "INSERT INTO session_operation_leases (lease_id, "
                "operation_kind, operation_identity, preimage_hash, "
                "captured_lifecycle_generation, holder_generation, "
                "fencing_token, state, revision, recovery_ref, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, 1, 1, 'active', 1, ?, "
                "?, ?)",
                (
                    create_prefixed_id("lease"),
                    operation_kind,
                    operation_identity,
                    preimage_hash,
                    fence_generation,
                    recovery_ref,
                    now_text,
                    now_text,
                ),
            )
            inserted = connection.execute(
                "SELECT * FROM session_operation_leases "
                "WHERE captured_lifecycle_generation = ? "
                "AND operation_identity = ?",
                (fence_generation, operation_identity),
            ).fetchone()
            return _lease_from_row(inserted)

    def mark_lease_settling(
        self, *, lease_id: str, expected_fencing_token: int
    ) -> SessionOperationLease:
        """CAS active → settling（主体 durable commit 阶段开始）。

        只允许当前 fencing token 持有者推进；token 不符或状态非 active
        即 fail loud（旧 holder 不得继续，不静默重试）。
        """
        with self._write_transaction() as connection:
            updated = connection.execute(
                "UPDATE session_operation_leases "
                "SET state = 'settling', revision = revision + 1, "
                "updated_at = ? WHERE lease_id = ? AND state = 'active' "
                "AND fencing_token = ?",
                (
                    datetime.now(UTC).isoformat(),
                    lease_id,
                    expected_fencing_token,
                ),
            ).rowcount
            if updated != 1:
                row = self._lease_row_or_raise(connection, lease_id)
                raise RuntimeError(
                    "lease settling CAS 失败（token 不符或状态非 active）: "
                    f"lease_id={row['lease_id']}, state={row['state']}, "
                    f"fencing_token={row['fencing_token']}, "
                    f"expected_token={expected_fencing_token}"
                )
            row = self._lease_row_or_raise(connection, lease_id)
            return _lease_from_row(row)

    def settle_lease(
        self,
        *,
        lease_id: str,
        expected_fencing_token: int,
        outcome: str,
    ) -> SessionOperationLease:
        """CAS settling → 终态（跨库顺序：主体已 durable commit 之后调用）。

        终态只能从 settling 进入：调用方先 mark_lease_settling 再提交主体，
        最后以同一 fencing token settle；崩溃窗口由恢复路径按稳定
        identity/ref 核对后 takeover 或重放 settle。
        """
        if outcome not in SESSION_OPERATION_LEASE_TERMINAL_STATES:
            raise ValueError(
                f"lease 终态非法: {outcome!r}"
                f"（闭集 {SESSION_OPERATION_LEASE_TERMINAL_STATES!r}）"
            )
        with self._write_transaction() as connection:
            updated = connection.execute(
                "UPDATE session_operation_leases "
                "SET state = ?, revision = revision + 1, updated_at = ? "
                "WHERE lease_id = ? AND state = 'settling' "
                "AND fencing_token = ?",
                (
                    outcome,
                    datetime.now(UTC).isoformat(),
                    lease_id,
                    expected_fencing_token,
                ),
            ).rowcount
            if updated != 1:
                row = self._lease_row_or_raise(connection, lease_id)
                raise RuntimeError(
                    "lease settle CAS 失败（token 不符或状态非 settling）: "
                    f"lease_id={row['lease_id']}, state={row['state']}, "
                    f"fencing_token={row['fencing_token']}, "
                    f"expected_token={expected_fencing_token}, "
                    f"outcome={outcome!r}"
                )
            row = self._lease_row_or_raise(connection, lease_id)
            return _lease_from_row(row)

    def takeover_lease(
        self, *, lease_id: str, expected_fencing_token: int
    ) -> SessionOperationLease:
        """恢复接管：验证旧 token 后 CAS 新 token（token/holder 各 +1）。

        恢复 owner 必须先自行验证旧 holder 已失效（进程消失、settling
        且主体无提交证据等），本方法只做令牌交换，不做业务判断；接管
        后 lease 回到 active，旧 token 的全部后续 callback 一律失败。
        """
        with self._write_transaction() as connection:
            updated = connection.execute(
                "UPDATE session_operation_leases "
                "SET state = 'active', fencing_token = fencing_token + 1, "
                "holder_generation = holder_generation + 1, "
                "revision = revision + 1, updated_at = ? "
                "WHERE lease_id = ? AND fencing_token = ? "
                "AND state IN ('active', 'settling')",
                (
                    datetime.now(UTC).isoformat(),
                    lease_id,
                    expected_fencing_token,
                ),
            ).rowcount
            if updated != 1:
                row = self._lease_row_or_raise(connection, lease_id)
                raise RuntimeError(
                    "lease takeover CAS 失败（token 不符或已终态）: "
                    f"lease_id={row['lease_id']}, state={row['state']}, "
                    f"fencing_token={row['fencing_token']}, "
                    f"expected_token={expected_fencing_token}"
                )
            row = self._lease_row_or_raise(connection, lease_id)
            return _lease_from_row(row)

    def get_lease(self, lease_id: str) -> SessionOperationLease:
        """按 lease_id 读取 lease；缺失抛 KeyError。"""
        self._ensure_open()
        row = self._lease_row_or_raise(self._connection, lease_id)
        return _lease_from_row(row)

    def find_lease_by_operation(
        self, operation_identity: str
    ) -> SessionOperationLease | None:
        """按稳定 operation identity 取最新 lease 行；缺失返回 None。"""
        self._ensure_open()
        row = self._connection.execute(
            "SELECT * FROM session_operation_leases "
            "WHERE operation_identity = ? ORDER BY rowid DESC LIMIT 1",
            (operation_identity,),
        ).fetchone()
        return None if row is None else _lease_from_row(row)

    def list_non_terminal_leases(self) -> tuple[SessionOperationLease, ...]:
        """列出全部非终态（active|settling）lease（删除 drain/恢复路径）。"""
        self._ensure_open()
        rows = self._connection.execute(
            "SELECT * FROM session_operation_leases "
            "WHERE state IN ('active', 'settling') "
            "ORDER BY created_at, rowid"
        ).fetchall()
        return tuple(_lease_from_row(row) for row in rows)

    def verify_lease_token(self, *, lease_id: str, fencing_token: int) -> bool:
        """callback 合同：仅非终态且 token 精确匹配返回 True。

        终态（含 fence 关闭后收敛完成的 lease）或 token 不符一律 False，
        旧 generation callback 据此只能收敛 control outcome。
        """
        self._ensure_open()
        row = self._connection.execute(
            "SELECT state, fencing_token FROM session_operation_leases "
            "WHERE lease_id = ?",
            (lease_id,),
        ).fetchone()
        if row is None:
            return False
        return row["state"] in ("active", "settling") and int(
            row["fencing_token"]
        ) == fencing_token

    def _lease_row_or_raise(
        self, connection: sqlite3.Connection, lease_id: str
    ) -> sqlite3.Row:
        """按 lease_id 取行；缺失抛 KeyError（fail closed）。

        事务内 CAS 失败诊断路径与只读 ``get_lease`` 共用本方法，
        避免同一行查询与同一 KeyError 文案出现第二份实现。
        """
        row = connection.execute(
            "SELECT * FROM session_operation_leases WHERE lease_id = ?",
            (lease_id,),
        ).fetchone()
        if row is None:
            raise KeyError(
                f"operation lease 不存在: lease_id={lease_id!r}, "
                f"path={self.database_path}"
            )
        return row
