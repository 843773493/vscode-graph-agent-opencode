"""单文件 rollout 与 SQLite 权威 checkpoint 状态存储。

本模块故意不提供旧的 ``segment-*.jsonl``、manifest 或 message mutation
兼容层。JSONL 只保存已经稳定的 canonical item；所有 checkpoint、view、
branch、fork 和 projection 状态都保存在同一份 SQLite 中。
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from app.core.path_utils import get_session_path_resolver
from app.core.session_catalog_store import (
    SessionCreationRecord,
    validate_session_id,
    validate_thread_id,
)
from app.core.session_thread_context import require_current_session_thread_owner
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.assembly.store import (
    ContextAssemblyStorageMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.boundary.compaction_boundary_adapter import (
    RolloutCompactionPreflightOwnerMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.boundary.fork_boundary import (
    RolloutForkBoundaryOwnerMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.durable.context_source_versions import (
    ContextSourceVersionsStorageMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.durable.operations import (
    RolloutCheckpointOperationsMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.durable.persistence import (
    RolloutCheckpointPersistenceMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.durable.view_anchor import (
    RolloutViewAnchorMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.owner.context_source_control import (
    ContextSourceControlStorageMixin,
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
from app.services.infrastructure.rollout_context.storage.catalog.indexed_records import (
    IndexedRecordQueryMixin,
)
from app.services.infrastructure.rollout_context.storage.catalog.internal_history import (
    InternalExecutionHistoryMixin,
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
from app.services.infrastructure.rollout_context.storage.queries import (
    RolloutCheckpointQueriesMixin,
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
_prepared_fork_target: ContextVar[SessionCreationRecord | None] = ContextVar(
    "prepared_fork_target",
    default=None,
)


@dataclass(frozen=True, slots=True)
class _ForkSourceSnapshot:
    session_id: str
    thread_id: str
    session_node: Path


_fork_source_snapshot: ContextVar[_ForkSourceSnapshot | None] = ContextVar(
    "fork_source_snapshot",
    default=None,
)


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
    InternalExecutionHistoryMixin,
    RolloutPartsMixin,
    ContextAssemblyStorageMixin,
    ContextSourceControlStorageMixin,
    ContextSourceVersionsStorageMixin,
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
        self._active_fork_materializations: set[tuple[str, str, str]] = set()
        self._maintenance_owner = RolloutMaintenanceOwner(self)
        self._pruning_owner = RolloutPruningOwner(self)

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
        owner_thread_id = self._resolve_thread_identity(session_id, thread_id)
        with self._locks_guard:
            key = (session_id, owner_thread_id, checkpoint_ns)
            return self._locks.setdefault(
                key,
                _RolloutOperationLock(
                    self.root(
                        session_id,
                        checkpoint_ns,
                        thread_id=owner_thread_id,
                    ).parent
                    / ".rollout.write.lock",
                    timeout_seconds=_ROLLOUT_FILE_LOCK_TIMEOUT_SECONDS,
                ),
            )

    @contextmanager
    def prepared_fork_target_scope(
        self,
        record: SessionCreationRecord,
    ) -> Iterator[Path]:
        """把创建记录 capability 限定为本次 fork 的 target 路径解析权限。"""
        if not isinstance(record, SessionCreationRecord):
            raise TypeError("prepared fork target 必须是 SessionCreationRecord")
        target_path = self._path_resolver.resolve_prepared_session_node(record)
        token: Token[SessionCreationRecord | None] = _prepared_fork_target.set(record)
        try:
            yield target_path
        finally:
            _prepared_fork_target.reset(token)

    @contextmanager
    def fork_source_snapshot_scope(
        self,
        *,
        session_id: str,
        thread_id: str,
        session_node: Path,
    ) -> Iterator[Path]:
        """把 fork source 的所有 rollout I/O 定向到已冻结的私有目录。"""
        validate_session_id(session_id)
        validate_thread_id(thread_id)
        if not isinstance(session_node, Path):
            raise TypeError("fork source snapshot session_node 必须是 Path")
        if _fork_source_snapshot.get() is not None:
            raise RuntimeError("fork source snapshot scope 不能嵌套")
        snapshot_node = session_node.resolve(strict=True)
        thread_node = snapshot_node
        if (
            snapshot_node.is_symlink()
            or not snapshot_node.is_dir()
            or thread_node.is_symlink()
            or not thread_node.is_dir()
        ):
            raise RuntimeError(
                "fork source snapshot 必须是含冻结 main thread 的普通目录: "
                f"session_node={snapshot_node}, thread_id={thread_id}"
            )
        snapshot = _ForkSourceSnapshot(session_id, thread_id, snapshot_node)
        token: Token[_ForkSourceSnapshot | None] = _fork_source_snapshot.set(
            snapshot
        )
        try:
            yield thread_node
        finally:
            _fork_source_snapshot.reset(token)

    @staticmethod
    def _fork_source_snapshot_for_session(
        session_id: str,
    ) -> _ForkSourceSnapshot | None:
        snapshot = _fork_source_snapshot.get()
        return snapshot if snapshot is not None and snapshot.session_id == session_id else None

    @staticmethod
    def _prepared_target_record(
        session_id: str,
    ) -> SessionCreationRecord | None:
        record = _prepared_fork_target.get()
        return record if record is not None and record.session_id == session_id else None

    def _resolve_session_node(self, session_id: str) -> Path:
        record = self._prepared_target_record(session_id)
        if record is not None:
            return self._path_resolver.resolve_prepared_session_node(record)
        snapshot = self._fork_source_snapshot_for_session(session_id)
        if snapshot is not None:
            return snapshot.session_node
        return self._path_resolver.resolve_session_node(session_id)

    def _resolve_thread_node(self, session_id: str, thread_id: str) -> Path:
        record = self._prepared_target_record(session_id)
        if record is not None:
            return self._path_resolver.resolve_prepared_thread_node(record, thread_id)
        snapshot = self._fork_source_snapshot_for_session(session_id)
        if snapshot is not None:
            if thread_id != snapshot.thread_id:
                raise RuntimeError(
                    "fork source snapshot 只包含冻结的 source main thread: "
                    f"session_id={session_id}, expected={snapshot.thread_id}, "
                    f"actual={thread_id}"
                )
            return snapshot.session_node
        return self._path_resolver.resolve_thread_node(session_id, thread_id)

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
        if thread_id is None:
            record = self._prepared_target_record(session_id)
            snapshot = self._fork_source_snapshot_for_session(session_id)
            thread_id = (
                record.main_thread_id
                if record is not None
                else snapshot.thread_id
                if snapshot is not None
                else self._path_resolver.main_thread_id(session_id)
            )
        if not isinstance(thread_id, str):
            raise TypeError("rollout 定位的 thread_id 必须是字符串")
        if thread_id in ("", "main", session_id):
            raise RuntimeError(
                "rollout 定位拒绝 thread alias；必须传入 catalog 的真实 thread_id: "
                f"session_id={session_id!r}, thread_id={thread_id!r}"
            )
        try:
            validate_thread_id(thread_id)
        except (TypeError, ValueError) as error:
            raise RuntimeError(
                f"rollout 定位的 thread_id 不是 canonical Thread 身份: {thread_id!r}"
            ) from error
        record = self._prepared_target_record(session_id)
        if record is not None:
            self._path_resolver.resolve_prepared_thread_node(record, thread_id)
        return thread_id

    def root(
        self,
        session_id: str,
        checkpoint_ns: str = "",
        *,
        thread_id: str | None = None,
    ) -> Path:
        """返回该 SessionThread 的 rollout 目录（OpenSpec 8.3 thread-qualified）。

        main thread 折叠到 Session 根目录，child thread 位于受检 thread node；
        每个 rollout、index.sqlite、rollout.jsonl 与 database_meta 都由精确
        (session_id, thread_id) 独占。checkpoint_ns 不是 owner 维度。
        thread_id 缺省时经 thread catalog 显式解析 main thread。

        字面量 `main`、Session ID 或缺失 Thread 只允许在入口显式解析一次；
        此处将缺省 main 映射到 catalog 中冻结的真实 ID，之后所有路径都由该
        `(session_id, thread_id)` 解析，不保留第二套旧物理布局。
        """
        resolved_thread_id = self._resolve_thread_identity(session_id, thread_id)
        thread_node = self._resolve_thread_node(session_id, resolved_thread_id)
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
        session_id: str,
        read_only: bool = False,
    ) -> sqlite3.Connection:
        if not isinstance(checkpoint_ns, str):
            raise TypeError("rollout checkpoint_ns 必须是字符串")
        owner_thread_id = self._resolve_thread_identity(session_id, thread_id)
        index_path = self.index_path(
            session_id, checkpoint_ns, thread_id=owner_thread_id
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

    def _validate_offline_restore_candidate(self, connection: sqlite3.Connection, *, session_id: str, thread_id: str, checkpoint_ns: str) -> None:
        return self._maintenance_owner._validate_offline_restore_candidate(connection, session_id=session_id, thread_id=thread_id, checkpoint_ns=checkpoint_ns)

    def _prepare_offline_restore_candidate(self, source_path: Path, candidate_path: Path, *, session_id: str, thread_id: str, checkpoint_ns: str) -> str:
        return self._maintenance_owner._prepare_offline_restore_candidate(source_path, candidate_path, session_id=session_id, thread_id=thread_id, checkpoint_ns=checkpoint_ns)

    def _require_standalone_offline_restore_source(self, source_path: Path) -> None:
        return self._maintenance_owner._require_standalone_offline_restore_source(source_path)

    def _require_offline_restore_target(self, target_path: Path) -> None:
        return self._maintenance_owner._require_offline_restore_target(target_path)

    def backup_index(self, thread_id: str, checkpoint_ns: str='', *, session_id: str, destination: str | Path | None=None) -> Path:
        return self._maintenance_owner.backup_index(thread_id, checkpoint_ns, session_id=session_id, destination=destination)

    def _backup_index_unlocked(self, thread_id: str, checkpoint_ns: str, *, session_id: str, destination: str | Path | None) -> Path:
        return self._maintenance_owner._backup_index_unlocked(thread_id, checkpoint_ns, session_id=session_id, destination=destination)

    def restore_index_backup(self, thread_id: str, backup_path: str | Path, checkpoint_ns: str='', *, session_id: str) -> RolloutReadSnapshot:
        return self._maintenance_owner.restore_index_backup(thread_id, backup_path, checkpoint_ns, session_id=session_id)

    def restore_index_backup_offline(self, thread_id: str, backup_path: str | Path, checkpoint_ns: str='', *, session_id: str) -> RolloutReadSnapshot:
        return self._maintenance_owner.restore_index_backup_offline(thread_id, backup_path, checkpoint_ns, session_id=session_id)

    def _restore_index_backup_unlocked(self, thread_id: str, checkpoint_ns: str, source_path: Path, *, session_id: str) -> None:
        return self._maintenance_owner._restore_index_backup_unlocked(thread_id, checkpoint_ns, source_path, session_id=session_id)

    def _recover_fork_materialization(
        self,
        session_id: str,
        thread_id: str,
        checkpoint_ns: str,
        connection: sqlite3.Connection,
        jsonl_path: Path,
    ) -> None:
        self._maintenance_owner._recover_fork_materialization(
            session_id,
            thread_id,
            checkpoint_ns,
            connection,
            jsonl_path,
        )

    def migrate_schema(self, thread_id: str, *, session_id: str, to_version: int, migration_name: str, migration_sql: str, checkpoint_ns: str='') -> RolloutReadSnapshot:
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

    def open_read_snapshot(self, session_id: str, thread_id: str, checkpoint_ns: str='', *, validate_integrity: bool=False) -> RolloutReadSnapshot:
        return self._maintenance_owner.open_read_snapshot(session_id, thread_id, checkpoint_ns, validate_integrity=validate_integrity)

    def validate_index(self, session_id: str, thread_id: str, checkpoint_ns: str='') -> RolloutReadSnapshot:
        return self._maintenance_owner.validate_index(session_id, thread_id, checkpoint_ns)

    def main_thread_id(self, session_id: str) -> str:
        record = self._prepared_target_record(session_id)
        if record is not None:
            self._path_resolver.resolve_prepared_session_node(record)
            return record.main_thread_id
        snapshot = self._fork_source_snapshot_for_session(session_id)
        if snapshot is not None:
            return snapshot.thread_id
        return self._path_resolver.main_thread_id(session_id)

    def repair_index(self, thread_id: str, checkpoint_ns: str='', *, session_id: str, manifest: RolloutManifest | None=None) -> None:
        return self._maintenance_owner.repair_index(thread_id, checkpoint_ns, session_id=session_id, manifest=manifest)

    def _append_v2_records_transaction(self, connection: sqlite3.Connection, thread_id: str, checkpoint_ns: str, items: Sequence[CanonicalItemRecord], *, session_id: str, commit_kind: str, commit_mode: str | None=None, outcome: str | None=None, metadata: Mapping[str, object] | None=None, subject_id: str | None=None, idempotency_key: str | None=None, begin_transaction: bool=True) -> tuple[int, int]:
        return self._maintenance_owner._append_v2_records_transaction(connection, thread_id, checkpoint_ns, items, session_id=session_id, commit_kind=commit_kind, commit_mode=commit_mode, outcome=outcome, metadata=metadata, subject_id=subject_id, idempotency_key=idempotency_key, begin_transaction=begin_transaction)

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

    def repair_active_context_view(self, thread_id: str, checkpoint_ns: str='', *, session_id: str) -> bool:
        return self._maintenance_owner.repair_active_context_view(thread_id, checkpoint_ns, session_id=session_id)

    def ensure_active_view_contains_turn_root(self, session_id: str, thread_id: str, *, turn_id: str, checkpoint_ns: str='') -> bool:
        return self._maintenance_owner.ensure_active_view_contains_turn_root(session_id, thread_id, turn_id=turn_id, checkpoint_ns=checkpoint_ns)
