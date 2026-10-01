"""单文件 rollout 与 SQLite 权威 checkpoint 状态存储。

本模块故意不提供旧的 ``segment-*.jsonl``、manifest 或 message mutation
兼容层。JSONL 只保存已经稳定的 canonical item；所有 checkpoint、view、
branch、fork 和 projection 状态都保存在同一份 SQLite 中。
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from app.core.path_utils import get_session_path_resolver
from app.core.session_catalog_store import validate_session_id
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.assembly.store import (
    ContextAssemblyStorageMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.compaction_boundary_adapter import (
    RolloutCompactionPreflightOwnerMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.context_source_control import (
    ContextSourceControlStorageMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.fork_boundary import (
    RolloutForkBoundaryOwnerMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.operations import (
    RolloutCheckpointOperationsMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.persistence import (
    RolloutCheckpointPersistenceMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.projection.message_materializer import (
    RolloutMessageMaterializerMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.projection.message_projections import (
    RolloutMessageProjectionMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.projection.message_view import (
    RolloutMessageViewMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.view_anchor import (
    RolloutViewAnchorMixin,
)
from app.services.infrastructure.rollout_context.execution.executions import (
    RolloutExecutionsMixin,
)
from app.services.infrastructure.rollout_context.execution.lifecycle import (
    RolloutTurnLifecycleMixin,
)
from app.services.infrastructure.rollout_context.execution.model_calls import (
    RolloutModelCallsMixin,
)
from app.services.infrastructure.rollout_context.execution.replay import (
    RolloutReplayMixin,
)
from app.services.infrastructure.rollout_context.execution.turn_index import (
    RolloutTurnIndexMixin,
)
from app.services.infrastructure.rollout_context.execution.turns import (
    RolloutTurnsMixin,
)
from app.services.infrastructure.rollout_context.fork.completion import (
    ForkCompletionMixin,
)
from app.services.infrastructure.rollout_context.fork.copy_items import (
    ForkItemCopyMixin,
)
from app.services.infrastructure.rollout_context.fork.identity import (
    RolloutIdentityMixin,
)
from app.services.infrastructure.rollout_context.fork.identity_mapping import (
    ForkIdentityMappingMixin,
)
from app.services.infrastructure.rollout_context.fork.materialization import (
    ForkMaterializationMixin,
)
from app.services.infrastructure.rollout_context.fork.metadata import ForkMetadataMixin
from app.services.infrastructure.rollout_context.fork.remap import ForkRemapMixin
from app.services.infrastructure.rollout_context.operations.pruning_owner import (
    RolloutPruningOwner,
)
from app.services.infrastructure.rollout_context.storage.rollout_maintenance_owner import (
    RolloutMaintenanceOwner,
)
from app.services.infrastructure.rollout_context.storage.catalog.indexed_records import (
    IndexedRecordQueryMixin,
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
from app.services.infrastructure.rollout_context.storage.primitives import (
    MessageCodec,
    RolloutCheckpointIndex,
    RolloutManifest,
    RolloutPruningCandidate,
    RolloutPruningPlan,
    RolloutReadSnapshot,
    RolloutTurnAnchor,
    _RolloutOperationLock,
    _RolloutSQLiteConnection,
)
from app.services.infrastructure.rollout_context.storage.schema_upgrade import (
    SchemaUpgradeArtifacts,
    SchemaUpgradePlan,
)
from app.services.infrastructure.rollout_context.storage.queries import (
    RolloutCheckpointQueriesMixin,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    RolloutSerializationMixin,
)
from app.services.infrastructure.rollout_context.storage.transaction_projections import (
    RolloutTransactionProjectionMixin,
)
from app.services.infrastructure.rollout_context.storage.writes import RolloutWriteMixin

__all__ = (
    "RolloutCheckpointIndex",
    "RolloutManifest",
    "RolloutPruningCandidate",
    "RolloutPruningPlan",
    "RolloutReadSnapshot",
    "RolloutTurnAnchor",
)

_ROLLOUT_FILE_LOCK_TIMEOUT_SECONDS = 10.0


class SerializerPort(Protocol):
    """checkpoint 层注入的 typed serializer；storage 不依赖 LangChain。"""

    def dumps_typed(self, value: object) -> tuple[str, bytes]: ...

    def loads_typed(self, value: tuple[str, bytes]) -> object: ...


class RolloutStorage(
    RolloutTurnsMixin,
    RolloutReplayMixin,
    RolloutTurnIndexMixin,
    RolloutExecutionsMixin,
    RolloutModelCallsMixin,
    RolloutTurnLifecycleMixin,
    RolloutCheckpointPersistenceMixin,
    RolloutViewAnchorMixin,
    RolloutCheckpointOperationsMixin,
    RolloutCompactionPreflightOwnerMixin,
    RolloutForkBoundaryOwnerMixin,
    RolloutCheckpointQueriesMixin,
    RolloutMessageViewMixin,
    RolloutMessageMaterializerMixin,
    IndexedRecordQueryMixin,
    TurnProjectionQueryMixin,
    TurnProjectionReadMixin,
    RolloutWriteMixin,
    ForkIdentityMappingMixin,
    RolloutIdentityMixin,
    ForkMetadataMixin,
    ForkRemapMixin,
    ForkMaterializationMixin,
    ForkCompletionMixin,
    ForkItemCopyMixin,
    RolloutItemsMixin,
    RolloutViewMembershipMixin,
    RolloutPartsMixin,
    ContextAssemblyStorageMixin,
    ContextSourceControlStorageMixin,
    RolloutSerializationMixin,
    RolloutTransactionProjectionMixin,
    RolloutMessageProjectionMixin,
    RolloutProjectionMixin,
):
    """一个会话的单 JSONL canonical message 文件和权威 SQLite。"""

    def __init__(
        self,
        sessions_dir: str | Path,
        *,
        serde: SerializerPort | None = None,
        message_codec: MessageCodec | None = None,
    ) -> None:
        self.sessions_dir = Path(sessions_dir).resolve()
        self._path_resolver = get_session_path_resolver(self.sessions_dir)
        self._serde = serde
        self._message_codec = message_codec
        self._locks: dict[tuple[str, str, str], _RolloutOperationLock] = {}
        self._locks_guard = threading.Lock()
        self._active_fork_materializations: set[tuple[str, str]] = set()
        self._maintenance_owner = RolloutMaintenanceOwner(self)
        self._pruning_owner = RolloutPruningOwner(self)

    def _codec(self) -> MessageCodec:
        """返回由 checkpoint 组装层注入的消息适配器。"""
        if self._message_codec is None:
            raise RuntimeError(
                "RolloutStorage 未注入 MessageCodec；storage 不能自行导入或构造 LangChain message"
            )
        return self._message_codec

    def _legacy_migration_is_active(self, key: tuple[str, str]) -> bool:
        """仅供显式 migration storage 暂停其自身的 staging recovery。"""
        active = getattr(self, "_active_legacy_migrations", None)
        return isinstance(active, set) and key in active

    def _lock(
        self,
        thread_id: str,
        checkpoint_ns: str,
        *,
        session_id: str | None = None,
    ) -> _RolloutOperationLock:
        owner_session_id = thread_id if session_id is None else session_id
        owner_thread_id = None if session_id is None else thread_id
        with self._locks_guard:
            key = (owner_session_id, owner_thread_id or "", checkpoint_ns)
            return self._locks.setdefault(
                key,
                _RolloutOperationLock(
                    self.root(
                        owner_session_id,
                        checkpoint_ns,
                        thread_id=owner_thread_id,
                    ).parent
                    / ".rollout.write.lock",
                    timeout_seconds=_ROLLOUT_FILE_LOCK_TIMEOUT_SECONDS,
                ),
            )

    def _resolve_thread_identity(self, session_id: str, thread_id: str | None) -> str:
        """解析 (session_id, thread_id) 的 thread 半。

        thread_id 未显式给出、或给成裸 main 别名 / 裸 session_id 时，必须经
        thread catalog 显式解析该 Session 的唯一 main thread，绝不把 session_id
        或裸 main 当作 thread identity 拼路径。
        """
        if not isinstance(session_id, str) or not session_id:
            raise TypeError("rollout 定位的 session_id 必须是非空字符串")
        try:
            validate_session_id(session_id)
        except (TypeError, ValueError) as error:
            raise RuntimeError(
                "rollout 定位的 session_id 不是 canonical Session 身份（OpenSpec "
                f"8.3 要求 (session_id, thread_id)）：session_id={session_id!r}"
            ) from error
        if thread_id is None or thread_id in ("", "main", session_id):
            # 裸 main/session_id 不是 thread identity，必须经 thread catalog 取
            # 该 Session 冻结的唯一 main thread。
            return self._path_resolver.main_thread_id(session_id)
        if not isinstance(thread_id, str):
            raise TypeError("rollout 定位的 thread_id 必须是字符串")
        return thread_id

    def root(
        self,
        session_id: str,
        checkpoint_ns: str = "",
        *,
        thread_id: str | None = None,
    ) -> Path:
        """返回该 SessionThread 的 rollout 目录（OpenSpec 8.3 thread-qualified）。

        rollout 物理节点、index.sqlite、rollout.jsonl 与单行 database_meta 由精确
        (session_id, thread_id) 的 thread node 独占；checkpoint_ns 不是 owner
        维度，不参与定位。thread_id 缺省时经 thread catalog 显式解析 main thread。

        TODO(8.3 main 落点)：设计冻结 main thread node 为 threads/{main_thread_id}，
        但 SessionCatalogPathResolver.resolve_thread_node 目前仍把 main 折叠到
        session node（R3a 过渡形态）。该解析属 app/core/session_catalog_resolver.py
        （本切片禁改），主 thread 物理落点须待该 resolver 改造后收敛；非 main
        durable thread 已按其自身 frozen locator 解析到独立节点。
        """
        resolved_thread_id = self._resolve_thread_identity(session_id, thread_id)
        return (
            self._path_resolver.resolve_thread_node(session_id, resolved_thread_id)
            / "rollout"
        )

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

    @staticmethod
    def _safe_session_relative_path(
        session_root: Path,
        relative_path: str | Path,
    ) -> Path:
        """解析 session 节点内的相对路径，并逐级拒绝 symlink/越界。"""
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise RuntimeError(f"session relative path 非法: {relative}")
        if session_root.is_symlink() or not session_root.is_dir():
            raise RuntimeError(f"session node 不是安全普通目录: {session_root}")
        current = session_root
        for index, component in enumerate(relative.parts):
            if current.is_symlink() or not current.is_dir():
                raise RuntimeError(f"session path 父目录不是安全普通目录: {current}")
            current = current / component
            if current.is_symlink():
                raise RuntimeError(f"session path 不能包含符号链接: {current}")
            if (
                index < len(relative.parts) - 1
                and current.exists()
                and not current.is_dir()
            ):
                raise RuntimeError(f"session path 父组件不是目录: {current}")
        try:
            current.resolve(strict=False).relative_to(session_root.resolve())
        except ValueError as error:
            raise RuntimeError(f"session path 越出 session node: {relative}") from error
        return current

    def _connect(
        self,
        thread_id: str,
        checkpoint_ns: str = "",
        *,
        session_id: str | None = None,
        read_only: bool = False,
    ) -> sqlite3.Connection:
        if not isinstance(thread_id, str) or not thread_id:
            raise TypeError("rollout thread_id 必须是非空字符串")
        if not isinstance(checkpoint_ns, str):
            raise TypeError("rollout checkpoint_ns 必须是字符串")
        owner_session_id = thread_id if session_id is None else session_id
        owner_thread_id = None if session_id is None else thread_id
        index_path = self.index_path(
            owner_session_id, checkpoint_ns, thread_id=owner_thread_id
        )
        rollout_root = index_path.parent
        if rollout_root.is_symlink():
            raise RuntimeError(f"rollout 目录不能是符号链接: {rollout_root}")
        if rollout_root.exists() and not rollout_root.is_dir():
            raise RuntimeError(f"rollout 路径必须是普通目录: {rollout_root}")
        if index_path.is_symlink():
            raise RuntimeError(f"rollout index.sqlite 不能是符号链接: {index_path}")
        if index_path.exists() and not index_path.is_file():
            raise RuntimeError(f"rollout index.sqlite 必须是普通文件: {index_path}")
        # 不能裸 open/read/close 数据库文件：POSIX close 会释放同进程
        # 其它 SQLite 连接的文件锁。格式校验由下方 SQLite schema 查询完成。
        if read_only:
            # 历史读取不能执行任何会创建 WAL/SHM 或修改 SQLite 文件的操作。
            # 使用 mode=ro 也能让连接层直接拒绝误写，避免只读请求污染工作区
            # 文件监听并触发无意义的前端文件树刷新。
            connection = sqlite3.connect(
                f"file:{quote(str(index_path), safe='/')}?mode=ro",
                uri=True,
                timeout=30,
                check_same_thread=False,
                factory=_RolloutSQLiteConnection,
            )
        else:
            connection = sqlite3.connect(
                index_path,
                timeout=30,
                check_same_thread=False,
                factory=_RolloutSQLiteConnection,
            )
        try:
            # 先做只读 schema 探测，避免对损坏的 index.sqlite 直接执行
            # journal_mode=WAL；SQLite 可能因此创建一个看似可用的空数据库。
            connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
            if read_only:
                connection.execute("PRAGMA query_only = ON")
            else:
                connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 30000")
        except BaseException:
            connection.close()
            raise
        return connection

    @staticmethod
    def _snapshot_connection(snapshot: RolloutReadSnapshot) -> sqlite3.Connection:
        if snapshot.closed:
            raise RuntimeError("rollout read snapshot 已关闭")
        return snapshot.connection

    def plan_pruning(
        self,
        thread_id: str,
        checkpoint_ns: str = "",
        *,
        retain_checkpoint_ids: Iterable[str] = (),
        audit_before_sequence: int | None = None,
    ) -> RolloutPruningPlan:
        """委托 pruning owner 规划逻辑裁剪。"""
        return self._pruning_owner.plan_pruning(
            thread_id,
            checkpoint_ns,
            retain_checkpoint_ids=retain_checkpoint_ids,
            audit_before_sequence=audit_before_sequence,
        )

    def execute_pruning(
        self, thread_id: str, plan: RolloutPruningPlan, checkpoint_ns: str = ""
    ) -> tuple[str, ...]:
        """委托 pruning owner 执行已规划的逻辑裁剪。"""
        return self._pruning_owner.execute_pruning(thread_id, plan, checkpoint_ns)

    def _require_regular_file(self, path: Path, *, field: str) -> None:
        return self._maintenance_owner._require_regular_file(path, field=field)

    def _file_hash(self, path: Path) -> str:
        return self._maintenance_owner._file_hash(path)

    def _fsync_directory(self, path: Path) -> None:
        return self._maintenance_owner._fsync_directory(path)

    def _require_plain_directory(self, path: Path, *, field: str) -> None:
        return self._maintenance_owner._require_plain_directory(path, field=field)

    def _validate_offline_restore_candidate(self, connection: sqlite3.Connection, *, thread_id: str, checkpoint_ns: str) -> None:
        return self._maintenance_owner._validate_offline_restore_candidate(connection, thread_id=thread_id, checkpoint_ns=checkpoint_ns)

    def _prepare_offline_restore_candidate(self, source_path: Path, candidate_path: Path, *, thread_id: str, checkpoint_ns: str) -> str:
        return self._maintenance_owner._prepare_offline_restore_candidate(source_path, candidate_path, thread_id=thread_id, checkpoint_ns=checkpoint_ns)

    def _require_standalone_offline_restore_source(self, source_path: Path) -> None:
        return self._maintenance_owner._require_standalone_offline_restore_source(source_path)

    def _require_offline_restore_target(self, target_path: Path) -> None:
        return self._maintenance_owner._require_offline_restore_target(target_path)

    def backup_index(self, thread_id: str, checkpoint_ns: str='', *, destination: str | Path | None=None) -> Path:
        return self._maintenance_owner.backup_index(thread_id, checkpoint_ns, destination=destination)

    def _backup_index_unlocked(self, thread_id: str, checkpoint_ns: str, *, destination: str | Path | None) -> Path:
        return self._maintenance_owner._backup_index_unlocked(thread_id, checkpoint_ns, destination=destination)

    def restore_index_backup(self, thread_id: str, backup_path: str | Path, checkpoint_ns: str='') -> RolloutReadSnapshot:
        return self._maintenance_owner.restore_index_backup(thread_id, backup_path, checkpoint_ns)

    def restore_index_backup_offline(self, thread_id: str, backup_path: str | Path, checkpoint_ns: str='') -> RolloutReadSnapshot:
        return self._maintenance_owner.restore_index_backup_offline(thread_id, backup_path, checkpoint_ns)

    def _restore_index_backup_unlocked(self, thread_id: str, checkpoint_ns: str, source_path: Path) -> None:
        return self._maintenance_owner._restore_index_backup_unlocked(thread_id, checkpoint_ns, source_path)

    def _recover_fork_materialization(self, thread_id: str, checkpoint_ns: str, connection: sqlite3.Connection, jsonl_path: Path) -> None:
        return self._maintenance_owner._recover_fork_materialization(thread_id, checkpoint_ns, connection, jsonl_path)

    def migrate_schema(self, thread_id: str, *, to_version: int, migration_name: str, migration_sql: str, checkpoint_ns: str='') -> RolloutReadSnapshot:
        return self._maintenance_owner.migrate_schema(thread_id, to_version=to_version, migration_name=migration_name, migration_sql=migration_sql, checkpoint_ns=checkpoint_ns)

    def _migrate_schema_locked(self, thread_id: str, *, checkpoint_ns: str, to_version: int, migration_name: str, migration_sql: str, validate_migrated: Callable[[sqlite3.Connection], None] | None=None, pending_artifact_audit: bool=False, allow_failed_retry: bool=False) -> None:
        return self._maintenance_owner._migrate_schema_locked(thread_id, checkpoint_ns=checkpoint_ns, to_version=to_version, migration_name=migration_name, migration_sql=migration_sql, validate_migrated=validate_migrated, pending_artifact_audit=pending_artifact_audit, allow_failed_retry=allow_failed_retry)

    def _manifest_from_connection(self, connection: sqlite3.Connection, checkpoint_ns: str, *, allow_migrating: bool=False) -> RolloutManifest:
        return self._maintenance_owner._manifest_from_connection(connection, checkpoint_ns, allow_migrating=allow_migrating)

    def _namespace_state(self, connection: sqlite3.Connection, checkpoint_ns: str) -> tuple[str, int]:
        return self._maintenance_owner._namespace_state(connection, checkpoint_ns)

    def _ensure_namespace_state(self, connection: sqlite3.Connection, checkpoint_ns: str, timestamp: str) -> None:
        return self._maintenance_owner._ensure_namespace_state(connection, checkpoint_ns, timestamp)

    def _initialize_schema(self, thread_id: str, checkpoint_ns: str) -> None:
        return self._maintenance_owner._initialize_schema(thread_id, checkpoint_ns)

    def _commit_connection(self, connection: sqlite3.Connection) -> None:
        return self._maintenance_owner._commit_connection(connection)

    def _is_removed_rollout_layout(self, root: Path) -> bool:
        return self._maintenance_owner._is_removed_rollout_layout(root)

    def _validate_schema_state(self, connection: sqlite3.Connection, *, allow_older_schema: bool=False, pending_retry: tuple[int, int, str, str | None] | None=None) -> None:
        return self._maintenance_owner._validate_schema_state(connection, allow_older_schema=allow_older_schema, pending_retry=pending_retry)

    def _validate_v2_commit_offsets(self, connection: sqlite3.Connection, jsonl_path: Path, *, validate_jsonl_items: bool=True) -> None:
        return self._maintenance_owner._validate_v2_commit_offsets(connection, jsonl_path, validate_jsonl_items=validate_jsonl_items)

    def open_read_snapshot(self, thread_id: str, checkpoint_ns: str='', *, validate_integrity: bool=False) -> RolloutReadSnapshot:
        return self._maintenance_owner.open_read_snapshot(thread_id, checkpoint_ns, validate_integrity=validate_integrity)

    def validate_index(self, thread_id: str, checkpoint_ns: str='') -> RolloutReadSnapshot:
        return self._maintenance_owner.validate_index(thread_id, checkpoint_ns)

    def repair_index(self, thread_id: str, checkpoint_ns: str='', *, manifest: RolloutManifest | None=None) -> None:
        return self._maintenance_owner.repair_index(thread_id, checkpoint_ns, manifest=manifest)

    def _append_v2_records_transaction(self, connection: sqlite3.Connection, thread_id: str, checkpoint_ns: str, items: Sequence[CanonicalItemRecord], *, commit_kind: str, commit_mode: str | None=None, outcome: str | None=None, metadata: Mapping[str, object] | None=None, subject_id: str | None=None, idempotency_key: str | None=None, begin_transaction: bool=True) -> tuple[int, int]:
        return self._maintenance_owner._append_v2_records_transaction(connection, thread_id, checkpoint_ns, items, commit_kind=commit_kind, commit_mode=commit_mode, outcome=outcome, metadata=metadata, subject_id=subject_id, idempotency_key=idempotency_key, begin_transaction=begin_transaction)

    def _checkpoint_index(self, row: Sequence[object]) -> RolloutCheckpointIndex:
        return self._maintenance_owner._checkpoint_index(row)

    def rollout_id(self, thread_id: str, checkpoint_ns: str='') -> str:
        return self._maintenance_owner.rollout_id(thread_id, checkpoint_ns)

    def _existing_index_connection(self, thread_id: str, checkpoint_ns: str) -> Iterator[sqlite3.Connection]:
        return self._maintenance_owner._existing_index_connection(thread_id, checkpoint_ns)

    def initialize(self, thread_id: str, checkpoint_ns: str='', *, validate_jsonl_items: bool=True, _allow_schema_upgrade: bool=False) -> RolloutManifest:
        return self._maintenance_owner.initialize(thread_id, checkpoint_ns, validate_jsonl_items=validate_jsonl_items, _allow_schema_upgrade=_allow_schema_upgrade)

    def _reject_unpublished_migration(self, thread_id: str, checkpoint_ns: str) -> None:
        return self._maintenance_owner._reject_unpublished_migration(thread_id, checkpoint_ns)

    def repair_active_context_view(self, thread_id: str, checkpoint_ns: str='') -> bool:
        return self._maintenance_owner.repair_active_context_view(thread_id, checkpoint_ns)

    def ensure_active_view_contains_turn_root(self, thread_id: str, *, turn_id: str, checkpoint_ns: str='') -> bool:
        return self._maintenance_owner.ensure_active_view_contains_turn_root(thread_id, turn_id=turn_id, checkpoint_ns=checkpoint_ns)

    def upgrade_v2_schema(
        self, thread_id: str, *, checkpoint_ns: str = "",
        prepare_artifact_upgrade: Callable[[sqlite3.Connection], SchemaUpgradeArtifacts] | None = None,
        resume_artifact_upgrade: Callable[[sqlite3.Connection], bool] | None = None,
        prepare_plan_upgrade: Callable[[sqlite3.Connection], SchemaUpgradePlan] | None = None,
    ) -> RolloutReadSnapshot:
        return self._maintenance_owner.upgrade_v2_schema(
            thread_id,
            checkpoint_ns=checkpoint_ns,
            prepare_artifact_upgrade=prepare_artifact_upgrade,
            resume_artifact_upgrade=resume_artifact_upgrade,
            prepare_plan_upgrade=prepare_plan_upgrade,
        )
