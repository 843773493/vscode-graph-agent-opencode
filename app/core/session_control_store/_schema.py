"""per-session ``session-control.sqlite`` 的 schema 初始化与版本升级链路。

承载 ``SessionControlStore._initialize`` 与 ``thread_execution_intents`` 的
v2→v3 加法升级：幂等建表、``PRAGMA user_version`` fail-closed 版本闸门，以及
升级期间「建临时新表→确定性拷贝→行数校验→删旧→改名」的单事务迁移。

各表 DDL 的唯一定义点仍在对应垂直链路模块，本模块只负责编排建表顺序。

错误分类沿用宿主约定：``TypeError`` 输入类型错误、``ValueError`` 输入形态
非法、``KeyError`` 目标行不存在、``RuntimeError`` 语义冲突。
"""

from __future__ import annotations

from app.core.session_catalog_store import validate_session_id, validate_thread_id
from app.core.session_control_communication_ledger.communication_ledger import (
    COMMUNICATION_INBOX_TABLE_DDL,
    COMMUNICATION_OUTBOX_TABLE_DDL,
    IDX_COMMUNICATION_INBOX_TARGET_ACCEPTED_DDL,
)
from app.core.session_control_operation_lease.operation_lease import (
    IDX_SESSION_OPERATION_LEASES_NON_TERMINAL_DDL,
    SESSION_OPERATION_LEASES_TABLE_DDL,
)
from app.core.session_control_primitives import validate_thread_creation_key
from app.core.session_control_store.collaboration import (
    _COLLABORATION_LEDGER_TABLE_DDL,
    _COLLABORATION_MEMBERS_TABLE_DDL,
)
from app.core.session_control_store.execution_intent import (
    _IDX_THREAD_EXECUTION_INTENT_STATE_DDL,
    _IDX_THREAD_EXECUTION_INTENT_THREAD_DDL,
    _THREAD_EXECUTION_INTENT_COLUMNS,
    _THREAD_EXECUTION_INTENT_V2_COLUMNS,
    _THREAD_EXECUTION_INTENTS_TABLE_DDL,
    _THREAD_EXECUTION_INTENTS_V3_UPGRADE_TABLE_DDL,
    compute_initial_execution_binding_preimage_hash,
    derive_initial_execution_identity,
)
from app.core.session_control_store.thread_creation_record import (
    _IDX_THREAD_CREATION_DELEGATION_DDL,
    _INITIAL_STATE_VALUES,
    _THREAD_CREATION_RECORDS_TABLE_DDL,
)
from app.core.session_control_thread_catalog.thread_catalog import (
    LIFECYCLE_FENCE_TABLE_DDL,
    THREAD_CATALOG_TABLE_DDL,
)
from app.core.session_control_thread_owner_binding.thread_owner_binding import (
    THREAD_OWNER_BINDINGS_TABLE_DDL,
)


