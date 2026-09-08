"""checkpoint view 的显式 item 顺序适配。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping


def require_explicit_item_order(
    item_ids: Iterable[str],
    *,
    source_ordinals: Mapping[str, int],
) -> tuple[str, ...]:
    """按调用方已冻结的 ordinal 验证 view，不按 dict/created_at 猜顺序。"""
    values = tuple(item_ids)
    if len(values) != len(set(values)):
        raise ValueError("context view item identity 重复")
    missing = [item_id for item_id in values if item_id not in source_ordinals]
    if missing:
        raise ValueError(f"context view 缺少 item ordinal: {missing}")
    ordinals = [source_ordinals[item_id] for item_id in values]
    if ordinals != sorted(ordinals):
        raise ValueError("context view item 必须按已提交 logical ordinal")
    return values


__all__ = ["require_explicit_item_order"]
