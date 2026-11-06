"""Saver-owned selection 的无 I/O校验入口。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from app.domain.itemized.hashing import (
    canonical_json_bytes_and_hash,
    content_hash,
    contribution_content_hash_and_length,
    payload_content_length,
)
from app.domain.itemized.identity.refs import ContextRef, ref_identity
from app.domain.itemized.plans.contributions import ContextContribution
from app.domain.itemized.records import CanonicalItemRecord
from app.domain.itemized.selection import ContextSelectionEntry
from app.domain.itemized.serialization import ordered_selection
from app.services.mapping.itemized.carrier_dedup import (
    superseded_canonical_message_item_ids,
)


def committed_context_refs(
    items: tuple[CanonicalItemRecord, ...],
    *,
    session_id: str,
    thread_id: str,
) -> tuple[ContextRef, ...]:
    """将 Saver 已提交的有序 item 投影为 canonical refs，不读取存储。"""
    replaced = superseded_canonical_message_item_ids(items)
    selected = tuple(item for item in items if item.item_id not in replaced)
    by_message_id: dict[str, CanonicalItemRecord] = {}
    for item in selected:
        identity = item.projection_identity
        message_id = identity.projection_message_id if identity is not None else None
        if message_id is None:
            continue
        if message_id in by_message_id:
            raise RuntimeError(f"canonical message selection 版本歧义: {message_id}")
        by_message_id[message_id] = item
    superseded_message_ids: set[str] = set()
    for item in selected:
        superseded = next(
            (
                relation.target_id
                for relation in item.provenance_relations
                if relation.relation == "replaces"
                and relation.target_kind == "projection_message"
            ),
            None,
        )
        if superseded is None:
            continue
        if not isinstance(superseded, str) or not superseded:
            raise RuntimeError(
                f"canonical item typed replaces relation 非法: {item.item_id}"
            )
        previous = by_message_id.get(superseded)
        if previous is None:
            continue
        if previous.turn_id != item.turn_id:
            raise RuntimeError(
                "canonical assistant supersedes 跨 Turn: "
                f"{previous.item_id}->{item.item_id}"
            )
        if previous.item_sequence >= item.item_sequence:
            raise RuntimeError(
                "canonical assistant supersedes 顺序非法: "
                f"{previous.item_id}->{item.item_id}"
            )
        if superseded in superseded_message_ids:
            raise RuntimeError(f"canonical assistant 被多个消息替代: {superseded}")
        superseded_message_ids.add(superseded)
    return tuple(
        ContextRef.canonical_item(
            item,
            session_id=session_id,
            thread_id=thread_id,
        )
        for item in selected
        if (
            item.projection_identity.projection_message_id
            if item.projection_identity is not None
            else None
        )
        not in superseded_message_ids
    )


def validate_projection_selection(
    entries: Iterable[ContextSelectionEntry],
) -> tuple[ContextSelectionEntry, ...]:
    """确认 projector 使用 sealed plan 的原始 plan ordinal，不重新排序。"""
    result = ordered_selection(entries)
    if not all(isinstance(entry, ContextSelectionEntry) for entry in result):
        raise TypeError("projection selection entry 类型非法")
    return result


def resolve_selected_item(
    entry: ContextSelectionEntry,
    items: Mapping[tuple[str, str, str], CanonicalItemRecord],
) -> CanonicalItemRecord:
    """三投影共用 canonical source 的正文完整性验证。"""
    ref = entry.ref
    item = items.get(ref_identity(ref))
    if item is None:
        raise ValueError(
            f"source-mismatch: context plan canonical ref 缺失: {ref.ref_id}"
        )
    expected_source = (
        (item.source_manifest.source_revision if item.source_manifest is not None else None)
        or f"canonical:{item.item_id}:{item.content_hash}"
    )
    if (
        ref.ref_id != item.item_id
        or ref.semantic_kind != item.semantic_kind
        or ref.payload_kind != item.payload_kind
        or ref.status != item.status
        or ref.source_revision != expected_source
        or ref.item_sequence != item.item_sequence
        or ref.content_hash != item.content_hash
        or item.content_hash != content_hash(item.payload_kind, item.payload)
        or ref.content_length != payload_content_length(item.payload_kind, item.payload)
    ):
        raise ValueError(
            f"source-mismatch: context plan ref 与 canonical item 不一致: {ref.ref_id}"
        )
    return item


def resolve_selected_request_body(
    contributions: Mapping[str, ContextContribution],
    entry: ContextSelectionEntry,
    bodies: Mapping[str, object],
) -> object:
    """contribution-backed 正文只按显式 contribution_id 解析。"""
    ref = entry.ref
    body_key = entry.contribution_id or ref.ref_id
    if body_key not in bodies:
        raise ValueError(f"detail-unavailable: request-only ref 缺失: {ref.ref_id}")
    # 各投影共用本次 plan 的索引，避免每条正文都重新线性扫描全部 contribution。
    contribution = contributions.get(entry.contribution_id) if entry.contribution_id is not None else None
    if entry.contribution_id is not None and contribution is None:
        raise ValueError(
            f"plan-order-integrity: contribution manifest 缺失: {entry.contribution_id}"
        )
    body = bodies[body_key]
    if contribution is not None:
        expected_hash, body_length = contribution_content_hash_and_length(
            contribution.contribution_kind, body,
        )
    else:
        encoded, expected_hash = canonical_json_bytes_and_hash(body)
        body_length = len(encoded)
    if ref.content_hash == expected_hash and ref.payload_kind == "text":
        body_length = payload_content_length("text", body)
    if ref.content_hash != expected_hash or ref.content_length != body_length:
        raise ValueError(f"source-mismatch: request-only ref: {ref.ref_id}")
    return body


__all__ = [
    "committed_context_refs",
    "resolve_selected_item",
    "resolve_selected_request_body",
    "validate_projection_selection",
]
