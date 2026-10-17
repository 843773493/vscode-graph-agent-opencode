"""目标 rollout 内的冻结历史 scope；只授权本地捕获事实，不重定向源路径。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from app.domain.itemized.hashing import sha256_jcs


@dataclass(frozen=True, slots=True)
class HistoricalNamespaceBinding:
    """session/thread 是历史执行 scope，source 字段描述该 binding 的复制来源。

    source_snapshot_sha256 是来源快照校验值；当前存储 scope 只由
    database_meta 决定，来源 URI 不参与本地正文路径解析。
    """

    session_id: str
    thread_id: str
    source_uri: str
    source_rollout_id: str
    source_snapshot_sha256: str

    def to_dict(self) -> dict[str, str]:
        return {
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "source_uri": self.source_uri,
            "source_rollout_id": self.source_rollout_id,
            "source_snapshot_sha256": self.source_snapshot_sha256,
        }


def current_scope(connection: sqlite3.Connection) -> tuple[str, str]:
    row = connection.execute(
        "SELECT session_id, thread_id, database_state FROM database_meta "
        "WHERE singleton_id = 1"
    ).fetchone()
    if row is None or row[2] != "active":
        raise ValueError("source-mismatch: rollout 当前 owner 缺失或不是 active")
    if any(not isinstance(value, str) or not value for value in row[:2]):
        raise ValueError("source-mismatch: rollout 当前 SessionThread owner 非法")
    return row[0], row[1]


def read_namespace_bindings(
    connection: sqlite3.Connection,
    *,
    historical_scope_key: tuple[str, str] | None = None,
) -> tuple[HistoricalNamespaceBinding, ...]:
    """核验目标已有的冻结 binding；指定历史 scope 主键时只读取该 binding。"""
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'history_namespace_bindings'"
    ).fetchone() is None:
        return ()
    rows = connection.execute(
        "SELECT session_id, thread_id, source_uri, source_rollout_id, "
        "source_snapshot_sha256, binding_hash FROM history_namespace_bindings "
        + ("WHERE session_id=? AND thread_id=?" if historical_scope_key is not None else "ORDER BY session_id, thread_id"),
        historical_scope_key if historical_scope_key is not None else (),
    )
    bindings = []
    for row in rows:
        if any(not isinstance(value, str) or not value for value in row):
            raise ValueError("source-mismatch: 历史 namespace binding 含非法字段")
        binding = HistoricalNamespaceBinding(*row[:5])
        if sha256_jcs(binding.to_dict()) != row[5]:
            raise ValueError("source-mismatch: 冻结历史 namespace binding hash 不一致")
        bindings.append(binding)
    return tuple(bindings)


def read_local_scopes(
    connection: sqlite3.Connection,
    *,
    historical_scope_key: tuple[str, str] | None = None,
) -> frozenset[tuple[str, str]]:
    """唯一的本地可读 scope 集合；binding 必须先通过冻结 hash 校验。"""
    return frozenset({
        current_scope(connection),
        *((binding.session_id, binding.thread_id) for binding in read_namespace_bindings(
            connection, historical_scope_key=historical_scope_key,
        )),
    })


def require_historical_scope(
    connection: sqlite3.Connection,
    session_id: str,
    thread_id: str,
    *,
    current_owner: tuple[str, str] | None = None,
) -> None:
    """历史正文保留原 scope，读取必须同时符合当前 owner 与冻结来源。"""
    owner = current_scope(connection)
    if current_owner is not None and current_owner != owner:
        raise ValueError("source-mismatch: 读取方与目标 rollout catalog owner 不一致")
    requested = (session_id, thread_id)
    if requested == owner:
        return
    if requested not in read_local_scopes(connection, historical_scope_key=requested):
        raise ValueError(
            "source-mismatch: 历史 SessionThread 不属于目标本地冻结 snapshot: "
            f"current={owner}, historical={requested}"
        )


def bind_captured_namespace(
    connection: sqlite3.Connection,
    binding: HistoricalNamespaceBinding,
) -> None:
    """在私有目标事务中继承旧 binding，并登记直接来源；不改历史实体。"""
    if not connection.in_transaction:
        raise RuntimeError("冻结 namespace binding 必须属于 snapshot 安装事务")
    for existing in read_namespace_bindings(connection):
        if (existing.session_id, existing.thread_id) == (
            binding.session_id, binding.thread_id
        ):
            if existing != binding:
                raise ValueError("source-mismatch: namespace binding 的不可变前像冲突")
            return
    values = binding.to_dict()
    if any(not isinstance(value, str) or not value for value in values.values()):
        raise ValueError("source-mismatch: namespace binding 缺少捕获 scope")
    connection.execute(
        "INSERT INTO history_namespace_bindings "
        "(session_id, thread_id, source_uri, source_rollout_id, "
        "source_snapshot_sha256, binding_hash) VALUES (?, ?, ?, ?, ?, ?)",
        (*values.values(), sha256_jcs(values)),
    )


def require_resumable_turn(
    connection: sqlite3.Connection,
    session_id: str,
    thread_id: str,
    turn_id: str,
) -> None:
    """复制的历史事实不可恢复；新输入和 replay_as_new_turn 另建当前 Turn。"""
    if current_scope(connection) != (session_id, thread_id):
        raise ValueError("source-mismatch: execution 目标与当前 rollout owner 不一致")
    row = connection.execute(
        "SELECT session_id, thread_id FROM turn_records WHERE turn_id = ?",
        (turn_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"Turn 不存在: {turn_id}")
    if tuple(row) != (session_id, thread_id):
        require_historical_scope(connection, row[0], row[1])
        raise ValueError("turn_not_resumable")


__all__ = [
    "HistoricalNamespaceBinding",
    "bind_captured_namespace",
    "current_scope",
    "read_local_scopes",
    "read_namespace_bindings",
    "require_historical_scope",
    "require_resumable_turn",
]
