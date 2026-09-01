"""单文件 rollout 与 SQLite 权威 checkpoint 状态存储。

本模块故意不提供旧的 ``segment-*.jsonl``、manifest 或 message mutation
兼容层。JSONL 只保存已经稳定的 canonical message；所有 checkpoint、view、
branch、fork 和 projection 状态都保存在同一份 SQLite 中。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Self
from urllib.parse import quote
from uuid import uuid4

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    ToolMessage,
    message_to_dict,
    messages_from_dict,
)
from langgraph.checkpoint.base import Checkpoint, CheckpointMetadata, PendingWrite
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.core.message_content_projection import reasoning_projection_rows
from app.core.message_content_projection import (
    visible_text as canonical_visible_text,
)
from app.core.path_utils import get_session_path_resolver

ROLLOUT_SCHEMA_VERSION = 1
MESSAGE_FORMAT_VERSION = 1
_DEFAULT_NAMESPACE = ""
_VISIBLE_TEXT_LIMIT = 64 * 1024
_ROLLOUT_FILE_LOCK_TIMEOUT_SECONDS = 10.0
_ROLLOUT_FILE_LOCK_POLL_INTERVAL_SECONDS = 0.05
# 旧版本曾把隐藏 system_reminder 建成 normal Turn。读取上下文时只把含有
# 可见用户消息的 normal Turn 作为聊天轮次，避免历史中出现空的 assistant Turn。
_VISIBLE_NORMAL_TURN_PREDICATE = (
    "EXISTS (SELECT 1 FROM messages AS visible_user_message "
    "WHERE visible_user_message.turn_id = t.turn_id "
    "AND visible_user_message.role = 'user' "
    "AND visible_user_message.visibility = 'visible')"
)


@dataclass(frozen=True, slots=True)
class RolloutManifest:
    """从 SQLite 快照生成的内存状态，不是磁盘 manifest 文件。"""

    rollout_id: str
    checkpoint_ns: str
    active_branch_id: str
    committed_sequence: int
    latest_checkpoint_id: str | None
    projection_epoch: int


@dataclass(frozen=True, slots=True)
class RolloutTurnAnchor:
    """从 active view 解析出的完整 Turn 用户锚点。"""

    turn_id: str
    view_id: str
    checkpoint_id: str
    branch_id: str
    logical_turn_ordinal: int
    first_message_sequence: int
    user_message_sequence: int
    last_message_sequence: int
    final_message_sequence: int | None
    anchor_mode: str
    cutoff_message_sequence: int


class _RolloutFileLock:
    """为 JSONL 与 SQLite 的跨文件读取/写入提供进程间文件锁。"""

    def __init__(
        self,
        path: Path,
        *,
        exclusive: bool,
        timeout_seconds: float = _ROLLOUT_FILE_LOCK_TIMEOUT_SECONDS,
    ) -> None:
        if isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
            raise ValueError("rollout 文件锁 timeout_seconds 必须大于 0")
        self._path = path
        self._exclusive = exclusive
        self._timeout_seconds = timeout_seconds
        self._handle: BinaryIO | None = None

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+b")
        try:
            if os.name == "nt":
                # TODO: Windows CI 覆盖共享读锁；Windows 当前使用独占锁保证跨文件一致性。
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b" ")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                mode = fcntl.LOCK_EX if self._exclusive else fcntl.LOCK_SH
                deadline = time.monotonic() + self._timeout_seconds
                while True:
                    try:
                        fcntl.flock(handle.fileno(), mode | fcntl.LOCK_NB)
                    except BlockingIOError as error:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError(
                                "rollout 文件锁获取超时: "
                                f"path={self._path} timeout_seconds={self._timeout_seconds:g}"
                            ) from error
                        time.sleep(
                            min(_ROLLOUT_FILE_LOCK_POLL_INTERVAL_SECONDS, remaining)
                        )
                    else:
                        break
        except TimeoutError:
            handle.close()
            raise
        except (BlockingIOError, OSError) as error:
            handle.close()
            raise RuntimeError(f"rollout 文件锁获取失败: {self._path}") from error
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._handle = None


class _RolloutOperationLock:
    """同一进程可重入、跨进程独占的 rollout 写入锁。"""

    def __init__(self, path: Path) -> None:
        self._thread_lock = threading.RLock()
        self._path = path
        self._depth = 0
        self._file_lock: _RolloutFileLock | None = None

    def __enter__(self) -> Self:
        self._thread_lock.acquire()
        if self._depth == 0:
            file_lock = _RolloutFileLock(self._path, exclusive=True)
            try:
                file_lock.acquire()
            except Exception:
                self._thread_lock.release()
                raise
            self._file_lock = file_lock
        self._depth += 1
        return self

    def __exit__(self, *_: object) -> None:
        self._depth -= 1
        if self._depth == 0:
            file_lock = self._file_lock
            self._file_lock = None
            if file_lock is not None:
                file_lock.release()
        self._thread_lock.release()


class _RolloutSQLiteConnection(sqlite3.Connection):
    """让 ``with connection`` 同时负责事务和连接生命周期。"""

    def __exit__(self, *args: object) -> bool | None:
        result = super().__exit__(*args)
        self.close()
        return result


@dataclass(slots=True)
class RolloutReadSnapshot:
    thread_id: str
    checkpoint_ns: str
    manifest: RolloutManifest
    connection: sqlite3.Connection
    file_lock: _RolloutFileLock
    _closed: bool = False

    @property
    def rollout_id(self) -> str:
        return self.manifest.rollout_id

    @property
    def projection_epoch(self) -> int:
        return self.manifest.projection_epoch

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.connection.rollback()
        finally:
            self.connection.close()
            self.file_lock.release()

    def __enter__(self) -> Self:
        if self._closed:
            raise RuntimeError("rollout read snapshot 已关闭")
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


@dataclass(frozen=True, slots=True)
class RolloutCheckpointIndex:
    checkpoint_id: str
    checkpoint_ns: str
    commit_sequence: int
    message_sequence: int
    message_count: int
    parent_checkpoint_id: str | None
    view_id: str
    branch_id: str
    checkpoint_version: int
    checkpoint_timestamp: str
    checkpoint_json: str
    metadata_json: str
    versions_seen_type: str
    versions_seen_blob: bytes
    pending_sends_type: str
    pending_sends_blob: bytes


@dataclass(frozen=True, slots=True)
class RolloutPruningCandidate:
    """SQLite 中待逻辑裁剪的不可见 context view。"""

    checkpoint_id: str
    view_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class RolloutPruningPlan:
    rollout_id: str
    committed_sequence: int
    candidates: tuple[RolloutPruningCandidate, ...]


@dataclass(frozen=True, slots=True)
class RolloutCompactionResult:
    """一次显式离线 JSONL 回收的结果。"""

    removed_message_count: int
    retained_message_count: int
    bytes_before: int
    bytes_after: int


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_namespace(value: str) -> str:
    if not value:
        return "default"
    return "ns-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _message_role(message: BaseMessage) -> str:
    if isinstance(message, HumanMessage):
        return "user"
    if isinstance(message, ToolMessage):
        return "tool"
    if isinstance(message, AIMessage):
        return "assistant"
    raise TypeError(f"rollout 不支持的消息类型: {type(message).__name__}")


def _metadata(message: BaseMessage) -> Mapping[str, object]:
    value = message.response_metadata or {}
    return value if isinstance(value, Mapping) else {}


def _message_metadata(message: BaseMessage) -> Mapping[str, object]:
    value = _metadata(message).get("message_metadata")
    return value if isinstance(value, Mapping) else {}


def _canonical_message_dict(message: BaseMessage) -> dict[str, object]:
    """生成包含 LangChain 完整消息字段的 canonical rollout 消息。"""
    return message_to_dict(message)


def _is_internal(message: BaseMessage) -> bool:
    return bool(
        _metadata(message).get("internal") is True
        or _message_metadata(message).get("internal") is True
    )


def _message_id(message: BaseMessage, index: int) -> str:
    if isinstance(message.id, str) and message.id:
        return message.id
    raw = _metadata(message).get("message_id")
    if isinstance(raw, str) and raw:
        return raw
    digest = hashlib.sha256(
        _json({"index": index, "message": _canonical_message_dict(message)}).encode()
    ).hexdigest()[:32]
    return f"generated-{digest}"


def _turn_id(message: BaseMessage, current: str | None, message_id: str) -> str:
    metadata = _message_metadata(message)
    for value in (
        metadata.get("turn_id"),
        metadata.get("job_id"),
        _metadata(message).get("turn_id"),
    ):
        if isinstance(value, str) and value:
            return value
    # 隐藏的 system_reminder 是 checkpoint 的控制记录，不是用户发起的
    # 新 Turn。若沿用 current 会把它挂到旧 Turn，若按 HumanMessage 的
    # 默认规则则会生成一个空的可见 Turn；两者都会破坏历史与重试关联。
    if _is_internal(message):
        return f"internal-{hashlib.sha256(message_id.encode()).hexdigest()[:24]}"
    if isinstance(message, HumanMessage):
        return f"turn-{hashlib.sha256(message_id.encode()).hexdigest()[:24]}"
    return current or f"internal-{hashlib.sha256(message_id.encode()).hexdigest()[:24]}"


def _stringify_content(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        block_type = value.get("type")
        if block_type in {"text", "output_text", "input_text"} and isinstance(
            value.get("text"), str
        ):
            return str(value["text"])
        if block_type == "reasoning" or "summary" in value:
            return ""
        return ""
    if isinstance(value, list):
        parts = [_stringify_content(item) for item in value]
        return "".join(part for part in parts if part)
    return ""


def _visible_text(message: Mapping[str, object]) -> str:
    data = message.get("data")
    if not isinstance(data, Mapping):
        return ""
    content_value = data.get("content")
    content = canonical_visible_text(content_value)
    if not content:
        content = _stringify_content(content_value)
    return content[:_VISIBLE_TEXT_LIMIT]


def _reasoning_rows(message: Mapping[str, object]) -> list[dict[str, object]]:
    data = message.get("data")
    content = data.get("content") if isinstance(data, Mapping) else None
    return [
        row
        for row in reasoning_projection_rows(content)
        if isinstance(row.get("kind"), str)
    ]


def _tool_calls(message: Mapping[str, object]) -> list[Mapping[str, object]]:
    data = message.get("data")
    values = data.get("tool_calls") if isinstance(data, Mapping) else None
    return (
        [value for value in values if isinstance(value, Mapping)]
        if isinstance(values, list)
        else []
    )


class RolloutStorage:
    """一个会话的单 JSONL canonical message 文件和权威 SQLite。"""

    def __init__(
        self, sessions_dir: str | Path, *, serde: JsonPlusSerializer | None = None
    ) -> None:
        self.sessions_dir = Path(sessions_dir).resolve()
        self._path_resolver = get_session_path_resolver(self.sessions_dir)
        self._serde = serde or JsonPlusSerializer()
        self._locks: dict[tuple[str, str], _RolloutOperationLock] = {}
        self._locks_guard = threading.Lock()
        self._active_fork_materializations: set[tuple[str, str]] = set()

    def _lock(self, thread_id: str, checkpoint_ns: str) -> _RolloutOperationLock:
        with self._locks_guard:
            key = (thread_id, checkpoint_ns)
            return self._locks.setdefault(
                key,
                _RolloutOperationLock(
                    self.root(thread_id, checkpoint_ns).parent / ".rollout.write.lock"
                ),
            )

    def root(self, thread_id: str, checkpoint_ns: str = "") -> Path:
        del checkpoint_ns
        return self._path_resolver.resolve_session_node(thread_id) / "rollout"

    def index_path(self, thread_id: str, checkpoint_ns: str = "") -> Path:
        return self.root(thread_id, checkpoint_ns) / "index.sqlite"

    def jsonl_path(self, thread_id: str, checkpoint_ns: str = "") -> Path:
        return self.root(thread_id, checkpoint_ns) / "rollout.jsonl"

    def _connect(
        self,
        thread_id: str,
        checkpoint_ns: str = "",
        *,
        read_only: bool = False,
    ) -> sqlite3.Connection:
        index_path = self.index_path(thread_id, checkpoint_ns)
        if index_path.is_file() and index_path.stat().st_size > 0:
            with index_path.open("rb") as stream:
                header = stream.read(16)
            if header != b"SQLite format 3\x00":
                raise sqlite3.DatabaseError(
                    f"rollout SQLite 文件头非法，拒绝继续读取: {index_path}"
                )
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

    def _commit_connection(self, connection: sqlite3.Connection) -> None:
        """提交一个已经完成跨文件索引写入的 SQLite 事务。

        单独保留这个边界，便于测试“SQLite 已提交但调用方在返回前崩溃”的
        窗口；生产路径仍由 ``append_checkpoint`` 在 JSONL fsync 后调用它。
        """
        connection.commit()

    def initialize(self, thread_id: str, checkpoint_ns: str = "") -> RolloutManifest:
        with self._lock(thread_id, checkpoint_ns):
            root = self.root(thread_id, checkpoint_ns)
            root.mkdir(parents=True, exist_ok=True)
            if self._is_removed_rollout_layout(root):
                raise RuntimeError(f"rollout 使用了已移除的旧布局: {root}")
            path = self.jsonl_path(thread_id, checkpoint_ns)
            # 只读历史请求会频繁经过 initialize；已有文件不能重复 touch，
            # 否则会改变 rollout.jsonl 的 mtime，触发工作区文件监听并造成
            # 无意义的资源刷新。首次创建时才建立空的 canonical 文件。
            if not path.exists():
                path.touch()
            self._initialize_schema(thread_id, checkpoint_ns)
            self._validate_reasoning_projection_schema(thread_id, checkpoint_ns)
            self._recover_compaction_journal(thread_id, checkpoint_ns)
            with self._connect(thread_id, checkpoint_ns) as connection:
                meta = connection.execute(
                    "SELECT * FROM database_meta WHERE singleton_id = 1"
                ).fetchone()
                if meta is None:
                    rollout_id = self._rollout_id(thread_id)
                    timestamp = _now()
                    connection.execute(
                        """INSERT INTO database_meta(singleton_id, rollout_id, session_id,
                        schema_version, message_format_version, database_state,
                        last_message_sequence, last_control_sequence, committed_jsonl_offset,
                        projection_epoch, created_at, updated_at)
                        VALUES (1, ?, ?, ?, ?, 'active', 0, 0, 0, 1, ?, ?)""",
                        (
                            rollout_id,
                            thread_id,
                            ROLLOUT_SCHEMA_VERSION,
                            MESSAGE_FORMAT_VERSION,
                            timestamp,
                            timestamp,
                        ),
                    )
                    connection.execute(
                        "INSERT INTO branches(branch_id, branch_kind, status, head_view_id, head_checkpoint_id, created_at, updated_at) VALUES (?, 'root', 'active', NULL, NULL, ?, ?)",
                        ("branch-001", timestamp, timestamp),
                    )
                    connection.execute(
                        "UPDATE database_meta SET active_branch_id = 'branch-001' WHERE singleton_id = 1"
                    )
                    connection.execute(
                        "INSERT INTO schema_migrations(from_version, to_version, migration_name, migration_checksum, status, started_at, completed_at) VALUES (0, ?, 'rollout_sqlite_v1', ?, 'completed', ?, ?)",
                        (
                            ROLLOUT_SCHEMA_VERSION,
                            _hash_bytes(b"rollout_sqlite_v1"),
                            timestamp,
                            timestamp,
                        ),
                    )
                self._ensure_namespace_state(connection, checkpoint_ns, _now())
                self._validate_schema_state(connection)
                if (thread_id, checkpoint_ns) not in self._active_fork_materializations:
                    self._recover_fork_materialization(
                        thread_id,
                        checkpoint_ns,
                        connection,
                        path,
                    )
                committed_offset = int(
                    connection.execute(
                        "SELECT committed_jsonl_offset FROM database_meta WHERE singleton_id = 1"
                    ).fetchone()[0]
                )
                file_size = path.stat().st_size
                if file_size < committed_offset:
                    connection.execute(
                        "UPDATE database_meta SET database_state = 'recovery_required', updated_at = ? WHERE singleton_id = 1",
                        (_now(),),
                    )
                    raise TypeError(
                        "rollout.jsonl 小于 SQLite 已提交偏移，无法安全恢复"
                    )
                if file_size > committed_offset:
                    with path.open("r+b") as stream:
                        stream.truncate(committed_offset)
                return self._manifest_from_connection(connection, checkpoint_ns)

    def repair_active_context_view(
        self,
        thread_id: str,
        checkpoint_ns: str = "",
    ) -> bool:
        """修复 active view 的 Turn 索引，不重写 canonical 消息文件。

        旧版本按全局消息序号连续性判断 Turn 完整性。并发执行时不同 Turn
        的消息会交错，导致 view 的消息范围存在但 ``context_view_turns`` 被
        错误删空。这里依据每个 Turn 自身的消息集合重新计算索引；只有索引
        与规范结果不一致时才写 SQLite，避免普通只读请求产生文件监听噪声。
        """
        with self._lock(thread_id, checkpoint_ns):
            self.initialize(thread_id, checkpoint_ns)
            with self._connect(thread_id, checkpoint_ns) as connection:
                namespace = self._namespace_state(connection, checkpoint_ns)
                branch_row = connection.execute(
                    "SELECT head_view_id FROM branches WHERE branch_id = ? AND status = 'active'",
                    (namespace[0],),
                ).fetchone()
                if branch_row is None or branch_row[0] is None:
                    return False
                view_id = str(branch_row[0])
                visible_sequences = set(
                    self._view_message_sequences_from_connection(connection, view_id)
                )
                expected: list[tuple[str, int, int | None, int | None]] = []
                turn_rows = connection.execute(
                    f"SELECT turn_id, first_message_sequence, last_message_sequence, user_message_sequence, final_message_sequence FROM turns AS t WHERE t.turn_kind = 'normal' AND t.user_message_sequence IS NOT NULL AND {_VISIBLE_NORMAL_TURN_PREDICATE} ORDER BY t.turn_ordinal"
                ).fetchall()
                for row in turn_rows:
                    turn_id = str(row[0])
                    turn_sequences = {
                        int(message_row[0])
                        for message_row in connection.execute(
                            "SELECT message_sequence FROM messages WHERE turn_id = ?",
                            (turn_id,),
                        ).fetchall()
                    }
                    if turn_sequences and turn_sequences.issubset(visible_sequences):
                        expected.append(
                            (
                                turn_id,
                                int(row[1]),
                                int(row[3]) if row[3] is not None else None,
                                int(row[4]) if row[4] is not None else None,
                            )
                        )
                current = [
                    (str(row[0]), int(row[1]), row[2], row[3])
                    for row in connection.execute(
                        "SELECT turn_id, logical_turn_ordinal, user_message_sequence, final_message_sequence FROM context_view_turns WHERE view_id = ? ORDER BY logical_turn_ordinal",
                        (view_id,),
                    ).fetchall()
                ]
                normalized_expected = [
                    (turn_id, ordinal, user_sequence, final_sequence)
                    for ordinal, (turn_id, _first, user_sequence, final_sequence) in enumerate(
                        expected, start=1
                    )
                ]
                view_header = connection.execute(
                    "SELECT head_turn_id, head_message_sequence, logical_turn_count FROM context_views WHERE view_id = ?",
                    (view_id,),
                ).fetchone()
                expected_head = normalized_expected[-1][0] if normalized_expected else None
                expected_sequence = max(visible_sequences, default=0)
                if (
                    current == normalized_expected
                    and view_header is not None
                    and view_header[0] == expected_head
                    and int(view_header[1]) == expected_sequence
                    and int(view_header[2]) == len(normalized_expected)
                ):
                    return False
                connection.execute(
                    "DELETE FROM context_view_turns WHERE view_id = ?",
                    (view_id,),
                )
                connection.executemany(
                    "INSERT INTO context_view_turns(view_id, turn_id, logical_turn_ordinal, user_message_sequence, final_message_sequence) VALUES (?, ?, ?, ?, ?)",
                    (
                        (view_id, turn_id, ordinal, user_sequence, final_sequence)
                        for ordinal, (turn_id, _first, user_sequence, final_sequence) in enumerate(
                            expected, start=1
                        )
                    ),
                )
                connection.execute(
                    "UPDATE context_views SET head_turn_id = ?, head_message_sequence = ?, logical_turn_count = ? WHERE view_id = ?",
                    (expected_head, expected_sequence, len(normalized_expected), view_id),
                )
                connection.commit()
                return True

    @staticmethod
    def _is_removed_rollout_layout(root: Path) -> bool:
        """判断目录是否是已经移除的旧 rollout 布局。

        该判定只用于禁止正常读写和允许删除流程跳过无效 checkpoint 元数据。
        删除整个会话不需要初始化旧 checkpoint，也不能因旧布局阻止用户删除会话。
        """
        return (root / "manifest.json").exists() or any(
            root.glob("segment-*.jsonl")
        )

    @staticmethod
    def _file_hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _copy_file_fsync(source: Path, target: Path) -> None:
        with source.open("rb") as source_stream, target.open("wb") as target_stream:
            shutil.copyfileobj(source_stream, target_stream)
            target_stream.flush()
            os.fsync(target_stream.fileno())

    def _recover_fork_materialization(
        self,
        thread_id: str,
        checkpoint_ns: str,
        connection: sqlite3.Connection,
        jsonl_path: Path,
    ) -> None:
        """恢复子 rollout 的 fork 两阶段提交日志。

        ``prepared`` 只可能指向一个尚未对外可见的目标副本，直接清空目标
        rollout；``target_committed`` 已经拥有完整的目标数据，只需补做父库
        pinned retention。这里不从 JSONL 推断任何 checkpoint 或上下文状态。
        """
        row = connection.execute(
            """
            SELECT materialization_id, fork_id, source_session_id,
                   source_checkpoint_id, source_view_id, relationship, status
            FROM fork_materializations
            WHERE status IN ('prepared', 'target_committed')
            ORDER BY created_at
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return

        (
            materialization_id,
            fork_id,
            source_session_id,
            source_checkpoint_id,
            source_view_id,
            relationship,
            status,
        ) = row
        if str(status) == "target_committed":
            if str(relationship) == "pinned":
                source_root = self.root(str(source_session_id), checkpoint_ns)
                if not source_root.is_dir() or not self.index_path(
                    str(source_session_id), checkpoint_ns
                ).is_file():
                    error_message = (
                        "fork 已提交目标 rollout，但 pinned 父 rollout 不存在，"
                        "无法安全恢复 retention"
                    )
                    connection.execute(
                        "UPDATE database_meta SET database_state = 'recovery_required', updated_at = ? WHERE singleton_id = 1",
                        (_now(),),
                    )
                    connection.execute(
                        "UPDATE fork_materializations SET error_message = ? WHERE materialization_id = ?",
                        (error_message, str(materialization_id)),
                    )
                    connection.commit()
                    raise RuntimeError(error_message)
                self._retain_fork_source(
                    source_session_id=str(source_session_id),
                    source_checkpoint_id=(
                        str(source_checkpoint_id)
                        if source_checkpoint_id is not None
                        else None
                    ),
                    source_view_id=(
                        str(source_view_id) if source_view_id is not None else None
                    ),
                    fork_id=str(fork_id),
                    owner_session_id=thread_id,
                    checkpoint_ns=checkpoint_ns,
                )
            connection.execute(
                "UPDATE fork_materializations SET status = 'committed', committed_at = ?, error_message = NULL WHERE materialization_id = ?",
                (_now(), str(materialization_id)),
            )
            connection.commit()
            return

        connection.execute("BEGIN IMMEDIATE")
        if jsonl_path.exists():
            with jsonl_path.open("r+b") as stream:
                stream.truncate(0)
                stream.flush()
                os.fsync(stream.fileno())
        for table in (
            "control_events",
            "messages",
            "message_projections",
            "tool_calls",
            "reasoning_blocks",
            "turns",
            "context_view_turns",
            "context_view_ranges",
            "context_view_jumps",
            "context_views",
            "checkpoint_channels",
            "pending_writes",
            "checkpoints",
            "branches",
            "checkpoint_namespace_state",
            "storage_commits",
            "fork_origins",
            "retention_refs",
        ):
            connection.execute(f"DELETE FROM {table}")
        timestamp = _now()
        connection.execute(
            "INSERT INTO branches(branch_id, branch_kind, status, head_view_id, head_checkpoint_id, created_at, updated_at) VALUES ('branch-001', 'root', 'active', NULL, NULL, ?, ?)",
            (timestamp, timestamp),
        )
        connection.execute(
            "INSERT INTO checkpoint_namespace_state(checkpoint_ns, active_branch_id, projection_epoch, created_at, updated_at) VALUES (?, 'branch-001', 1, ?, ?)",
            (checkpoint_ns, timestamp, timestamp),
        )
        connection.execute(
            "UPDATE database_meta SET database_state = 'active', last_commit_id = NULL, last_message_sequence = 0, last_control_sequence = 0, committed_jsonl_offset = 0, active_branch_id = 'branch-001', projection_epoch = 1, updated_at = ? WHERE singleton_id = 1",
            (timestamp,),
        )
        connection.execute(
            "UPDATE fork_materializations SET status = 'aborted', error_message = ?, committed_at = NULL WHERE materialization_id = ?",
            ("fork 物化中断，已回滚未提交的目标 rollout", str(materialization_id)),
        )
        connection.commit()

    def _recover_compaction_journal(self, thread_id: str, checkpoint_ns: str) -> None:
        """恢复未完成的离线 compaction，不从 JSONL 重建 SQLite。"""
        root = self.root(thread_id, checkpoint_ns)
        with self._connect(thread_id, checkpoint_ns) as connection:
            row = connection.execute(
                "SELECT compaction_id, status, old_file_name, temp_file_name, index_backup_name, new_file_hash, new_file_size FROM compaction_runs WHERE status IN ('prepared', 'replaced') ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return
            (
                compaction_id,
                status,
                old_file_name,
                temp_file_name,
                index_backup_name,
                new_file_hash,
                new_file_size,
            ) = row
            old_backup = root / str(old_file_name)
            temp_path = root / str(temp_file_name)
            index_backup = root / str(index_backup_name)
            current_path = self.jsonl_path(thread_id, checkpoint_ns)
            current_is_new = (
                current_path.is_file()
                and current_path.stat().st_size == int(new_file_size)
                and self._file_hash(current_path) == str(new_file_hash)
            )
            meta = connection.execute(
                "SELECT database_state, committed_jsonl_offset FROM database_meta WHERE singleton_id = 1"
            ).fetchone()
            database_points_to_new = bool(
                meta
                and meta[0] == "active"
                and int(meta[1]) == int(new_file_size)
                and current_is_new
            )
            if status == "completed" or database_points_to_new:
                connection.execute(
                    "DELETE FROM compaction_runs WHERE compaction_id = ?",
                    (str(compaction_id),),
                )
                connection.commit()
                old_backup.unlink(missing_ok=True)
                temp_path.unlink(missing_ok=True)
                index_backup.unlink(missing_ok=True)
                return
            if current_is_new:
                if not old_backup.is_file() or not index_backup.is_file():
                    connection.execute(
                        "UPDATE database_meta SET database_state = 'recovery_required', updated_at = ? WHERE singleton_id = 1",
                        (_now(),),
                    )
                    connection.commit()
                    raise TypeError(
                        "rollout compaction 缺少旧 JSONL 或 SQLite 备份，无法安全恢复"
                    )
                self._copy_file_fsync(old_backup, current_path)
                self._copy_file_fsync(
                    index_backup, self.index_path(thread_id, checkpoint_ns)
                )
            connection.execute(
                "UPDATE database_meta SET database_state = 'active', updated_at = ? WHERE singleton_id = 1",
                (_now(),),
            )
            connection.execute(
                "DELETE FROM compaction_runs WHERE compaction_id = ?",
                (str(compaction_id),),
            )
            connection.commit()
            temp_path.unlink(missing_ok=True)
            old_backup.unlink(missing_ok=True)
            index_backup.unlink(missing_ok=True)

    @staticmethod
    def _validate_schema_state(connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT schema_version, message_format_version FROM database_meta WHERE singleton_id = 1"
        ).fetchone()
        if row is None:
            raise RuntimeError("rollout database_meta 缺失")
        if int(row[0]) > ROLLOUT_SCHEMA_VERSION:
            raise RuntimeError(
                "rollout SQLite schema 版本高于当前程序支持范围: "
                f"database={row[0]}, supported={ROLLOUT_SCHEMA_VERSION}"
            )
        if int(row[1]) != MESSAGE_FORMAT_VERSION:
            raise RuntimeError(
                "rollout JSONL message format 版本不受支持: "
                f"database={row[1]}, supported={MESSAGE_FORMAT_VERSION}"
            )
        migrations = connection.execute(
            "SELECT from_version, to_version, migration_checksum, status, completed_at FROM schema_migrations ORDER BY migration_id"
        ).fetchall()
        if not migrations:
            raise RuntimeError("rollout schema_migrations 缺失")
        incomplete = [
            row for row in migrations if row[3] != "completed" or row[4] is None
        ]
        if incomplete:
            raise RuntimeError("rollout 存在未完成的 SQLite schema migration")
        if int(migrations[-1][1]) != int(row[0]):
            raise RuntimeError(
                "rollout schema_version 与 schema_migrations 不一致: "
                f"database={row[0]}, migration={migrations[-1][1]}"
            )
        if (
            int(migrations[0][0]) == 0
            and int(migrations[0][1]) == 1
            and migrations[0][2] != _hash_bytes(b"rollout_sqlite_v1")
        ):
            raise RuntimeError("rollout v1 schema migration checksum 不匹配")

    def open_read_snapshot(
        self,
        thread_id: str,
        checkpoint_ns: str = "",
        *,
        validate_integrity: bool = False,
    ) -> RolloutReadSnapshot:
        root = self.root(thread_id, checkpoint_ns)
        jsonl_path = self.jsonl_path(thread_id, checkpoint_ns)
        index_path = self.index_path(thread_id, checkpoint_ns)
        # 现有 rollout 的只读请求不能再次执行初始化 DDL 或 WAL pragma；
        # 初始化阶段会触碰 SQLite sidecar，进而被工作区文件监听当作文件修改。
        # 仅在第一次访问、canonical 文件尚不存在时走写入初始化路径。
        if not root.is_dir() or not jsonl_path.is_file() or not index_path.is_file():
            self.initialize(thread_id, checkpoint_ns)
        file_lock = _RolloutFileLock(
            root.parent / ".rollout.write.lock",
            exclusive=False,
        )
        file_lock.acquire()
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect(thread_id, checkpoint_ns, read_only=True)
            self._validate_schema_state(connection)
            self._validate_reasoning_projection_connection(connection)
            committed_offset = int(
                connection.execute(
                    "SELECT committed_jsonl_offset FROM database_meta WHERE singleton_id = 1"
                ).fetchone()[0]
            )
            if jsonl_path.stat().st_size < committed_offset:
                raise RuntimeError("rollout.jsonl 小于 SQLite 已提交偏移，无法安全恢复")
            integrity = (
                connection.execute("PRAGMA integrity_check").fetchone()[0]
                if validate_integrity
                else "ok"
            )
            if integrity != "ok":
                connection.rollback()
                connection.close()
                file_lock.release()
                # 只读快照不能写入 recovery 标记；释放共享锁后再通过统一
                # 写锁更新权威状态，避免维护按钮得到一个二次 readonly 错误。
                with self._lock(thread_id, checkpoint_ns), self._connect(
                    thread_id, checkpoint_ns
                ) as writable:
                        writable.execute(
                            "UPDATE database_meta SET database_state = 'recovery_required', updated_at = ? WHERE singleton_id = 1",
                            (_now(),),
                        )
                raise RuntimeError(
                    "rollout SQLite integrity_check 失败: "
                    f"{self.index_path(thread_id, checkpoint_ns)}: {integrity}"
                )
            connection.execute("BEGIN")
            manifest = self._manifest_from_connection(connection, checkpoint_ns)
        except Exception:
            if connection is not None:
                connection.close()
            file_lock.release()
            raise
        return RolloutReadSnapshot(
            thread_id,
            checkpoint_ns,
            manifest,
            connection,
            file_lock,
        )

    def validate_index(
        self, thread_id: str, checkpoint_ns: str = ""
    ) -> RolloutReadSnapshot:
        """显式执行完整 SQLite integrity_check，供维护操作或 UI 检查按钮调用。"""
        return self.open_read_snapshot(
            thread_id,
            checkpoint_ns,
            validate_integrity=True,
        )

    def repair_index(
        self,
        thread_id: str,
        checkpoint_ns: str = "",
        *,
        manifest: RolloutManifest | None = None,
    ) -> None:
        del manifest
        raise RuntimeError(
            "SQLite 是 rollout 控制状态的权威来源，不能从 rollout.jsonl 重建 index；请恢复 SQLite 备份"
        )

    def backup_index(
        self,
        thread_id: str,
        checkpoint_ns: str = "",
        *,
        destination: str | Path | None = None,
    ) -> Path:
        """使用 SQLite backup API 创建可恢复的 index 副本。"""
        with self._lock(thread_id, checkpoint_ns):
            self.initialize(thread_id, checkpoint_ns)
            with self._connect(thread_id, checkpoint_ns) as connection:
                self._validate_schema_state(connection)
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
                if integrity != "ok":
                    raise RuntimeError(
                        f"SQLite backup integrity_check 失败: {integrity}"
                    )
            return self._backup_index_unlocked(
                thread_id,
                checkpoint_ns,
                destination=destination,
            )

    def _backup_index_unlocked(
        self,
        thread_id: str,
        checkpoint_ns: str,
        *,
        destination: str | Path | None,
    ) -> Path:
        """在调用方已经持有 rollout 写锁时复制 SQLite。"""
        source_path = self.index_path(thread_id, checkpoint_ns)
        target_path = (
            Path(destination).resolve()
            if destination
            else source_path.with_name("index.sqlite.backup")
        )
        if target_path == source_path:
            raise ValueError("SQLite backup 目标不能覆盖当前 index")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = target_path.with_name(f".{target_path.name}.{uuid4().hex}.tmp")
        source = sqlite3.connect(source_path)
        target = sqlite3.connect(temporary_path)
        try:
            source.backup(target)
            target.commit()
            integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(f"SQLite backup integrity_check 失败: {integrity}")
        finally:
            target.close()
            source.close()
        os.replace(temporary_path, target_path)
        return target_path

    def restore_index_backup(
        self,
        thread_id: str,
        backup_path: str | Path,
        checkpoint_ns: str = "",
    ) -> RolloutReadSnapshot:
        """恢复显式指定的 SQLite backup；不会从 JSONL 重建控制状态。"""
        source_path = Path(backup_path).resolve()
        if not source_path.is_file():
            raise FileNotFoundError(source_path)
        with self._lock(thread_id, checkpoint_ns):
            self._restore_index_backup_unlocked(
                thread_id,
                checkpoint_ns,
                source_path,
            )
        return self.validate_index(thread_id, checkpoint_ns)

    def _restore_index_backup_unlocked(
        self,
        thread_id: str,
        checkpoint_ns: str,
        source_path: Path,
    ) -> None:
        """在调用方已经持有 rollout 写锁时恢复 SQLite backup。"""
        target_path = self.index_path(thread_id, checkpoint_ns)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = target_path.with_name(
            f".{target_path.name}.{uuid4().hex}.restore"
        )
        source = sqlite3.connect(source_path)
        target = sqlite3.connect(temporary_path)
        try:
            source.backup(target)
            target.commit()
            integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(f"SQLite restore integrity_check 失败: {integrity}")
        finally:
            target.close()
            source.close()
        os.replace(temporary_path, target_path)

    def migrate_schema(
        self,
        thread_id: str,
        *,
        to_version: int,
        migration_name: str,
        migration_sql: str,
        checkpoint_ns: str = "",
    ) -> RolloutReadSnapshot:
        """执行一个事务性的 SQLite schema migration。

        每次调用只执行一条 SQLite DDL/DML 语句；升级前保留 backup。当前
        程序支持的最高版本由 ``ROLLOUT_SCHEMA_VERSION`` 控制，发布新 schema
        时必须同时提升该常量并提供对应 migration。
        """
        if to_version < 1:
            raise ValueError("SQLite schema version 必须从 1 开始")
        if not migration_name.strip() or not migration_sql.strip():
            raise ValueError("SQLite migration 必须包含名称和 SQL")
        with self._lock(thread_id, checkpoint_ns):
            self.initialize(thread_id, checkpoint_ns)
            with self._connect(thread_id, checkpoint_ns) as connection:
                row = connection.execute(
                    "SELECT schema_version FROM database_meta WHERE singleton_id = 1"
                ).fetchone()
                if row is None:
                    raise RuntimeError("rollout database_meta 缺失")
                current_version = int(row[0])
            if to_version != current_version + 1:
                raise ValueError(
                    "SQLite migration 必须按版本顺序执行: "
                    f"current={current_version}, target={to_version}"
                )
            if to_version > ROLLOUT_SCHEMA_VERSION:
                raise RuntimeError(
                    "当前程序尚未声明目标 SQLite schema 版本: "
                    f"target={to_version}, supported={ROLLOUT_SCHEMA_VERSION}"
                )

            backup_path = self.root(thread_id, checkpoint_ns) / (
                f"index.sqlite.migration-{uuid4().hex}.backup"
            )
            self._backup_index_unlocked(
                thread_id,
                checkpoint_ns,
                destination=backup_path,
            )
            checksum = _hash_bytes(migration_sql.encode("utf-8"))
            transaction_id = uuid4().hex
            timestamp = _now()
            try:
                with self._connect(thread_id, checkpoint_ns) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    migration_cursor = connection.execute(
                        "INSERT INTO schema_migrations(from_version, to_version, migration_name, migration_checksum, status, started_at) VALUES (?, ?, ?, ?, 'started', ?)",
                        (
                            current_version,
                            to_version,
                            migration_name,
                            checksum,
                            timestamp,
                        ),
                    )
                    connection.execute(migration_sql)
                    connection.execute(
                        "UPDATE database_meta SET schema_version = ?, updated_at = ? WHERE singleton_id = 1",
                        (to_version, timestamp),
                    )
                    connection.execute(
                        "UPDATE schema_migrations SET status = 'completed', completed_at = ? WHERE migration_id = ?",
                        (timestamp, migration_cursor.lastrowid),
                    )
                    control_sequence = self._insert_control(
                        connection,
                        "schema_migration",
                        "schema",
                        migration_name,
                        None,
                        None,
                        None,
                        {"from_version": current_version, "to_version": to_version},
                        transaction_id,
                        timestamp,
                    )
                    connection.execute(
                        "UPDATE database_meta SET last_control_sequence = ?, updated_at = ? WHERE singleton_id = 1",
                        (control_sequence, timestamp),
                    )
                    connection.commit()
            except BaseException as error:
                self._restore_index_backup_unlocked(
                    thread_id,
                    checkpoint_ns,
                    backup_path,
                )
                with self._connect(thread_id, checkpoint_ns) as connection:
                    failure_time = _now()
                    connection.execute(
                        "INSERT INTO schema_migrations(from_version, to_version, migration_name, migration_checksum, status, started_at, completed_at, error_message) VALUES (?, ?, ?, ?, 'failed', ?, ?, ?)",
                        (
                            current_version,
                            to_version,
                            migration_name,
                            checksum,
                            timestamp,
                            failure_time,
                            str(error),
                        ),
                    )
                    connection.execute(
                        "UPDATE database_meta SET database_state = 'recovery_required', updated_at = ? WHERE singleton_id = 1",
                        (failure_time,),
                    )
                    connection.commit()
                raise
        return self.validate_index(thread_id, checkpoint_ns)

    def _rollout_id(self, thread_id: str) -> str:
        return (
            "rollout-"
            + hashlib.sha256(str(self.root(thread_id)).encode()).hexdigest()[:24]
        )

    def _manifest_from_connection(
        self, connection: sqlite3.Connection, checkpoint_ns: str
    ) -> RolloutManifest:
        row = connection.execute(
            "SELECT rollout_id, active_branch_id, last_message_sequence, projection_epoch, last_commit_id, database_state FROM database_meta WHERE singleton_id = 1"
        ).fetchone()
        if row is None:
            raise RuntimeError("rollout database_meta 缺失")
        if row[5] != "active":
            raise RuntimeError(f"rollout SQLite 状态不可读取: {row[5]}")
        namespace_state = connection.execute(
            "SELECT active_branch_id, projection_epoch FROM checkpoint_namespace_state WHERE checkpoint_ns = ?",
            (checkpoint_ns,),
        ).fetchone()
        if namespace_state is None:
            raise RuntimeError(
                f"rollout checkpoint namespace 状态缺失: {checkpoint_ns!r}"
            )
        latest = connection.execute(
            "SELECT checkpoint_id FROM checkpoints WHERE checkpoint_ns = ? AND status = 'active' ORDER BY commit_id DESC LIMIT 1",
            (checkpoint_ns,),
        ).fetchone()
        return RolloutManifest(
            str(row[0]),
            checkpoint_ns,
            str(namespace_state[0]),
            int(row[2]),
            str(latest[0]) if latest else None,
            int(namespace_state[1]),
        )

    @staticmethod
    def _namespace_state(
        connection: sqlite3.Connection,
        checkpoint_ns: str,
    ) -> tuple[str, int]:
        row = connection.execute(
            "SELECT active_branch_id, projection_epoch FROM checkpoint_namespace_state WHERE checkpoint_ns = ?",
            (checkpoint_ns,),
        ).fetchone()
        if row is None:
            raise RuntimeError(
                f"rollout checkpoint namespace 状态缺失: {checkpoint_ns!r}"
            )
        return str(row[0]), int(row[1])

    def _ensure_namespace_state(
        self,
        connection: sqlite3.Connection,
        checkpoint_ns: str,
        timestamp: str,
    ) -> None:
        existing = connection.execute(
            "SELECT active_branch_id FROM checkpoint_namespace_state WHERE checkpoint_ns = ?",
            (checkpoint_ns,),
        ).fetchone()
        if existing is not None:
            return
        if checkpoint_ns == _DEFAULT_NAMESPACE:
            meta = connection.execute(
                "SELECT active_branch_id FROM database_meta WHERE singleton_id = 1"
            ).fetchone()
            if meta is None or meta[0] is None:
                raise RuntimeError("rollout 默认 checkpoint namespace 缺少 active branch")
            branch_id = str(meta[0])
        else:
            branch_id = "branch-" + uuid4().hex[:12]
            connection.execute(
                "INSERT INTO branches(branch_id, branch_kind, status, head_view_id, head_checkpoint_id, created_at, updated_at) VALUES (?, 'root', 'active', NULL, NULL, ?, ?)",
                (branch_id, timestamp, timestamp),
            )
        connection.execute(
            "INSERT INTO checkpoint_namespace_state(checkpoint_ns, active_branch_id, projection_epoch, created_at, updated_at) VALUES (?, ?, 1, ?, ?)",
            (checkpoint_ns, branch_id, timestamp, timestamp),
        )

    def _initialize_schema(self, thread_id: str, checkpoint_ns: str) -> None:
        del checkpoint_ns
        with self._connect(thread_id) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS database_meta (
                    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1), rollout_id TEXT NOT NULL UNIQUE,
                    session_id TEXT NOT NULL UNIQUE, schema_version INTEGER NOT NULL,
                    message_format_version INTEGER NOT NULL, database_state TEXT NOT NULL,
                    last_commit_id INTEGER, last_message_sequence INTEGER NOT NULL,
                    last_control_sequence INTEGER NOT NULL, committed_jsonl_offset INTEGER NOT NULL,
                    active_branch_id TEXT, projection_epoch INTEGER NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    migration_id INTEGER PRIMARY KEY AUTOINCREMENT, from_version INTEGER NOT NULL,
                    to_version INTEGER NOT NULL, migration_name TEXT NOT NULL,
                    migration_checksum TEXT NOT NULL, status TEXT NOT NULL,
                    started_at TEXT NOT NULL, completed_at TEXT, error_message TEXT
                );
                CREATE TABLE IF NOT EXISTS storage_commits (
                    commit_id INTEGER PRIMARY KEY AUTOINCREMENT, transaction_id TEXT NOT NULL UNIQUE,
                    first_message_sequence INTEGER, last_message_sequence INTEGER,
                    jsonl_start_offset INTEGER NOT NULL, jsonl_end_offset INTEGER NOT NULL,
                    first_control_sequence INTEGER, last_control_sequence INTEGER,
                    jsonl_fsync_at TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL, committed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS compaction_runs (
                    compaction_id TEXT PRIMARY KEY, status TEXT NOT NULL,
                    old_file_name TEXT NOT NULL, temp_file_name TEXT NOT NULL,
                    index_backup_name TEXT NOT NULL, new_file_hash TEXT NOT NULL,
                    new_file_size INTEGER NOT NULL, created_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS control_events (
                    control_sequence INTEGER PRIMARY KEY AUTOINCREMENT, control_id TEXT NOT NULL UNIQUE,
                    control_kind TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
                    branch_id TEXT, view_id TEXT, checkpoint_id TEXT, payload_json TEXT NOT NULL,
                    transaction_id TEXT NOT NULL, previous_event_hash TEXT, event_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS branches (
                    branch_id TEXT PRIMARY KEY, branch_kind TEXT NOT NULL, status TEXT NOT NULL,
                    head_view_id TEXT, head_checkpoint_id TEXT, parent_branch_id TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS checkpoint_namespace_state (
                    checkpoint_ns TEXT PRIMARY KEY, active_branch_id TEXT NOT NULL,
                    projection_epoch INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS context_views (
                    view_id TEXT PRIMARY KEY, branch_id TEXT NOT NULL, parent_view_id TEXT,
                    view_kind TEXT NOT NULL, head_turn_id TEXT, head_message_sequence INTEGER NOT NULL,
                    logical_turn_count INTEGER NOT NULL, control_sequence INTEGER, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS context_view_ranges (
                    view_id TEXT NOT NULL, range_index INTEGER NOT NULL, source_kind TEXT NOT NULL,
                    source_view_id TEXT, start_message_sequence INTEGER, end_message_sequence INTEGER,
                    source_start_turn_ordinal INTEGER, source_end_turn_ordinal INTEGER,
                    logical_start_turn_ordinal INTEGER, logical_end_turn_ordinal INTEGER,
                    range_ordinal INTEGER, source_start_ordinal INTEGER, source_end_ordinal INTEGER,
                    message_start_sequence INTEGER, message_end_sequence INTEGER,
                    logical_start_ordinal INTEGER, logical_end_ordinal INTEGER,
                    PRIMARY KEY(view_id, range_index)
                );
                CREATE TABLE IF NOT EXISTS context_view_jumps (
                    view_id TEXT NOT NULL, jump_level INTEGER NOT NULL,
                    ancestor_view_id TEXT NOT NULL, ancestor_depth INTEGER NOT NULL,
                    PRIMARY KEY(view_id, jump_level)
                );
                CREATE TABLE IF NOT EXISTS messages (
                    message_sequence INTEGER PRIMARY KEY, message_id TEXT NOT NULL UNIQUE,
                    turn_id TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('user','assistant','tool')),
                    jsonl_offset INTEGER NOT NULL, jsonl_length INTEGER NOT NULL,
                    content_length INTEGER NOT NULL, content_hash TEXT NOT NULL,
                    visibility TEXT NOT NULL, commit_id INTEGER NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS message_projections (
                    message_sequence INTEGER PRIMARY KEY, text_preview TEXT, visible_text TEXT,
                    visible_text_length INTEGER NOT NULL, visible_text_truncated INTEGER NOT NULL DEFAULT 0,
                    has_reasoning INTEGER NOT NULL, has_encrypted_reasoning INTEGER NOT NULL,
                    has_tool_calls INTEGER NOT NULL, phase TEXT, projection_version INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS turns (
                    turn_id TEXT PRIMARY KEY, turn_ordinal INTEGER NOT NULL UNIQUE,
                    turn_kind TEXT NOT NULL, branch_id TEXT NOT NULL,
                    first_message_sequence INTEGER NOT NULL, last_message_sequence INTEGER NOT NULL,
                    user_message_sequence INTEGER, final_message_sequence INTEGER,
                    final_message_id TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS context_view_turns (
                    view_id TEXT NOT NULL, turn_id TEXT NOT NULL, logical_turn_ordinal INTEGER NOT NULL,
                    user_message_sequence INTEGER, final_message_sequence INTEGER,
                    PRIMARY KEY(view_id, turn_id), UNIQUE(view_id, logical_turn_ordinal)
                );
                CREATE TABLE IF NOT EXISTS tool_calls (
                    tool_call_id TEXT NOT NULL, assistant_message_sequence INTEGER NOT NULL,
                    call_index INTEGER NOT NULL, tool_name TEXT NOT NULL, status TEXT NOT NULL,
                    result_message_sequence INTEGER, argument_length INTEGER NOT NULL,
                    result_length INTEGER, argument_hash TEXT, result_hash TEXT, summary_text TEXT,
                    started_at TEXT, completed_at TEXT, projection_version INTEGER NOT NULL
                    , PRIMARY KEY(tool_call_id, assistant_message_sequence)
                );
                CREATE TABLE IF NOT EXISTS reasoning_blocks (
                    message_sequence INTEGER NOT NULL,
                    content_block_index INTEGER NOT NULL,
                    item_index INTEGER NOT NULL DEFAULT 0,
                    carrier_type TEXT NOT NULL,
                    item_id TEXT,
                    reasoning_text TEXT,
                    summary_text TEXT,
                    signature_present INTEGER NOT NULL DEFAULT 0,
                    encrypted_length INTEGER,
                    encrypted_hash TEXT,
                    provider_id TEXT,
                    projection_version INTEGER NOT NULL,
                    PRIMARY KEY(message_sequence, content_block_index, item_index)
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    checkpoint_id TEXT PRIMARY KEY, checkpoint_ns TEXT NOT NULL, commit_id INTEGER NOT NULL,
                    message_sequence INTEGER NOT NULL, message_count INTEGER NOT NULL,
                    parent_checkpoint_id TEXT, view_id TEXT NOT NULL, branch_id TEXT NOT NULL,
                    checkpoint_version INTEGER NOT NULL, checkpoint_timestamp TEXT NOT NULL,
                    checkpoint_kind TEXT NOT NULL DEFAULT 'normal', status TEXT NOT NULL DEFAULT 'active',
                    checkpoint_json TEXT NOT NULL, metadata_json TEXT NOT NULL,
                    envelope_serializer_name TEXT NOT NULL DEFAULT 'msgpack',
                    versions_seen_type TEXT NOT NULL, versions_seen_blob BLOB NOT NULL,
                    versions_seen_length INTEGER NOT NULL, versions_seen_hash TEXT NOT NULL,
                    pending_sends_type TEXT NOT NULL, pending_sends_blob BLOB NOT NULL,
                    pending_sends_length INTEGER NOT NULL, pending_sends_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS checkpoint_channels (
                    checkpoint_id TEXT NOT NULL, channel_name TEXT NOT NULL,
                    storage_kind TEXT NOT NULL, value_state TEXT NOT NULL, channel_version TEXT,
                    serializer_name TEXT, value_blob BLOB, value_length INTEGER, value_hash TEXT,
                    context_view_id TEXT, updated_index INTEGER, created_at TEXT NOT NULL,
                    PRIMARY KEY(checkpoint_id, channel_name)
                );
                CREATE TABLE IF NOT EXISTS pending_writes (
                    checkpoint_id TEXT NOT NULL, task_id TEXT NOT NULL, task_path TEXT NOT NULL,
                    write_index INTEGER NOT NULL, channel TEXT NOT NULL, serializer_name TEXT NOT NULL,
                    value_blob BLOB NOT NULL, value_length INTEGER NOT NULL, value_hash TEXT NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(checkpoint_id, task_id, task_path, write_index)
                );
                CREATE TABLE IF NOT EXISTS fork_origins (
                    fork_id TEXT PRIMARY KEY, child_session_id TEXT NOT NULL,
                    source_session_id TEXT NOT NULL, source_checkpoint_id TEXT,
                    source_view_id TEXT, fork_mode TEXT NOT NULL, relationship TEXT NOT NULL,
                    copied_message_count INTEGER NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS retention_refs (
                    retention_id TEXT PRIMARY KEY, reference_kind TEXT NOT NULL, reference_id TEXT NOT NULL,
                    target_view_id TEXT, target_message_sequence INTEGER, owner_session_id TEXT,
                    expires_at TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fork_materializations (
                    materialization_id TEXT PRIMARY KEY, fork_id TEXT NOT NULL UNIQUE,
                    target_session_id TEXT NOT NULL, source_session_id TEXT NOT NULL,
                    source_checkpoint_id TEXT, source_view_id TEXT, fork_mode TEXT NOT NULL,
                    relationship TEXT NOT NULL, status TEXT NOT NULL,
                    rollback_jsonl_offset INTEGER NOT NULL,
                    copied_message_count INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT, created_at TEXT NOT NULL,
                    target_committed_at TEXT, committed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS messages_turn_index ON messages(turn_id, message_sequence);
                CREATE INDEX IF NOT EXISTS messages_role_index ON messages(role, message_sequence);
                CREATE INDEX IF NOT EXISTS messages_offset_index ON messages(jsonl_offset);
                CREATE INDEX IF NOT EXISTS turns_ordinal_index ON turns(turn_ordinal);
                CREATE INDEX IF NOT EXISTS context_view_turns_ordinal_index ON context_view_turns(view_id, logical_turn_ordinal);
                CREATE INDEX IF NOT EXISTS context_view_turns_turn_index ON context_view_turns(turn_id, view_id);
                CREATE INDEX IF NOT EXISTS tool_calls_id_index ON tool_calls(tool_call_id, assistant_message_sequence);
                CREATE INDEX IF NOT EXISTS tool_calls_result_sequence_index ON tool_calls(result_message_sequence);
                CREATE INDEX IF NOT EXISTS checkpoint_commit_index ON checkpoints(commit_id DESC);
                CREATE INDEX IF NOT EXISTS checkpoint_channels_view_index ON checkpoint_channels(context_view_id);
                CREATE INDEX IF NOT EXISTS pending_writes_checkpoint_index ON pending_writes(checkpoint_id, task_path, write_index);
                CREATE INDEX IF NOT EXISTS fork_materializations_status_index ON fork_materializations(status, created_at);
                """
            )

    def _encode(self, value: object) -> tuple[str, bytes, int, str]:
        serializer, blob = self._serde.dumps_typed(value)
        return serializer, blob, len(blob), _hash_bytes(blob)

    def _validate_reasoning_projection_schema(
        self,
        thread_id: str,
        checkpoint_ns: str,
    ) -> None:
        with self._connect(thread_id, checkpoint_ns, read_only=True) as connection:
            self._validate_reasoning_projection_connection(connection)

    @staticmethod
    def _validate_reasoning_projection_connection(
        connection: sqlite3.Connection,
    ) -> None:
        columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(reasoning_blocks)"
            ).fetchall()
        }
        if {"block_index", "block_kind"} & columns:
            raise RuntimeError(
                "rollout SQLite 使用已移除的 reasoning_blocks 旧 schema；"
                "原型阶段不提供兼容迁移，请重新生成该 rollout"
            )
        required = {
            "message_sequence",
            "content_block_index",
            "item_index",
            "carrier_type",
        }
        if not required.issubset(columns):
            raise RuntimeError(
                "rollout SQLite reasoning_blocks schema 不完整: "
                f"missing={sorted(required - columns)}"
            )

    def decode_value(self, value: Mapping[str, object] | tuple[str, bytes]) -> object:
        if isinstance(value, tuple):
            serializer, blob = value
        else:
            serializer = value.get("serializer_name")
            blob = value.get("value_blob")
        if not isinstance(serializer, str) or not isinstance(blob, (bytes, bytearray)):
            raise TypeError("SQLite serialized value 结构非法")
        return self._serde.loads_typed((serializer, bytes(blob)))

    def append_checkpoint(
        self,
        *,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        parent_checkpoint_id: str | None,
        current_messages: list[BaseMessage],
        branch_id: str | None = None,
    ) -> None:
        with self._lock(thread_id, checkpoint_ns):
            self.initialize(thread_id, checkpoint_ns)
            jsonl = self.jsonl_path(thread_id, checkpoint_ns)
            with self._connect(thread_id, checkpoint_ns) as connection:
                meta = connection.execute(
                    "SELECT * FROM database_meta WHERE singleton_id = 1"
                ).fetchone()
                if meta is None:
                    raise RuntimeError("rollout database_meta 缺失")
                checkpoint_id = checkpoint.get("id")
                if not isinstance(checkpoint_id, str) or not checkpoint_id:
                    raise ValueError("checkpoint 缺少字符串 id")
                checkpoint_core = {
                    key: value
                    for key, value in checkpoint.items()
                    if key
                    not in {
                        "channel_values",
                        "channel_versions",
                        "updated_channels",
                        "versions_seen",
                        "pending_sends",
                    }
                }
                encoded_checkpoint_core = _json(checkpoint_core)
                encoded_metadata = _json(metadata)
                existing_checkpoint = connection.execute(
                    "SELECT checkpoint_json, metadata_json, status FROM checkpoints WHERE checkpoint_id = ? AND checkpoint_ns = ?",
                    (checkpoint_id, checkpoint_ns),
                ).fetchone()
                if existing_checkpoint is not None:
                    if (
                        existing_checkpoint[2] == "active"
                        and existing_checkpoint[0] == encoded_checkpoint_core
                        and existing_checkpoint[1] == encoded_metadata
                    ):
                        return
                    raise ValueError(
                        f"checkpoint_id 已存在但内容不一致: {checkpoint_id}"
                    )
                active_branch, _projection_epoch = self._namespace_state(
                    connection, checkpoint_ns
                )
                if branch_id is not None and branch_id != active_branch:
                    raise RuntimeError(
                        f"checkpoint branch 不是 active branch: {branch_id}"
                    )
                parent = self._checkpoint_row(
                    connection,
                    checkpoint_ns,
                    parent_checkpoint_id,
                )
                current_ids = [
                    _message_id(message, index)
                    for index, message in enumerate(current_messages)
                ]
                existing = connection.execute(
                    "SELECT message_id, message_sequence, turn_id, content_hash FROM messages WHERE message_sequence <= ? ORDER BY message_sequence",
                    (int(meta[7]),),
                ).fetchall()
                existing_by_id = {str(row[0]): int(row[1]) for row in existing}
                existing_turn_by_id = {str(row[0]): str(row[2]) for row in existing}
                existing_hash_by_id = {str(row[0]): str(row[3]) for row in existing}
                if len(set(current_ids)) != len(current_ids):
                    raise ValueError(
                        "一个 checkpoint 不能重复引用同一个 canonical message_id"
                    )
                offset = jsonl.stat().st_size
                first_sequence: int | None = None
                last_sequence = int(meta[7])
                prepared: list[
                    tuple[int, str, str, str, str, int, int, bytes, str, str]
                ] = []
                current_turn: str | None = None
                visible_sequences: list[int] = []
                for index, message in enumerate(current_messages):
                    message_id = current_ids[index]
                    if message_id in existing_by_id:
                        content = _canonical_message_dict(message).get("data", {}).get(
                            "content"
                        )
                        content_hash = _hash_bytes(_json(content).encode("utf-8"))
                        if existing_hash_by_id[message_id] != content_hash:
                            raise ValueError(
                                f"canonical message_id 内容不可变: {message_id}"
                            )
                        visible_sequences.append(existing_by_id[message_id])
                        current_turn = existing_turn_by_id[message_id]
                        continue
                    turn_id = _turn_id(message, current_turn, message_id)
                    current_turn = turn_id
                    serialized = _canonical_message_dict(message)
                    sequence = last_sequence + 1
                    envelope = {
                        "sequence": sequence,
                        "message_id": message_id,
                        "turn_id": turn_id,
                        "role": _message_role(message),
                        "message": serialized,
                    }
                    raw = (_json(envelope) + "\n").encode("utf-8")
                    prepared.append(
                        (
                            sequence,
                            message_id,
                            turn_id,
                            _message_role(message),
                            _json(serialized),
                            offset,
                            len(raw),
                            raw,
                            "internal" if _is_internal(message) else "visible",
                            _json(envelope),
                        )
                    )
                    offset += len(raw)
                    last_sequence = sequence
                    first_sequence = (
                        sequence if first_sequence is None else first_sequence
                    )
                    visible_sequences.append(sequence)
                with jsonl.open("ab") as stream:
                    for row in prepared:
                        stream.write(row[7])
                    stream.flush()
                    os.fsync(stream.fileno())
                transaction_id = uuid4().hex
                timestamp = _now()
                connection.execute("BEGIN IMMEDIATE")
                commit_cursor = connection.execute(
                    "INSERT INTO storage_commits(transaction_id, first_message_sequence, last_message_sequence, jsonl_start_offset, jsonl_end_offset, status, created_at) VALUES (?, ?, ?, ?, ?, 'prepared', ?)",
                    (
                        transaction_id,
                        first_sequence,
                        last_sequence if first_sequence is not None else None,
                        jsonl.stat().st_size - sum(row[6] for row in prepared),
                        jsonl.stat().st_size,
                        timestamp,
                    ),
                )
                commit_id = int(commit_cursor.lastrowid)
                for (
                    sequence,
                    message_id,
                    turn_id,
                    role,
                    serialized_json,
                    message_offset,
                    byte_length,
                    _raw,
                    visibility,
                    _envelope,
                ) in prepared:
                    message_value = json.loads(serialized_json)
                    content = (
                        message_value.get("data", {}).get("content")
                        if isinstance(message_value, Mapping)
                        else None
                    )
                    content_bytes = _json(content).encode("utf-8")
                    connection.execute(
                        "INSERT INTO messages(message_sequence, message_id, turn_id, role, jsonl_offset, jsonl_length, content_length, content_hash, visibility, commit_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            sequence,
                            message_id,
                            turn_id,
                            role,
                            message_offset,
                            byte_length,
                            len(content_bytes),
                            _hash_bytes(content_bytes),
                            visibility,
                            commit_id,
                            timestamp,
                        ),
                    )
                    self._insert_message_projection(
                        connection, sequence, message_value, timestamp
                    )
                    self._upsert_turn(
                        connection,
                        turn_id,
                        sequence,
                        message_id,
                        role,
                        active_branch,
                        timestamp,
                    )
                active_view_row = connection.execute(
                    "SELECT head_view_id, head_checkpoint_id FROM branches WHERE branch_id = ? AND status = 'active'",
                    (active_branch,),
                ).fetchone()
                parent_view_id = (
                    str(active_view_row[0])
                    if active_view_row is not None and active_view_row[0] is not None
                    else str(parent[6])
                    if parent
                    else None
                )
                if active_view_row is not None and active_view_row[0] is not None:
                    active_view_sequences = set(
                        self._view_message_sequences_from_connection(
                            connection,
                            str(active_view_row[0]),
                        )
                    )
                    # rewind 创建的 view 是一次上下文边界。重放 checkpoint
                    # 通常同时携带边界内的旧前缀和新的 assistant 消息；此时
                    # 父 view 中被替换的 canonical suffix 不能再次继承。只在
                    # 已有前缀与新消息同时出现时切换为 replacement，避免
                    # 增量 ToolMessage/新用户消息丢失 rewind 前缀。
                    active_view_kind = connection.execute(
                        "SELECT view_kind FROM context_views WHERE view_id = ?",
                        (str(active_view_row[0]),),
                    ).fetchone()
                    is_rewind_replacement = (
                        active_view_kind is not None
                        and active_view_kind[0] == "rewind"
                        and any(
                            sequence in active_view_sequences
                            for sequence in visible_sequences
                        )
                        and any(
                            sequence not in active_view_sequences
                            for sequence in visible_sequences
                        )
                    )
                    if is_rewind_replacement:
                        parent_view_id = None
                channel_values = checkpoint.get("channel_values")
                has_compaction_event = isinstance(channel_values, Mapping) and (
                    "_summarization_event" in channel_values
                )
                view_kind = (
                    "compaction"
                    if has_compaction_event
                    or checkpoint.get("checkpoint_kind") == "compaction"
                    or metadata.get("source") == "compaction"
                    else "checkpoint"
                )
                checkpoint_kind = (
                    "compaction" if view_kind == "compaction" else "normal"
                )
                view_id = self._create_view(
                    connection,
                    active_branch,
                    parent_view_id,
                    visible_sequences,
                    timestamp,
                    view_kind=view_kind,
                )
                (
                    versions_seen_type,
                    versions_seen_blob,
                    versions_seen_length,
                    versions_seen_hash,
                ) = self._encode(checkpoint.get("versions_seen", {}))
                pending_type, pending_blob, pending_length, pending_hash = self._encode(
                    checkpoint.get("pending_sends", [])
                )
                message_sequence = max(visible_sequences, default=last_sequence)
                connection.execute(
                    "INSERT INTO checkpoints(checkpoint_id, checkpoint_ns, commit_id, message_sequence, message_count, parent_checkpoint_id, view_id, branch_id, checkpoint_version, checkpoint_timestamp, checkpoint_kind, status, checkpoint_json, metadata_json, envelope_serializer_name, versions_seen_type, versions_seen_blob, versions_seen_length, versions_seen_hash, pending_sends_type, pending_sends_blob, pending_sends_length, pending_sends_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        checkpoint_id,
                        checkpoint_ns,
                        commit_id,
                        message_sequence,
                        len(current_messages),
                        parent_checkpoint_id,
                        view_id,
                        active_branch,
                        int(checkpoint_core.get("v", 2)),
                        str(checkpoint_core.get("ts", timestamp)),
                        checkpoint_kind,
                        encoded_checkpoint_core,
                        encoded_metadata,
                        "json",
                        versions_seen_type,
                        versions_seen_blob,
                        versions_seen_length,
                        versions_seen_hash,
                        pending_type,
                        pending_blob,
                        pending_length,
                        pending_hash,
                    ),
                )
                self._insert_checkpoint_channels(
                    connection, checkpoint_id, checkpoint, view_id, timestamp
                )
                connection.execute(
                    "UPDATE branches SET head_view_id = ?, head_checkpoint_id = ?, updated_at = ? WHERE branch_id = ?",
                    (view_id, checkpoint_id, timestamp, active_branch),
                )
                control_sequence = self._insert_control(
                    connection,
                    "checkpoint_created",
                    "checkpoint",
                    checkpoint_id,
                    active_branch,
                    view_id,
                    checkpoint_id,
                    self._checkpoint_control_payload(
                        connection,
                        checkpoint=checkpoint,
                        message_count=len(current_messages),
                    ),
                    transaction_id,
                    timestamp,
                )
                connection.execute(
                    "UPDATE context_views SET control_sequence = ? WHERE view_id = ?",
                    (control_sequence, view_id),
                )
                connection.execute(
                    "UPDATE storage_commits SET status = 'committed', committed_at = ? WHERE commit_id = ?",
                    (timestamp, commit_id),
                )
                connection.execute(
                    "UPDATE database_meta SET last_commit_id = ?, last_message_sequence = ?, last_control_sequence = ?, committed_jsonl_offset = ?, updated_at = ? WHERE singleton_id = 1",
                    (
                        commit_id,
                        last_sequence,
                        control_sequence,
                        jsonl.stat().st_size,
                        timestamp,
                    ),
                )
                self._commit_connection(connection)

    @staticmethod
    def _checkpoint_control_payload(
        connection: sqlite3.Connection,
        *,
        checkpoint: Checkpoint,
        message_count: int,
    ) -> dict[str, object]:
        """保存 checkpoint 控制信息，并保留 compaction 的 message cutoff。"""
        payload: dict[str, object] = {"message_count": message_count}
        channel_values = checkpoint.get("channel_values")
        event = (
            channel_values.get("_summarization_event")
            if isinstance(channel_values, Mapping)
            else None
        )
        if not isinstance(event, Mapping):
            return payload
        cutoff_index = event.get("cutoff_index")
        if isinstance(cutoff_index, int) and cutoff_index >= 0:
            payload["cutoff_index"] = cutoff_index
        cutoff_message_id = event.get("cutoff_message_id")
        if not isinstance(cutoff_message_id, str) or not cutoff_message_id:
            return payload
        row = connection.execute(
            "SELECT message_sequence FROM messages WHERE message_id = ?",
            (cutoff_message_id,),
        ).fetchone()
        if row is not None:
            payload["cutoff_message_id"] = cutoff_message_id
            payload["cutoff_message_sequence"] = int(row[0])
        return payload

    def _checkpoint_row(
        self,
        connection: sqlite3.Connection,
        checkpoint_ns: str,
        checkpoint_id: str | None,
    ) -> tuple[object, ...] | None:
        if checkpoint_id is None:
            return connection.execute(
                "SELECT c.checkpoint_id, c.checkpoint_ns, c.commit_id, c.message_sequence, c.message_count, c.parent_checkpoint_id, c.view_id, c.branch_id, c.checkpoint_version, c.checkpoint_timestamp, c.checkpoint_json, c.metadata_json, c.versions_seen_type, c.versions_seen_blob, c.pending_sends_type, c.pending_sends_blob FROM checkpoints c JOIN branches b ON b.head_checkpoint_id = c.checkpoint_id WHERE b.branch_id = (SELECT active_branch_id FROM checkpoint_namespace_state WHERE checkpoint_ns = ?) AND c.checkpoint_ns = ? AND c.status = 'active' LIMIT 1",
                (checkpoint_ns, checkpoint_ns),
            ).fetchone()
        return connection.execute(
            "SELECT checkpoint_id, checkpoint_ns, commit_id, message_sequence, message_count, parent_checkpoint_id, view_id, branch_id, checkpoint_version, checkpoint_timestamp, checkpoint_json, metadata_json, versions_seen_type, versions_seen_blob, pending_sends_type, pending_sends_blob FROM checkpoints WHERE checkpoint_id = ? AND checkpoint_ns = ? AND status = 'active'",
            (checkpoint_id, checkpoint_ns),
        ).fetchone()

    def _create_view(
        self,
        connection: sqlite3.Connection,
        branch_id: str,
        parent_view_id: str | None,
        visible_sequences: Sequence[int],
        timestamp: str,
        *,
        view_kind: str = "checkpoint",
    ) -> str:
        view_id = "view-" + uuid4().hex
        sequence_values = tuple(
            dict.fromkeys(int(value) for value in visible_sequences)
        )
        materialized_sequences = sequence_values
        if parent_view_id and view_kind == "checkpoint":
            # 新 checkpoint 的 visible_sequences 可能只是本次 delta；Turn
            # 索引也必须基于完整的父链，否则最新 view 会只有 ToolMessage，
            # 历史分页会暂时看不到这条正在执行的 Turn。
            materialized_sequences = tuple(
                dict.fromkeys(
                    [
                        *self._view_message_sequences_from_connection(
                            connection,
                            parent_view_id,
                        ),
                        *sequence_values,
                    ]
                )
            )
        message_rows = (
            connection.execute(
                "SELECT message_sequence, turn_id FROM messages WHERE message_sequence IN ("
                + ",".join("?" for _ in materialized_sequences)
                + ")",
                materialized_sequences,
            ).fetchall()
            if materialized_sequences
            else []
        )
        turn_by_sequence = {
            int(sequence): str(turn_id) for sequence, turn_id in message_rows
        }
        turn_ids: list[str] = []
        sequence_set = set(materialized_sequences)
        for sequence in materialized_sequences:
            value = turn_by_sequence.get(sequence)
            if value is None:
                continue
            if value in turn_ids:
                continue
            row = connection.execute(
                "SELECT turn_kind, first_message_sequence, last_message_sequence, user_message_sequence, final_message_sequence FROM turns WHERE turn_id = ?",
                (value,),
            ).fetchone()
            if row is None or row[0] != "normal" or row[3] is None:
                continue
            # 不同并发 Turn 的 canonical message sequence 可能交错。不能用
            # first..last 的连续整数判断完整性，否则后来完成的 Turn 会因为
            # 中间插入了其它 Turn 的消息而从 active view 消失，历史读取随后
            # 被错误识别成 stale reference。
            turn_sequences = {
                int(message_row[0])
                for message_row in connection.execute(
                    "SELECT message_sequence FROM messages WHERE turn_id = ?",
                    (value,),
                ).fetchall()
            }
            if turn_sequences and turn_sequences.issubset(sequence_set):
                turn_ids.append(value)
        rows: list[tuple[str, int, object, object]] = []
        for ordinal, turn_id in enumerate(turn_ids, start=1):
            row = connection.execute(
                "SELECT user_message_sequence, final_message_sequence FROM turns WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
            if row is not None:
                rows.append((turn_id, ordinal, row[0], row[1]))
        head_turn = str(rows[-1][0]) if rows else None
        head_message_sequence = (
            max(materialized_sequences) if materialized_sequences else 0
        )
        connection.execute(
            "INSERT INTO context_views(view_id, branch_id, parent_view_id, view_kind, head_turn_id, head_message_sequence, logical_turn_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                view_id,
                branch_id,
                parent_view_id,
                view_kind,
                head_turn,
                head_message_sequence,
                len(rows),
                timestamp,
            ),
        )
        ranges: list[tuple[int, int]] = []
        for sequence in sequence_values:
            if not ranges or sequence != ranges[-1][1] + 1:
                ranges.append((sequence, sequence))
            else:
                ranges[-1] = (ranges[-1][0], sequence)
        range_index = 0
        if parent_view_id and view_kind == "checkpoint":
            # 普通 checkpoint 可能只携带本次 LangGraph delta（例如单独的
            # ToolMessage），而不是完整 messages 快照。parent_view_id 仅用于
            # 跳转索引并不能参与消息物化；显式保留父 view，才能让对应的
            # assistant tool call 与后续 ToolMessage 一起进入下一次模型请求。
            connection.execute(
                "INSERT INTO context_view_ranges(view_id, range_index, source_kind, source_view_id, start_message_sequence, end_message_sequence, message_start_sequence, message_end_sequence, range_ordinal, logical_start_turn_ordinal, logical_end_turn_ordinal) VALUES (?, ?, 'view', ?, NULL, NULL, NULL, NULL, ?, NULL, NULL)",
                (view_id, range_index, parent_view_id, range_index),
            )
            range_index += 1
        for start_sequence, end_sequence in ranges:
            connection.execute(
                "INSERT INTO context_view_ranges(view_id, range_index, source_kind, start_message_sequence, end_message_sequence, message_start_sequence, message_end_sequence, range_ordinal, logical_start_turn_ordinal, logical_end_turn_ordinal) VALUES (?, ?, 'messages', ?, ?, ?, ?, ?, NULL, NULL)",
                (
                    view_id,
                    range_index,
                    start_sequence,
                    end_sequence,
                    start_sequence,
                    end_sequence,
                    range_index,
                ),
            )
            range_index += 1
        if parent_view_id:
            connection.execute(
                "INSERT INTO context_view_jumps(view_id, jump_level, ancestor_view_id, ancestor_depth) VALUES (?, 0, ?, 1)",
                (view_id, parent_view_id),
            )
            parent_by_view = {
                str(row[0]): (str(row[1]) if row[1] is not None else None)
                for row in connection.execute(
                    "SELECT view_id, parent_view_id FROM context_views"
                ).fetchall()
            }
            level = 1
            while True:
                ancestor = view_id
                for _ in range(2**level):
                    ancestor = parent_by_view.get(ancestor)
                    if ancestor is None:
                        break
                if ancestor is None:
                    break
                connection.execute(
                    "INSERT INTO context_view_jumps(view_id, jump_level, ancestor_view_id, ancestor_depth) VALUES (?, ?, ?, ?)",
                    (
                        view_id,
                        level,
                        ancestor,
                        2**level,
                    ),
                )
                level += 1
        for turn_id, ordinal, user_sequence, final_sequence in rows:
            connection.execute(
                "INSERT INTO context_view_turns(view_id, turn_id, logical_turn_ordinal, user_message_sequence, final_message_sequence) VALUES (?, ?, ?, ?, ?)",
                (view_id, turn_id, ordinal, user_sequence, final_sequence),
            )
        return view_id

    def resolve_turn_anchor(
        self,
        snapshot: RolloutReadSnapshot,
        turn_id: str,
        *,
        anchor_mode: str = "inclusive",
        require_completed: bool = False,
    ) -> RolloutTurnAnchor:
        """从 active head 沿 view lineage 解析用户可见的 Turn 锚点。

        `context_view_turns` 只提供候选完整 Turn，真正的选择顺序由 active
        head 的祖先链决定。这样同一个 Turn 出现在多个 branch/view 时，不会
        因为物理 sequence 或全局 control sequence 较大而选错上下文。
        """
        return self._resolve_turn_anchor_connection(
            self._snapshot_connection(snapshot),
            turn_id,
            checkpoint_ns=snapshot.checkpoint_ns,
            anchor_mode=anchor_mode,
            require_completed=require_completed,
        )

    def resolve_latest_completed_turn_anchor(
        self,
        snapshot: RolloutReadSnapshot,
        *,
        anchor_mode: str = "inclusive",
    ) -> RolloutTurnAnchor | None:
        """从 active view lineage 找到最近一个已完成 Turn 的锚点。

        最新物理消息可能属于尚未完成的 Turn，不能用它作为前端默认 fork
        起点。这里先按 active head 到祖先 view 的逻辑顺序查找最近完成的
        normal Turn，再复用同一个 Turn resolver 取得 source view/checkpoint。
        """
        connection = self._snapshot_connection(snapshot)
        active = connection.execute(
            "SELECT b.head_view_id FROM branches b WHERE b.branch_id = (SELECT active_branch_id FROM checkpoint_namespace_state WHERE checkpoint_ns = ?)"
            ,
            (snapshot.checkpoint_ns,),
        ).fetchone()
        if active is None or active[0] is None:
            has_turn = connection.execute(
                f"SELECT 1 FROM turns AS t WHERE t.turn_kind = 'normal' AND t.user_message_sequence IS NOT NULL AND {_VISIBLE_NORMAL_TURN_PREDICATE} LIMIT 1"
            ).fetchone()
            if has_turn is None:
                return None
            raise ValueError("当前会话没有已完成的 Turn，无法创建 fork")

        current_view_id = str(active[0])
        visited: set[str] = set()
        while current_view_id:
            if current_view_id in visited:
                raise RuntimeError(f"context view 父链成环: {current_view_id}")
            visited.add(current_view_id)
            row = connection.execute(
                f"SELECT cvt.turn_id FROM context_view_turns cvt JOIN turns t ON t.turn_id = cvt.turn_id WHERE cvt.view_id = ? AND t.turn_kind = 'normal' AND t.user_message_sequence IS NOT NULL AND t.final_message_sequence IS NOT NULL AND t.status IN ('completed', 'succeeded') AND {_VISIBLE_NORMAL_TURN_PREDICATE} ORDER BY cvt.logical_turn_ordinal DESC LIMIT 1",
                (current_view_id,),
            ).fetchone()
            if row is not None:
                return self._resolve_turn_anchor_connection(
                    connection,
                    str(row[0]),
                    checkpoint_ns=snapshot.checkpoint_ns,
                    anchor_mode=anchor_mode,
                    require_completed=True,
                )
            parent = connection.execute(
                "SELECT parent_view_id FROM context_views WHERE view_id = ?",
                (current_view_id,),
            ).fetchone()
            if parent is None:
                raise RuntimeError(f"context view 不存在: {current_view_id}")
            current_view_id = str(parent[0]) if parent[0] is not None else ""

        running = connection.execute(
            f"SELECT 1 FROM turns AS t WHERE t.turn_kind = 'normal' AND t.user_message_sequence IS NOT NULL AND (t.final_message_sequence IS NULL OR t.status NOT IN ('completed', 'succeeded')) AND {_VISIBLE_NORMAL_TURN_PREDICATE} LIMIT 1"
        ).fetchone()
        if running is not None:
            raise ValueError("当前会话没有已完成的 Turn，无法创建 fork")
        return None

    def _resolve_turn_anchor_connection(
        self,
        connection: sqlite3.Connection,
        turn_id: str,
        *,
        checkpoint_ns: str,
        anchor_mode: str,
        require_completed: bool = False,
    ) -> RolloutTurnAnchor:
        if anchor_mode not in {"inclusive", "before"}:
            raise ValueError("Turn anchor_mode 必须是 inclusive 或 before")
        turn = connection.execute(
            "SELECT turn_id, turn_kind, first_message_sequence, user_message_sequence, last_message_sequence, final_message_sequence, status FROM turns WHERE turn_id = ?",
            (turn_id,),
        ).fetchone()
        if turn is None:
            raise KeyError(f"Turn 不存在: {turn_id}")
        if turn[1] != "normal" or turn[3] is None:
            raise KeyError(f"Turn 不是可定位的 normal Turn: {turn_id}")
        if require_completed and (
            turn[5] is None or turn[6] not in {"completed", "succeeded"}
        ):
            raise ValueError(f"运行中的 Turn 不支持 fork: turn_id={turn_id}")
        checkpoint_sequence_limit = None
        if require_completed:
            checkpoint_sequence_limit = (
                int(turn[5])
                if anchor_mode == "inclusive"
                else int(turn[3]) - 1
            )

        candidates = {
            str(row[0]): {
                "branch_id": str(row[1]),
                "logical_turn_ordinal": int(row[2]),
            }
            for row in connection.execute(
                "SELECT cvt.view_id, cv.branch_id, cvt.logical_turn_ordinal FROM context_view_turns cvt JOIN context_views cv ON cv.view_id = cvt.view_id WHERE cvt.turn_id = ?",
                (turn_id,),
            ).fetchall()
        }
        active = connection.execute(
            "SELECT b.branch_id, b.head_view_id FROM branches b WHERE b.branch_id = (SELECT active_branch_id FROM checkpoint_namespace_state WHERE checkpoint_ns = ?)"
            ,
            (checkpoint_ns,),
        ).fetchone()
        if active is None or active[1] is None:
            raise RuntimeError(f"会话没有可用的 active context view: turn_id={turn_id}")

        current_view_id = str(active[1])
        visited: set[str] = set()
        while current_view_id:
            if current_view_id in visited:
                raise RuntimeError(f"context view 父链成环: {current_view_id}")
            visited.add(current_view_id)
            candidate = candidates.get(current_view_id)
            if candidate is not None:
                checkpoint_id = self._source_checkpoint_for_view(
                    connection,
                    current_view_id,
                    checkpoint_ns=checkpoint_ns,
                    max_message_sequence=checkpoint_sequence_limit,
                )
                if checkpoint_id is not None:
                    user_sequence = int(turn[3])
                    cutoff = (
                        int(turn[4])
                        if anchor_mode == "inclusive"
                        else user_sequence - 1
                    )
                    return RolloutTurnAnchor(
                        turn_id=str(turn[0]),
                        view_id=current_view_id,
                        checkpoint_id=checkpoint_id,
                        branch_id=str(candidate["branch_id"]),
                        logical_turn_ordinal=int(candidate["logical_turn_ordinal"]),
                        first_message_sequence=int(turn[2]),
                        user_message_sequence=user_sequence,
                        last_message_sequence=int(turn[4]),
                        final_message_sequence=(
                            int(turn[5]) if turn[5] is not None else None
                        ),
                        anchor_mode=anchor_mode,
                        cutoff_message_sequence=cutoff,
                    )
            parent = connection.execute(
                "SELECT parent_view_id FROM context_views WHERE view_id = ?",
                (current_view_id,),
            ).fetchone()
            if parent is None:
                raise RuntimeError(f"context view 不存在: {current_view_id}")
            current_view_id = str(parent[0]) if parent[0] is not None else ""

        raise KeyError(
            f"当前 active view lineage 不包含可恢复的完整 Turn: turn_id={turn_id}"
        )

    @staticmethod
    def _source_checkpoint_for_view(
        connection: sqlite3.Connection,
        view_id: str,
        *,
        checkpoint_ns: str,
        max_message_sequence: int | None = None,
    ) -> str | None:
        row = connection.execute(
            "SELECT checkpoint_id FROM checkpoints WHERE checkpoint_ns = ? AND view_id = ? AND status = 'active' AND (? IS NULL OR message_sequence <= ?) ORDER BY commit_id DESC LIMIT 1",
            (checkpoint_ns, view_id, max_message_sequence, max_message_sequence),
        ).fetchone()
        if row is not None:
            return str(row[0])
        row = connection.execute(
            "SELECT b.head_checkpoint_id FROM branches b JOIN checkpoints c ON c.checkpoint_id = b.head_checkpoint_id WHERE c.checkpoint_ns = ? AND b.head_view_id = ? AND b.head_checkpoint_id IS NOT NULL AND c.status = 'active' AND (? IS NULL OR c.message_sequence <= ?) ORDER BY b.updated_at DESC LIMIT 1",
            (checkpoint_ns, view_id, max_message_sequence, max_message_sequence),
        ).fetchone()
        if row is not None:
            return str(row[0])
        row = connection.execute(
            "SELECT ce.checkpoint_id FROM control_events ce JOIN checkpoints c ON c.checkpoint_id = ce.checkpoint_id WHERE c.checkpoint_ns = ? AND ce.view_id = ? AND ce.checkpoint_id IS NOT NULL AND c.status = 'active' AND (? IS NULL OR c.message_sequence <= ?) ORDER BY ce.control_sequence DESC LIMIT 1",
            (checkpoint_ns, view_id, max_message_sequence, max_message_sequence),
        ).fetchone()
        return str(row[0]) if row is not None else None

    def materialize_turn_anchor(
        self,
        snapshot: RolloutReadSnapshot,
        anchor: RolloutTurnAnchor,
    ) -> list[BaseMessage]:
        """读取 anchor view 中截至边界的消息，不读取父 rollout。"""
        connection = self._snapshot_connection(snapshot)
        sequences = self._view_message_sequences_from_connection(
            connection,
            anchor.view_id,
        )
        selected = [
            sequence
            for sequence in sequences
            if sequence <= anchor.cutoff_message_sequence
        ]
        values = self._read_messages(
            snapshot.thread_id,
            snapshot.checkpoint_ns,
            selected,
            connection=connection,
        )
        return [messages_from_dict([value])[0] for value in values]

    def _upsert_turn(
        self,
        connection: sqlite3.Connection,
        turn_id: str,
        sequence: int,
        message_id: str,
        role: str,
        branch_id: str,
        timestamp: str,
    ) -> None:
        row = connection.execute(
            "SELECT first_message_sequence, last_message_sequence, user_message_sequence, final_message_sequence, final_message_id FROM turns WHERE turn_id = ?",
            (turn_id,),
        ).fetchone()
        if row is None:
            if role != "user" or turn_id.startswith("internal-"):
                return
            ordinal = int(
                connection.execute(
                    "SELECT COALESCE(MAX(turn_ordinal), 0) + 1 FROM turns"
                ).fetchone()[0]
            )
            connection.execute(
                "INSERT INTO turns(turn_id, turn_ordinal, turn_kind, branch_id, first_message_sequence, last_message_sequence, user_message_sequence, final_message_sequence, final_message_id, status, created_at, updated_at) VALUES (?, ?, 'normal', ?, ?, ?, ?, NULL, NULL, 'running', ?, ?)",
                (
                    turn_id,
                    ordinal,
                    branch_id,
                    sequence,
                    sequence,
                    sequence,
                    timestamp,
                    timestamp,
                ),
            )
            return
        user_sequence = (
            int(row[2])
            if row[2] is not None
            else (sequence if role == "user" else None)
        )
        reopened = (
            row[3] is not None
            and role in {"assistant", "tool"}
            and sequence > int(row[3])
        )
        connection.execute(
            "UPDATE turns SET last_message_sequence = ?, user_message_sequence = COALESCE(user_message_sequence, ?), final_message_sequence = ?, final_message_id = ?, status = ?, updated_at = ? WHERE turn_id = ?",
            (
                sequence,
                user_sequence,
                None if reopened else row[3],
                None if reopened else row[4],
                "running"
                if reopened
                else "completed"
                if row[3] is not None
                else "running",
                timestamp,
                turn_id,
            ),
        )

    def _insert_message_projection(
        self,
        connection: sqlite3.Connection,
        sequence: int,
        value: Mapping[str, object],
        timestamp: str,
    ) -> None:
        visible = _visible_text(value)
        data = value.get("data") if isinstance(value, Mapping) else {}
        calls = _tool_calls(value)
        reasoning = _reasoning_rows(value)
        response_metadata = (
            data.get("response_metadata") if isinstance(data, Mapping) else None
        )
        provider_id = (
            response_metadata.get("provider_id")
            if isinstance(response_metadata, Mapping)
            else None
        )
        provider_id = provider_id if isinstance(provider_id, str) else None
        has_encrypted = any(row.get("kind") == "encrypted" for row in reasoning)
        message_type = value.get("type")
        phase = (
            "tool_request"
            if calls
            else "assistant_text"
            if message_type == "ai"
            else None
        )
        connection.execute(
            "INSERT INTO message_projections(message_sequence, text_preview, visible_text, visible_text_length, visible_text_truncated, has_reasoning, has_encrypted_reasoning, has_tool_calls, phase, projection_version, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
            (
                sequence,
                visible[:512],
                visible,
                len(visible),
                len(visible) >= _VISIBLE_TEXT_LIMIT,
                bool(reasoning),
                has_encrypted,
                bool(calls),
                phase,
                timestamp,
            ),
        )
        for row in reasoning:
            block_index = row.get("content_block_index")
            item_index = row.get("item_index", 0)
            kind = row.get("kind")
            carrier_type = row.get("carrier_type")
            if (
                not isinstance(block_index, int)
                or isinstance(block_index, bool)
                or not isinstance(item_index, int)
                or isinstance(item_index, bool)
                or not isinstance(kind, str)
                or not isinstance(carrier_type, str)
            ):
                continue
            text = row.get("text")
            reasoning_text = row.get("reasoning_text")
            summary_text = row.get("summary_text")
            encrypted_length = row.get("encrypted_length")
            encrypted_hash = row.get("encrypted_hash")
            item_id = row.get("item_id")
            signature_present = row.get("signature_present") is True
            connection.execute(
                "INSERT INTO reasoning_blocks(message_sequence, content_block_index, item_index, carrier_type, item_id, reasoning_text, summary_text, signature_present, encrypted_length, encrypted_hash, provider_id, projection_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 2)",
                (
                    sequence,
                    block_index,
                    item_index,
                    carrier_type,
                    item_id if isinstance(item_id, str) else None,
                    reasoning_text[:_VISIBLE_TEXT_LIMIT]
                    if isinstance(reasoning_text, str)
                    else text[:_VISIBLE_TEXT_LIMIT]
                    if isinstance(text, str) and kind == "reasoning"
                    else None,
                    summary_text[:_VISIBLE_TEXT_LIMIT]
                    if isinstance(summary_text, str)
                    else text[:_VISIBLE_TEXT_LIMIT]
                    if isinstance(text, str) and kind == "summary"
                    else None,
                    int(signature_present),
                    encrypted_length
                    if isinstance(encrypted_length, int)
                    else None,
                    encrypted_hash if isinstance(encrypted_hash, str) else None,
                    provider_id,
                ),
            )
        for call_index, call in enumerate(calls):
            call_id = call.get("id")
            name = call.get("name")
            if (
                not isinstance(call_id, str)
                or not call_id
                or not isinstance(name, str)
                or not name
            ):
                continue
            args = call.get("args", {})
            args_blob = _json(args).encode("utf-8")
            connection.execute(
                "INSERT INTO tool_calls(tool_call_id, assistant_message_sequence, call_index, tool_name, status, argument_length, argument_hash, summary_text, projection_version) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, 1)",
                (
                    call_id,
                    sequence,
                    call_index,
                    name,
                    len(args_blob),
                    _hash_bytes(args_blob),
                    f"{name} (pending)",
                ),
            )
        if message_type == "tool" and isinstance(data, Mapping):
            call_id = data.get("tool_call_id")
            if isinstance(call_id, str) and call_id:
                result_blob = _json(data.get("content")).encode("utf-8")
                status = (
                    data.get("status")
                    if isinstance(data.get("status"), str)
                    else "success"
                )
                pending_call = connection.execute(
                    "SELECT assistant_message_sequence FROM tool_calls WHERE tool_call_id = ? AND status = 'pending' ORDER BY assistant_message_sequence DESC LIMIT 1",
                    (call_id,),
                ).fetchone()
                if pending_call is not None:
                    connection.execute(
                        "UPDATE tool_calls SET result_message_sequence = ?, result_length = ?, result_hash = ?, status = ?, completed_at = ? WHERE tool_call_id = ? AND assistant_message_sequence = ?",
                        (
                            sequence,
                            len(result_blob),
                            _hash_bytes(result_blob),
                            status,
                            timestamp,
                            call_id,
                            int(pending_call[0]),
                        ),
                    )

    def _insert_checkpoint_channels(
        self,
        connection: sqlite3.Connection,
        checkpoint_id: str,
        checkpoint: Checkpoint,
        view_id: str,
        timestamp: str,
    ) -> None:
        values = checkpoint.get("channel_values", {})
        versions = checkpoint.get("channel_versions", {})
        updated = checkpoint.get("updated_channels") or []
        if (
            not isinstance(values, Mapping)
            or not isinstance(versions, Mapping)
            or not isinstance(updated, list)
        ):
            raise TypeError(
                "checkpoint channel_values/channel_versions/updated_channels 结构非法"
            )
        names = (
            set(values)
            | set(versions)
            | {item for item in updated if isinstance(item, str)}
        )
        for name in sorted(names):
            if not isinstance(name, str):
                raise TypeError("checkpoint channel 名称必须是字符串")
            version = versions.get(name)
            if name in values and version is None:
                raise ValueError(f"checkpoint channel 缺少版本: {name}")
            updated_index = updated.index(name) if name in updated else None
            if name == "messages":
                connection.execute(
                    "INSERT INTO checkpoint_channels(checkpoint_id, channel_name, storage_kind, value_state, channel_version, context_view_id, updated_index, created_at) VALUES (?, ?, 'rollout_view', 'view', ?, ?, ?, ?)",
                    (
                        checkpoint_id,
                        name,
                        str(version) if version is not None else None,
                        view_id,
                        updated_index,
                        timestamp,
                    ),
                )
                continue
            if name in values:
                serializer, blob, length, digest = self._encode(values[name])
                connection.execute(
                    "INSERT INTO checkpoint_channels(checkpoint_id, channel_name, storage_kind, value_state, channel_version, serializer_name, value_blob, value_length, value_hash, updated_index, created_at) VALUES (?, ?, 'sqlite_value', 'present', ?, ?, ?, ?, ?, ?, ?)",
                    (
                        checkpoint_id,
                        name,
                        str(version) if version is not None else None,
                        serializer,
                        blob,
                        length,
                        digest,
                        updated_index,
                        timestamp,
                    ),
                )
            else:
                connection.execute(
                    "INSERT INTO checkpoint_channels(checkpoint_id, channel_name, storage_kind, value_state, channel_version, updated_index, created_at) VALUES (?, ?, 'sqlite_value', 'absent', ?, ?, ?)",
                    (
                        checkpoint_id,
                        name,
                        str(version) if version is not None else None,
                        updated_index,
                        timestamp,
                    ),
                )

    def _insert_control(
        self,
        connection: sqlite3.Connection,
        kind: str,
        entity_type: str,
        entity_id: str,
        branch_id: str | None,
        view_id: str | None,
        checkpoint_id: str | None,
        payload: Mapping[str, object],
        transaction_id: str,
        timestamp: str,
    ) -> int:
        previous = connection.execute(
            "SELECT event_hash FROM control_events ORDER BY control_sequence DESC LIMIT 1"
        ).fetchone()
        previous_hash = str(previous[0]) if previous else ""
        event_hash = _hash_bytes(
            _json(
                {
                    "kind": kind,
                    "entity": entity_id,
                    "payload": payload,
                    "previous": previous_hash,
                }
            ).encode()
        )
        cursor = connection.execute(
            "INSERT INTO control_events(control_id, control_kind, entity_type, entity_id, branch_id, view_id, checkpoint_id, payload_json, transaction_id, previous_event_hash, event_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                uuid4().hex,
                kind,
                entity_type,
                entity_id,
                branch_id,
                view_id,
                checkpoint_id,
                _json(payload),
                transaction_id,
                previous_hash or None,
                event_hash,
                timestamp,
            ),
        )
        return int(cursor.lastrowid)

    def latest_checkpoint(
        self,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str | None,
        *,
        snapshot: RolloutReadSnapshot | None = None,
    ) -> RolloutCheckpointIndex | None:
        if snapshot is not None:
            row = self._checkpoint_row(
                self._snapshot_connection(snapshot),
                checkpoint_ns,
                checkpoint_id,
            )
            return self._checkpoint_index(row) if row else None
        if snapshot is None:
            self.initialize(thread_id, checkpoint_ns)
        with self._connect(thread_id, checkpoint_ns) as connection:
            row = self._checkpoint_row(connection, checkpoint_ns, checkpoint_id)
        return self._checkpoint_index(row) if row else None

    def active_view_id(
        self,
        snapshot: RolloutReadSnapshot,
    ) -> str | None:
        row = (
            self._snapshot_connection(snapshot)
            .execute(
                "SELECT head_view_id FROM branches WHERE branch_id = ?",
                (snapshot.manifest.active_branch_id,),
            )
            .fetchone()
        )
        return str(row[0]) if row is not None and row[0] is not None else None

    def list_checkpoints(
        self,
        thread_id: str,
        checkpoint_ns: str | None,
        *,
        before_checkpoint_id: str | None,
        limit: int | None,
    ) -> list[RolloutCheckpointIndex]:
        self.initialize(thread_id, checkpoint_ns or "")
        with self._connect(thread_id, checkpoint_ns or "") as connection:
            before = (
                connection.execute(
                    "SELECT commit_id FROM checkpoints WHERE checkpoint_id = ? AND checkpoint_ns = ?",
                    (before_checkpoint_id, checkpoint_ns or ""),
                ).fetchone()
                if before_checkpoint_id
                else None
            )
            query = (
                "SELECT checkpoint_id, checkpoint_ns, commit_id, message_sequence, message_count, parent_checkpoint_id, view_id, branch_id, checkpoint_version, checkpoint_timestamp, checkpoint_json, metadata_json, versions_seen_type, versions_seen_blob, pending_sends_type, pending_sends_blob FROM checkpoints WHERE checkpoint_ns = ? AND status = 'active' AND (? IS NULL OR commit_id < ?) ORDER BY commit_id DESC"
                + (" LIMIT ?" if limit is not None else "")
            )
            params: tuple[object, ...] = (
                checkpoint_ns or "",
                (int(before[0]) if before else None),
                (int(before[0]) if before else None),
            ) + ((limit,) if limit is not None else ())
            rows = connection.execute(query, params).fetchall()
        return [self._checkpoint_index(row) for row in rows]

    @staticmethod
    def _checkpoint_index(row: Sequence[object]) -> RolloutCheckpointIndex:
        return RolloutCheckpointIndex(
            str(row[0]),
            str(row[1]),
            int(row[2]),
            int(row[3]),
            int(row[4]),
            str(row[5]) if row[5] is not None else None,
            str(row[6]),
            str(row[7]),
            int(row[8]),
            str(row[9]),
            str(row[10]),
            str(row[11]),
            str(row[12]),
            bytes(row[13]),
            str(row[14]),
            bytes(row[15]),
        )

    def checkpoint_values(
        self,
        thread_id: str,
        checkpoint_ns: str,
        index: RolloutCheckpointIndex,
        *,
        snapshot: RolloutReadSnapshot | None = None,
        context_view_id_override: str | None = None,
    ) -> dict[str, object]:
        def read(connection: sqlite3.Connection) -> dict[str, object]:
            rows = connection.execute(
                "SELECT channel_name, storage_kind, value_state, serializer_name, value_blob, value_hash, context_view_id FROM checkpoint_channels WHERE checkpoint_id = ?",
                (index.checkpoint_id,),
            ).fetchall()
            values: dict[str, object] = {}
            has_messages = False
            for channel, storage_kind, state, serializer, blob, digest, view_id in rows:
                if channel == "messages":
                    context_view_id = context_view_id_override or index.view_id
                    if (
                        storage_kind != "rollout_view"
                        or state != "view"
                        or view_id != index.view_id
                    ):
                        raise RuntimeError(
                            f"checkpoint messages view 指针非法: {index.checkpoint_id}"
                        )
                    values[channel] = self._messages_for_view(
                        thread_id,
                        checkpoint_ns,
                        context_view_id,
                        connection=connection,
                    )
                    has_messages = True
                elif state == "present":
                    if (
                        not isinstance(blob, bytes)
                        or not isinstance(serializer, str)
                        or _hash_bytes(blob) != digest
                    ):
                        raise RuntimeError(
                            f"checkpoint channel BLOB 校验失败: {index.checkpoint_id}/{channel}"
                        )
                    values[str(channel)] = self.decode_value((str(serializer), blob))
            if not has_messages:
                raise RuntimeError(
                    f"checkpoint 缺少 messages channel: {index.checkpoint_id}"
                )
            return values

        if snapshot is not None:
            return read(self._snapshot_connection(snapshot))
        with self._connect(thread_id, checkpoint_ns) as connection:
            return read(connection)

    def load_checkpoint(
        self,
        thread_id: str,
        checkpoint_ns: str,
        index: RolloutCheckpointIndex,
        *,
        snapshot: RolloutReadSnapshot | None = None,
        context_view_id_override: str | None = None,
    ) -> Checkpoint:
        checkpoint = json.loads(index.checkpoint_json)
        if not isinstance(checkpoint, dict):
            raise TypeError("checkpoint core JSON 必须是对象")
        checkpoint["channel_values"] = self.checkpoint_values(
            thread_id,
            checkpoint_ns,
            index,
            snapshot=snapshot,
            context_view_id_override=context_view_id_override,
        )
        checkpoint["channel_versions"] = {}
        checkpoint["updated_channels"] = []
        if snapshot is None:
            with self._connect(thread_id, checkpoint_ns) as connection:
                rows = connection.execute(
                    "SELECT channel_name, channel_version, updated_index FROM checkpoint_channels WHERE checkpoint_id = ? ORDER BY updated_index",
                    (index.checkpoint_id,),
                ).fetchall()
        else:
            connection = self._snapshot_connection(snapshot)
            rows = connection.execute(
                "SELECT channel_name, channel_version, updated_index FROM checkpoint_channels WHERE checkpoint_id = ? ORDER BY updated_index",
                (index.checkpoint_id,),
            ).fetchall()
        for name, version, updated_index in rows:
            if version is not None:
                checkpoint["channel_versions"][str(name)] = str(version)
            if updated_index is not None:
                checkpoint["updated_channels"].append(str(name))
        checkpoint["versions_seen"] = self.decode_value(
            (index.versions_seen_type, index.versions_seen_blob)
        )
        checkpoint["pending_sends"] = self.decode_value(
            (index.pending_sends_type, index.pending_sends_blob)
        )
        return checkpoint

    def metadata(self, index: RolloutCheckpointIndex) -> CheckpointMetadata:
        value = json.loads(index.metadata_json)
        if not isinstance(value, dict):
            raise TypeError("checkpoint metadata JSON 必须是对象")
        return value

    def _messages_for_view(
        self,
        thread_id: str,
        checkpoint_ns: str,
        view_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> list[BaseMessage]:
        sequences = self._view_message_sequences(
            thread_id,
            checkpoint_ns,
            view_id,
            set(),
            connection=connection,
        )
        if not sequences:
            return []
        values = self._read_messages(
            thread_id,
            checkpoint_ns,
            sequences,
            connection=connection,
        )
        return [messages_from_dict([value])[0] for value in values]

    def _view_message_sequences(
        self,
        thread_id: str,
        checkpoint_ns: str,
        view_id: str,
        visited: set[str],
        *,
        connection: sqlite3.Connection | None = None,
    ) -> list[int]:
        if connection is None:
            with self._connect(thread_id, checkpoint_ns) as owned_connection:
                return self._view_message_sequences(
                    thread_id,
                    checkpoint_ns,
                    view_id,
                    visited,
                    connection=owned_connection,
                )
        if view_id in visited:
            raise RuntimeError(f"context view 引用形成循环: {view_id}")
        visited.add(view_id)
        ranges = connection.execute(
            "SELECT source_kind, source_view_id, start_message_sequence, end_message_sequence FROM context_view_ranges WHERE view_id = ? ORDER BY range_index",
            (view_id,),
        ).fetchall()
        view_row = connection.execute(
            "SELECT view_kind, parent_view_id FROM context_views WHERE view_id = ?",
            (view_id,),
        ).fetchone()
        result: list[int] = []
        has_parent_range = False
        for source_kind, source_view, start, end in ranges:
            if source_kind == "view":
                if not isinstance(source_view, str):
                    raise TypeError(
                        f"context view range 缺少 source_view_id: {view_id}"
                    )
                result.extend(
                    self._view_message_sequences(
                        thread_id,
                        checkpoint_ns,
                        source_view,
                        visited.copy(),
                        connection=connection,
                    )
                )
                if (
                    view_row is not None
                    and view_row[1] is not None
                    and source_view == str(view_row[1])
                ):
                    has_parent_range = True
            elif (
                source_kind == "messages"
                and isinstance(start, int)
                and isinstance(end, int)
            ):
                rows = connection.execute(
                    "SELECT message_sequence FROM messages WHERE message_sequence BETWEEN ? AND ? ORDER BY message_sequence",
                    (start, end),
                ).fetchall()
                result.extend(int(row[0]) for row in rows)
        if (
            view_row is not None
            and view_row[0] == "checkpoint"
            and isinstance(view_row[1], str)
            and not has_parent_range
        ):
            # 兼容旧版本已落盘的普通 view：旧数据只有 parent_view_id 和
            # context_view_jumps，没有可物化的父 view range。读取时补入父链，
            # 避免旧 ToolMessage 在恢复后失去对应的 assistant tool call。
            parent_sequences = self._view_message_sequences(
                thread_id,
                checkpoint_ns,
                str(view_row[1]),
                visited.copy(),
                connection=connection,
            )
            result = [*parent_sequences, *result]
        return self._include_tool_call_declarations(connection, result)

    @staticmethod
    def _include_tool_call_declarations(
        connection: sqlite3.Connection,
        sequences: Sequence[int],
    ) -> list[int]:
        """确保 view 中的工具结果始终带有对应的 assistant 工具声明。

        旧版本在增量 checkpoint 只携带 ToolMessage，或在并行工具组被拆成
        多段 delta 时，可能把结果范围写入 view，却漏掉父 view 中的
        AIMessage。Responses provider 会把这种状态编码为孤立
        ``function_call_output`` 并直接拒绝请求。tool_calls projection 是
        canonical 消息之外的配对索引；读取时补回声明序号不会修改 JSONL，
        也不会把缺少配对索引的损坏结果伪装成成功。
        """
        ordered = list(dict.fromkeys(int(sequence) for sequence in sequences))
        if not ordered:
            return []
        rows: list[sqlite3.Row | tuple[object, ...]] = []
        for offset in range(0, len(ordered), 500):
            chunk = ordered[offset : offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows.extend(
                connection.execute(
                    "SELECT assistant_message_sequence FROM tool_calls "
                    f"WHERE result_message_sequence IN ({placeholders})",
                    tuple(chunk),
                ).fetchall()
            )
        present = set(ordered)
        missing = {
            int(row[0])
            for row in rows
            if row[0] is not None and int(row[0]) not in present
        }
        if not missing:
            return ordered
        return sorted((*present, *missing))

    def _view_message_sequences_from_connection(
        self,
        connection: sqlite3.Connection,
        view_id: str,
        visited: set[str] | None = None,
    ) -> list[int]:
        """在单个 SQLite snapshot 中解析 view，供离线 compaction 使用。"""
        seen = set(visited or ())
        if view_id in seen:
            raise RuntimeError(f"context view 引用形成循环: {view_id}")
        seen.add(view_id)
        ranges = connection.execute(
            "SELECT source_kind, source_view_id, start_message_sequence, end_message_sequence FROM context_view_ranges WHERE view_id = ? ORDER BY range_index",
            (view_id,),
        ).fetchall()
        view_row = connection.execute(
            "SELECT view_kind, parent_view_id FROM context_views WHERE view_id = ?",
            (view_id,),
        ).fetchone()
        result: list[int] = []
        has_parent_range = False
        for source_kind, source_view, start, end in ranges:
            if source_kind == "view":
                if not isinstance(source_view, str):
                    raise TypeError(
                        f"context view range 缺少 source_view_id: {view_id}"
                    )
                result.extend(
                    self._view_message_sequences_from_connection(
                        connection, source_view, seen
                    )
                )
                if (
                    view_row is not None
                    and view_row[1] is not None
                    and source_view == str(view_row[1])
                ):
                    has_parent_range = True
            elif source_kind == "messages" and start is not None and end is not None:
                rows = connection.execute(
                    "SELECT message_sequence FROM messages WHERE message_sequence BETWEEN ? AND ? ORDER BY message_sequence",
                    (int(start), int(end)),
                ).fetchall()
                result.extend(int(row[0]) for row in rows)
        if (
            view_row is not None
            and view_row[0] == "checkpoint"
            and isinstance(view_row[1], str)
            and not has_parent_range
        ):
            # 兼容旧版本的普通 view，详见上面的在线读取路径。
            parent_sequences = self._view_message_sequences_from_connection(
                connection, str(view_row[1]), seen
            )
            result = [*parent_sequences, *result]
        return self._include_tool_call_declarations(connection, result)

    def _read_messages(
        self,
        thread_id: str,
        checkpoint_ns: str,
        sequences: Iterable[int],
        *,
        connection: sqlite3.Connection | None = None,
    ) -> list[dict[str, object]]:
        sequence_values = tuple(dict.fromkeys(int(value) for value in sequences))
        if not sequence_values:
            return []
        values: list[dict[str, object]] = []
        root = self.root(thread_id, checkpoint_ns)
        if connection is None:
            with self._connect(thread_id, checkpoint_ns) as owned_connection:
                return self._read_messages(
                    thread_id,
                    checkpoint_ns,
                    sequence_values,
                    connection=owned_connection,
                )
        rows = connection.execute(
            "SELECT message_sequence, message_id, turn_id, created_at, jsonl_offset, jsonl_length FROM messages WHERE message_sequence IN ("
            + ",".join("?" for _ in sequence_values)
            + ") ORDER BY message_sequence",
            sequence_values,
        ).fetchall()
        with self.jsonl_path(thread_id, checkpoint_ns).open("rb") as stream:
            for sequence, message_id, turn_id, created_at, offset, length in rows:
                stream.seek(int(offset))
                raw = stream.read(int(length))
                envelope = json.loads(raw.decode("utf-8"))
                if (
                    not isinstance(envelope, dict)
                    or envelope.get("sequence") != sequence
                ):
                    raise RuntimeError(
                        f"rollout.jsonl message 与 SQLite 不一致: sequence={sequence}"
                    )
                message = envelope.get("message")
                if not isinstance(message, dict):
                    raise TypeError(
                        f"rollout.jsonl message 非对象: sequence={sequence}"
                    )
                values.append(
                    self._hydrate_message_identity(
                        message,
                        message_id=str(message_id),
                        turn_id=str(turn_id),
                        created_at=str(created_at),
                    )
                )
        del root
        return values

    @staticmethod
    def _hydrate_message_identity(
        message: Mapping[str, object],
        *,
        message_id: str,
        turn_id: str,
        created_at: str,
    ) -> dict[str, object]:
        """把 SQLite 的 canonical identity 回填到 LangChain 消息。

        JSONL 只保存模型消息原文，原文不要求把业务层的 message_id、Turn
        和持久化时间重复写入 response_metadata。SQLite 是这些字段的权威
        来源；读取 checkpoint、Replay 和 Fork 时必须把它们恢复到
        BaseMessage，否则同一条消息在 Web DTO 和 replay 定位中会失去身份。
        该回填只发生在内存中的反序列化副本，不改变 JSONL canonical 内容。
        """
        hydrated = dict(message)
        raw_data = hydrated.get("data")
        if not isinstance(raw_data, Mapping):
            raise TypeError("rollout 消息 data 必须是对象")
        data = dict(raw_data)
        raw_metadata = data.get("response_metadata")
        metadata = dict(raw_metadata) if isinstance(raw_metadata, Mapping) else {}
        metadata["message_id"] = message_id
        metadata["created_at"] = created_at
        metadata["updated_at"] = created_at
        raw_message_metadata = metadata.get("message_metadata")
        message_metadata = (
            dict(raw_message_metadata)
            if isinstance(raw_message_metadata, Mapping)
            else {}
        )
        message_metadata["turn_id"] = turn_id
        message_metadata["job_id"] = turn_id
        metadata["message_metadata"] = message_metadata
        data["response_metadata"] = metadata
        data["id"] = message_id
        hydrated["data"] = data
        return hydrated

    def materialize_messages(
        self,
        thread_id: str,
        checkpoint_ns: str,
        message_sequence: int,
        *,
        snapshot: RolloutReadSnapshot | None = None,
        context_ranges: Iterable[tuple[str, int, int]] | None = None,
    ) -> list[BaseMessage]:
        connection = (
            self._snapshot_connection(snapshot) if snapshot is not None else None
        )
        ranges = tuple(context_ranges or ())
        if ranges:
            sequences = self._view_message_sequences(
                thread_id,
                checkpoint_ns,
                ranges[-1][0],
                set(),
                connection=connection,
            )
        else:
            if connection is None:
                with self._connect(thread_id, checkpoint_ns) as owned_connection:
                    row = self._checkpoint_row(owned_connection, checkpoint_ns, None)
            else:
                row = self._checkpoint_row(connection, checkpoint_ns, None)
            sequences = (
                self._view_message_sequences(
                    thread_id,
                    checkpoint_ns,
                    str(row[6]),
                    set(),
                    connection=connection,
                )
                if row
                else []
            )
        return [
            messages_from_dict([value])[0]
            for value in self._read_messages(
                thread_id,
                checkpoint_ns,
                sequences,
                connection=connection,
            )
        ]

    def resolve_context_chain_ranges(
        self, snapshot: RolloutReadSnapshot, message_sequence: int
    ) -> tuple[tuple[str, int, int], ...]:
        connection = self._snapshot_connection(snapshot)
        row = connection.execute(
            "SELECT view_id, message_sequence FROM checkpoints WHERE checkpoint_ns = ? AND message_sequence = ? ORDER BY commit_id DESC LIMIT 1",
            (snapshot.checkpoint_ns, message_sequence),
        ).fetchone()
        if row is None:
            row = connection.execute(
                "SELECT head_view_id, head_message_sequence FROM context_views WHERE view_id = (SELECT head_view_id FROM branches WHERE branch_id = ?) LIMIT 1",
                (snapshot.manifest.active_branch_id,),
            ).fetchone()
        if row is None or row[0] is None:
            return ()
        self._validate_context_view_chain_connection(connection, str(row[0]))
        return ((str(row[0]), 0, int(row[1])),)

    def validate_context_view_chain(
        self,
        thread_id: str,
        checkpoint_ns: str,
        view_id: str,
    ) -> None:
        """校验 context view 父链、jump table 和消息范围，发现损坏立即失败。"""
        self.initialize(thread_id, checkpoint_ns)
        with self._connect(thread_id, checkpoint_ns) as connection:
            self._validate_context_view_chain_connection(connection, view_id)

    def _validate_context_view_chain_connection(
        self,
        connection: sqlite3.Connection,
        view_id: str,
    ) -> None:
        rows = connection.execute(
            "SELECT view_id, parent_view_id FROM context_views"
        ).fetchall()
        parents = {
            str(row[0]): (str(row[1]) if row[1] is not None else None) for row in rows
        }
        if view_id not in parents:
            raise RuntimeError(f"context view 不存在: {view_id}")
        current = view_id
        visited: set[str] = set()
        while current is not None:
            if current in visited:
                raise RuntimeError(f"context view 父链成环: {view_id}")
            visited.add(current)
            parent = parents[current]
            if parent is not None and parent not in parents:
                raise RuntimeError(
                    f"context view 引用不存在的 parent: {current} -> {parent}"
                )
            current = parent

        jumps = connection.execute(
            "SELECT jump_level, ancestor_view_id, ancestor_depth FROM context_view_jumps WHERE view_id = ? ORDER BY jump_level",
            (view_id,),
        ).fetchall()
        for level, ancestor_view_id, ancestor_depth in jumps:
            expected = view_id
            for _ in range(2 ** int(level)):
                parent = parents.get(expected)
                if parent is None:
                    raise RuntimeError(
                        f"context view jump 越界: view={view_id}, level={level}"
                    )
                expected = parent
            if expected != ancestor_view_id or int(ancestor_depth) != 2 ** int(level):
                raise RuntimeError(
                    f"context view jump 非法: view={view_id}, level={level}"
                )

        ranges = connection.execute(
            "SELECT source_kind, start_message_sequence, end_message_sequence FROM context_view_ranges WHERE view_id = ? ORDER BY range_index",
            (view_id,),
        ).fetchall()
        for source_kind, start, end in ranges:
            if source_kind != "messages" or start is None or end is None:
                continue
            expected_count = 1 if int(start) == int(end) else 2
            present_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM messages WHERE message_sequence IN (?, ?)",
                    (int(start), int(end)),
                ).fetchone()[0]
            )
            if present_count != expected_count:
                raise RuntimeError(
                    f"context view range 引用不存在消息: view={view_id}, start={start}, end={end}"
                )

    def _records_for_turns(
        self,
        thread_id: str,
        checkpoint_ns: str,
        turn_ids: Iterable[str],
        *,
        message_roles: Iterable[str] | None = None,
        sequences: Iterable[int] | None = None,
        tool_kinds: Iterable[str] | None = None,
        tool_call_ids: Iterable[str] | None = None,
        view_id: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> list[tuple[int, str, int, int]]:
        ids = tuple(dict.fromkeys(turn_ids))
        if not ids:
            return []
        if connection is None:
            with self._connect(thread_id, checkpoint_ns) as owned_connection:
                return self._records_for_turns(
                    thread_id,
                    checkpoint_ns,
                    ids,
                    message_roles=message_roles,
                    sequences=sequences,
                    tool_kinds=tool_kinds,
                    tool_call_ids=tool_call_ids,
                    view_id=view_id,
                    connection=owned_connection,
                )
        predicates = ["m.turn_id IN (" + ",".join("?" for _ in ids) + ")"]
        params: list[object] = list(ids)
        roles = tuple(dict.fromkeys(message_roles or ()))
        if message_roles is not None and not roles:
            # 显式空角色集合表示调用方不需要普通 message 行；不能退化为
            # 不加 role 条件，否则 ToolRow 定点补载会重新读取整轮消息。
            predicates.append("0")
        elif roles:
            predicates.append("m.role IN (" + ",".join("?" for _ in roles) + ")")
            params.extend(roles)
        selected = {int(value) for value in sequences or ()}
        if selected:
            predicates.append(
                "m.message_sequence IN (" + ",".join("?" for _ in selected) + ")"
            )
            params.extend(sorted(selected))
        if view_id:
            predicates.append(
                "EXISTS (SELECT 1 FROM context_view_turns cvt WHERE cvt.view_id = ? AND cvt.turn_id = m.turn_id)"
            )
            params.append(view_id)
        rows = connection.execute(
            "SELECT m.message_sequence, m.turn_id, m.jsonl_offset, m.jsonl_length FROM messages m WHERE "
            + " AND ".join(predicates)
            + " ORDER BY m.message_sequence",
            tuple(params),
        ).fetchall()
        result = {(int(row[0]), str(row[1]), int(row[2]), int(row[3])) for row in rows}
        selected_tool_call_ids = tuple(dict.fromkeys(tool_call_ids or ()))
        tool_id_filter = ""
        tool_id_params: tuple[object, ...] = ()
        if selected_tool_call_ids:
            tool_id_filter = " AND tc.tool_call_id IN (" + ",".join(
                "?" for _ in selected_tool_call_ids
            ) + ")"
            tool_id_params = tuple(selected_tool_call_ids)

            # 一个 assistant message 可能同时声明多个工具；先取命中 call 所在的
            # assistant message，后续 mapper 再按 tool_call_id 过滤 payload。
            assistant_rows = connection.execute(
                "SELECT tc.assistant_message_sequence, m.turn_id, m.jsonl_offset, m.jsonl_length "
                "FROM tool_calls tc JOIN messages m ON m.message_sequence = tc.assistant_message_sequence "
                "WHERE m.turn_id IN (" + ",".join("?" for _ in ids) + ")"
                + tool_id_filter,
                (*ids, *tool_id_params),
            ).fetchall()
            result.update(
                (int(row[0]), str(row[1]), int(row[2]), int(row[3]))
                for row in assistant_rows
            )
        if tool_kinds:
            for kind in tool_kinds:
                if kind == "tool_call":
                    tool_rows = connection.execute(
                        "SELECT tc.assistant_message_sequence, m.turn_id, m.jsonl_offset, m.jsonl_length FROM tool_calls tc JOIN messages m ON m.message_sequence = tc.assistant_message_sequence WHERE m.turn_id IN ("
                        + ",".join("?" for _ in ids)
                        + ")"
                        + tool_id_filter,
                        (*ids, *tool_id_params),
                    ).fetchall()
                elif kind == "tool_result":
                    tool_rows = connection.execute(
                        "SELECT tc.result_message_sequence, m.turn_id, m.jsonl_offset, m.jsonl_length FROM tool_calls tc JOIN messages m ON m.message_sequence = tc.result_message_sequence WHERE tc.result_message_sequence IS NOT NULL AND m.turn_id IN ("
                        + ",".join("?" for _ in ids)
                        + ")"
                        + tool_id_filter,
                        (*ids, *tool_id_params),
                    ).fetchall()
                else:
                    tool_rows = []
                result.update(
                    (int(row[0]), str(row[1]), int(row[2]), int(row[3]))
                    for row in tool_rows
                )
        return sorted(result)

    def read_indexed_records(
        self,
        thread_id: str,
        checkpoint_ns: str = "",
        *,
        after_sequence: int = 0,
        through_sequence: int | None = None,
        sequence_ranges: Iterable[tuple[str, int, int]] | None = None,
        branch_id: str | None = None,
        turn_id: str | None = None,
        kinds: Iterable[str] | None = None,
        snapshot: RolloutReadSnapshot | None = None,
    ) -> list[dict[str, object]]:
        del branch_id, kinds
        if turn_id is None:
            return []
        upper_bound = (
            through_sequence if through_sequence is not None else after_sequence
        )
        rows = self._records_for_turns(
            thread_id,
            checkpoint_ns,
            (turn_id,),
            sequences=range(after_sequence + 1, upper_bound + 1),
            view_id=next(iter(sequence_ranges), (None, 0, 0))[0]
            if sequence_ranges
            else None,
            connection=(self._snapshot_connection(snapshot) if snapshot else None),
        )
        return self._read_record_envelopes(thread_id, checkpoint_ns, rows)

    def read_indexed_records_batch(
        self,
        snapshot: RolloutReadSnapshot,
        *,
        turn_ids: Iterable[str],
        sequence_ranges: Iterable[tuple[str, int, int]] | None = None,
        branch_id: str | None = None,
        kinds: Iterable[str] | None = None,
        message_roles: Iterable[str] | None = None,
        tool_kinds: Iterable[str] | None = None,
        tool_call_ids: Iterable[str] | None = None,
        required_sequences: Mapping[str, Iterable[int]] | None = None,
    ) -> dict[str, list[dict[str, object]]]:
        del branch_id, kinds
        connection = self._snapshot_connection(snapshot)
        ids = tuple(dict.fromkeys(turn_ids))
        view_id = (
            next(iter(sequence_ranges), (None, 0, 0))[0] if sequence_ranges else None
        )
        result: dict[str, list[dict[str, object]]] = {turn_id: [] for turn_id in ids}
        rows = self._records_for_turns(
            snapshot.thread_id,
            snapshot.checkpoint_ns,
            ids,
            message_roles=message_roles,
            tool_kinds=tool_kinds,
            tool_call_ids=tool_call_ids,
            view_id=view_id,
            connection=connection,
        )
        required = required_sequences or {}
        required_values = {
            int(sequence) for sequences in required.values() for sequence in sequences
        }
        if required_values:
            rows.extend(
                self._records_for_turns(
                    snapshot.thread_id,
                    snapshot.checkpoint_ns,
                    ids,
                    sequences=required_values,
                    view_id=view_id,
                    connection=connection,
                )
            )
        unique_rows = {
            (sequence, turn_id, offset, length): (sequence, turn_id, offset, length)
            for sequence, turn_id, offset, length in rows
        }
        records = self._read_record_envelopes(
            snapshot.thread_id,
            snapshot.checkpoint_ns,
            sorted(unique_rows.values()),
        )
        for record in records:
            turn_id = record.get("turn_id")
            if isinstance(turn_id, str) and turn_id in result:
                result[turn_id].append(record)
        return result

    def _read_record_envelopes(
        self,
        thread_id: str,
        checkpoint_ns: str,
        rows: Iterable[tuple[int, str, int, int]],
    ) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        with self.jsonl_path(thread_id, checkpoint_ns).open("rb") as stream:
            for sequence, _turn_id_value, offset, length in rows:
                stream.seek(offset)
                value = json.loads(stream.read(length).decode("utf-8"))
                if not isinstance(value, dict) or not isinstance(
                    value.get("message"), dict
                ):
                    raise TypeError(
                        f"rollout.jsonl message envelope 非法: sequence={sequence}"
                    )
                result.append(
                    {
                        "kind": "message_append",
                        "sequence": sequence,
                        "_indexed_sequence": sequence,
                        "turn_id": value.get("turn_id"),
                        "message_id": value.get("message_id"),
                        "message": value["message"],
                        "role": value.get("role"),
                    }
                )
        return result

    def indexed_turn_spans(
        self,
        thread_id: str,
        checkpoint_ns: str = "",
        *,
        after_sequence: int = 0,
        through_sequence: int | None = None,
        branch_id: str | None = None,
        snapshot: RolloutReadSnapshot | None = None,
    ) -> list[tuple[str, int, int]]:
        del branch_id, snapshot
        self.initialize(thread_id, checkpoint_ns)
        with self._connect(thread_id, checkpoint_ns) as connection:
            rows = connection.execute(
                f"SELECT t.turn_id, t.first_message_sequence, t.last_message_sequence FROM turns AS t WHERE t.turn_kind = 'normal' AND t.user_message_sequence IS NOT NULL AND t.last_message_sequence > ? AND (? IS NULL OR t.first_message_sequence <= ?) AND {_VISIBLE_NORMAL_TURN_PREDICATE} ORDER BY t.turn_ordinal",
                (after_sequence, through_sequence, through_sequence),
            ).fetchall()
        return [(str(row[0]), int(row[1]), int(row[2])) for row in rows]

    def indexed_turn_spans_for_ranges(
        self, snapshot: RolloutReadSnapshot, ranges: Iterable[tuple[str, int, int]]
    ) -> list[tuple[str, int, int]]:
        values = tuple(ranges)
        if not values:
            return []
        view_id = values[-1][0]
        connection = self._snapshot_connection(snapshot)
        rows = connection.execute(
            f"SELECT cvt.turn_id, t.first_message_sequence, t.last_message_sequence FROM context_view_turns cvt JOIN turns t ON t.turn_id = cvt.turn_id WHERE cvt.view_id = ? AND {_VISIBLE_NORMAL_TURN_PREDICATE} ORDER BY cvt.logical_turn_ordinal",
            (view_id,),
        ).fetchall()
        return [(str(row[0]), int(row[1]), int(row[2])) for row in rows]

    def context_turn_count(
        self,
        snapshot: RolloutReadSnapshot,
        view_id: str,
    ) -> int:
        """返回一个逻辑 context view 的 Turn 数量，不读取消息正文。"""
        connection = self._snapshot_connection(snapshot)
        row = connection.execute(
            f"SELECT COUNT(*) FROM context_view_turns AS cvt JOIN turns AS t ON t.turn_id = cvt.turn_id WHERE cvt.view_id = ? AND {_VISIBLE_NORMAL_TURN_PREDICATE}",
            (view_id,),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def read_context_turn_page(
        self,
        snapshot: RolloutReadSnapshot,
        view_id: str,
        *,
        direction: str,
        anchor_ordinal: int | None,
        limit: int,
    ) -> tuple[list[tuple[str, int, int, int]], bool]:
        """按逻辑 Turn 序号执行 keyset 分页，并多读一行判断是否还有数据。"""
        if direction not in {"tail", "before", "head", "after"}:
            raise ValueError(f"不支持的 Turn keyset 方向: {direction}")
        if limit < 1:
            raise ValueError("Turn keyset limit 必须大于 0")
        connection = self._snapshot_connection(snapshot)
        query_limit = limit + 1
        params: list[object] = [view_id]
        where = "cvt.view_id = ?"
        order = "ASC"
        if direction == "tail":
            order = "DESC"
        elif direction == "before":
            if anchor_ordinal is None:
                raise ValueError("before keyset 必须提供 anchor_ordinal")
            where += " AND cvt.logical_turn_ordinal < ?"
            params.append(anchor_ordinal)
            order = "DESC"
        elif direction == "head":
            order = "ASC"
        elif direction == "after":
            if anchor_ordinal is None:
                raise ValueError("after keyset 必须提供 anchor_ordinal")
            where += " AND cvt.logical_turn_ordinal > ?"
            params.append(anchor_ordinal)
            order = "ASC"
        rows = connection.execute(
            f"SELECT cvt.turn_id, t.first_message_sequence, t.last_message_sequence, cvt.logical_turn_ordinal "
            f"FROM context_view_turns cvt JOIN turns t ON t.turn_id = cvt.turn_id "
            f"WHERE {where} AND {_VISIBLE_NORMAL_TURN_PREDICATE} ORDER BY cvt.logical_turn_ordinal {order} LIMIT ?",
            (*params, query_limit),
        ).fetchall()
        has_more = len(rows) > limit
        selected = rows[:limit]
        if direction in {"tail", "before"}:
            selected.reverse()
        return [
            (str(row[0]), int(row[1]), int(row[2]), int(row[3])) for row in selected
        ], has_more

    def read_context_turn_window(
        self,
        snapshot: RolloutReadSnapshot,
        view_id: str,
        *,
        anchor_ordinal: int,
        before: int,
        after: int,
    ) -> list[tuple[str, int, int, int]]:
        """只读取游标附近的逻辑 Turn，用于 around 请求。"""
        if anchor_ordinal < 1 or before < 0 or after < 0:
            raise ValueError("around keyset 参数非法")
        connection = self._snapshot_connection(snapshot)
        rows = connection.execute(
            "SELECT cvt.turn_id, t.first_message_sequence, t.last_message_sequence, cvt.logical_turn_ordinal "
            "FROM context_view_turns cvt JOIN turns t ON t.turn_id = cvt.turn_id "
            f"WHERE cvt.view_id = ? AND cvt.logical_turn_ordinal BETWEEN ? AND ? AND {_VISIBLE_NORMAL_TURN_PREDICATE} "
            "ORDER BY cvt.logical_turn_ordinal",
            (view_id, max(1, anchor_ordinal - before), anchor_ordinal + after),
        ).fetchall()
        return [(str(row[0]), int(row[1]), int(row[2]), int(row[3])) for row in rows]

    def read_context_turn_ids(
        self,
        snapshot: RolloutReadSnapshot,
        view_id: str,
        turn_ids: Iterable[str],
    ) -> list[tuple[str, int, int, int]]:
        """按指定 Turn ID 读取当前 view 中存在的 Turn，并按逻辑序号排序。"""
        ids = tuple(dict.fromkeys(turn_ids))
        if not ids:
            return []
        connection = self._snapshot_connection(snapshot)
        placeholders = ",".join("?" for _ in ids)
        rows = connection.execute(
            f"SELECT cvt.turn_id, t.first_message_sequence, t.last_message_sequence, cvt.logical_turn_ordinal "
            f"FROM context_view_turns cvt JOIN turns t ON t.turn_id = cvt.turn_id "
            f"WHERE cvt.view_id = ? AND cvt.turn_id IN ({placeholders}) AND {_VISIBLE_NORMAL_TURN_PREDICATE} "
            "ORDER BY cvt.logical_turn_ordinal",
            (view_id, *ids),
        ).fetchall()
        return [(str(row[0]), int(row[1]), int(row[2]), int(row[3])) for row in rows]

    def read_turn_projections(
        self, snapshot: RolloutReadSnapshot, turn_ids: Iterable[str]
    ) -> dict[str, dict[str, object]]:
        ids = tuple(dict.fromkeys(turn_ids))
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        connection = self._snapshot_connection(snapshot)
        turns = connection.execute(
            f"SELECT turn_id, first_message_sequence, last_message_sequence, final_message_sequence, final_message_id, status, created_at, updated_at FROM turns WHERE turn_id IN ({placeholders})",
            ids,
        ).fetchall()
        message_count_rows = connection.execute(
            f"SELECT turn_id, COUNT(*) FROM messages WHERE turn_id IN ({placeholders}) GROUP BY turn_id",
            ids,
        ).fetchall()
        tool_rows = connection.execute(
            f"SELECT m.turn_id, tc.tool_call_id, tc.tool_name, tc.status, tc.result_message_sequence, tc.assistant_message_sequence, tc.call_index FROM tool_calls tc JOIN messages m ON m.message_sequence = tc.assistant_message_sequence WHERE m.turn_id IN ({placeholders}) ORDER BY tc.assistant_message_sequence, tc.call_index",
            ids,
        ).fetchall()
        final_rows = connection.execute(
            f"SELECT t.turn_id, mp.visible_text, mp.visible_text_truncated FROM turns t LEFT JOIN message_projections mp ON mp.message_sequence = t.final_message_sequence WHERE t.turn_id IN ({placeholders})",
            ids,
        ).fetchall()
        thinking_rows = connection.execute(
            f"SELECT m.turn_id, rb.message_sequence, rb.content_block_index, rb.item_index, rb.carrier_type, rb.reasoning_text, rb.summary_text, rb.signature_present, rb.encrypted_length FROM reasoning_blocks rb JOIN messages m ON m.message_sequence = rb.message_sequence WHERE m.turn_id IN ({placeholders}) ORDER BY rb.message_sequence, rb.content_block_index, rb.item_index",
            ids,
        ).fetchall()
        result: dict[str, dict[str, object]] = {}
        for row in turns:
            result[str(row[0])] = {
                "first_sequence": int(row[1]),
                "last_sequence": int(row[2]),
                "final_message_sequence": int(row[3]) if row[3] is not None else None,
                "final_message_id": row[4],
                "status": str(row[5]),
                "final_source": "turn_finalize" if row[3] is not None else None,
                "final_response_text": "",
                "final_response_text_truncated": False,
                "thinking_blocks": [],
                "assistant_text_sequences": [],
                "has_encrypted_reasoning": False,
                "tool_items": [],
                "created_at": str(row[6]),
                "updated_at": str(row[7]),
                "activity_stats": {
                    "duration_ms": None,
                    "message_count": 0,
                },
            }
        for turn_id, count in message_count_rows:
            projection = result.get(str(turn_id))
            if projection is None:
                continue
            activity_stats = projection["activity_stats"]
            if isinstance(activity_stats, dict):
                activity_stats["message_count"] = int(count)
        for turn_id, text, truncated in final_rows:
            if str(turn_id) in result:
                result[str(turn_id)]["final_response_text"] = str(text or "")
                result[str(turn_id)]["final_response_text_truncated"] = bool(truncated)
        for (
            turn_id,
            message_sequence,
            content_block_index,
            item_index,
            carrier_type,
            reasoning_text,
            summary_text,
            signature_present,
            encrypted_length,
        ) in thinking_rows:
            if str(turn_id) in result:
                blocks = result[str(turn_id)]["thinking_blocks"]
                if isinstance(blocks, list):
                    source = {
                        "message_sequence": int(message_sequence),
                        "carrier_type": str(carrier_type),
                        "content_block_index": int(content_block_index),
                        "item_index": int(item_index),
                        "signature_present": bool(signature_present),
                    }
                    if reasoning_text:
                        blocks.append({"kind": "reasoning", "text": str(reasoning_text), **source})
                    elif summary_text:
                        blocks.append({"kind": "summary", "text": str(summary_text), **source})
                    elif encrypted_length is not None:
                        blocks.append({"kind": "encrypted", "text": "", **source})
                if encrypted_length is not None:
                    result[str(turn_id)]["has_encrypted_reasoning"] = True
        for turn_id, call_id, name, status, result_sequence, assistant_sequence, call_index in tool_rows:
            if str(turn_id) in result:
                items = result[str(turn_id)]["tool_items"]
                if isinstance(items, list):
                    items.append(
                        {
                            "sequence": int(assistant_sequence),
                            "call_index": int(call_index),
                            "item_kind": "tool_call",
                            "tool_name": str(name),
                            "tool_call_id": str(call_id),
                            "status": str(status),
                        }
                    )
                    if result_sequence is not None:
                        items.append(
                            {
                                "sequence": int(result_sequence),
                                "assistant_message_sequence": int(assistant_sequence),
                                "call_index": int(call_index),
                                "item_kind": "tool_result",
                                "tool_name": str(name),
                                "tool_call_id": str(call_id),
                                "status": str(status),
                            }
                        )
        return result

    def decode_indexed_message(
        self, value: object, *, summary_only: bool = False
    ) -> BaseMessage:
        del summary_only
        if not isinstance(value, dict):
            raise TypeError("rollout message 必须是对象")
        return messages_from_dict([value])[0]

    def pending_writes(
        self,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str,
        *,
        snapshot: RolloutReadSnapshot | None = None,
    ) -> list[PendingWrite]:
        def read(connection: sqlite3.Connection) -> list[PendingWrite]:
            rows = connection.execute(
                "SELECT task_id, channel, serializer_name, value_blob FROM pending_writes WHERE checkpoint_id = ? AND status = 'pending' ORDER BY task_path, write_index",
                (checkpoint_id,),
            ).fetchall()
            return [
                (
                    str(task_id),
                    str(channel),
                    self.decode_value((str(serializer), bytes(blob))),
                )
                for task_id, channel, serializer, blob in rows
            ]

        if snapshot is not None:
            return read(self._snapshot_connection(snapshot))
        with self._connect(thread_id, checkpoint_ns) as connection:
            return read(connection)

    def copy_pending_writes(
        self,
        *,
        source_thread_id: str,
        source_checkpoint_id: str,
        target_thread_id: str,
        target_checkpoint_id: str,
        checkpoint_ns: str = "",
    ) -> None:
        """将源 checkpoint 的完整 pending_writes 行复制到子 rollout。"""
        source_root = self.root(source_thread_id, checkpoint_ns)
        if not source_root.is_dir():
            raise KeyError(source_thread_id)
        source_lock = _RolloutFileLock(
            source_root.parent / ".rollout.write.lock",
            exclusive=False,
        )
        source_lock.acquire()
        try:
            with self._connect(source_thread_id, checkpoint_ns) as connection:
                rows = connection.execute(
                    "SELECT task_id, task_path, write_index, channel, serializer_name, value_blob, value_length, value_hash, status, created_at FROM pending_writes WHERE checkpoint_id = ? ORDER BY task_path, write_index",
                    (source_checkpoint_id,),
                ).fetchall()
        finally:
            source_lock.release()
        if not rows:
            return
        with self._lock(target_thread_id, checkpoint_ns):
            self.initialize(target_thread_id, checkpoint_ns)
            with self._connect(target_thread_id, checkpoint_ns) as connection:
                checkpoint_exists = connection.execute(
                    "SELECT 1 FROM checkpoints WHERE checkpoint_id = ? AND checkpoint_ns = ?",
                    (target_checkpoint_id, checkpoint_ns),
                ).fetchone()
                if checkpoint_exists is None:
                    raise KeyError(target_checkpoint_id)
                connection.executemany(
                    "INSERT OR REPLACE INTO pending_writes(checkpoint_id, task_id, task_path, write_index, channel, serializer_name, value_blob, value_length, value_hash, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            target_checkpoint_id,
                            str(task_id),
                            str(task_path),
                            int(write_index),
                            str(channel),
                            str(serializer_name),
                            bytes(value_blob),
                            int(value_length),
                            str(value_hash),
                            str(status),
                            str(created_at),
                        )
                        for task_id, task_path, write_index, channel, serializer_name, value_blob, value_length, value_hash, status, created_at in rows
                    ],
                )

    def append_writes(
        self,
        *,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str,
        writes: Iterable[PendingWrite],
        task_id: str,
        task_path: str,
    ) -> None:
        with self._lock(thread_id, checkpoint_ns):
            self.initialize(thread_id, checkpoint_ns)
            with self._connect(thread_id, checkpoint_ns) as connection:
                for index, (channel, value) in enumerate(writes):
                    serializer, blob, length, digest = self._encode(value)
                    connection.execute(
                        "INSERT OR REPLACE INTO pending_writes(checkpoint_id, task_id, task_path, write_index, channel, serializer_name, value_blob, value_length, value_hash, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                        (
                            checkpoint_id,
                            task_id,
                            task_path,
                            index,
                            channel,
                            serializer,
                            blob,
                            length,
                            digest,
                            _now(),
                        ),
                    )

    def append_turn_finalize(
        self,
        *,
        thread_id: str,
        checkpoint_ns: str = "",
        turn_id: str,
        final_message_sequence: int,
        final_message_id: str,
    ) -> None:
        with self._lock(thread_id, checkpoint_ns):
            self.initialize(thread_id, checkpoint_ns)
            with self._connect(thread_id, checkpoint_ns) as connection:
                row = connection.execute(
                    "SELECT message_id FROM messages WHERE message_sequence = ?",
                    (final_message_sequence,),
                ).fetchone()
                if row is None or row[0] != final_message_id:
                    raise RuntimeError("turn_finalize 指向不存在或 ID 不匹配的消息")
                timestamp = _now()
                connection.execute(
                    "UPDATE turns SET final_message_sequence = ?, final_message_id = ?, status = 'completed', updated_at = ? WHERE turn_id = ?",
                    (final_message_sequence, final_message_id, timestamp, turn_id),
                )
                connection.execute(
                    "UPDATE message_projections SET phase = 'final_answer', updated_at = ? WHERE message_sequence = ?",
                    (timestamp, final_message_sequence),
                )
                active_view = connection.execute(
                    "SELECT head_view_id FROM branches WHERE branch_id = (SELECT active_branch_id FROM checkpoint_namespace_state WHERE checkpoint_ns = ?)",
                    (checkpoint_ns,),
                ).fetchone()
                if active_view is not None and active_view[0] is not None:
                    connection.execute(
                        "UPDATE context_view_turns SET final_message_sequence = ? WHERE view_id = ? AND turn_id = ?",
                        (final_message_sequence, str(active_view[0]), turn_id),
                )
                transaction_id = uuid4().hex
                self._insert_control(
                    connection,
                    "checkpoint_finalized",
                    "turn",
                    turn_id,
                    None,
                    None,
                    None,
                    {"final_message_sequence": final_message_sequence},
                    transaction_id,
                    timestamp,
                )

    def copy_turn_finalizations(
        self,
        *,
        source_thread_id: str,
        source_checkpoint_id: str | None,
        target_thread_id: str,
        checkpoint_ns: str = "",
    ) -> int:
        """把源 checkpoint 中已完成 Turn 的 SQLite 指针复制到目标 rollout。

        fork 通过 ``put`` 重新追加消息时，消息正文可以独立复制，但
        ``final_message_id``、``final_message_sequence`` 和 Turn 终态并不属于
        LangGraph checkpoint 的 channel value。如果不显式复制，目标 rollout
        会把这些本来已经完成的历史 Turn 当成 running，前端就会永久显示
        “正在处理”。这里按源 view 的 Turn membership 选择完成指针，再用
        不可变 message_id 在目标 rollout 中定位对应消息；找不到的消息跳过，
        以支持按 Turn 截断的 context fork。
        """
        source_root = self.root(source_thread_id, checkpoint_ns)
        if not source_root.is_dir():
            raise KeyError(source_thread_id)
        source_lock = _RolloutFileLock(
            source_root.parent / ".rollout.write.lock",
            exclusive=False,
        )
        source_lock.acquire()
        try:
            with self._connect(source_thread_id, checkpoint_ns) as source_connection:
                if source_checkpoint_id is not None:
                    source_view_row = source_connection.execute(
                        "SELECT view_id FROM checkpoints WHERE checkpoint_id = ? AND checkpoint_ns = ? AND status = 'active'",
                        (source_checkpoint_id, checkpoint_ns),
                    ).fetchone()
                    if source_view_row is None:
                        raise KeyError(source_checkpoint_id)
                    source_view_id = str(source_view_row[0])
                else:
                    source_view_row = source_connection.execute(
                        "SELECT head_view_id FROM branches WHERE branch_id = (SELECT active_branch_id FROM checkpoint_namespace_state WHERE checkpoint_ns = ?)",
                        (checkpoint_ns,),
                    ).fetchone()
                    source_view_id = (
                        str(source_view_row[0])
                        if source_view_row is not None and source_view_row[0] is not None
                        else None
                    )
                if source_view_id is None:
                    return 0
                source_rows = source_connection.execute(
                    """
                    SELECT DISTINCT t.turn_id, t.final_message_id
                    FROM context_view_turns AS cvt
                    JOIN turns AS t ON t.turn_id = cvt.turn_id
                    WHERE cvt.view_id = ?
                      AND t.status IN ('completed', 'succeeded')
                      AND t.final_message_id IS NOT NULL
                    ORDER BY t.turn_ordinal
                    """,
                    (source_view_id,),
                ).fetchall()
        finally:
            source_lock.release()

        if not source_rows:
            return 0

        with self._lock(target_thread_id, checkpoint_ns):
            self.initialize(target_thread_id, checkpoint_ns)
            with self._connect(target_thread_id, checkpoint_ns) as target_connection:
                timestamp = _now()
                transaction_id = uuid4().hex
                active_branch_row = target_connection.execute(
                    "SELECT active_branch_id FROM checkpoint_namespace_state WHERE checkpoint_ns = ?",
                    (checkpoint_ns,),
                ).fetchone()
                active_branch_id = (
                    str(active_branch_row[0])
                    if active_branch_row is not None and active_branch_row[0] is not None
                    else None
                )
                copied = 0
                for source_turn_id, source_final_message_id in source_rows:
                    target_message = target_connection.execute(
                        "SELECT message_sequence, turn_id FROM messages WHERE message_id = ?",
                        (str(source_final_message_id),),
                    ).fetchone()
                    if target_message is None or str(target_message[1]) != str(source_turn_id):
                        continue
                    target_turn = target_connection.execute(
                        "SELECT turn_id FROM turns WHERE turn_id = ?",
                        (str(source_turn_id),),
                    ).fetchone()
                    if target_turn is None:
                        continue
                    target_sequence = int(target_message[0])
                    target_connection.execute(
                        "UPDATE turns SET final_message_sequence = ?, final_message_id = ?, status = 'completed', updated_at = ? WHERE turn_id = ?",
                        (
                            target_sequence,
                            str(source_final_message_id),
                            timestamp,
                            str(source_turn_id),
                        ),
                    )
                    target_connection.execute(
                        "UPDATE context_view_turns SET final_message_sequence = ? WHERE turn_id = ?",
                        (target_sequence, str(source_turn_id)),
                    )
                    target_connection.execute(
                        "UPDATE message_projections SET phase = 'final_answer', updated_at = ? WHERE message_sequence = ?",
                        (timestamp, target_sequence),
                    )
                    self._insert_control(
                        target_connection,
                        "checkpoint_finalized",
                        "turn",
                        str(source_turn_id),
                        active_branch_id,
                        None,
                        None,
                        {
                            "final_message_sequence": target_sequence,
                            "copied_from_session_id": source_thread_id,
                            "copied_from_message_id": str(source_final_message_id),
                        },
                        transaction_id,
                        timestamp,
                    )
                    copied += 1
                if copied:
                    last_control = target_connection.execute(
                        "SELECT control_sequence FROM control_events WHERE transaction_id = ? ORDER BY control_sequence DESC LIMIT 1",
                        (transaction_id,),
                    ).fetchone()
                    target_connection.execute(
                        "UPDATE database_meta SET last_control_sequence = ?, updated_at = ? WHERE singleton_id = 1",
                        (
                            int(last_control[0]) if last_control is not None else None,
                            timestamp,
                        ),
                    )
                target_connection.commit()
                return copied

    def cancel_unfinished_turns(
        self,
        *,
        thread_id: str,
        checkpoint_ns: str = "",
    ) -> int:
        """取消 fork 后不会由子会话继续执行的未完成 Turn。

        fork 只复制 checkpoint 内容，不复制源会话的运行时 Job。若目标 rollout
        仍保留 running/streaming 等状态，前端会在没有任何活动 Job 的情况下永久
        显示“正在处理”。没有最终消息指针的 Turn 在子会话中只能明确标记为取消，
        等用户需要时再通过重试创建新的 Job。
        """
        active_statuses = (
            "accepted",
            "queued",
            "running",
            "streaming",
            "waiting_input",
            "paused",
            "interrupt_pending",
            "cancelling",
        )
        with self._lock(thread_id, checkpoint_ns):
            self.initialize(thread_id, checkpoint_ns)
            with self._connect(thread_id, checkpoint_ns) as connection:
                placeholders = ", ".join("?" for _ in active_statuses)
                rows = connection.execute(
                    f"SELECT turn_id FROM turns WHERE status IN ({placeholders}) "
                    "AND final_message_sequence IS NULL ORDER BY turn_ordinal",
                    active_statuses,
                ).fetchall()
                if not rows:
                    return 0
                timestamp = _now()
                transaction_id = uuid4().hex
                for row in rows:
                    turn_id = str(row[0])
                    connection.execute(
                        "UPDATE turns SET status = 'cancelled', updated_at = ? WHERE turn_id = ?",
                        (timestamp, turn_id),
                    )
                    self._insert_control(
                        connection,
                        "turn_status",
                        "turn",
                        turn_id,
                        None,
                        None,
                        None,
                        {"status": "cancelled", "reason": "fork_runtime_not_copied"},
                        transaction_id,
                        timestamp,
                    )
                last_control = connection.execute(
                    "SELECT control_sequence FROM control_events WHERE transaction_id = ? "
                    "ORDER BY control_sequence DESC LIMIT 1",
                    (transaction_id,),
                ).fetchone()
                connection.execute(
                    "UPDATE database_meta SET last_control_sequence = ?, updated_at = ? "
                    "WHERE singleton_id = 1",
                    (int(last_control[0]), timestamp),
                )
                return len(rows)

    def mark_turn_terminal_status(
        self,
        *,
        thread_id: str,
        turn_id: str,
        status: str,
        checkpoint_ns: str = "",
    ) -> bool:
        """把失败或取消等终态写入 SQLite，供历史恢复继续显示可重试状态。

        不是所有 Job 都对应聊天 Turn（例如后台任务），因此找不到 Turn 时
        返回 ``False``。找到后状态和控制事件在同一事务中提交，避免页面切换
        后只能从短生命周期的 JobService 内存状态恢复失败信息。
        """
        if status not in {"failed", "cancelled", "timed_out"}:
            raise ValueError(f"非法的 Turn 终态: {status}")
        if not self.index_path(thread_id, checkpoint_ns).is_file():
            # 后台 Job 也会进入统一终态事件流，但它们未必拥有聊天 Turn。
            return False
        with self._lock(thread_id, checkpoint_ns):
            self.initialize(thread_id, checkpoint_ns)
            with self._connect(thread_id, checkpoint_ns) as connection:
                row = connection.execute(
                    "SELECT status FROM turns WHERE turn_id = ?",
                    (turn_id,),
                ).fetchone()
                if row is None:
                    return False
                timestamp = _now()
                connection.execute(
                    "UPDATE turns SET status = ?, updated_at = ? WHERE turn_id = ?",
                    (status, timestamp, turn_id),
                )
                transaction_id = uuid4().hex
                self._insert_control(
                    connection,
                    "turn_status",
                    "turn",
                    turn_id,
                    None,
                    None,
                    None,
                    {"status": status},
                    transaction_id,
                    timestamp,
                )
                return True

    def final_message_sequence(
        self,
        *,
        thread_id: str,
        checkpoint_ns: str,
        turn_id: str,
        final_message_id: str,
    ) -> int:
        """解析已提交 final message 的物理序号，供受控 writer 使用。"""
        self.initialize(thread_id, checkpoint_ns)
        with self._connect(thread_id, checkpoint_ns) as connection:
            row = connection.execute(
                "SELECT message_sequence FROM messages WHERE turn_id = ? AND message_id = ? ORDER BY message_sequence DESC LIMIT 1",
                (turn_id, final_message_id),
            ).fetchone()
        if row is None:
            raise RuntimeError(
                "最终 assistant 消息未出现在 rollout projection 中: "
                f"session_id={thread_id}, turn_id={turn_id}, message_id={final_message_id}"
            )
        return int(row[0])

    def create_context_boundary(
        self,
        *,
        thread_id: str,
        checkpoint_ns: str,
        boundary: str,
        source_checkpoint_id: str | None,
        source_anchor: str | None,
        source_view_id: str | None = None,
        source_turn_id: str | None = None,
        base_message_sequence: int | None = None,
        anchor_mode: str = "inclusive",
    ) -> RolloutManifest:
        if anchor_mode not in {"inclusive", "before"}:
            raise ValueError("context anchor_mode 必须是 inclusive 或 before")
        if boundary not in {"rewind", "compaction"}:
            raise ValueError("context boundary 必须是 rewind 或 compaction")
        with self._lock(thread_id, checkpoint_ns):
            self.initialize(thread_id, checkpoint_ns)
            with self._connect(thread_id, checkpoint_ns) as connection:
                if source_turn_id is not None:
                    resolved = self._resolve_turn_anchor_connection(
                        connection,
                        source_turn_id,
                        checkpoint_ns=checkpoint_ns,
                        anchor_mode=anchor_mode,
                    )
                    source_checkpoint_id = resolved.checkpoint_id
                    source_view_id = resolved.view_id
                    base_message_sequence = resolved.cutoff_message_sequence
                    source_anchor = None
                source = (
                    self._checkpoint_row(
                        connection,
                        checkpoint_ns,
                        source_checkpoint_id,
                    )
                    if source_checkpoint_id
                    else self._checkpoint_row(connection, checkpoint_ns, None)
                )
                if source is None:
                    return self._manifest_from_connection(connection, checkpoint_ns)
                source_view_id = source_view_id or str(source[6])
                source_sequences = self._view_message_sequences(
                    thread_id,
                    checkpoint_ns,
                    source_view_id,
                    set(),
                    connection=connection,
                )
                cutoff = len(source_sequences)
                if base_message_sequence is not None:
                    matching = [
                        index
                        for index, sequence in enumerate(source_sequences)
                        if sequence <= base_message_sequence
                    ]
                    cutoff = matching[-1] + 1 if matching else 0
                if source_anchor is not None:
                    anchor_row = connection.execute(
                        "SELECT message_sequence FROM messages WHERE message_id = ?",
                        (source_anchor,),
                    ).fetchone()
                    if anchor_row is None:
                        raise KeyError(f"rewind anchor 不存在: {source_anchor}")
                    try:
                        anchor_index = source_sequences.index(int(anchor_row[0]))
                    except ValueError as error:
                        raise KeyError(
                            f"rewind anchor 不属于 source view: {source_anchor}"
                        ) from error
                    cutoff = anchor_index + (1 if anchor_mode == "inclusive" else 0)
                visible_sequences = source_sequences[:cutoff]
                branch_id = "branch-" + uuid4().hex[:12]
                timestamp = _now()
                old_active_branch, _projection_epoch = self._namespace_state(
                    connection, checkpoint_ns
                )
                view_id = self._create_view(
                    connection,
                    branch_id,
                    source_view_id,
                    visible_sequences,
                    timestamp,
                    view_kind=boundary,
                )
                connection.execute(
                    "INSERT INTO branches(branch_id, branch_kind, status, head_view_id, head_checkpoint_id, parent_branch_id, created_at, updated_at) VALUES (?, ?, 'active', ?, ?, ?, ?, ?)",
                    (
                        branch_id,
                        boundary,
                        view_id,
                        str(source[0]),
                        old_active_branch,
                        timestamp,
                        timestamp,
                    ),
                )
                connection.execute(
                    "UPDATE branches SET status = 'inactive', updated_at = ? WHERE branch_id = ?",
                    (timestamp, old_active_branch),
                )
                connection.execute(
                    "UPDATE checkpoint_namespace_state SET active_branch_id = ?, projection_epoch = projection_epoch + 1, updated_at = ? WHERE checkpoint_ns = ?",
                    (branch_id, timestamp, checkpoint_ns),
                )
                control_sequence = self._insert_control(
                    connection,
                    boundary,
                    "branch",
                    branch_id,
                    branch_id,
                    view_id,
                    str(source[0]),
                    {
                        "source_checkpoint_id": source_checkpoint_id,
                        "source_anchor": source_anchor,
                        "source_turn_id": source_turn_id,
                        "source_view_id": source_view_id,
                        "anchor_mode": anchor_mode,
                        "cutoff_message_sequence": (
                            visible_sequences[-1] if visible_sequences else None
                        ),
                    },
                    uuid4().hex,
                    timestamp,
                )
                connection.execute(
                    "UPDATE database_meta SET last_control_sequence = ?, updated_at = ? WHERE singleton_id = 1",
                    (control_sequence, timestamp),
                )
                connection.execute(
                    "UPDATE context_views SET control_sequence = ? WHERE view_id = ?",
                    (control_sequence, view_id),
                )
                connection.commit()
                return self._manifest_from_connection(connection, checkpoint_ns)

    def rewind_to_checkpoint(
        self,
        *,
        thread_id: str,
        checkpoint_ns: str = "",
        checkpoint_id: str,
        source_anchor: str | None = None,
        anchor_mode: str = "inclusive",
    ) -> RolloutManifest:
        return self.create_context_boundary(
            thread_id=thread_id,
            checkpoint_ns=checkpoint_ns,
            boundary="rewind",
            source_checkpoint_id=checkpoint_id,
            source_anchor=source_anchor,
            anchor_mode=anchor_mode,
        )

    def rewind_to_turn(
        self,
        *,
        thread_id: str,
        checkpoint_ns: str = "",
        turn_id: str,
        anchor_mode: str = "inclusive",
    ) -> RolloutManifest:
        """只接受用户 Turn 锚点，内部解析实际 source view/checkpoint。"""
        return self.create_context_boundary(
            thread_id=thread_id,
            checkpoint_ns=checkpoint_ns,
            boundary="rewind",
            source_checkpoint_id=None,
            source_anchor=None,
            source_turn_id=turn_id,
            anchor_mode=anchor_mode,
        )

    def rollout_id(self, thread_id: str, checkpoint_ns: str = "") -> str:
        return self.initialize(thread_id, checkpoint_ns).rollout_id

    def delete_thread(self, thread_id: str) -> None:
        rollout = self.root(thread_id)
        self._active_fork_materializations.discard((thread_id, ""))
        if not rollout.exists():
            return
        self.release_fork_retentions(thread_id)
        with self._lock(thread_id, ""):
            if rollout.exists():
                shutil.rmtree(rollout)

    def begin_fork_materialization(
        self,
        *,
        target_session_id: str,
        source_session_id: str,
        source_checkpoint_id: str | None,
        source_view_id: str | None,
        fork_mode: str,
        relationship: str,
        checkpoint_ns: str = "",
    ) -> tuple[str, str]:
        """为目标 rollout 建立一次可恢复的 fork 物化 journal。"""
        self.initialize(target_session_id, checkpoint_ns)
        materialization_id = uuid4().hex
        fork_id = uuid4().hex
        with self._lock(target_session_id, checkpoint_ns), self._connect(
            target_session_id, checkpoint_ns
        ) as connection:
                active = connection.execute(
                    "SELECT materialization_id FROM fork_materializations WHERE status IN ('prepared', 'target_committed') LIMIT 1"
                ).fetchone()
                if active is not None:
                    raise RuntimeError(
                        "目标 rollout 已存在未完成的 fork 物化: " f"{active[0]}"
                    )
                rollback_offset = int(
                    connection.execute(
                        "SELECT committed_jsonl_offset FROM database_meta WHERE singleton_id = 1"
                    ).fetchone()[0]
                )
                connection.execute(
                    "INSERT INTO fork_materializations(materialization_id, fork_id, target_session_id, source_session_id, source_checkpoint_id, source_view_id, fork_mode, relationship, status, rollback_jsonl_offset, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?)",
                    (
                        materialization_id,
                        fork_id,
                        target_session_id,
                        source_session_id,
                        source_checkpoint_id,
                        source_view_id,
                        fork_mode,
                        relationship,
                        rollback_offset,
                        _now(),
                    ),
                )
                self._active_fork_materializations.add(
                    (target_session_id, checkpoint_ns)
                )
        return materialization_id, fork_id

    def abort_fork_materialization(
        self,
        materialization_id: str,
        *,
        target_session_id: str,
        checkpoint_ns: str = "",
    ) -> None:
        """显式回滚尚未提交的 fork；崩溃时由 initialize 执行同一恢复路径。"""
        with self._lock(target_session_id, checkpoint_ns), self._connect(
            target_session_id, checkpoint_ns
        ) as connection:
                row = connection.execute(
                    "SELECT status FROM fork_materializations WHERE materialization_id = ?",
                    (materialization_id,),
                ).fetchone()
                if row is None or str(row[0]) == "committed":
                    return
                self._recover_fork_materialization(
                    target_session_id,
                    checkpoint_ns,
                    connection,
                    self.jsonl_path(target_session_id, checkpoint_ns),
                )
        self._active_fork_materializations.discard(
            (target_session_id, checkpoint_ns)
        )

    def commit_fork_materialization(
        self,
        materialization_id: str,
        *,
        target_session_id: str,
        source_session_id: str,
        source_checkpoint_id: str | None,
        source_view_id: str | None,
        fork_mode: str,
        relationship: str,
        checkpoint_ns: str = "",
    ) -> str:
        """在目标库收敛 fork，并幂等完成父库 retention。

        消息/Checkpoint 的物化可以包含多个普通 append commit，但这些 commit
        都被 journal 保护。真正对外可见的边界在本方法的目标事务中：Turn
        finalization、运行态终止、唯一 provenance 和 ``target_committed`` 一起
        提交；父库 retention 完成后才进入 ``committed``。
        """
        if source_view_id is None:
            source_view_id = self._fork_source_view_id(
                source_session_id,
                source_checkpoint_id,
                checkpoint_ns,
            )
        source_finalizations = self._fork_completed_turn_finalizations(
            source_session_id,
            source_checkpoint_id,
            checkpoint_ns,
        )
        with self._lock(target_session_id, checkpoint_ns), self._connect(
            target_session_id, checkpoint_ns
        ) as connection:
                journal = connection.execute(
                    "SELECT fork_id, status, relationship FROM fork_materializations WHERE materialization_id = ? AND target_session_id = ?",
                    (materialization_id, target_session_id),
                ).fetchone()
                if journal is None:
                    raise KeyError(f"fork materialization 不存在: {materialization_id}")
                fork_id = str(journal[0])
                status = str(journal[1])
                if status == "committed":
                    self._active_fork_materializations.discard(
                        (target_session_id, checkpoint_ns)
                    )
                    return fork_id
                if status == "target_committed":
                    pass
                elif status != "prepared":
                    raise RuntimeError(
                        f"fork materialization 状态不可提交: {materialization_id}={status}"
                    )
                else:
                    connection.execute("BEGIN IMMEDIATE")
                    timestamp = _now()
                    transaction_id = f"fork:{materialization_id}"
                    active_branch_row = connection.execute(
                        "SELECT active_branch_id FROM checkpoint_namespace_state WHERE checkpoint_ns = ?",
                        (checkpoint_ns,),
                    ).fetchone()
                    active_branch_id = (
                        str(active_branch_row[0])
                        if active_branch_row is not None
                        else None
                    )
                    for turn_id, final_message_id in source_finalizations:
                        target_message = connection.execute(
                            "SELECT message_sequence, turn_id FROM messages WHERE message_id = ?",
                            (final_message_id,),
                        ).fetchone()
                        if target_message is None or str(target_message[1]) != turn_id:
                            continue
                        target_turn = connection.execute(
                            "SELECT 1 FROM turns WHERE turn_id = ?",
                            (turn_id,),
                        ).fetchone()
                        if target_turn is None:
                            continue
                        target_sequence = int(target_message[0])
                        connection.execute(
                            "UPDATE turns SET final_message_sequence = ?, final_message_id = ?, status = 'completed', updated_at = ? WHERE turn_id = ?",
                            (target_sequence, final_message_id, timestamp, turn_id),
                        )
                        connection.execute(
                            "UPDATE context_view_turns SET final_message_sequence = ? WHERE turn_id = ?",
                            (target_sequence, turn_id),
                        )
                        connection.execute(
                            "UPDATE message_projections SET phase = 'final_answer', updated_at = ? WHERE message_sequence = ?",
                            (timestamp, target_sequence),
                        )
                        self._insert_control(
                            connection,
                            "checkpoint_finalized",
                            "turn",
                            turn_id,
                            active_branch_id,
                            None,
                            None,
                            {
                                "final_message_sequence": target_sequence,
                                "copied_from_session_id": source_session_id,
                                "copied_from_message_id": final_message_id,
                            },
                            transaction_id,
                            timestamp,
                        )

                    active_statuses = (
                        "accepted",
                        "queued",
                        "running",
                        "streaming",
                        "waiting_input",
                        "paused",
                        "interrupt_pending",
                        "cancelling",
                    )
                    placeholders = ", ".join("?" for _ in active_statuses)
                    unfinished = connection.execute(
                        f"SELECT turn_id FROM turns WHERE status IN ({placeholders}) AND final_message_sequence IS NULL ORDER BY turn_ordinal",
                        active_statuses,
                    ).fetchall()
                    for unfinished_row in unfinished:
                        turn_id = str(unfinished_row[0])
                        connection.execute(
                            "UPDATE turns SET status = 'cancelled', updated_at = ? WHERE turn_id = ?",
                            (timestamp, turn_id),
                        )
                        self._insert_control(
                            connection,
                            "turn_status",
                            "turn",
                            turn_id,
                            active_branch_id,
                            None,
                            None,
                            {
                                "status": "cancelled",
                                "reason": "fork_runtime_not_copied",
                            },
                            transaction_id,
                            timestamp,
                        )

                    connection.execute(
                        "INSERT INTO fork_origins(fork_id, child_session_id, source_session_id, source_checkpoint_id, source_view_id, fork_mode, relationship, copied_message_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, (SELECT COUNT(*) FROM messages), ?)",
                        (
                            fork_id,
                            target_session_id,
                            source_session_id,
                            source_checkpoint_id,
                            source_view_id,
                            fork_mode,
                            relationship,
                            timestamp,
                        ),
                    )
                    control_sequence = self._insert_control(
                        connection,
                        "fork_created",
                        "fork",
                        fork_id,
                        active_branch_id,
                        None,
                        source_checkpoint_id,
                        {
                            "source_session_id": source_session_id,
                            "source_view_id": source_view_id,
                            "fork_mode": fork_mode,
                            "relationship": relationship,
                        },
                        transaction_id,
                        timestamp,
                    )
                    connection.execute(
                        "UPDATE database_meta SET last_control_sequence = ?, updated_at = ? WHERE singleton_id = 1",
                        (control_sequence, timestamp),
                    )
                    connection.execute(
                        "UPDATE fork_materializations SET status = 'target_committed', copied_message_count = (SELECT COUNT(*) FROM messages), target_committed_at = ?, error_message = NULL WHERE materialization_id = ?",
                        (timestamp, materialization_id),
                    )
                    connection.commit()

        if relationship == "pinned":
            self._retain_fork_source(
                source_session_id=source_session_id,
                source_checkpoint_id=source_checkpoint_id,
                source_view_id=source_view_id,
                fork_id=fork_id,
                owner_session_id=target_session_id,
                checkpoint_ns=checkpoint_ns,
            )
        with self._lock(target_session_id, checkpoint_ns), self._connect(
            target_session_id, checkpoint_ns
        ) as connection:
                connection.execute(
                    "UPDATE fork_materializations SET status = 'committed', committed_at = ?, error_message = NULL WHERE materialization_id = ? AND status = 'target_committed'",
                    (_now(), materialization_id),
                )
        self._active_fork_materializations.discard(
            (target_session_id, checkpoint_ns)
        )
        return fork_id

    def _fork_completed_turn_finalizations(
        self,
        source_thread_id: str,
        source_checkpoint_id: str | None,
        checkpoint_ns: str,
    ) -> list[tuple[str, str]]:
        source_root = self.root(source_thread_id, checkpoint_ns)
        if not source_root.is_dir():
            return []
        source_lock = _RolloutFileLock(
            source_root.parent / ".rollout.write.lock",
            exclusive=False,
        )
        source_lock.acquire()
        try:
            with self._connect(source_thread_id, checkpoint_ns) as connection:
                if source_checkpoint_id is not None:
                    source_view_row = connection.execute(
                        "SELECT view_id FROM checkpoints WHERE checkpoint_id = ? AND checkpoint_ns = ? AND status = 'active'",
                        (source_checkpoint_id, checkpoint_ns),
                    ).fetchone()
                    if source_view_row is None:
                        raise KeyError(source_checkpoint_id)
                    source_view_id = str(source_view_row[0])
                else:
                    source_view_row = connection.execute(
                        "SELECT head_view_id FROM branches WHERE branch_id = (SELECT active_branch_id FROM checkpoint_namespace_state WHERE checkpoint_ns = ?)",
                        (checkpoint_ns,),
                    ).fetchone()
                    source_view_id = (
                        str(source_view_row[0])
                        if source_view_row is not None and source_view_row[0] is not None
                        else None
                    )
                if source_view_id is None:
                    return []
                rows = connection.execute(
                    """
                    SELECT DISTINCT t.turn_id, t.final_message_id
                    FROM context_view_turns AS cvt
                    JOIN turns AS t ON t.turn_id = cvt.turn_id
                    WHERE cvt.view_id = ?
                      AND t.status IN ('completed', 'succeeded')
                      AND t.final_message_id IS NOT NULL
                    ORDER BY t.turn_ordinal
                    """,
                    (source_view_id,),
                ).fetchall()
                return [(str(turn_id), str(message_id)) for turn_id, message_id in rows]
        finally:
            source_lock.release()

    def clone_rollout(
        self,
        *,
        source_thread_id: str,
        target_thread_id: str,
        checkpoint_ns: str = "",
        source_checkpoint_id: str | None,
    ) -> str | None:
        source = self.root(source_thread_id, checkpoint_ns)
        target = self.root(target_thread_id, checkpoint_ns)
        if not source.is_dir():
            raise KeyError(source_thread_id)
        source_lock = _RolloutFileLock(
            source.parent / ".rollout.write.lock",
            exclusive=False,
        )
        target_lock = self._lock(target_thread_id, checkpoint_ns)
        source_lock.acquire()
        source_view_id: str | None = None
        try:
            with target_lock:
                if target.exists():
                    raise FileExistsError(target)
                shutil.copytree(
                    source,
                    target,
                    ignore=shutil.ignore_patterns(".rollout.write.lock"),
                )
                with self._connect(target_thread_id, checkpoint_ns) as connection:
                    target_rollout_id = self._rollout_id(target_thread_id)
                    connection.execute(
                        "UPDATE database_meta SET rollout_id = ?, session_id = ?, updated_at = ? WHERE singleton_id = 1",
                        (target_rollout_id, target_thread_id, _now()),
                    )
                    # fork_origins 和 retention_refs 属于原 rollout 的会话关系，
                    # 其中的 child/source/owner ID 不能随完整副本带入新会话。
                    # 新 fork 的唯一来源记录由统一 materialization writer 写入。
                    connection.execute("DELETE FROM fork_origins")
                    connection.execute("DELETE FROM retention_refs")
                    # fork journal 只描述这次目标物化，不能把父会话过去的
                    # prepared/committed 记录复制成子会话的新操作历史。
                    connection.execute("DELETE FROM fork_materializations")
                    source_view = (
                        connection.execute(
                            "SELECT view_id FROM checkpoints WHERE checkpoint_id = ? AND checkpoint_ns = ?",
                            (source_checkpoint_id, checkpoint_ns),
                        ).fetchone()
                        if source_checkpoint_id
                        else connection.execute(
                            "SELECT head_view_id FROM branches WHERE branch_id = (SELECT active_branch_id FROM checkpoint_namespace_state WHERE checkpoint_ns = ?)",
                            (checkpoint_ns,),
                        ).fetchone()
                    )
                    source_view_id = (
                        str(source_view[0])
                        if source_view is not None and source_view[0] is not None
                        else None
                    )
        finally:
            source_lock.release()
        return source_view_id

    def record_fork_origin(
        self,
        *,
        target_thread_id: str,
        source_session_id: str,
        source_checkpoint_id: str | None,
        source_view_id: str | None,
        fork_mode: str,
        relationship: str = "detached",
        checkpoint_ns: str = "",
    ) -> str:
        if source_view_id is None:
            source_view_id = self._fork_source_view_id(
                source_session_id,
                source_checkpoint_id,
                checkpoint_ns,
            )
        fork_id = uuid4().hex
        with self._lock(target_thread_id, checkpoint_ns):
            self.initialize(target_thread_id, checkpoint_ns)
            with self._connect(target_thread_id, checkpoint_ns) as connection:
                connection.execute(
                    "INSERT INTO fork_origins(fork_id, child_session_id, source_session_id, source_checkpoint_id, source_view_id, fork_mode, relationship, copied_message_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, (SELECT COUNT(*) FROM messages), ?)",
                    (
                        fork_id,
                        target_thread_id,
                        source_session_id,
                        source_checkpoint_id,
                        source_view_id,
                        fork_mode,
                        relationship,
                        _now(),
                    ),
                )
        if relationship == "pinned":
            self._retain_fork_source(
                source_session_id=source_session_id,
                source_checkpoint_id=source_checkpoint_id,
                source_view_id=source_view_id,
                fork_id=fork_id,
                owner_session_id=target_thread_id,
                checkpoint_ns=checkpoint_ns,
            )
        return fork_id

    def _fork_source_view_id(
        self,
        source_session_id: str,
        source_checkpoint_id: str | None,
        checkpoint_ns: str,
    ) -> str | None:
        source_root = self.root(source_session_id, checkpoint_ns)
        if not source_root.is_dir():
            return None
        source_lock = _RolloutFileLock(
            source_root.parent / ".rollout.write.lock",
            exclusive=False,
        )
        source_lock.acquire()
        try:
            with self._connect(source_session_id, checkpoint_ns) as connection:
                row = (
                    connection.execute(
                        "SELECT view_id FROM checkpoints WHERE checkpoint_id = ? AND checkpoint_ns = ? AND status = 'active'",
                        (source_checkpoint_id, checkpoint_ns),
                    ).fetchone()
                    if source_checkpoint_id
                    else connection.execute(
                        "SELECT head_view_id FROM branches WHERE branch_id = (SELECT active_branch_id FROM checkpoint_namespace_state WHERE checkpoint_ns = ?)",
                        (checkpoint_ns,),
                    ).fetchone()
                )
                return str(row[0]) if row is not None and row[0] is not None else None
        finally:
            source_lock.release()

    def _retain_fork_source(
        self,
        *,
        source_session_id: str,
        source_checkpoint_id: str | None,
        source_view_id: str | None,
        fork_id: str,
        owner_session_id: str,
        checkpoint_ns: str,
    ) -> None:
        if source_view_id is None and source_checkpoint_id is None:
            return
        with self._lock(source_session_id, checkpoint_ns):
            self.initialize(source_session_id, checkpoint_ns)
            with self._connect(source_session_id, checkpoint_ns) as connection:
                existing = connection.execute(
                    "SELECT 1 FROM retention_refs WHERE reference_kind = 'fork' AND reference_id = ? AND owner_session_id = ? AND status = 'active' LIMIT 1",
                    (fork_id, owner_session_id),
                ).fetchone()
                if existing is not None:
                    return
                now = _now()
                connection.execute(
                    "INSERT INTO retention_refs(retention_id, reference_kind, reference_id, target_view_id, target_message_sequence, owner_session_id, expires_at, status, created_at) VALUES (?, 'fork', ?, ?, NULL, ?, NULL, 'active', ?)",
                    (
                        uuid4().hex,
                        fork_id,
                        source_view_id,
                        owner_session_id,
                        now,
                    ),
                )

    def release_fork_retentions(self, child_session_id: str) -> None:
        child_root = self.root(child_session_id)
        if (
            not child_root.is_dir()
            or self._is_removed_rollout_layout(child_root)
            or not self.index_path(child_session_id).is_file()
        ):
            # 删除旧 rollout 时没有可读取的 pinned provenance；整个会话目录
            # 会由 SessionService 随后删除，不能在此处初始化旧布局。
            return
        child_lock = _RolloutFileLock(
            child_root.parent / ".rollout.write.lock",
            exclusive=False,
        )
        child_lock.acquire()
        try:
            with self._connect(child_session_id) as connection:
                origins = connection.execute(
                    "SELECT fork_id, source_session_id FROM fork_origins WHERE child_session_id = ? AND relationship = 'pinned'",
                    (child_session_id,),
                ).fetchall()
        finally:
            child_lock.release()
        for fork_id, source_session_id in origins:
            source_root = self.root(str(source_session_id))
            if (
                not source_root.is_dir()
                or self._is_removed_rollout_layout(source_root)
                or not self.index_path(str(source_session_id)).is_file()
            ):
                continue
            with self._lock(str(source_session_id), ""), self._connect(
                str(source_session_id)
            ) as connection:
                connection.execute(
                    "UPDATE retention_refs SET status = 'released' WHERE reference_kind = 'fork' AND reference_id = ? AND owner_session_id = ? AND status = 'active'",
                    (str(fork_id), child_session_id),
                )

    def pinned_fork_children(
        self, source_thread_id: str, checkpoint_ns: str = ""
    ) -> tuple[str, ...]:
        root = self.root(source_thread_id, checkpoint_ns)
        if self._is_removed_rollout_layout(root):
            # 旧 rollout 没有新 schema 的 retention_refs，删除整棵会话目录时
            # 不应触发 initialize()，否则用户无法清理原型阶段遗留的会话。
            return ()
        self.initialize(source_thread_id, checkpoint_ns)
        with self._connect(source_thread_id, checkpoint_ns) as connection:
            rows = connection.execute(
                "SELECT DISTINCT owner_session_id FROM retention_refs WHERE reference_kind = 'fork' AND status = 'active' AND owner_session_id IS NOT NULL ORDER BY owner_session_id"
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def list_thread_ids(self) -> tuple[str, ...]:
        return tuple(
            node.node_id
            for node in self._path_resolver.list_nodes()
            if node.kind == "session"
        )

    def plan_pruning(
        self,
        thread_id: str,
        checkpoint_ns: str = "",
        *,
        retain_checkpoint_ids: Iterable[str] = (),
        audit_before_sequence: int | None = None,
    ) -> RolloutPruningPlan:
        self.initialize(thread_id, checkpoint_ns)
        retained = tuple(dict.fromkeys(retain_checkpoint_ids))
        with self._connect(thread_id, checkpoint_ns) as connection:
            meta = connection.execute(
                "SELECT active_branch_id, projection_epoch, last_message_sequence FROM database_meta WHERE singleton_id = 1"
            ).fetchone()
            if meta is None:
                raise RuntimeError("rollout database_meta 缺失")
            active_branch_id, _projection_epoch = self._namespace_state(
                connection, checkpoint_ns
            )
            query = """
                SELECT c.checkpoint_id, c.view_id, c.commit_id
                FROM checkpoints c
                WHERE c.checkpoint_ns = ? AND c.status = 'active'
                  AND c.checkpoint_id NOT IN (
                      SELECT head_checkpoint_id FROM branches
                      WHERE branch_id = ? AND status = 'active' AND head_checkpoint_id IS NOT NULL
                  )
            """
            params: list[object] = [checkpoint_ns, active_branch_id]
            if retained:
                query += (
                    " AND c.checkpoint_id NOT IN ("
                    + ",".join("?" for _ in retained)
                    + ")"
                )
                params.extend(retained)
            if audit_before_sequence is not None:
                query += " AND c.commit_id < ?"
                params.append(audit_before_sequence)
            query += " ORDER BY c.commit_id"
            rows = connection.execute(query, tuple(params)).fetchall()
            candidates: list[RolloutPruningCandidate] = []
            for checkpoint_id, view_id, _commit_id in rows:
                protected = connection.execute(
                    """
                    SELECT 1 FROM retention_refs
                    WHERE status = 'active' AND (target_view_id = ? OR reference_id = ?)
                    UNION ALL
                    SELECT 1 FROM fork_origins
                    WHERE relationship = 'pinned' AND (source_view_id = ? OR source_checkpoint_id = ?)
                    LIMIT 1
                    """,
                    (view_id, checkpoint_id, view_id, checkpoint_id),
                ).fetchone()
                if protected is not None:
                    continue
                candidates.append(
                    RolloutPruningCandidate(
                        checkpoint_id=str(checkpoint_id),
                        view_id=str(view_id),
                        reason="unreferenced_checkpoint",
                    )
                )
        return RolloutPruningPlan(
            self.rollout_id(thread_id, checkpoint_ns),
            self.initialize(thread_id, checkpoint_ns).committed_sequence,
            tuple(candidates),
        )

    def execute_pruning(
        self, thread_id: str, plan: RolloutPruningPlan, checkpoint_ns: str = ""
    ) -> tuple[str, ...]:
        current = self.initialize(thread_id, checkpoint_ns)
        if (
            current.rollout_id != plan.rollout_id
            or current.committed_sequence != plan.committed_sequence
        ):
            raise RuntimeError("pruning plan 不属于当前 rollout 水位")
        if not plan.candidates:
            return ()
        with self._lock(thread_id, checkpoint_ns), self._connect(
            thread_id, checkpoint_ns
        ) as connection:
                transaction_id = uuid4().hex
                timestamp = _now()
                connection.execute("BEGIN IMMEDIATE")
                for candidate in plan.candidates:
                    row = connection.execute(
                        "SELECT status, view_id FROM checkpoints WHERE checkpoint_id = ? AND checkpoint_ns = ?",
                        (candidate.checkpoint_id, checkpoint_ns),
                    ).fetchone()
                    if row is None:
                        raise RuntimeError(
                            f"pruning checkpoint 不存在: {candidate.checkpoint_id}"
                        )
                    if row[0] != "active" or row[1] != candidate.view_id:
                        raise RuntimeError(
                            f"pruning checkpoint 状态或 view 已变化: {candidate.checkpoint_id}"
                        )
                    connection.execute(
                        "UPDATE checkpoints SET status = 'pruned' WHERE checkpoint_id = ? AND checkpoint_ns = ?",
                        (candidate.checkpoint_id, checkpoint_ns),
                    )
                    self._insert_control(
                        connection,
                        "prune_marked",
                        "checkpoint",
                        candidate.checkpoint_id,
                        None,
                        candidate.view_id,
                        candidate.checkpoint_id,
                        {"reason": candidate.reason, "physical_jsonl": False},
                        transaction_id,
                        timestamp,
                    )
                connection.execute(
                    "UPDATE checkpoint_namespace_state SET projection_epoch = projection_epoch + 1, updated_at = ? WHERE checkpoint_ns = ?",
                    (timestamp, checkpoint_ns),
                )
                connection.commit()
        return tuple(candidate.checkpoint_id for candidate in plan.candidates)

    def compact_jsonl_offline(
        self,
        thread_id: str,
        checkpoint_ns: str = "",
    ) -> RolloutCompactionResult:
        """显式离线回收不再被任何 active checkpoint 引用的 JSONL 消息。

        这是唯一允许改变 JSONL 物理布局的操作。它不改变 message_sequence、
        message_id 或任何有效 view，只更新 SQLite 中的 offset；普通读取、
        pruning、rewind 和 fork 都不会调用此方法。
        """
        with self._lock(thread_id, checkpoint_ns):
            self.initialize(thread_id, checkpoint_ns)
            root = self.root(thread_id, checkpoint_ns)
            jsonl_path = self.jsonl_path(thread_id, checkpoint_ns)
            bytes_before = jsonl_path.stat().st_size
            with self._connect(thread_id, checkpoint_ns) as connection:
                meta = connection.execute(
                    "SELECT database_state, last_message_sequence FROM database_meta WHERE singleton_id = 1"
                ).fetchone()
                if meta is None or meta[0] != "active":
                    raise RuntimeError("离线 compaction 要求 rollout 处于 active 状态")
                active_views = [
                    str(row[0])
                    for row in connection.execute(
                        "SELECT view_id FROM checkpoints WHERE checkpoint_ns = ? AND status = 'active'",
                        (checkpoint_ns,),
                    ).fetchall()
                ]
                retained_sequences: set[int] = set()
                for view_id in active_views:
                    retained_sequences.update(
                        self._view_message_sequences_from_connection(
                            connection, view_id
                        )
                    )
                message_rows = connection.execute(
                    "SELECT message_sequence, turn_id, role, jsonl_offset, jsonl_length, commit_id FROM messages ORDER BY message_sequence"
                ).fetchall()
                removed_rows = [
                    row for row in message_rows if int(row[0]) not in retained_sequences
                ]
                kept_rows = [
                    row for row in message_rows if int(row[0]) in retained_sequences
                ]
                if not removed_rows:
                    return RolloutCompactionResult(
                        removed_message_count=0,
                        retained_message_count=len(kept_rows),
                        bytes_before=bytes_before,
                        bytes_after=bytes_before,
                    )

                compaction_id = uuid4().hex
                old_backup = root / f".rollout.jsonl.compaction-{compaction_id}.old"
                temp_path = root / f".rollout.jsonl.compaction-{compaction_id}.tmp"
                index_backup = root / f".index.sqlite.compaction-{compaction_id}.backup"
                new_positions: dict[int, tuple[int, int]] = {}
                with jsonl_path.open("rb") as source, temp_path.open("wb") as target:
                    for row in kept_rows:
                        sequence = int(row[0])
                        source.seek(int(row[3]))
                        raw = source.read(int(row[4]))
                        envelope = json.loads(raw.decode("utf-8"))
                        if (
                            not isinstance(envelope, dict)
                            or envelope.get("sequence") != sequence
                        ):
                            raise RuntimeError(
                                f"离线 compaction 发现 JSONL 与 SQLite 不一致: sequence={sequence}"
                            )
                        offset = target.tell()
                        target.write(raw)
                        new_positions[sequence] = (offset, len(raw))
                    target.flush()
                    os.fsync(target.fileno())
                new_file_hash = self._file_hash(temp_path)
                self._copy_file_fsync(jsonl_path, old_backup)
                self.backup_index(
                    thread_id,
                    checkpoint_ns,
                    destination=index_backup,
                )
                connection.execute(
                    "INSERT INTO compaction_runs(compaction_id, status, old_file_name, temp_file_name, index_backup_name, new_file_hash, new_file_size, created_at) VALUES (?, 'prepared', ?, ?, ?, ?, ?, ?)",
                    (
                        compaction_id,
                        old_backup.name,
                        temp_path.name,
                        index_backup.name,
                        new_file_hash,
                        temp_path.stat().st_size,
                        _now(),
                    ),
                )
                connection.execute(
                    "UPDATE database_meta SET database_state = 'compacting', updated_at = ? WHERE singleton_id = 1",
                    (_now(),),
                )
                connection.commit()
                os.replace(temp_path, jsonl_path)
                connection.execute(
                    "UPDATE compaction_runs SET status = 'replaced' WHERE compaction_id = ?",
                    (compaction_id,),
                )
                connection.commit()

                transaction_id = uuid4().hex
                timestamp = _now()
                connection.execute("BEGIN IMMEDIATE")
                for sequence, (offset, length) in new_positions.items():
                    connection.execute(
                        "UPDATE messages SET jsonl_offset = ?, jsonl_length = ? WHERE message_sequence = ?",
                        (offset, length, sequence),
                    )
                removed_sequences = tuple(int(row[0]) for row in removed_rows)
                placeholders = ",".join("?" for _ in removed_sequences)
                connection.execute(
                    f"DELETE FROM message_projections WHERE message_sequence IN ({placeholders})",
                    removed_sequences,
                )
                connection.execute(
                    f"DELETE FROM reasoning_blocks WHERE message_sequence IN ({placeholders})",
                    removed_sequences,
                )
                connection.execute(
                    f"DELETE FROM tool_calls WHERE assistant_message_sequence IN ({placeholders})",
                    removed_sequences,
                )
                connection.execute(
                    f"UPDATE tool_calls SET result_message_sequence = NULL, result_length = NULL, result_hash = NULL, status = 'pending', completed_at = NULL WHERE result_message_sequence IN ({placeholders})",
                    removed_sequences,
                )
                connection.execute(
                    f"DELETE FROM messages WHERE message_sequence IN ({placeholders})",
                    removed_sequences,
                )

                kept_by_turn: dict[str, list[tuple[int, str, str]]] = {}
                for row in kept_rows:
                    kept_by_turn.setdefault(str(row[1]), []).append(
                        (int(row[0]), str(row[2]), str(row[1]))
                    )
                turn_rows = connection.execute(
                    "SELECT turn_id, final_message_sequence FROM turns"
                ).fetchall()
                for turn_id, final_sequence in turn_rows:
                    kept_turn = kept_by_turn.get(str(turn_id), [])
                    if not kept_turn:
                        connection.execute(
                            "DELETE FROM context_view_turns WHERE turn_id = ?",
                            (str(turn_id),),
                        )
                        connection.execute(
                            "DELETE FROM turns WHERE turn_id = ?",
                            (str(turn_id),),
                        )
                        continue
                    first_sequence = kept_turn[0][0]
                    last_sequence = kept_turn[-1][0]
                    user_sequence = next(
                        (sequence for sequence, role, _ in kept_turn if role == "user"),
                        None,
                    )
                    final_kept = (
                        int(final_sequence)
                        if final_sequence is not None
                        and int(final_sequence) in new_positions
                        else None
                    )
                    final_id = (
                        connection.execute(
                            "SELECT message_id FROM messages WHERE message_sequence = ?",
                            (final_kept,),
                        ).fetchone()[0]
                        if final_kept is not None
                        else None
                    )
                    connection.execute(
                        "UPDATE turns SET first_message_sequence = ?, last_message_sequence = ?, user_message_sequence = ?, final_message_sequence = ?, final_message_id = ?, status = ?, updated_at = ? WHERE turn_id = ?",
                        (
                            first_sequence,
                            last_sequence,
                            user_sequence,
                            final_kept,
                            final_id,
                            "completed" if final_kept is not None else "running",
                            timestamp,
                            str(turn_id),
                        ),
                    )
                    connection.execute(
                        "UPDATE context_view_turns SET user_message_sequence = ?, final_message_sequence = ? WHERE turn_id = ?",
                        (user_sequence, final_kept, str(turn_id)),
                    )

                commit_rows = connection.execute(
                    "SELECT commit_id FROM storage_commits ORDER BY commit_id"
                ).fetchall()
                next_offset = 0
                for (commit_id_value,) in commit_rows:
                    commit_positions = [
                        new_positions[int(row[0])]
                        for row in kept_rows
                        if int(row[5]) == int(commit_id_value)
                    ]
                    start_offset = next_offset
                    if commit_positions:
                        start_offset = min(position[0] for position in commit_positions)
                        next_offset = max(
                            position[0] + position[1] for position in commit_positions
                        )
                    connection.execute(
                        "UPDATE storage_commits SET jsonl_start_offset = ?, jsonl_end_offset = ? WHERE commit_id = ?",
                        (start_offset, next_offset, int(commit_id_value)),
                    )
                control_sequence = self._insert_control(
                    connection,
                    "offline_compaction",
                    "rollout",
                    str(thread_id),
                    None,
                    None,
                    None,
                    {
                        "removed_message_count": len(removed_rows),
                        "retained_message_count": len(kept_rows),
                        "physical_jsonl": True,
                    },
                    transaction_id,
                    timestamp,
                )
                connection.execute(
                    "UPDATE database_meta SET database_state = 'active', committed_jsonl_offset = ?, last_control_sequence = ?, projection_epoch = projection_epoch + 1, updated_at = ? WHERE singleton_id = 1",
                    (
                        temp_path.stat().st_size
                        if temp_path.exists()
                        else jsonl_path.stat().st_size,
                        control_sequence,
                        timestamp,
                    ),
                )
                connection.execute(
                    "DELETE FROM compaction_runs WHERE compaction_id = ?",
                    (compaction_id,),
                )
                connection.commit()
                bytes_after = jsonl_path.stat().st_size
                old_backup.unlink(missing_ok=True)
                index_backup.unlink(missing_ok=True)
                temp_path.unlink(missing_ok=True)
                return RolloutCompactionResult(
                    removed_message_count=len(removed_rows),
                    retained_message_count=len(kept_rows),
                    bytes_before=bytes_before,
                    bytes_after=bytes_after,
                )
