"""catalog 的逐 Item Turn activity 归并（由 turn_projection_reads 复用）。

纯函数：只接收已 fetch 的行与中间结构，不持有连接、不触碰 self。
"""

from __future__ import annotations

from app.domain.itemized.identity.item_projection import ItemProjectionManifest
from app.domain.itemized.identity.tool_call_identity import provider_tool_call_id
from app.services.infrastructure.rollout_context.storage.catalog.turn_projection_helpers import (
    _finalize_activity_projection,
    _json_object,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_optional_non_negative_int,
    strict_text,
)


def _merge_activity_items(
    result: dict[str, dict[str, object]],
    tool_rows: object,
    activity_rows: object,
) -> None:
    """每个工具 Item 只投影自身调用，坐标沿精确 ModelCall／调用身份取得。"""
    manifests: dict[tuple[str, str], ItemProjectionManifest] = {}
    tools_by_item: dict[tuple[str, str, str], dict[str, object]] = {}
    tools_by_call: dict[tuple[str, str, str], dict[str, object]] = {}
    for (
        turn_id, anchor_item_id, call_id, name, _status, result_sequence,
        assistant_sequence, call_index, projection_manifest_json,
    ) in tool_rows:
        turn_id = strict_text(turn_id, field="messages.turn_id")
        anchor_item_id = strict_text(anchor_item_id, field="item_catalog.item_id")
        manifest_key = (turn_id, anchor_item_id)
        if manifest_key not in manifests:
            manifests[manifest_key] = ItemProjectionManifest.from_dict(_json_object(
                projection_manifest_json, field="item_catalog.projection_manifest_json:tool",
            ))
        identity = manifests[manifest_key].projection_identity
        call_id = strict_text(call_id, field="tool_calls.tool_call_id")
        raw_call_id = provider_tool_call_id(identity, call_id)
        tool = {
            "tool_name": strict_text(name, field=f"tool_calls.tool_name:{call_id}"),
            "result_message_sequence": strict_optional_non_negative_int(
                result_sequence, field=f"tool_calls.result_message_sequence:{call_id}",
            ),
            "assistant_message_sequence": strict_non_negative_int(
                assistant_sequence, field=f"tool_calls.assistant_message_sequence:{call_id}",
            ),
            "call_index": strict_non_negative_int(call_index, field="tool_calls.call_index"),
        }
        tools_by_item[(turn_id, anchor_item_id, raw_call_id)] = tool
        if identity is not None and identity.model_call_id is not None:
            # 同一声明的 stream／checkpoint carrier 可以共享调用来源，
            # 但只规范化真实 ID，不能用另一个声明的相同 index 建立别名。
            tools_by_call.setdefault((turn_id, identity.model_call_id, raw_call_id), tool)

    for (
        turn_id, item_sequence, item_id, semantic_kind, payload_kind, item_status,
        item_created_at, producer_ref_json, projection_manifest_json, content,
        content_truncated, indexed_tool_call_id, logical_item_ordinal, message_group_id, message_sequence, reasoning_kind,
    ) in activity_rows:
        turn_id = strict_text(turn_id, field="item_catalog.turn_id")
        item_id = strict_text(item_id, field="item_catalog.item_id")
        semantic_kind = strict_text(semantic_kind, field=f"item_catalog.semantic_kind:{item_id}")
        manifest = manifests.get((turn_id, item_id))
        if manifest is None:
            manifest = ItemProjectionManifest.from_dict(_json_object(
                projection_manifest_json, field=f"item_catalog.projection_manifest_json:{item_id}",
            ))
        identity = manifest.projection_identity
        group = manifest.projection_group
        model_call_id = identity.model_call_id if identity is not None else None
        kind = ("content" if manifest.output_block_type is not None else "text") if semantic_kind == "assistant_output" else semantic_kind
        if semantic_kind == "reasoning":
            kind = strict_text(reasoning_kind, field=f"reasoning_blocks.kind:{item_id}")
        activity_item: dict[str, object] = {
            "item_id": item_id,
            "item_sequence": strict_non_negative_int(item_sequence, field="item_catalog.item_sequence"),
            "logical_item_ordinal": strict_non_negative_int(
                logical_item_ordinal, field=f"context_view_items.logical_item_ordinal:{item_id}",
            ),

            "kind": kind,
            "carrier_type": manifest.output_block_type,
            "status": strict_text(item_status, field=f"item_catalog.status:{item_id}"),
            "created_at": strict_text(item_created_at, field=f"item_catalog.created_at:{item_id}"),
            "text": strict_text(content, field=f"item_projections.content:{item_id}", allow_empty=True),
            "truncated": strict_non_negative_int(content_truncated, field="item_projections.content_truncated") == 1,
            "producer_ref": _json_object(producer_ref_json, field=f"item_catalog.producer_ref_json:{item_id}"),
            "model_call_id": model_call_id,
            "message_group": {
                "message_group_id": message_group_id, "ordinal": group.ordinal, "size": group.size,
            } if group is not None else None,
            "block_ordinal": identity.block_index if identity is not None and identity.block_index is not None
                else group.ordinal if group is not None else 0,
            "content_block_index": identity.block_index if identity is not None else None,
            "block_id": identity.block_id if identity is not None else None,
            "completion_reason": identity.completion_reason if identity is not None else None,
            "message_sequence": message_sequence if message_sequence is not None else 0,
        }
        if semantic_kind in {"tool_call", "tool_result"}:
            call_id = strict_text(indexed_tool_call_id, field=f"item_catalog.tool_call_id:{item_id}")
            raw_call_id = provider_tool_call_id(identity, call_id)
            tool = tools_by_item.get((turn_id, item_id, raw_call_id))
            if tool is None and model_call_id is not None:
                tool = tools_by_call.get((turn_id, model_call_id, raw_call_id))
            # 没有声明来源时保留独立回执；不能用 block ID、工具名或本页
            # 唯一候选补配。参数／结果详情仍由原 locator 读取。
            activity_item.update({
                "tool_call_id": call_id,
                "tool_name": tool["tool_name"] if tool is not None else content if semantic_kind == "tool_call" else None,
                "assistant_message_sequence": tool["assistant_message_sequence"] if tool is not None else None,
                "result_message_sequence": message_sequence if semantic_kind == "tool_result"
                    else tool["result_message_sequence"] if tool is not None else None,
                "call_index": tool["call_index"] if tool is not None else None,
            })
        items = result[turn_id]["activity_items"]
        if not isinstance(items, list):
            raise TypeError("Turn activity_items projection 必须是列表")
        items.append(activity_item)


def _finalize_projections(
    result: dict[str, dict[str, object]],
) -> None:
    """按统一坐标排序并派生统计。"""
    for projection in result.values():
        activity_items = projection["activity_items"]
        if not isinstance(activity_items, list):
            raise TypeError("Turn activity_items projection 必须是列表")
        activity_items.sort(
            key=lambda item: strict_non_negative_int(
                item.get("logical_item_ordinal"), field="activity_item.logical_item_ordinal"
            )
        )
        _finalize_activity_projection(projection)
