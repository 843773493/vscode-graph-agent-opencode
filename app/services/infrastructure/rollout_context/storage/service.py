"""单文件 rollout 与 SQLite 权威 checkpoint 状态存储。

本模块故意不提供旧的 ``segment-*.jsonl``、manifest 或 message mutation
兼容层。JSONL 只保存已经稳定的 canonical item；所有 checkpoint、view、
branch、fork 和 projection 状态都保存在同一份 SQLite 中。
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Protocol

from app.core.path_utils import get_session_path_resolver
from app.core.session_thread_context import require_current_session_thread_owner
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.fork.path_scope import ForkPathScope
from app.services.infrastructure.rollout_context.storage.catalog.integrity import (
    catalog_body_validation_scope,
)
from app.services.infrastructure.rollout_context.storage.catalog.internal_history import (
    InternalExecutionHistoryMixin,
)
from app.services.infrastructure.rollout_context.storage.catalog.item_reader import (
    RolloutCanonicalReadMixin,
)
from app.services.infrastructure.rollout_context.storage.catalog.items import (
    RolloutItemsMixin,
)
from app.services.infrastructure.rollout_context.storage.catalog.parts import (
    RolloutPartsMixin,
)
from app.services.infrastructure.rollout_context.storage.catalog.projections import (
    RolloutProjectionMixin,
)
from app.services.infrastructure.rollout_context.storage.catalog.turn_projection_reads import (
    TurnProjectionReadMixin,
)
from app.services.infrastructure.rollout_context.storage.catalog.turn_projections import (
    TurnProjectionQueryMixin,
)
from app.services.infrastructure.rollout_context.storage.catalog.view_membership import (
    RolloutViewMembershipMixin,
)
from app.services.infrastructure.rollout_context.storage.catalog.view_sequences import (
    RolloutViewSequenceMixin,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    MessageCodec,
    RolloutCheckpointIndex,
    RolloutManifest,
    RolloutReadSnapshot,
    _RolloutFileLock,
    _RolloutOperationLock,
    _RolloutSQLiteConnection,
)
from app.services.infrastructure.rollout_context.storage.rollout_maintenance_owner import (
    RolloutMaintenanceOwner,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    RolloutSerializationMixin,
)
from app.services.infrastructure.rollout_context.storage.transaction_projections import (
    RolloutTransactionProjectionMixin,
)

_ROLLOUT_FILE_LOCK_TIMEOUT_SECONDS = 10.0
class SerializerPort(Protocol):
    """checkpoint 层注入的 typed serializer；storage 不依赖 LangChain。"""

    def dumps_typed(self, value: object) -> tuple[str, bytes]: ...

    def loads_typed(self, value: tuple[str, bytes]) -> object: ...

class RolloutStorage(
    TurnProjectionQueryMixin,
    TurnProjectionReadMixin,
    RolloutItemsMixin,
    RolloutViewMembershipMixin,
    InternalExecutionHistoryMixin,
    RolloutPartsMixin,
    RolloutSerializationMixin,
    RolloutTransactionProjectionMixin,
    RolloutProjectionMixin,
    RolloutCanonicalReadMixin,
    RolloutViewSequenceMixin,
):
    """一个会话的单 JSONL canonical message 文件和权威 SQLite。"""

    def __init__(
        self,
        sessions_dir: str | Path,
        *,
        serde: SerializerPort | None = None,
        message_codec: MessageCodec | None = None,
        fork_recovery: Callable[[str, str, str, sqlite3.Connection, Path], None],
        initialize_activation: Callable[[sqlite3.Connection], None],
    ) -> None:
        self.sessions_dir = Path(sessions_dir).resolve()
        self._path_resolver = get_session_path_resolver(self.sessions_dir)
        self.fork_paths = ForkPathScope(self._path_resolver)
        self._serde = serde
        self._message_codec = message_codec
        self._locks: dict[tuple[str, str], _RolloutOperationLock] = {}
        self._locks_guard = threading.Lock()
        self._active_fork_materializations: set[tuple[str, str, str]] = set()
        self._maintenance_owner = RolloutMaintenanceOwner(self)
        self._fork_recovery = fork_recovery
        self._initialize_activation = initialize_activation

    def catalog_body_validation_scope(self) -> AbstractContextManager[None]:
        """为单次 provider prepare 提供有界正文成功校验作用域。"""
        return catalog_body_validation_scope()

    def _codec(self) -> MessageCodec:
        """返回由 checkpoint 组装层注入的消息适配器。"""
        if self._message_codec is None:
            raise RuntimeError(
                "RolloutStorage 未注入 MessageCodec；storage 不能自行导入或构造 LangChain message"
            )
        return self._message_codec


    @staticmethod
    def _session_id_from_owner_scope(thread_id: str) -> str:
        """取显式 LangGraph owner scope 中与 thread_id 绑定的 Session。"""
        return require_current_session_thread_owner(
            langgraph_thread_id=thread_id
        ).session_id

    def _lock(
        self,
        thread_id: str,
        checkpoint_ns: str,
        *,
        session_id: str,
    ) -> _RolloutOperationLock:
        owner_thread_id = self.fork_paths.thread_identity(session_id, thread_id)
        with self._locks_guard:
            key = (session_id, owner_thread_id)
            return self._locks.setdefault(
                key,
                _RolloutOperationLock(
                    self.root(session_id, checkpoint_ns, thread_id=owner_thread_id).parent
                    / ".rollout.write.lock",
                    timeout_seconds=_ROLLOUT_FILE_LOCK_TIMEOUT_SECONDS,
                ),
            )


    def root(
        self,
        session_id: str,
        checkpoint_ns: str = "",
        *,
        thread_id: str | None = None,
    ) -> Path:
        """返回该 SessionThread 的 rollout 目录（OpenSpec 8.3 thread-qualified）。

        main 与 child 都位于 catalog 约束的独立 thread node；
        每个 rollout、index.sqlite、rollout.jsonl 与 database_meta 都由精确
        (session_id, thread_id) 独占。checkpoint_ns 不是 owner 维度。
        thread_id 缺省时经 thread catalog 显式解析 main thread。

        字面量 `main`、Session ID 或缺失 Thread 只允许在入口显式解析一次；
        此处将缺省 main 映射到 catalog 中冻结的真实 ID，之后所有路径都由该
        `(session_id, thread_id)` 解析，不保留第二套旧物理布局。
        """
        resolved_thread_id = self.fork_paths.thread_identity(session_id, thread_id)
        thread_node = self.fork_paths.thread_node(session_id, resolved_thread_id)
        return thread_node / "rollout"

    def index_path(
        self,
        session_id: str,
        checkpoint_ns: str = "",
        *,
        thread_id: str | None = None,
    ) -> Path:
        return self.root(session_id, checkpoint_ns, thread_id=thread_id) / "index.sqlite"

    def jsonl_path(
        self,
        session_id: str,
        checkpoint_ns: str = "",
        *,
        thread_id: str | None = None,
    ) -> Path:
        return self.root(session_id, checkpoint_ns, thread_id=thread_id) / "rollout.jsonl"


    def _connect(
        self,
        thread_id: str,
        checkpoint_ns: str = "",
        *,
        session_id: str,
        read_only: bool = False,
    ) -> sqlite3.Connection:
        if not isinstance(checkpoint_ns, str):
            raise TypeError("rollout checkpoint_ns 必须是字符串")
        if not read_only and self.fork_paths.source_snapshot(session_id) is not None:
            raise RuntimeError(
                "fork frozen source snapshot 禁止可写 SQLite 连接: "
                f"session_id={session_id}, thread_id={thread_id}, checkpoint_ns={checkpoint_ns!r}"
            )
        owner_thread_id = self.fork_paths.thread_identity(session_id, thread_id)
        index_path = self.index_path(
            session_id, checkpoint_ns, thread_id=owner_thread_id
        )
        return _RolloutSQLiteConnection.open(
            index_path, read_only=read_only,
            frozen_source=self.fork_paths.source_snapshot(session_id) is not None,
        )

    @staticmethod
    def _snapshot_connection(snapshot: RolloutReadSnapshot) -> sqlite3.Connection:
        if snapshot.closed:
            raise RuntimeError("rollout read snapshot 已关闭")
        return snapshot.connection

    @contextmanager
    def read_connection(
        self,
        session_id: str,
        thread_id: str,
        checkpoint_ns: str = "",
        *,
        snapshot: RolloutReadSnapshot | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> Iterator[sqlite3.Connection]:
        """复用严格快照；writer 可借用自己已持锁的事务连接。"""
        if snapshot is not None:
            if connection is not None:
                raise ValueError("读取不能同时指定 snapshot 和 connection")
            if (snapshot.session_id, snapshot.thread_id, snapshot.checkpoint_ns) != (
                session_id, thread_id, checkpoint_ns,
            ):
                raise ValueError("read snapshot 与目标 rollout 不一致")
            yield self._snapshot_connection(snapshot)
        elif connection is not None:
            self._require_v2_runtime(connection)
            owner = connection.execute(
                "SELECT session_id, thread_id FROM database_meta WHERE singleton_id = 1"
            ).fetchone()
            if owner != (session_id, thread_id):
                raise ValueError("借用的 writer connection 与目标 rollout owner 不一致")
            yield connection
        else:
            with self.open_read_snapshot(session_id, thread_id, checkpoint_ns) as owned:
                yield owned.connection

    def backup_index(self, thread_id: str, checkpoint_ns: str='', *, session_id: str, destination: str | Path | None=None) -> Path:
        return self._maintenance_owner.backup_index(thread_id, checkpoint_ns, session_id=session_id, destination=destination)

    def restore_index_backup(self, thread_id: str, backup_path: str | Path, checkpoint_ns: str='', *, session_id: str) -> RolloutReadSnapshot:
        return self._maintenance_owner.restore_index_backup(thread_id, backup_path, checkpoint_ns, session_id=session_id)

    def restore_index_backup_offline(self, thread_id: str, backup_path: str | Path, checkpoint_ns: str='', *, session_id: str) -> RolloutReadSnapshot:
        return self._maintenance_owner.restore_index_backup_offline(thread_id, backup_path, checkpoint_ns, session_id=session_id)

    def _recover_fork_materialization(
        self,
        session_id: str,
        thread_id: str,
        checkpoint_ns: str,
        connection: sqlite3.Connection,
        jsonl_path: Path,
    ) -> None:
        self._fork_recovery(
            session_id,
            thread_id,
            checkpoint_ns,
            connection,
            jsonl_path,
        )

    def migrate_schema(self, thread_id: str, *, session_id: str, to_version: int, migration_name: str, migration_sql: str, checkpoint_ns: str='') -> None:
        return self._maintenance_owner.migrate_schema(thread_id, session_id=session_id, to_version=to_version, migration_name=migration_name, migration_sql=migration_sql, checkpoint_ns=checkpoint_ns)


    def _manifest_from_connection(self, connection: sqlite3.Connection, checkpoint_ns: str) -> RolloutManifest:
        return self._maintenance_owner._manifest_from_connection(connection, checkpoint_ns)

    def _namespace_state(self, connection: sqlite3.Connection, checkpoint_ns: str) -> tuple[str, int]:
        return self._maintenance_owner._namespace_state(connection, checkpoint_ns)

    def _ensure_namespace_state(self, connection: sqlite3.Connection, checkpoint_ns: str, timestamp: str) -> None:
        return self._maintenance_owner._ensure_namespace_state(connection, checkpoint_ns, timestamp)

    def _commit_connection(self, connection: sqlite3.Connection) -> None:
        return self._maintenance_owner._commit_connection(connection)

    def _is_removed_rollout_layout(self, root: Path) -> bool:
        return self._maintenance_owner._is_removed_rollout_layout(root)

    def _validate_schema_state(self, connection: sqlite3.Connection) -> None:
        return self._maintenance_owner._validate_schema_state(connection)

    def _validate_v2_commit_offsets(self, connection: sqlite3.Connection, jsonl_path: Path, *, validate_jsonl_items: bool=True) -> None:
        return self._maintenance_owner._validate_v2_commit_offsets(connection, jsonl_path, validate_jsonl_items=validate_jsonl_items)

    def read_lock(self, session_id: str, thread_id: str, checkpoint_ns: str='') -> _RolloutFileLock:
        return self._maintenance_owner.read_lock(session_id, thread_id, checkpoint_ns)

    def open_read_snapshot(self, session_id: str, thread_id: str, checkpoint_ns: str | None='', *, validate_integrity: bool=False) -> RolloutReadSnapshot:
        return self._maintenance_owner.open_read_snapshot(session_id, thread_id, checkpoint_ns, validate_integrity=validate_integrity)

    def validate_index(self, session_id: str, thread_id: str, checkpoint_ns: str='') -> RolloutReadSnapshot:
        return self._maintenance_owner.validate_index(session_id, thread_id, checkpoint_ns)

    def main_thread_id(self, session_id: str) -> str:
        return self.fork_paths.main_thread_id(session_id)

    def repair_index(self, thread_id: str, checkpoint_ns: str='', *, session_id: str, manifest: RolloutManifest | None=None) -> None:
        return self._maintenance_owner.repair_index(thread_id, checkpoint_ns, session_id=session_id, manifest=manifest)

    def _append_v2_records_transaction(
        self, connection: sqlite3.Connection, thread_id: str, checkpoint_ns: str,
        items: Sequence[CanonicalItemRecord], *, session_id: str, commit_kind: str,
        commit_mode: str | None = None, outcome: str | None = None,
        metadata: Mapping[str, object] | None = None, subject_id: str | None = None,
        idempotency_key: str | None = None, begin_transaction: bool = True,
        transaction_id: str | None = None, timestamp: str | None = None,
        first_message_sequence: int | None = None, last_message_sequence: int | None = None,
    ) -> tuple[int, int]:
        return self._maintenance_owner._append_v2_records_transaction(
            connection, thread_id, checkpoint_ns, items, session_id=session_id,
            commit_kind=commit_kind, commit_mode=commit_mode, outcome=outcome,
            metadata=metadata, subject_id=subject_id, idempotency_key=idempotency_key,
            begin_transaction=begin_transaction, transaction_id=transaction_id,
            timestamp=timestamp, first_message_sequence=first_message_sequence,
            last_message_sequence=last_message_sequence,
        )

    def _checkpoint_index(self, row: Sequence[object]) -> RolloutCheckpointIndex:
        return self._maintenance_owner._checkpoint_index(row)

    def rollout_id(self, thread_id: str, checkpoint_ns: str='', *, session_id: str) -> str:
        return self._maintenance_owner.rollout_id(thread_id, checkpoint_ns, session_id=session_id)

    def _existing_index_connection(self, thread_id: str, checkpoint_ns: str, *, session_id: str) -> Iterator[sqlite3.Connection]:
        return self._maintenance_owner._existing_index_connection(thread_id, checkpoint_ns, session_id=session_id)

    def initialize(self, session_id: str, checkpoint_ns: str='', *, thread_id: str | None=None, validate_jsonl_items: bool=True) -> RolloutManifest:
        return self._maintenance_owner.initialize(session_id, checkpoint_ns, thread_id=thread_id, validate_jsonl_items=validate_jsonl_items)

    def _reject_unpublished_migration(self, thread_id: str, checkpoint_ns: str, *, session_id: str) -> None:
        return self._maintenance_owner._reject_unpublished_migration(thread_id, checkpoint_ns, session_id=session_id)

    def _rollout_id(
        self, session_id: str, *, thread_id: str, checkpoint_ns: str
    ) -> str:
        return (
            "rollout-"
            + hashlib.sha256(
                str(self.root(session_id, checkpoint_ns, thread_id=thread_id)).encode()
            ).hexdigest()[:24]
        )
