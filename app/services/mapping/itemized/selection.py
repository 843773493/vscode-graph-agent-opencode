"""Saver-owned selection 的无 I/O校验入口。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from app.domain.itemized.hashing import (
    content_hash,
    contribution_content_hash,
    payload_content_length,
    sha256_jcs,
)
from app.domain.itemized.records import CanonicalItemRecord
from app.domain.itemized.request_plan import ContextRequestPlan
from app.domain.itemized.selection import ContextSelectionEntry
from app.domain.itemized.serialization import ordered_selection


@dataclass(frozen=True, slots=True)
class VerifiedRequestBody:
    """Saver 经 protected detail backend 验证后交付的正文和完整性 token。"""

    body: object
    redacted_stable_digest: str


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
    items: Mapping[str, CanonicalItemRecord],
) -> CanonicalItemRecord:
    """三投影共用 canonical source 的正文完整性验证。"""
    ref = entry.ref
    item = items.get(ref.ref_id)
    if item is None:
        raise ValueError(
            f"source-mismatch: context plan canonical ref 缺失: {ref.ref_id}"
        )
    expected_source = (
        item.metadata.get("source_revision")
        or f"canonical:{item.item_id}:{item.content_hash}"
    )
    if (
        ref.semantic_kind != item.semantic_kind
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
    plan: ContextRequestPlan,
    entry: ContextSelectionEntry,
    bodies: Mapping[str, object],
) -> object:
    """contribution-backed 正文只按显式 contribution_id 解析。"""
    ref = entry.ref
    body_key = entry.contribution_id or ref.ref_id
    if body_key not in bodies:
        raise ValueError(f"detail-unavailable: request-only ref 缺失: {ref.ref_id}")
    contribution = next(
        (
            item
            for item in plan.contributions
            if item.contribution_id == entry.contribution_id
        ),
        None,
    )
    if entry.contribution_id is not None and contribution is None:
        raise ValueError(
            f"plan-order-integrity: contribution manifest 缺失: {entry.contribution_id}"
        )
    supplied = bodies[body_key]
    body = supplied.body if isinstance(supplied, VerifiedRequestBody) else supplied
    expected_hash = (
        contribution_content_hash(contribution.contribution_kind, body)
        if contribution is not None
        else sha256_jcs(body)
    )
    if ref.content_length != payload_content_length(
        ref.payload_kind or "structured_content", body
    ) or (
        expected_hash != ref.content_hash
        if ref.content_hash is not None
        else not isinstance(supplied, VerifiedRequestBody)
        or supplied.redacted_stable_digest != ref.redacted_stable_digest
    ):
        raise ValueError(f"source-mismatch: request-only ref: {ref.ref_id}")
    return body


__all__ = [
    "VerifiedRequestBody",
    "resolve_selected_item",
    "resolve_selected_request_body",
    "validate_projection_selection",
]
