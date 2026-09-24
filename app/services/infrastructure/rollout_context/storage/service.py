"""单文件 rollout 与 SQLite 权威 checkpoint 状态存储。

本模块故意不提供旧的 ``segment-*.jsonl``、manifest 或 message mutation
兼容层。JSONL 只保存已经稳定的 canonical item；所有 checkpoint、view、
branch、fork 和 projection 状态都保存在同一份 SQLite 中。
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from app.core.path_utils import get_session_path_resolver
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
from app.services.infrastructure.rollout_context.operations.pruning import (
    RolloutPruningMixin,
)
from app.services.infrastructure.rollout_context.storage.backups import (
    RolloutStorageBackupMixin,
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
from app.services.infrastructure.rollout_context.storage.maintenance import (
    RolloutStorageMaintenanceMixin,
)
from app.services.infrastructure.rollout_context.storage.migrations import (
    RolloutSchemaMigrationMixin,
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
from app.services.infrastructure.rollout_context.storage.recovery import (
    RolloutRecoveryMixin,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    RolloutSerializationMixin,
)
from app.services.infrastructure.rollout_context.storage.startup import (
    RolloutStartupMixin,
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


def _now() -> str:
    return datetime.now(UTC).isoformat()


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
    RolloutPruningMixin,
    RolloutItemsMixin,
    RolloutViewMembershipMixin,
    RolloutPartsMixin,
    RolloutStorageBackupMixin,
    RolloutRecoveryMixin,
    RolloutSchemaMigrationMixin,
    RolloutStorageMaintenanceMixin,
    RolloutStartupMixin,
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
        self._locks: dict[tuple[str, str], _RolloutOperationLock] = {}
        self._locks_guard = threading.Lock()
        self._active_fork_materializations: set[tuple[str, str]] = set()

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

    def _lock(self, thread_id: str, checkpoint_ns: str) -> _RolloutOperationLock:
        with self._locks_guard:
            key = (thread_id, checkpoint_ns)
            return self._locks.setdefault(
                key,
                _RolloutOperationLock(
                    self.root(thread_id, checkpoint_ns).parent / ".rollout.write.lock",
                    timeout_seconds=_ROLLOUT_FILE_LOCK_TIMEOUT_SECONDS,
                ),
            )

    def root(self, thread_id: str, checkpoint_ns: str = "") -> Path:
        del checkpoint_ns
        return (
            self._path_resolver.resolve_session_node_for_runtime(thread_id) / "rollout"
        )

    def index_path(self, thread_id: str, checkpoint_ns: str = "") -> Path:
        return self.root(thread_id, checkpoint_ns) / "index.sqlite"

    def jsonl_path(self, thread_id: str, checkpoint_ns: str = "") -> Path:
        return self.root(thread_id, checkpoint_ns) / "rollout.jsonl"

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
        read_only: bool = False,
    ) -> sqlite3.Connection:
        if not isinstance(thread_id, str) or not thread_id:
            raise TypeError("rollout thread_id 必须是非空字符串")
        if not isinstance(checkpoint_ns, str):
            raise TypeError("rollout checkpoint_ns 必须是字符串")
        index_path = self.index_path(thread_id, checkpoint_ns)
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
