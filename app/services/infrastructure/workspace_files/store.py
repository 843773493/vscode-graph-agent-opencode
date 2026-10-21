"""文件 owner 的短 SQLite 事务：目录观察、不可变受理与发布证明。"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from app.core.sqlite_state import load_sql
from app.schemas.internal_v2.session_navigation.operations import (
    NAVIGATION_MUTATION_TERMINAL_STATES,
    validate_operation_id,
)
from app.schemas.internal_v2.workspace_file_operations import (
    WorkspaceDirectoryKeyDTO,
    WorkspaceDirectoryRevisionDTO,
    WorkspaceFileListDTO,
    WorkspaceFileMutationIntentDTO,
    WorkspaceFileOperationReceiptDTO,
)


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def encode_directory_cursor(directory_key: WorkspaceDirectoryKeyDTO, revision: int, key: tuple[bool, str, str]) -> str:
    raw = json.dumps({"directory_key": directory_key.model_dump(), "revision": revision, "after": key},
                     ensure_ascii=False, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")

def decode_directory_cursor(cursor: str) -> dict:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("目录分页游标格式错误") from error
    if not isinstance(value, dict) or set(value) != {"directory_key", "revision", "after"}:
        raise ValueError("目录分页游标内容错误")
    key = value["after"]
    if not isinstance(value["revision"], int) or isinstance(value["revision"], bool) or value["revision"] < 1 or not isinstance(key, list) or len(key) != 3 or not isinstance(key[0], bool) or not all(isinstance(item, str) for item in key[1:]):
        raise ValueError("目录分页游标内容错误")
    WorkspaceDirectoryKeyDTO.model_validate(value["directory_key"])
    value["after"] = tuple(key)
    return value


class WorkspaceFileOperationConflictError(RuntimeError):
    """已接受的原意图、目录版本或路径事实发生冲突。"""


@dataclass(frozen=True)
class FileOperationRecord:
    operation_id: str
    actor: str
    payload: dict
    state: str
    token: int
    steps: list[dict]
    receipt: WorkspaceFileOperationReceiptDTO


class WorkspaceFileStore:
    def __init__(self, workspace: Path, workspace_id: str) -> None:
        self.path = workspace / ".boxteam/navigation/workspace-files.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.workspace_id = workspace_id
        self._lock = RLock()
        self._db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version == 0 and not self._db.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
            with self.transaction() as db:
                db.execute(load_sql(__package__, "sql/owner.sql"))
                db.execute(load_sql(__package__, "sql/owner_seed.sql"), (workspace_id,))
                # 每个已访问目录只留固定宽度摘要与版本，不存逐文件身份或正文。
                db.execute(load_sql(__package__, "sql/directories.sql"))
                db.execute(load_sql(__package__, "sql/file_directory_uri.sql"))
                db.execute(load_sql(__package__, "sql/operations.sql"))
                db.execute(load_sql(__package__, "sql/file_operations_pending.sql"))
                db.execute(load_sql(__package__, "sql/file_operations_cleanup.sql"))
                db.execute(load_sql(__package__, "sql/events.sql"))
                db.execute("PRAGMA user_version=3")
        elif version != 3:
            self._db.close()
            raise RuntimeError(f"文件导航库 schema 不受支持: path={self.path}, version={version}")
        tables = {row[0] for row in self._db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"owner", "directories", "operations", "events"}.issubset(tables):
            raise RuntimeError(f"文件导航库缺少权威表，不重建: {self.path}")
        rows = [row[0] for row in self._db.execute("SELECT workspace_id FROM owner")]
        if rows != [workspace_id]:
            raise RuntimeError(f"文件导航库 workspace owner 不一致: {rows!r}")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.rollback()
                raise
            else:
                self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def observe(self, observations: list[tuple[WorkspaceDirectoryKeyDTO, str, str, bool]],
                *, db: sqlite3.Connection | None = None) -> list[WorkspaceDirectoryRevisionDTO]:
        if db is None:
            with self.transaction() as connection:
                return self.observe(observations, db=connection)
        results = []
        for key, uri, signature, present in observations:
            identity = canonical_json(key.model_dump())
            row = db.execute("SELECT * FROM directories WHERE key=?", (identity,)).fetchone()
            revision = 1 if row is None else int(row["revision"]) + int((row["signature"], bool(row["present"])) != (signature, present))
            if row is not None and row["uri"] != uri:
                raise WorkspaceFileOperationConflictError(f"目录 URI 发生变化: {key.path}")
            db.execute("INSERT INTO directories VALUES (?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                       "revision=excluded.revision,signature=excluded.signature,present=excluded.present",
                       (identity, uri, revision, signature, present))
            results.append(WorkspaceDirectoryRevisionDTO(directory_key=key, directory_revision=revision, exists=present))
        return results

    def observed_directories(self) -> tuple[tuple[WorkspaceDirectoryKeyDTO, str], ...]:
        with self._lock:
            rows = self._db.execute("SELECT key,uri FROM directories")
            return tuple((WorkspaceDirectoryKeyDTO.model_validate(json.loads(row[0])), str(row[1])) for row in rows)

    def cleanup_pending(self) -> tuple[tuple[str, str], ...]:
        with self._lock:
            return tuple((str(row[0]), str(row[1])) for row in self._db.execute(
                "SELECT operation_id,actor FROM operations WHERE cleanup_done=0 AND state NOT IN ('queued','running') ORDER BY queue_seq"))

    def complete_cleanup(self, record: FileOperationRecord) -> None:
        with self.transaction() as db:
            if db.execute("UPDATE operations SET cleanup_done=1 WHERE operation_id=? AND token=? AND state NOT IN ('queued','running')",
                          (record.operation_id, record.token)).rowcount != 1:
                raise RuntimeError("文件 operation 暂存回收的原 owner/token 已失效")

    def staging_records(self) -> tuple[tuple[str, str], ...]:
        with self._lock:
            return tuple((str(row[0]), str(row[1])) for row in self._db.execute(
                "SELECT operation_id,actor FROM operations WHERE cleanup_done=0 ORDER BY queue_seq"))

    def get(self, operation_id: str, actor: str) -> FileOperationRecord:
        validate_operation_id(operation_id)
        with self._lock:
            row = self._db.execute("SELECT * FROM operations WHERE operation_id=? AND actor=?", (operation_id, actor)).fetchone()
            if row is None:
                raise KeyError(f"文件 operation 不存在: {operation_id}")
            payload = json.loads(row["payload"])
            if digest(payload) != row["payload_hash"] or payload["workspace_id"] != self.workspace_id or payload["actor"] != actor or payload["intent"]["client_operation_id"] != operation_id or payload["request_hash"] != row["request_hash"]:
                raise RuntimeError(f"文件 operation 冻结内容损坏: {operation_id}")
            receipt = WorkspaceFileOperationReceiptDTO.model_validate_json(row["receipt"])
            if (receipt.operation_id, receipt.state, receipt.queue_seq) != (operation_id, row["state"], row["queue_seq"]):
                raise RuntimeError(f"文件 operation 回执与原记录不一致: {operation_id}")
            steps = json.loads(row["steps"])
            if digest(steps) != row["steps_hash"]:
                raise RuntimeError(f"文件 operation 发布证明损坏: {operation_id}")
            token = row["token"]
            if not isinstance(token, int) or token < 0 or (row["state"] == "queued" and token != 0) or (row["state"] == "running" and token == 0):
                raise RuntimeError(f"文件 operation 执行 token 损坏: operation_id={operation_id}, state={row['state']}, token={token!r}")
            return FileOperationRecord(operation_id, actor, payload, str(row["state"]), token, steps, receipt)

    def replay(self, operation_id: str, actor: str, request_hash: str) -> WorkspaceFileOperationReceiptDTO | None:
        with self._lock:
            row = self._db.execute("SELECT actor,request_hash FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None:
                return None
            if (row[0], row[1]) != (actor, request_hash):
                raise WorkspaceFileOperationConflictError(f"同 ID 的文件意图 preimage 冲突: {operation_id}")
            return self.get(operation_id, actor).receipt

    def accept(self, *, actor: str, kind: str, intent: WorkspaceFileMutationIntentDTO,
               request_hash: str, payload: dict) -> WorkspaceFileOperationReceiptDTO:
        with self.transaction() as db:
            original = self.replay(intent.client_operation_id, actor, request_hash)
            if original is not None:
                return original
            for dependency in intent.depends_on:
                try:
                    self.get(dependency, actor)
                except KeyError as error:
                    raise WorkspaceFileOperationConflictError(f"前序文件 operation 尚未受理: {dependency}") from error
            cursor = db.execute("INSERT INTO operations(operation_id,actor,request_hash,payload,payload_hash,state,steps,steps_hash,receipt) "
                                "VALUES (?,?,?,?,?,'queued','[]',?,'{}')",
                                (intent.client_operation_id, actor, request_hash, canonical_json(payload), digest(payload), digest([])))
            receipt = WorkspaceFileOperationReceiptDTO(
                operation_id=intent.client_operation_id, client_sequence=intent.client_sequence,
                queue_seq=cursor.lastrowid, kind=kind, state="queued", receipt_revision=1, effect="none",
                created_path=payload.get("created_path"),
            )
            db.execute("UPDATE operations SET receipt=? WHERE operation_id=?", (receipt.model_dump_json(), intent.client_operation_id))
            return receipt

    def pending(self) -> tuple[tuple[str, str], ...]:
        with self._lock:
            return tuple((str(row[0]), str(row[1])) for row in self._db.execute(
                "SELECT operation_id,actor FROM operations WHERE state IN ('queued','running') ORDER BY queue_seq"))

    def claim(self, record: FileOperationRecord) -> FileOperationRecord:
        with self.transaction() as db:
            receipt = record.receipt.model_copy(update={"state": "running", "receipt_revision": record.receipt.receipt_revision + 1})
            changed = db.execute("UPDATE operations SET state='running',token=token+1,receipt=? "
                                 "WHERE operation_id=? AND token=? AND state IN ('queued','running')",
                                 (receipt.model_dump_json(), record.operation_id, record.token)).rowcount
            if changed != 1:
                raise RuntimeError("文件 operation 领取 token CAS 失败")
            return self.get(record.operation_id, record.actor)

    def save_steps(self, record: FileOperationRecord, steps: list[dict]) -> None:
        with self.transaction() as db:
            if db.execute("UPDATE operations SET steps=?,steps_hash=? WHERE operation_id=? AND token=? AND state='running'",
                          (canonical_json(steps), digest(steps), record.operation_id, record.token)).rowcount != 1:
                raise RuntimeError("文件发布证明的原 owner/token 已失效")

    def settle(self, record: FileOperationRecord, *, state: str, effect: str,
               items: list[dict], observations: list[tuple[WorkspaceDirectoryKeyDTO, str, str, bool]],
               snapshots: list[tuple[WorkspaceDirectoryKeyDTO, dict]],
               error_code: str | None = None, error_detail: str | None = None) -> WorkspaceFileOperationReceiptDTO:
        with self.transaction() as db:
            current = self.get(record.operation_id, record.actor)
            if current.token != record.token or current.state not in ("queued", "running"):
                raise RuntimeError("文件 operation terminal 的原 owner/token 已失效")
            directories = self.observe(observations, db=db)
            versions = {value.directory_key: value.directory_revision for value in directories if value.exists}
            listings = []
            for key, snapshot in snapshots:
                after = snapshot["after"]
                revision = versions[key]
                listings.append(WorkspaceFileListDTO(
                    **{name: value for name, value in snapshot.items() if name != "after"},
                    directory_key=key, directory_revision=revision, truncated=after is not None,
                    next_cursor=None if after is None else encode_directory_cursor(key, revision, after),
                ))
            receipt = WorkspaceFileOperationReceiptDTO(
                **{**current.receipt.model_dump(), "state": state, "effect": effect,
                   "receipt_revision": current.receipt.receipt_revision + 1, "items": items,
                   "directories": directories, "snapshots": listings,
                   "error_code": error_code, "error_detail": error_detail},
            )
            db.execute("UPDATE operations SET state=?,receipt=? WHERE operation_id=?",
                       (state, receipt.model_dump_json(), record.operation_id))
            if state in NAVIGATION_MUTATION_TERMINAL_STATES:
                db.execute("INSERT INTO events(payload) VALUES (?)", (canonical_json({
                    "changes": [], "overflow": False, "directories": [item.model_dump() for item in directories],
                    "snapshots": [item.model_dump() for item in listings],
                    "operation_id": record.operation_id}),))
            return receipt

    def latest_event_id(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COALESCE(MAX(event_id),0) FROM events").fetchone()[0])

    def events(self, after: int) -> list[dict]:
        with self._lock:
            return [{**json.loads(row[1]), "event_id": int(row[0])} for row in self._db.execute(
                "SELECT event_id,payload FROM events WHERE event_id>? ORDER BY event_id LIMIT 200", (after,))]
