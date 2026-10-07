"""复制后 plan 的 canonical manifest 与 target-local identity 本地化。"""

from __future__ import annotations

from collections.abc import Mapping

from app.domain.itemized.hashing import payload_content_length


def localize_plan_value(state, value: object) -> object:
    if isinstance(value, list):
        return [localize_plan_value(state, child) for child in value]
    if not isinstance(value, Mapping):
        return value
    result = {}
    for key, child in value.items():
        if key in {
            "source",
            "extensions",
            "lineage",
            "legacy_source_ref",
            "fork_source_ref",
            "tools",
            "tool_snapshot",
            "body",
        }:
            result[key] = child
        else:
            result[key] = localize_plan_value(state, child)
    if result.get("ref_type") == "canonical_item":
        item = next(
            (
                item
                for item in state.new_items.values()
                if item.item_id == result["ref_id"]
            ),
            None,
        )
        if item is None:
            if result.get("availability") != "available":
                return result
            raise ValueError("source-mismatch: fork canonical ref target item 缺失")
        replacements = {
            "source_revision": (item.source_manifest.source_revision if item.source_manifest else None)
            or f"canonical:{item.item_id}:{item.content_hash}",
            "content_hash": item.content_hash,
            "content_length": payload_content_length(item.payload_kind, item.payload),
        }
        for field, replacement in replacements.items():
            if result.get(field) is not None:
                result[field] = replacement
    if (
        isinstance(result.get("ref"), Mapping)
        and result["ref"].get("ref_type") == "canonical_item"
    ):
        for field in ("source_revision", "content_hash", "content_length"):
            if result.get(field) is not None:
                result[field] = result["ref"][field]
    return result
