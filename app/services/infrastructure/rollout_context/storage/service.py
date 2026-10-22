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
@dataclass(frozen=True, slots=True)
class _PreparedForkTarget:
    session_id: str
    thread_id: str
    session_node: Path
    thread_node: Path


_prepared_fork_target: ContextVar[_PreparedForkTarget | None] = ContextVar(
    "prepared_fork_target",
    default=None,
)


@dataclass(frozen=True, slots=True)
class _ForkSourceSnapshot:
    session_id: str
    thread_id: str
    captured_root: Path


_fork_source_snapshot: ContextVar[_ForkSourceSnapshot | None] = ContextVar(
    "fork_source_snapshot",
    default=None,
)


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
        owner_thread_id = self._resolve_thread_identity(session_id, thread_id)
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

    @contextmanager
    def prepared_fork_target_scope(
        self,
        record: SessionCreationRecord,
    ) -> Iterator[Path]:
        """把创建记录 capability 限定为本次 fork 的 target 路径解析权限。"""
        if not isinstance(record, SessionCreationRecord):
            raise TypeError("prepared fork target 必须是 SessionCreationRecord")
        target_path = self._path_resolver.resolve_prepared_session_node(record)
        thread_path = self._path_resolver.resolve_prepared_thread_node(record, record.main_thread_id)
        target = _PreparedForkTarget(record.session_id, record.main_thread_id, target_path, thread_path)
        token = _prepared_fork_target.set(target)
        try:
            yield target_path
        finally:
            _prepared_fork_target.reset(token)

    @contextmanager
    def prepared_child_fork_target_scope(self, session_id: str, record) -> Iterator[Path]:
        """Thread creation owner 的冻结 record 只授予该未发布 child 的路径能力。"""
        from app.core.session_control_store import (
            SessionControlStore,
            ThreadCreationRecord,
        )

        if not isinstance(record, ThreadCreationRecord) or record.purpose != "fork":
            raise TypeError("prepared child fork target 必须是 fork ThreadCreationRecord")
        session_node = self._path_resolver.resolve_session_node(session_id)
        control = SessionControlStore(session_node / "session-control.sqlite", read_only=True)
        try:
            current = control.get_thread_creation_record(record.thread_creation_idempotency_key)
            if current != record or current.state not in {"preparing", "published"}:
                raise RuntimeError("prepared child fork target 的冻结 creation intent 已变化")
        finally:
            control.close()
        thread_node = session_node / record.final_relative_locator
        if thread_node.is_symlink() or not thread_node.is_dir():
            raise RuntimeError("prepared child fork target 缺少已 rename 的普通节点目录")
        target = _PreparedForkTarget(session_id, record.child_thread_id, session_node, thread_node)
        token = _prepared_fork_target.set(target)
        try:
            yield thread_node
        finally:
            _prepared_fork_target.reset(token)

    @contextmanager
    def fork_source_snapshot_scope(
        self,
        *,
        session_id: str,
        thread_id: str,
        captured_root: Path,
    ) -> Iterator[Path]:
        """把 source Thread 的 rollout I/O 定向到私有 snapshot captured_root。

        ``session_id``/``thread_id`` 仅标识来源历史 owner，thread_id 可为 child；
        物理 I/O 只使用调用方传入的私有捕获副本 ``captured_root``，不按 main_thread_id
        或 thread_id 推导路径。
        """
        validate_session_id(session_id)
        validate_thread_id(thread_id)
        if not isinstance(captured_root, Path):
            raise TypeError("fork source snapshot captured_root 必须是 Path")
        if _fork_source_snapshot.get() is not None:
            raise RuntimeError("fork source snapshot scope 不能嵌套")
        snapshot_node = captured_root.resolve(strict=True)
        thread_node = snapshot_node
        if (
            snapshot_node.is_symlink()
            or not snapshot_node.is_dir()
            or thread_node.is_symlink()
            or not thread_node.is_dir()
        ):
            raise RuntimeError(
                "fork source Thread 的私有 snapshot captured_root 必须是普通目录: "
                f"captured_root={snapshot_node}, thread_id={thread_id}"
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
    ) -> _PreparedForkTarget | None:
        record = _prepared_fork_target.get()
        return record if record is not None and record.session_id == session_id else None

    def _resolve_session_node(self, session_id: str) -> Path:
        record = self._prepared_target_record(session_id)
        if record is not None:
            return record.session_node
        snapshot = self._fork_source_snapshot_for_session(session_id)
        if snapshot is not None:
            return snapshot.captured_root
        return self._path_resolver.resolve_session_node(session_id)

    def _resolve_thread_node(self, session_id: str, thread_id: str) -> Path:
        snapshot = self._fork_source_snapshot_for_session(session_id)
        if snapshot is not None and thread_id == snapshot.thread_id:
            return snapshot.captured_root
        record = self._prepared_target_record(session_id)
        if record is not None and thread_id == record.thread_id:
            return record.thread_node
        if snapshot is not None:
            raise RuntimeError("fork source snapshot 只授予冻结 source Thread 的路径能力")
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
                record.thread_id
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
        return thread_id

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
        if not read_only and self._fork_source_snapshot_for_session(session_id) is not None:
            raise RuntimeError(
                "fork frozen source snapshot 禁止可写 SQLite 连接: "
                f"session_id={session_id}, thread_id={thread_id}, checkpoint_ns={checkpoint_ns!r}"
            )
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
            # mode=ro 在连接层拒绝业务写入，但在线 WAL 读取仍会更新 SHM read-mark。
            # SHM 是 SQLite 协调状态；数据库、WAL 与 canonical 正文不得被读取链改写。
            # 仅冻结 source snapshot 使用 immutable，在线源必须读到已提交 WAL。
            connection = sqlite3.connect(
                f"file:{quote(str(index_path), safe='/')}?mode=ro"
                + ("&immutable=1" if self._fork_source_snapshot_for_session(session_id) is not None else ""),
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