class SchemaInitMixin:
    """session-control 库的幂等建表与版本升级方法族。"""

    def _initialize(self) -> None:
        """幂等建表并设置 user_version；未知版本 fail-closed 拒绝打开。

        支持的 current：0（全新库，建全部表并置 v3）、1（R12/R13 v1 库，
        同一事务内先以「建临时新表→拷贝→校验→删旧→改名」升级
        ``thread_catalog`` 的 kind CHECK、再幂等补建 8.5-B 新表并升
        v3，既有 main row 数据零丢失）、2（R20 v2 库，同一事务内以
        「建临时新表→确定性拷贝→校验→删旧→改名」升级
        ``thread_execution_intents`` 补齐稳定 binding/job identity）、
        3/4/5（历史版本，幂等补建新表后推进）、6（当前版本；全部 DDL
        均为幂等 no-op）。
        """
        current = int(
            self._connection.execute("PRAGMA user_version").fetchone()[0]
        )
        if current not in (0, 1, 2, 3, 4, 5, 6, self.SCHEMA_VERSION):
            raise RuntimeError(
                "session control schema 版本未知，fail-closed 拒绝打开: "
                f"path={self.database_path}, user_version={current}, "
                f"supported={self.SCHEMA_VERSION}"
            )
        self._begin_immediate()
        try:
            if current == 1:
                self._upgrade_thread_catalog_kind_v1_to_v2()
            if current == 2:
                self._upgrade_thread_execution_intents_v2_to_v3()
            self._connection.execute(THREAD_CATALOG_TABLE_DDL)
            self._connection.execute(LIFECYCLE_FENCE_TABLE_DDL)
            self._connection.execute(_THREAD_CREATION_RECORDS_TABLE_DDL)
            self._connection.execute(_IDX_THREAD_CREATION_DELEGATION_DDL)
            self._connection.execute(_COLLABORATION_LEDGER_TABLE_DDL)
            self._connection.execute(
                "INSERT INTO collaboration_ledger (id, revision) "
                "VALUES (1, 0) ON CONFLICT(id) DO NOTHING"
            )
            self._connection.execute(_COLLABORATION_MEMBERS_TABLE_DDL)
            self._connection.execute(_THREAD_EXECUTION_INTENTS_TABLE_DDL)
            self._connection.execute(_IDX_THREAD_EXECUTION_INTENT_THREAD_DDL)
            self._connection.execute(_IDX_THREAD_EXECUTION_INTENT_STATE_DDL)
            self._connection.execute(SESSION_OPERATION_LEASES_TABLE_DDL)
            self._connection.execute(
                IDX_SESSION_OPERATION_LEASES_NON_TERMINAL_DDL
            )
            self._connection.execute(THREAD_OWNER_BINDINGS_TABLE_DDL)
            self._connection.execute(COMMUNICATION_OUTBOX_TABLE_DDL)
            self._connection.execute(COMMUNICATION_INBOX_TABLE_DDL)
            self._connection.execute(IDX_COMMUNICATION_INBOX_TARGET_ACCEPTED_DDL)
            if current != self.SCHEMA_VERSION:
                self._connection.execute(
                    f"PRAGMA user_version = {self.SCHEMA_VERSION}"
                )
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        self._connection.execute("COMMIT")

    def _upgrade_thread_execution_intents_v2_to_v3(self) -> None:
        """v2→v3 加法升级：``thread_execution_intents`` 重建补齐稳定 identity。

        任务书 §2.1：v2 pending 行迁移时确定性补齐软件生成的稳定
        binding/job identity 与 preimage hash——identity 只由行内
        admission 幂等键派生（:func:`derive_initial_execution_identity`），
        不依赖当前时间或随机数，同库任意进程重复执行结果一致；preimage
        hash 用与全新落库相同的口径复算。claim 字段迁移为 NULL（v2 无
        消费语义）、``last_error`` 迁移为 NULL；``state`` 非
        ``pending`` 的行在 v2 从未被合法写入（v2 没有 bound 推进接口），
        fail closed 拒绝迁移。单事务内「建临时新表→确定性拷贝→行数校验
        →删旧→改名」，任何失败随 ``_initialize`` 事务整体回滚
        （``user_version`` 保持 2，库可原样重开）。
        """
        self._connection.execute(
            _THREAD_EXECUTION_INTENTS_V3_UPGRADE_TABLE_DDL
        )
        rows = self._connection.execute(
            f"SELECT {_THREAD_EXECUTION_INTENT_V2_COLUMNS} "
            "FROM thread_execution_intents "
            "ORDER BY admission_idempotency_key"
        ).fetchall()
        for row in rows:
            state = str(row["state"])
            if state != "pending":
                raise RuntimeError(
                    "thread_execution_intents v2→v3 升级发现非 pending 行"
                    "（v2 从未提供 bound 推进接口，库被外部改动，fail "
                    "closed）: admission_key="
                    f"{row['admission_idempotency_key']!r}, state={state!r}"
                )
            admission_key = str(row["admission_idempotency_key"])
            session_id = str(row["session_id"])
            thread_id = str(row["thread_id"])
            creation_key = str(row["creation_idempotency_key"])
            initial_state = str(row["initial_state"])
            # 行级身份复验（防绕过软件直改 v2 库）：与落库闸门同口径。
            validate_session_id(session_id)
            validate_thread_id(thread_id)
            validate_thread_creation_key(admission_key)
            validate_thread_creation_key(creation_key)
            if initial_state not in _INITIAL_STATE_VALUES:
                raise RuntimeError(
                    "thread_execution_intents v2→v3 升级发现非法 "
                    f"initial_state 行（fail closed）: admission_key="
                    f"{admission_key!r}, initial_state={initial_state!r}"
                )
            binding_id, job_id = derive_initial_execution_identity(
                admission_key
            )
            preimage_hash = compute_initial_execution_binding_preimage_hash(
                admission_idempotency_key=admission_key,
                session_id=session_id,
                thread_id=thread_id,
                creation_idempotency_key=creation_key,
                initial_state=initial_state,
                execution_binding_id=binding_id,
                job_id=job_id,
            )
            self._connection.execute(
                "INSERT INTO thread_execution_intents_v3_upgrade "
                f"({_THREAD_EXECUTION_INTENT_COLUMNS}) VALUES "
                "(?, ?, ?, ?, ?, 'pending', ?, ?, ?, NULL, NULL, NULL, ?, ?)",
                (
                    admission_key,
                    session_id,
                    thread_id,
                    creation_key,
                    initial_state,
                    binding_id,
                    job_id,
                    preimage_hash,
                    str(row["intent_created_at"]),
                    str(row["intent_updated_at"]),
                ),
            )
        before = len(rows)
        after = int(
            self._connection.execute(
                "SELECT COUNT(*) FROM thread_execution_intents_v3_upgrade"
            ).fetchone()[0]
        )
        if before != after:
            raise RuntimeError(
                "thread_execution_intents v2→v3 升级拷贝行数不一致（数据零"
                f"丢失保证被破坏，事务将回滚）: path={self.database_path}, "
                f"before={before}, after={after}"
            )
        self._connection.execute("DROP TABLE thread_execution_intents")
        self._connection.execute(
            "ALTER TABLE thread_execution_intents_v3_upgrade "
            "RENAME TO thread_execution_intents"
        )
