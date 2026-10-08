"""per-session session-control.sqlite 的当前 schema 初始化 owner。

只接受全新库与当前 schema；旧生成数据在任何 journal/WAL 设置前拒绝，
不提供升级或回填。各表 DDL 仍由对应垂直链路唯一维护，本模块只编排原子建表。
"""

from __future__ import annotations

from app.core.session_control_communication_ledger.communication_ledger import (
    COMMUNICATION_INBOX_TABLE_DDL,
    COMMUNICATION_OUTBOX_TABLE_DDL,
    IDX_COMMUNICATION_INBOX_TARGET_ACCEPTED_DDL,
)
from app.core.session_control_communication_ledger.observations import (
    REMOTE_OBSERVATION_EXPIRY_DDL,
    REMOTE_OBSERVATION_KEY_DDL,
    REMOTE_OBSERVATION_PAGES_DDL,
    REMOTE_OBSERVATIONS_DDL,
)
from app.core.session_control_operation_lease.operation_lease import (
    IDX_SESSION_OPERATION_LEASES_NON_TERMINAL_DDL,
    SESSION_OPERATION_LEASES_TABLE_DDL,
)
from app.core.session_control_store.collaboration import (
    _COLLABORATION_LEDGER_TABLE_DDL,
    _COLLABORATION_MEMBERS_TABLE_DDL,
)
from app.core.session_control_store.execution_intent import (
    _IDX_THREAD_EXECUTION_INTENT_STATE_DDL,
    _IDX_THREAD_EXECUTION_INTENT_THREAD_DDL,
    _THREAD_EXECUTION_INTENTS_TABLE_DDL,
)
from app.core.session_control_store.thread_creation_record import (
    _IDX_THREAD_CREATION_DELEGATION_DDL,
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
    """session-control 库的当前 schema 幂等建表方法族。"""

    def _initialize(self) -> None:
        """只为全新库/当前 schema 幂等建表，单事务提交完整表结构。

        schema 版本已在连接设置 journal/WAL 前验证；不升级旧生成数据。
        """
        current = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
        self._begin_immediate()
        try:
            self._connection.execute(THREAD_CATALOG_TABLE_DDL)
            self._require_thread_catalog_graph_binding_columns()
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
            self._connection.execute(IDX_SESSION_OPERATION_LEASES_NON_TERMINAL_DDL)
            self._connection.execute(THREAD_OWNER_BINDINGS_TABLE_DDL)
            self._connection.execute(COMMUNICATION_OUTBOX_TABLE_DDL)
            self._connection.execute(COMMUNICATION_INBOX_TABLE_DDL)
            self._connection.execute(REMOTE_OBSERVATIONS_DDL)
            self._connection.execute(REMOTE_OBSERVATION_PAGES_DDL)
            self._connection.execute(REMOTE_OBSERVATION_KEY_DDL)
            self._connection.execute(REMOTE_OBSERVATION_EXPIRY_DDL)
            self._connection.execute(IDX_COMMUNICATION_INBOX_TARGET_ACCEPTED_DDL)
            if current != self.SCHEMA_VERSION:
                self._connection.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        self._connection.execute("COMMIT")
