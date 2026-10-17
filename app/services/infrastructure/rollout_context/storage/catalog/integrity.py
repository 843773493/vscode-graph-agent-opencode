"""校验 canonical JSONL 与 catalog 的 manifest，不执行回填或修复。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar

from cachetools import LRUCache

from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import payload_content_length
from app.domain.itemized.records import (
    CanonicalItemRecord,
    parse_item_projection_manifest,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_line,
    canonical_json_text,
)

_BODY_VALIDATION_MAX_BYTES = 8 * 1024 * 1024
_BODY_VALIDATION_MAX_ENTRIES = 4096
_BodyValidationKey = tuple[bytes, int, str, str, int, str]
_body_validation_scope: ContextVar[tuple[LRUCache[_BodyValidationKey, bytes], int] | None] = (
    ContextVar("catalog_body_validation_scope", default=None)
)


@contextmanager
def catalog_body_validation_scope(
    *, max_bytes: int = _BODY_VALIDATION_MAX_BYTES, max_entries: int = _BODY_VALIDATION_MAX_ENTRIES,
) -> Iterator[None]:
    """单次 prepare 只复用相同实际字节的成功校验，退出后释放全部正文。"""
    if type(max_bytes) is not int or type(max_entries) is not int:
        raise TypeError(
            "正文校验作用域的字节和条目上限必须是严格整数: "
            f"max_bytes={max_bytes!r}, max_entries={max_entries!r}"
        )
    if max_bytes <= 0 or max_entries <= 0:
        raise ValueError("正文校验作用域的字节和条目上限必须大于 0")
    if max_bytes > _BODY_VALIDATION_MAX_BYTES or max_entries > _BODY_VALIDATION_MAX_ENTRIES:
        raise ValueError("正文校验作用域不能超过硬上限：8MiB / 4096 条")
    successful: LRUCache[_BodyValidationKey, bytes] = LRUCache(
        maxsize=max_bytes, getsizeof=len,
    )
    token = _body_validation_scope.set((successful, max_entries))
    try:
        yield
    finally:
        _body_validation_scope.reset(token)
        successful.clear()


def validate_catalog_projection(
    item: CanonicalItemRecord,
    *,
    projection_manifest_json: str,
    tool_call_id: str | None,
    relation_rows: Sequence[tuple[object, ...]],
) -> None:
    """所有正文读取入口共用 typed 镜像与有序关系索引校验。"""
    manifest = parse_item_projection_manifest(json.loads(projection_manifest_json))
    if canonical_json_text(manifest) != projection_manifest_json or manifest != item.projection_manifest():
        raise RuntimeError(f"item catalog 与 JSONL typed projection manifest 不一致: {item.item_id}")
    if tool_call_id != item.tool_call_id:
        raise RuntimeError(f"item catalog 与 JSONL tool_call_id 不一致: {item.item_id}")
    expected = [
        (ordinal, relation.relation, item.item_id, canonical_json_text(relation.to_dict()))
        for ordinal, relation in enumerate(item.provenance_relations)
    ]
    if list(relation_rows) != expected:
        raise RuntimeError(f"item relation index 与 canonical typed relations 不一致: {item.item_id}")


def validate_catalog_body(
    raw: bytes,
    *,
    sequence: int,
    item_id: str,
    catalog_hash: str,
    payload_length: int,
    source_revision: str,
) -> None:
    """以已提交正文验证 locator、hash 和逻辑 source manifest。"""
    scope = _body_validation_scope.get()
    key = (raw, sequence, item_id, catalog_hash, payload_length, source_revision)
    if scope is not None and scope[0].get(key) is not None:
        return
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"v2 item JSONL 无法解码: item_id={item_id}") from error
    if not isinstance(envelope, Mapping):
        raise TypeError(f"v2 item envelope 非 object: item_id={item_id}")
    if raw != canonical_json_line(envelope):
        raise RuntimeError(f"v2 item JSONL 不是 canonical JCS line: item_id={item_id}")
    if (
        envelope.get("format_version") != 2
        or envelope.get("record_type") != "item"
        or envelope.get("item_sequence") != sequence
        or envelope.get("item_id") != item_id
        or envelope.get("content_hash") != catalog_hash
    ):
        raise RuntimeError(f"v2 item catalog 与 JSONL identity 不一致: item_id={item_id}")
    try:
        item = CanonicalItemRecord.from_dict(envelope)
    except (KeyError, ItemSchemaError, TypeError, ValueError) as error:
        raise RuntimeError(f"v2 item JSONL schema/content 非法: item_id={item_id}") from error
    if payload_length != payload_content_length(item.payload_kind, item.payload):
        raise RuntimeError(f"item catalog 与 JSONL payload_length 不一致: {item_id}")
    expected_revision = (item.source_manifest.source_revision if item.source_manifest is not None else None)
    if expected_revision is None:
        expected_revision = f"canonical:{item.item_id}:{item.content_hash}"
    if source_revision != expected_revision:
        raise RuntimeError(f"item catalog 与 JSONL source_revision 不一致: {item_id}")
    if scope is not None:
        successful, max_entries = scope
        if len(raw) <= successful.maxsize:
            while len(successful) >= max_entries:
                successful.popitem()
            successful[key] = raw


def validate_projection_membership(connection: sqlite3.Connection) -> None:
    """已有 projection 必须有对应 catalog；最小 catalog 不强制重型索引。"""
    orphan = connection.execute(
        "SELECT p.item_id FROM item_projections p LEFT JOIN item_catalog c "
        "ON c.item_id = p.item_id AND c.item_sequence = p.item_sequence "
        "WHERE c.item_id IS NULL LIMIT 1"
    ).fetchone()
    if orphan is not None:
        raise RuntimeError(
            f"item projection 与 canonical catalog membership 不一致: "
            f"orphan={orphan}"
        )
