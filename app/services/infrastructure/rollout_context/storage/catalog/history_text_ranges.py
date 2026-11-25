"""安全投影的原 JSONL 字节位置；不保存正文，不重新决定哪些字段可见。"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterable
from typing import BinaryIO

from app.domain.itemized.identity.refs import ContextRef
from app.services.infrastructure.rollout_context.storage.catalog.history_scope import (
    require_historical_scope,
)
from app.services.infrastructure.rollout_context.storage.catalog.item_reader import (
    CATALOG_READ_COLUMNS,
    CATALOG_READ_JOINS,
    validate_catalog_read_boundary,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_text,
)

_STRING_TOKEN = re.compile(rb'"(?:[^"\\]|\\.)*"')
_GENERATED_FIELDS = frozenset({"role", "kind", "carrier_type", "encrypted_hash", "type"})


def history_line_locators(
    raw: bytes, fields: Iterable[tuple[str, object]], *, header: str,
) -> list[tuple[str, int | None, int | None, str | None, int, str | None]]:
    """采用时由已完整校验的原件建立 locator；长文本每行至多1024字符。"""
    tokens: dict[str, int] = {}
    for match in _STRING_TOKEN.finditer(raw):
        tokens.setdefault(json.loads(match[0]), match.start() + 1)
    result = [(header, None, None, None, 0, "")]
    for path, value in fields:
        prefix = f"{path}: "
        if not isinstance(value, str) or not value:
            encoded = canonical_json_text(value).encode("utf-8")
            start = raw.find(encoded)
            if start >= 0:
                result.append((prefix, start, len(encoded), hashlib.sha256(encoded).hexdigest(), 0, None))
            else:
                result.append((prefix, None, None, None, 0, encoded.decode("utf-8")))
            continue
        # 安全投影已保留原字符串边界；这里不猜测拼接或读取受保护字段。
        if value in tokens:
            offset = tokens[value]
        else:
            if path.rsplit(".", 1)[-1] in _GENERATED_FIELDS:
                result.append((prefix, None, None, None, 0, canonical_json_text(value)))
                continue
            raise ValueError(f"compaction-history-source-mismatch: 安全字段无法定位原件: {path}")
        for start in range(0, len(value), 1024):
            encoded = canonical_json_text(value[start:start + 1024]).encode("utf-8")[1:-1]
            if raw[offset:offset + len(encoded)] != encoded:
                raise ValueError("compaction-history-source-mismatch: 原件字符串编码与安全片段不一致")
            result.append((prefix, offset, len(encoded), hashlib.sha256(encoded).hexdigest(), 1, None))
            offset += len(encoded)
    return result


def read_history_page(
    connection: sqlite3.Connection, stream: BinaryIO, *, checkpoint_ns: str,
    ref: ContextRef, line_start: int, first: int, end: int, max_bytes: int,
) -> list[str]:
    """页读只读取本页 locator 命中的字节；原件完整性在采用时已验证。"""
    row = connection.execute(
        "SELECT item.content_hash,item.source_revision,item.jsonl_offset,item.jsonl_length,"
        + CATALOG_READ_COLUMNS + CATALOG_READ_JOINS + "WHERE item.item_id=?", (ref.ref_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"detail-unavailable: 压缩原文范围已不可读，line_start={line_start}")
    if row[:2] != (ref.content_hash, ref.source_revision):
        raise ValueError("source-mismatch: 原文 locator 与冻结版本不一致")
    offset, length = row[2:4]
    validate_catalog_read_boundary(connection, row[4:], item_id=ref.ref_id, offset=offset, length=length,
                                   validated_commits=set(), validated_scopes=set())
    require_historical_scope(connection, ref.session_id, ref.thread_id,
                             current_owner=(row[-3], row[-2]))
    lines: list[str] = []
    used = 0
    expected = first
    for ordinal, prefix, start, count, digest, quoted, literal in connection.execute(
        "SELECT line_ordinal,prefix,source_offset,source_length,source_hash,quoted,literal FROM compaction_history_lines "
        "WHERE checkpoint_ns=? AND item_id=? AND source_revision=? AND line_ordinal>=? AND line_ordinal<? ORDER BY line_ordinal",
        (checkpoint_ns, ref.ref_id, ref.source_revision, first, end),
    ):
        if ordinal != expected:
            raise ValueError("compaction-history-source-mismatch: 原文行位置索引不连续")
        expected += 1
        size = len(f"{line_start + ordinal}: {prefix}".encode()) + 1
        size += len(literal.encode("utf-8")) if literal is not None else 2 * quoted + count
        if used + size > max_bytes:
            break
        if literal is None:
            if type(start) is not int or type(count) is not int or not 0 <= start < start + count < length:
                raise ValueError("compaction-history-source-mismatch: 原文片段超出已提交 Item")
            stream.seek(offset + start)
            chunk = stream.read(count)
            if len(chunk) != count or hashlib.sha256(chunk).hexdigest() != digest:
                raise ValueError("source-mismatch: 原文片段与采用时冻结字节不一致")
            quote = '"' if quoted else ""
            body = quote + chunk.decode("utf-8") + quote
        else:
            body = literal
        lines.append(f"{line_start + ordinal}: {prefix}{body}")
        used += size
    else:
        if expected != end:
            raise ValueError("compaction-history-source-mismatch: 原文行位置索引缺失")
    return lines
