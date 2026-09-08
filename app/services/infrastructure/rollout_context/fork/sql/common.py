"""full_rollout_copy 的 SQLite identity/reference 收敛。"""

from __future__ import annotations

import hashlib
import sqlite3

from app.domain.itemized.hashing import (
    canonical_json_bytes,
)
from app.services.infrastructure.rollout_context.fork.validation import (
    non_negative_int,
)


def _json(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


_VISIBLE_TEXT_LIMIT = 64 * 1024


def rewrite_column(
    connection: sqlite3.Connection, table: str, column: str, old: object, new: object
) -> None:
    """在通用 remap 阶段按实际 schema 安全更新一列。"""
    if old is None:
        return
    columns = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
    if column in columns:
        count_row = connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", (old,)
        ).fetchone()
        if count_row is None:
            raise RuntimeError(f"无法读取 remap 行数: {table}.{column}")
        expected = non_negative_int(count_row[0], field=f"{table}.{column}.match_count")
        result = connection.execute(
            f"UPDATE {table} SET {column} = ? WHERE {column} = ?",
            (new, old),
        )
        if result.rowcount != expected:
            raise RuntimeError(
                f"full_rollout_copy remap 行数不一致: {table}.{column}, "
                f"expected={expected}, actual={result.rowcount}"
            )


def rewrite_reference_column(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    ref_type: str,
    old: str,
    new: str,
) -> None:
    """按 ref_type 重写可选引用，并验证实际命中数。"""
    columns = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
    if {column, "ref_type"} - columns:
        raise RuntimeError(f"full_rollout_copy 缺少 reference 列: {table}.{column}")
    count_row = connection.execute(
        f"SELECT COUNT(*) FROM {table} WHERE ref_type = ? AND {column} = ?",
        (ref_type, old),
    ).fetchone()
    if count_row is None:
        raise RuntimeError(f"无法读取 reference remap 行数: {table}.{column}")
    expected = non_negative_int(count_row[0], field=f"{table}.{column}.match_count")
    result = connection.execute(
        f"UPDATE {table} SET {column} = ? WHERE ref_type = ? AND {column} = ?",
        (new, ref_type, old),
    )
    if result.rowcount != expected:
        raise RuntimeError(
            f"full_rollout_copy reference remap 行数不一致: {table}.{column}, "
            f"expected={expected}, actual={result.rowcount}"
        )
