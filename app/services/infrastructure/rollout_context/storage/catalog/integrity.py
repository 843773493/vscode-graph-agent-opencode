"""校验 canonical JSONL 与 catalog 的 manifest，不执行回填或修复。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy

from cachetools import LRUCache

from app.domain.itemized.errors import FormatDispatchError, ItemSchemaError
from app.domain.itemized.hashing import payload_content_length
from app.domain.itemized.records import (
    CanonicalItemRecord,
    parse_item_projection_manifest,
)
from app.services.infrastructure.rollout_context.storage.catalog.message_revisions import (
    validate_message_revision_source,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_line,
    canonical_json_text,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_optional_text,
    strict_text,
)

_BODY_VALIDATION_MAX_BYTES = 8 * 1024 * 1024
_BODY_VALIDATION_MAX_ENTRIES = 4096
_BodyValidationKey = tuple[bytes, int, str, str, int, str]
_BodyValidationValue = tuple[bytes, CanonicalItemRecord]
_body_validation_scope: ContextVar[tuple[LRUCache[_BodyValidationKey, _BodyValidationValue], int] | None] = (
    ContextVar("catalog_body_validation_scope", default=None)
)


class CatalogBodyValidationCache:
    """存储实例内有界的纯校验回执，不保留正文或可变 item。"""

    def __init__(self, *, max_entries: int = _BODY_VALIDATION_MAX_ENTRIES) -> None:
        if type(max_entries) is not int or not 0 < max_entries <= _BODY_VALIDATION_MAX_ENTRIES:
            raise ValueError("正文校验回执上限必须是 1 至 4096 的整数")
        self._successful: LRUCache[_BodyValidationKey, _BodyValidationKey] = LRUCache(
            maxsize=_BODY_VALIDATION_MAX_BYTES,
            getsizeof=lambda receipt: sum(len(value.encode("utf-8")) if isinstance(value, str)
                                         else len(value) if isinstance(value, bytes) else 8 for value in receipt),
        )
        self._max_entries = max_entries
        self._lock = threading.Lock()

    def validate(self, key: _BodyValidationKey) -> None:
        raw, sequence, item_id, catalog_hash, payload_length, source_revision = key
        # 只复用当前实际字节与全部 catalog 参数相同的结果；mtime、commit ID
        # 和 catalog 自报的 hash 都不足以证明正文未被改写。摘要不持有历史正文。
        receipt = (hashlib.sha256(raw).digest(), sequence, item_id, catalog_hash, payload_length, source_revision)
        with self._lock:
            if self._successful.get(receipt) is not None:
                return
        # 解析和编码不占共享锁；并发首次验证可以重复计算，失败绝不发布回执。
        _validated_catalog_body(key)
        with self._lock:
            if self._successful.getsizeof(receipt) <= self._successful.maxsize:
                while len(self._successful) >= self._max_entries:
                    self._successful.popitem()
                self._successful[receipt] = receipt


@contextmanager
def catalog_body_validation_scope(
    *, max_bytes: int = _BODY_VALIDATION_MAX_BYTES, max_entries: int = _BODY_VALIDATION_MAX_ENTRIES,
    reuse_existing: bool = False,
) -> Iterator[None]:
    """单次操作复用相同字节的成功校验；借用外层时沿用其容量与生命周期。"""
    if type(max_bytes) is not int or type(max_entries) is not int:
        raise TypeError(
            "正文校验作用域的字节和条目上限必须是严格整数: "
            f"max_bytes={max_bytes!r}, max_entries={max_entries!r}"
        )
    if max_bytes <= 0 or max_entries <= 0:
        raise ValueError("正文校验作用域的字节和条目上限必须大于 0")
    if max_bytes > _BODY_VALIDATION_MAX_BYTES or max_entries > _BODY_VALIDATION_MAX_ENTRIES:
        raise ValueError("正文校验作用域不能超过硬上限：8MiB / 4096 条")
    if reuse_existing and _body_validation_scope.get() is not None:
        yield
        return
    successful: LRUCache[_BodyValidationKey, _BodyValidationValue] = LRUCache(
        maxsize=max_bytes, getsizeof=lambda value: len(value[0]),
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
    connection: sqlite3.Connection,
    projection_manifest_json: str,
    tool_call_id: str | None,
    relation_rows: Sequence[tuple[object, ...]],
) -> None:
    """所有正文读取入口共用 typed 镜像与有序关系索引校验。"""
    # JSONL item 已完成 typed 校验；直接比较其 JCS 镜像，失败时才解析目录以保留格式诊断。
    if canonical_json_text(item.projection_manifest()) != projection_manifest_json:
        parse_item_projection_manifest(json.loads(projection_manifest_json))
        raise RuntimeError(f"item catalog 与 JSONL typed projection manifest 不一致: {item.item_id}")
    if tool_call_id != item.tool_call_id:
        raise RuntimeError(f"item catalog 与 JSONL tool_call_id 不一致: {item.item_id}")
    expected = [
        (ordinal, relation.relation, item.item_id, canonical_json_text(relation.to_dict()))
        for ordinal, relation in enumerate(item.provenance_relations)
    ]
    if list(relation_rows) != expected:
        raise RuntimeError(f"item relation index 与 canonical typed relations 不一致: {item.item_id}")
    if item.projection_group is None or item.projection_group.ordinal == item.projection_group.size - 1:
        validate_message_revision_source(connection, item)


def validate_catalog_body(
    raw: bytes,
    *,
    sequence: int,
    item_id: str,
    catalog_hash: str,
    payload_length: int,
    source_revision: str,
    cache: CatalogBodyValidationCache | None = None,
) -> None:
    """校验实际正文与 catalog；初始化只验证，不分配调用方副本。"""
    key = (raw, sequence, item_id, catalog_hash, payload_length, source_revision)
    if cache is None:
        _validated_catalog_body(key)
    else:
        cache.validate(key)


def read_catalog_body(
    raw: bytes,
    *,
    sequence: int,
    item_id: str,
    catalog_hash: str,
    payload_length: int,
    source_revision: str,
) -> CanonicalItemRecord:
    """返回调用方独有的 typed item，避免可变正文污染同次校验结果。

    同次 prepare 只复用完整原字节和 catalog 参数均相等的成功结果；缓存
    不持有调用方可修改的容器，每次返回独立对象。投影索引仍由读取方现查。
    """
    item = _validated_catalog_body(
        (raw, sequence, item_id, catalog_hash, payload_length, source_revision),
    )
    # 无校验作用域时解析结果未被缓存，已由本次读取独占，无须再复制整份正文。
    return deepcopy(item) if _body_validation_scope.get() is not None else item


def _validated_catalog_body(key: _BodyValidationKey) -> CanonicalItemRecord:
    raw, sequence, item_id, catalog_hash, payload_length, source_revision = key
    scope = _body_validation_scope.get()
    cached = scope[0].get(key) if scope is not None else None
    if cached is not None:
        return cached[1]
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
    ):
        raise RuntimeError(f"v2 item catalog 与 JSONL identity 不一致: item_id={item_id}")
    if envelope.get("content_hash") != catalog_hash:
        raise RuntimeError(
            f"v2 item catalog 与 JSONL identity 不一致: item_id={item_id}; "
            "item catalog 与 JSONL content_hash 不一致"
        )
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
            successful[key] = (raw, item)
    return item


def validate_projection_membership(connection: sqlite3.Connection) -> None:
    """完整审计 projection 的 catalog 归属及全部 view 的成员坐标与顺序。"""
    _validate_context_view_ranges(connection)
    _validate_context_view_orders(connection)
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


def _validate_context_view_ranges(connection: sqlite3.Connection) -> None:
    """完整审计 range 图、父引用及范围格式；普通读取只消费目标 membership。"""
    # 一次取齐图结构，共享祖先只走一次，不为每个 view 重建历史闭包。
    views = {
        strict_text(row[0], field="context_views.view_id"): row[1:]
        for row in connection.execute(
            "SELECT view_id, view_kind, parent_view_id FROM context_views",
        )
    }
    ranges_by_view: dict[str, list[tuple[object, ...]]] = {}
    for view_id, *row in connection.execute(
        "SELECT view_id, source_kind, source_view_id, start_message_sequence, end_message_sequence "
        "FROM context_view_ranges ORDER BY view_id, range_index",
    ):
        if view_id not in views:
            raise RuntimeError(f"context view 不存在: {view_id}")
        ranges_by_view.setdefault(view_id, []).append(tuple(row))
    active: set[str] = set()
    completed: set[str] = set()
    pending = [(view_id, False) for view_id in reversed(views)]
    while pending:
        current, exiting = pending.pop()
        if exiting:
            active.remove(current)
            completed.add(current)
            continue
        if current in active:
            raise RuntimeError(f"context view 引用形成循环: {current}")
        if current in completed:
            continue
        active.add(current)
        view_row = views.get(current)
        if view_row is None:
            raise RuntimeError(f"context view 不存在: {current}")
        view_kind = strict_text(view_row[0], field=f"context_views.view_kind: {current}")
        parent_view_id = strict_optional_text(
            view_row[1], field=f"context_views.parent_view_id: {current}"
        )
        ranges = ranges_by_view.get(current, ())
        sources: list[str] = []
        for source_kind, source_view, start, end in ranges:
            source_kind = strict_text(
                source_kind, field=f"context_view_ranges.source_kind: {current}"
            )
            if source_kind == "view":
                sources.append(strict_text(
                    source_view, field=f"context_view_ranges.source_view_id: {current}"
                ))
            elif source_kind == "messages":
                start_value = strict_non_negative_int(start, field=f"context view range start: {current}")
                end_value = strict_non_negative_int(end, field=f"context view range end: {current}")
                if start_value == 0 or end_value < start_value:
                    raise RuntimeError(
                        f"context view range 不能反向: view={current}, start={start_value}, end={end_value}"
                    )
            else:
                raise RuntimeError(
                    f"context view range source_kind 非法: view={current}, source_kind={source_kind}"
                )
        if view_kind in {"checkpoint", "checkpoint_messages"} and parent_view_id is not None and parent_view_id not in sources:
            raise FormatDispatchError(
                f"v2_migration_required: checkpoint view 缺少显式 parent range: view_id={current}"
            )
        pending.append((current, True))
        pending.extend((source, False) for source in reversed(sources))
    # range 保留来源证据；前缀 fork 会合法裁去未选正文，不能要求原范围
    # 在当前库连续存在。实际读取按冻结 membership 定位并校验本地正文。


def _validate_context_view_orders(connection: sqlite3.Connection) -> None:
    """拒绝缺失、重复或互相冲突的既有 view membership 坐标。"""
    # 一次审计全部 view，避免 initialize 被写入方反复调用时逐 view 重做查询。
    # 普通分页只检查消费到的成员和顺序；未读旧历史留到恢复或显式完整校验。
    invalid_coordinate = connection.execute(
        "SELECT view_id, item_id FROM context_view_items "
        "WHERE (typeof(logical_item_ordinal) != 'integer' OR logical_item_ordinal < 0) "
        "LIMIT 1",
    ).fetchone()
    if invalid_coordinate is not None:
        raise RuntimeError(
            "context view projection integrity error: logical_item_ordinal 非法 "
            f"view_id={invalid_coordinate[0]}, item_id={invalid_coordinate[1]}"
        )

    duplicate_coordinate = connection.execute(
        "SELECT view_id, logical_item_ordinal FROM context_view_items "
        "GROUP BY view_id, logical_item_ordinal HAVING COUNT(*) > 1 LIMIT 1",
    ).fetchone()
    if duplicate_coordinate is not None:
        raise RuntimeError(
            "context view projection integrity error: logical_item_ordinal 重复 "
            f"view_id={duplicate_coordinate[0]}, ordinal={duplicate_coordinate[1]}"
        )

    unknown_membership = connection.execute(
        "SELECT item_id, source_kind FROM context_view_items "
        "WHERE source_kind NOT IN ('canonical', 'display_only') LIMIT 1",
    ).fetchone()
    if unknown_membership is not None:
        raise RuntimeError(
            "context view item source_kind 未注册: "
            f"{strict_text(unknown_membership[1], field='context_view_items.source_kind')}"
        )

    missing_membership = connection.execute(
        "SELECT cvt.view_id, cvt.turn_id FROM context_view_turns AS cvt "
        "JOIN turn_records AS canonical_turn ON canonical_turn.turn_id = cvt.turn_id "
        "JOIN item_catalog AS root ON root.item_id = canonical_turn.root_input_item_id "
        "LEFT JOIN context_view_items AS membership "
        "ON membership.view_id = cvt.view_id AND membership.item_id = root.item_id "
        "WHERE root.turn_id = canonical_turn.turn_id "
        "AND root.semantic_kind = 'user_input' AND root.turn_scope = 'turn_root' "
        "AND root.status = 'completed' AND membership.item_id IS NULL LIMIT 1",
    ).fetchone()
    if missing_membership is not None:
        raise RuntimeError(
            "context view projection integrity error: Turn root 缺少 view membership "
            f"view_id={missing_membership[0]}, turn_id={missing_membership[1]}"
        )

    invalid_root_membership = connection.execute(
        "SELECT cvt.view_id, cvt.turn_id FROM context_view_turns AS cvt "
        "JOIN turn_records AS canonical_turn ON canonical_turn.turn_id = cvt.turn_id "
        "JOIN item_catalog AS root ON root.item_id = canonical_turn.root_input_item_id "
        "JOIN context_view_items AS membership "
        "ON membership.view_id = cvt.view_id AND membership.item_id = root.item_id "
        "WHERE root.turn_id = canonical_turn.turn_id "
        "AND root.semantic_kind = 'user_input' AND root.turn_scope = 'turn_root' "
        "AND root.status = 'completed' AND membership.source_kind != 'canonical' LIMIT 1",
    ).fetchone()
    if invalid_root_membership is not None:
        raise RuntimeError(
            "context view projection integrity error: Turn root 不是 canonical member "
            f"view_id={invalid_root_membership[0]}, turn_id={invalid_root_membership[1]}"
        )

    unordered_roots = connection.execute(
        "SELECT view_id, turn_id, logical_turn_ordinal, root_ordinal, previous_root_ordinal FROM ("
        "SELECT cvt.view_id, cvt.turn_id, cvt.logical_turn_ordinal, membership.logical_item_ordinal AS root_ordinal, "
        "LAG(membership.logical_item_ordinal) OVER (PARTITION BY cvt.view_id ORDER BY cvt.logical_turn_ordinal) AS previous_root_ordinal "
        "FROM context_view_turns AS cvt "
        "JOIN turn_records AS canonical_turn ON canonical_turn.turn_id = cvt.turn_id "
        "JOIN item_catalog AS root ON root.item_id = canonical_turn.root_input_item_id "
        "JOIN context_view_items AS membership "
        "ON membership.view_id = cvt.view_id AND membership.item_id = root.item_id "
        "WHERE cvt.root_input_item_id = canonical_turn.root_input_item_id "
        "AND root.turn_id = canonical_turn.turn_id AND root.semantic_kind = 'user_input' "
        "AND root.turn_scope = 'turn_root' AND root.status = 'completed' "
        "AND membership.source_kind = 'canonical'"
        ") WHERE previous_root_ordinal IS NOT NULL AND root_ordinal <= previous_root_ordinal LIMIT 1",
    ).fetchone()
    if unordered_roots is not None:
        raise RuntimeError(
            "context view projection integrity error: Turn root membership 顺序与 Turn ordinal 冲突 "
            f"view_id={unordered_roots[0]}, turn_id={unordered_roots[1]}, "
            f"turn_ordinal={unordered_roots[2]}, root_ordinal={unordered_roots[3]}, "
            f"previous_root_ordinal={unordered_roots[4]}"
        )
