"""v2 catalog 的 Turn activity 与 final reasoning 归并（由 turn_projection_reads 复用）。

纯函数：只接收已 fetch 的行与中间结构，不持有连接、不触碰 self。
"""

from __future__ import annotations

from app.domain.itemized.identity.item_projection import (
    ItemProjectionIdentity,
    ProjectionGroup,
)
from app.domain.itemized.records import parse_item_projection_manifest
from app.services.infrastructure.rollout_context.storage.catalog.turn_projection_helpers import (
    _authoritative_tool_coordinates,
    _final_reasoning_source_refs,
    _finalize_activity_projection,
    _json_object,
    _logical_activity_key,
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
) -> tuple[dict[str, set[int]], dict[str, set[tuple[str | None, str]]]]:
    """归并 canonical activity item，返回 shadow 索引与 reasoning 源引用表。"""

    tool_by_message: dict[tuple[str, str], list[dict[str, object]]] = {}
    tool_by_call_id: dict[tuple[str, str | None, str], dict[str, object]] = {}
    tool_by_model_call_index: dict[tuple[str, str, int], dict[str, object]] = {}
    canonical_tools_by_model_call: dict[
        tuple[str, str], list[dict[str, object]]
    ] = {}
    for (
        turn_id,
        message_id,
        call_id,
        name,
        status,
        result_sequence,
        assistant_sequence,
        call_index,
        tool_projection_manifest_json,
    ) in tool_rows:
        turn_id = strict_text(turn_id, field="messages.turn_id")
        message_id = strict_text(message_id, field="messages.message_id")
        call_id = strict_text(call_id, field=f"tool_calls.tool_call_id:{turn_id}")
        tool = {
            "tool_call_id": call_id,
            "tool_name": strict_text(name, field=f"tool_calls.tool_name:{call_id}"),
            "status": strict_text(status, field=f"tool_calls.status:{call_id}"),
            "result_message_sequence": strict_optional_non_negative_int(
                result_sequence,
                field=f"tool_calls.result_message_sequence:{call_id}",
            ),
            "assistant_message_sequence": strict_non_negative_int(
                assistant_sequence,
                field=f"tool_calls.assistant_message_sequence:{call_id}",
            ),
            "call_index": strict_non_negative_int(
                call_index, field=f"tool_calls.call_index:{call_id}"
            ),
        }
        tool_manifest = parse_item_projection_manifest(_json_object(tool_projection_manifest_json, field="item_catalog.projection_manifest_json:tool"))
        tool_identity = ItemProjectionIdentity.from_dict(tool_manifest["projection_identity"]) if tool_manifest["projection_identity"] is not None else None
        model_call_id = tool_identity.model_call_id if tool_identity is not None else None
        tool_by_message.setdefault((turn_id, message_id), []).append(tool)
        tool_by_call_id[(turn_id, model_call_id, call_id)] = tool
        tool_by_model_call_index[
            (turn_id, model_call_id, tool["call_index"])
        ] = tool

    canonical_tool_call_ids_by_model_call: dict[
        tuple[str, str, int], str
    ] = {}
    canonical_tool_call_ids_by_raw_id: dict[tuple[str, str | None, str], str | None] = {}
    # 实时消息流的 tool-call 使用 model-call scoped ID；checkpoint shadow
    # 仍携带 provider 原始 ID。先建立 canonical 的 (模型调用, call index)
    # 映射，checkpoint carrier 通过这张表归一化。
    for (
        turn_id,
        _item_sequence,
        _item_id,
        semantic_kind,
        _payload_kind,
        _item_status,
        _item_created_at,
        producer_ref_json,
        projection_manifest_json,
        _content,
        _content_truncated,
        _tool_call_id,
        _logical_item_ordinal,
    ) in activity_rows:
        if semantic_kind != "tool_call":
            continue
        manifest = parse_item_projection_manifest(_json_object(projection_manifest_json, field="item_catalog.projection_manifest_json:canonical-tool"))
        identity = ItemProjectionIdentity.from_dict(manifest["projection_identity"]) if manifest["projection_identity"] is not None else None
        block_id = identity.block_id if identity is not None else None
        if not isinstance(block_id, str) or not block_id:
            continue
        producer_ref = _json_object(
            producer_ref_json, field="item_catalog.producer_ref_json:canonical-tool"
        )
        model_call_id = identity.model_call_id
        block_index = identity.block_index
        if not isinstance(block_index, int) or isinstance(block_index, bool):
            raise TypeError(
                "canonical tool_call 缺少稳定 block_index: "
                f"turn_id={turn_id} block_id={block_id}"
            )
        canonical_tool_call_ids_by_model_call[
            (turn_id, model_call_id, block_index)
        ] = block_id

    canonical_tool_call_keys = {
        (turn_id, model_call_id, call_id)
        for (turn_id, model_call_id, _call_index), call_id
        in canonical_tool_call_ids_by_model_call.items()
    }

    seen_activity: dict[str, set[tuple[object, ...]]] = {
        turn_id: set() for turn_id in result
    }
    seen_reasoning_source_refs: dict[str, set[tuple[str | None, str]]] = {
        turn_id: set() for turn_id in result
    }
    canonical_reasoning_keys: dict[
        str, dict[tuple[str, str, int], tuple[object, ...]]
    ] = {turn_id: {} for turn_id in result}
    shadow_reasoning_indices: dict[str, dict[tuple[str, str, int], int]] = {
        turn_id: {} for turn_id in result
    }
    discarded_activity_indices: dict[str, set[int]] = {
        turn_id: set() for turn_id in result
    }
    for (
        turn_id,
        item_sequence,
        item_id,
        semantic_kind,
        payload_kind,
        item_status,
        item_created_at,
        producer_ref_json,
        projection_manifest_json,
        content,
        content_truncated,
        indexed_tool_call_id,
        logical_item_ordinal,
    ) in activity_rows:
        turn_id = strict_text(turn_id, field="item_catalog.turn_id")
        manifest = parse_item_projection_manifest(_json_object(projection_manifest_json, field=f"item_catalog.projection_manifest_json:{item_id}"))
        identity = ItemProjectionIdentity.from_dict(manifest["projection_identity"]) if manifest["projection_identity"] is not None else None
        projection_group = ProjectionGroup.from_dict(manifest["projection_group"]) if manifest["projection_group"] is not None else None
        producer_ref = _json_object(
            producer_ref_json, field=f"item_catalog.producer_ref_json:{item_id}"
        )
        semantic_kind = strict_text(
            semantic_kind, field=f"item_catalog.semantic_kind:{item_id}"
        )
        source_part_id = identity.block_id if identity is not None else None
        projection_message_id = identity.projection_message_id if identity is not None else None
        activity_model_call_id = identity.model_call_id if identity is not None else None
        matching_tools = (
            tool_by_message.get((turn_id, projection_message_id), [])
            if isinstance(projection_message_id, str)
            else []
        )
        block_ordinal = identity.block_index if identity is not None else None
        if not isinstance(block_ordinal, int) or isinstance(block_ordinal, bool):
            block_ordinal = (
                projection_group.ordinal
                if projection_group is not None
                else 0
            )
        block_ordinal = strict_non_negative_int(
            block_ordinal, field=f"activity_item.block_ordinal:{item_id}"
        )
        activity_tools: list[dict[str, object] | None] = [None]
        if semantic_kind in {"tool_call", "tool_result"}:
            metadata_call_id = indexed_tool_call_id
            block_id = identity.block_id if identity is not None else None
            tool_call_id: str | None = None
            if isinstance(metadata_call_id, str) and metadata_call_id:
                tool_call_id = metadata_call_id
            elif isinstance(block_id, str) and block_id:
                tool_call_id = block_id
            if tool_call_id is not None:
                selected_tool = tool_by_call_id.get((turn_id, activity_model_call_id, tool_call_id))
                if selected_tool is None:
                    selected_tool = tool_by_model_call_index.get(
                        (turn_id, activity_model_call_id, block_ordinal)
                    )
                call_index = (
                    strict_non_negative_int(
                        selected_tool.get("call_index"), field="tool_calls.call_index",
                    )
                    if selected_tool is not None else block_ordinal
                )
                # typed canonical ID 不受独立 message 的局部 call_index 影响；
                # raw carrier 无论是否已有 message projection，都用同一模型
                # 调用的稳定 index 归一化，不能把物理自投影当作逻辑身份。
                tool_key = (turn_id, activity_model_call_id, tool_call_id)
                canonical_tool_call_id = canonical_tool_call_ids_by_raw_id.get(tool_key) or (
                    tool_call_id if tool_key in canonical_tool_call_keys else
                    canonical_tool_call_ids_by_model_call.get(
                        (turn_id, activity_model_call_id, call_index), tool_call_id,
                    )
                )
                selected_tool = tool_by_call_id.get(
                    (turn_id, activity_model_call_id, canonical_tool_call_id),
                    selected_tool,
                )
                if selected_tool is None:
                    if semantic_kind != "tool_call":
                        raise RuntimeError(
                            "canonical tool_result 缺少对应 tool_call: "
                            f"{item_id}"
                        )
                    coordinates = _authoritative_tool_coordinates(
                        tool_by_call_id, turn_id, identity, indexed_tool_call_id or block_id
                    )
                    selected_tool = {
                        "tool_call_id": canonical_tool_call_id,
                        "tool_name": strict_text(
                            content,
                            field=f"item_projections.content:{item_id}",
                        ),
                        "status": strict_text(
                            item_status,
                            field=f"item_catalog.status:{item_id}",
                        ),
                        "result_message_sequence": coordinates.get(
                            "result_message_sequence"
                        ),
                        "assistant_message_sequence": coordinates.get(
                            "assistant_message_sequence"
                        ),
                        "call_index": coordinates.get(
                            "call_index", block_ordinal
                        ),
                    }
                else:
                    selected_tool = {**selected_tool, "tool_call_id": canonical_tool_call_id}
                selected_tool.setdefault("call_index", block_ordinal)
                if semantic_kind == "tool_call":
                    tool_by_call_id[tool_key] = selected_tool
                    tool_by_call_id[(turn_id, activity_model_call_id, canonical_tool_call_id)] = selected_tool
                    canonical_tools_by_model_call.setdefault(
                        (turn_id, activity_model_call_id), []
                    ).append(selected_tool)
                if semantic_kind == "tool_result":
                    selected_tool = {
                        **selected_tool,
                        "status": strict_text(
                            item_status,
                            field=f"item_catalog.status:{item_id}",
                        ),
                        "result_message_sequence": selected_tool.get(
                            "result_message_sequence"
                        ),
                    }
                activity_tools = [selected_tool]
            elif matching_tools:
                # 一个 checkpoint tool_call carrier 可以包含多个 tool_calls；
                # 它们共享物理 item offset，但每个 call 都是独立逻辑 Item。
                # checkpoint message 仍携带 provider 原始 call id，必须先
                # 用同一 model-call 的 call_index 映射回 canonical identity，
                # 否则同一个工具会在历史中同时出现 raw call 和 canonical call。
                normalized_tools: list[dict[str, object]] = []
                model_call_key = (
                    turn_id,
                    activity_model_call_id,
                )
                for matching_tool in matching_tools:
                    canonical_tool_call_id = (
                        canonical_tool_call_ids_by_model_call.get(
                            (
                                *model_call_key,
                                strict_non_negative_int(
                                    matching_tool.get("call_index"),
                                    field="tool_calls.call_index",
                                ),
                            )
                        )
                    )
                    if canonical_tool_call_id is None:
                        normalized_tools.append(matching_tool)
                        continue
                    # raw carrier 复用已选 canonical 配对，不能用自己的物理
                    # message 坐标覆盖该逻辑调用与结果的共同来源。
                    normalized_tool = tool_by_call_id.setdefault(
                        (turn_id, activity_model_call_id, canonical_tool_call_id),
                        {**matching_tool, "tool_call_id": canonical_tool_call_id},
                    )
                    raw_tool_call_id = matching_tool.get("tool_call_id")
                    if isinstance(raw_tool_call_id, str) and raw_tool_call_id:
                        alias_key = (turn_id, activity_model_call_id, raw_tool_call_id)
                        if alias_key not in canonical_tool_call_ids_by_raw_id:
                            canonical_tool_call_ids_by_raw_id[alias_key] = (
                                canonical_tool_call_id
                            )
                        elif (
                            canonical_tool_call_ids_by_raw_id[alias_key]
                            != canonical_tool_call_id
                        ):
                            # 同一 raw ID 映射到多个 model-call 时不能猜测
                            # result 属于哪一次，保持未归一化以避免误合并。
                            canonical_tool_call_ids_by_raw_id[alias_key] = None
                    normalized_tools.append(normalized_tool)
                activity_tools = normalized_tools
            else:
                model_call_tools = canonical_tools_by_model_call.get(
                    (turn_id, activity_model_call_id),
                    [],
                )
                if semantic_kind == "tool_call" and model_call_tools:
                    # ensure_request_items 生成的 checkpoint shadow
                    # 可能只在 typed payload 中保存 call id。其 model-call
                    # provenance 与先到的实时 item 一致，直接复用后端已经
                    # 解析出的逻辑工具身份，不读取正文或依赖相邻关系。
                    activity_tools = list(model_call_tools)
                else:
                    raise RuntimeError(
                        f"canonical {semantic_kind} 缺少稳定 tool_call_id: {item_id}"
                    )
        kind = (
            "text"
            if semantic_kind == "assistant_output"
            else
            "reasoning_summary"
            if semantic_kind == "reasoning" and payload_kind == "summary"
            else "reasoning_encrypted"
            if semantic_kind == "reasoning" and payload_kind in {"opaque", "extension"}
            else semantic_kind
        )
        if (
            semantic_kind == "reasoning"
            and isinstance(source_part_id, str)
            and source_part_id
        ):
            prefix = f"{activity_model_call_id}:block:"
            seen_reasoning_source_refs[turn_id].add((activity_model_call_id, source_part_id.removeprefix(prefix)))
        for activity_tool in activity_tools:
            activity_item: dict[str, object] = {
                "item_id": strict_text(item_id, field="item_catalog.item_id"),
                "item_sequence": strict_non_negative_int(
                    item_sequence, field=f"item_catalog.item_sequence:{item_id}"
                ),
                "logical_item_ordinal": strict_non_negative_int(
                    logical_item_ordinal, field=f"context_view_items.logical_item_ordinal:{item_id}"
                ),
                "part_ordinal": 0,
                "kind": kind,
                "status": strict_text(item_status, field="item_catalog.status"),
                "created_at": strict_text(
                    item_created_at, field=f"item_catalog.created_at:{item_id}"
                ),
                "text": strict_text(
                    content,
                    field=f"item_projections.content:{item_id}",
                    allow_empty=True,
                ),
                "truncated": strict_non_negative_int(
                    content_truncated,
                    field=f"item_projections.content_truncated:{item_id}",
                )
                == 1,
                "producer_ref": producer_ref,
                "model_call_id": activity_model_call_id,
                "block_ordinal": block_ordinal,
                "block_id": (
                    source_part_id
                    if isinstance(source_part_id, str) and source_part_id
                    else None
                ),
                "completion_reason": identity.completion_reason if identity is not None else None,
                "message_sequence": 0,
            }
            if activity_tool is not None:
                activity_item.update(activity_tool)
                activity_item["tool_call_id"] = strict_text(
                    activity_tool.get("tool_call_id"),
                    field="tool_calls.tool_call_id",
                )
                activity_item["part_ordinal"] = strict_non_negative_int(
                    activity_tool.get("call_index"),
                    field="tool_calls.call_index",
                )
            logical_key = _logical_activity_key(activity_item, activity_model_call_id)
            reasoning_block_key = (
                (
                    kind,
                    activity_model_call_id,
                    block_ordinal,
                )
                if semantic_kind == "reasoning"
                else None
            )
            is_checkpoint_reasoning_shadow = (
                semantic_kind == "reasoning"
                and activity_item["block_id"] is None
                and projection_group is not None
                and activity_model_call_id is not None
            )
            if (
                is_checkpoint_reasoning_shadow
                and reasoning_block_key is not None
                and reasoning_block_key in canonical_reasoning_keys[turn_id]
            ):
                continue
            if logical_key in seen_activity[turn_id]:
                continue
            seen_activity[turn_id].add(logical_key)
            items = result[turn_id]["activity_items"]
            if not isinstance(items, list):
                raise TypeError("Turn activity_items projection 必须是列表")
            if (
                is_checkpoint_reasoning_shadow
                and reasoning_block_key is not None
            ):
                shadow_reasoning_indices[turn_id][reasoning_block_key] = len(items)
            elif reasoning_block_key is not None and activity_item["block_id"]:
                canonical_reasoning_keys[turn_id].setdefault(
                    reasoning_block_key,
                    logical_key,
                )
                shadow_index = shadow_reasoning_indices[turn_id].pop(
                    reasoning_block_key,
                    None,
                )
                if shadow_index is not None:
                    discarded_activity_indices[turn_id].add(shadow_index)
            items.append(activity_item)
    return discarded_activity_indices, seen_reasoning_source_refs


def _merge_final_reasoning(
    result: dict[str, dict[str, object]],
    final_reasoning_rows: object,
    seen_reasoning_source_refs: dict[str, set[tuple[str | None, str]]],
) -> None:
    """归并 final checkpoint reasoning 源引用。"""
    for (
        turn_id,
        message_sequence,
        content_block_index,
        item_index,
        carrier_type,
        reasoning_text,
        summary_text,
        signature_present,
        encrypted_length,
        reasoning_item_id,
        item_id,
        item_sequence,
        item_created_at,
        final_item_projection_manifest_json,
        logical_item_ordinal,
    ) in final_reasoning_rows:
        turn_id = strict_text(turn_id, field="turns.turn_id")
        content_block_index = strict_non_negative_int(
            content_block_index,
            field=f"reasoning_blocks.content_block_index:{turn_id}",
        )
        item_index = strict_non_negative_int(
            item_index, field=f"reasoning_blocks.item_index:{turn_id}"
        )
        encrypted_length = strict_optional_non_negative_int(
            encrypted_length,
            field=f"reasoning_blocks.encrypted_length:{turn_id}",
        )
        if reasoning_text:
            kind, text = "reasoning", strict_text(
                reasoning_text, field=f"reasoning_blocks.reasoning_text:{turn_id}"
            )
        elif summary_text:
            kind, text = "reasoning_summary", strict_text(
                summary_text, field=f"reasoning_blocks.summary_text:{turn_id}"
            )
        elif encrypted_length is not None:
            kind, text = "reasoning_encrypted", ""
            result[turn_id]["has_encrypted_reasoning"] = True
        else:
            continue
        signature_value = strict_non_negative_int(
            signature_present,
            field=f"reasoning_blocks.signature_present:{turn_id}",
        )
        if signature_value not in {0, 1}:
            raise RuntimeError(f"reasoning signature 标记非法: {turn_id}")
        final_manifest = parse_item_projection_manifest(_json_object(final_item_projection_manifest_json, field=f"item_catalog.projection_manifest_json:{item_id}"))
        final_identity = ItemProjectionIdentity.from_dict(final_manifest["projection_identity"]) if final_manifest["projection_identity"] is not None else None
        source_refs = _final_reasoning_source_refs(
            final_identity,
            content_block_index=content_block_index,
            item_index=item_index,
            provider_item_id=reasoning_item_id,
        )
        if source_refs & seen_reasoning_source_refs[turn_id]:
            continue
        seen_reasoning_source_refs[turn_id].update(source_refs)
        items = result[turn_id]["activity_items"]
        if not isinstance(items, list):
            raise TypeError("Turn activity_items projection 必须是列表")
        items.append(
            {
                "item_id": strict_text(item_id, field="item_catalog.item_id"),
                "item_sequence": strict_non_negative_int(
                    item_sequence, field="item_catalog.item_sequence"
                ),
                "logical_item_ordinal": strict_non_negative_int(
                    logical_item_ordinal, field=f"context_view_items.logical_item_ordinal:{item_id}"
                ),
                "part_ordinal": content_block_index * 1_000_000 + item_index,
                "kind": kind,
                "status": "completed",
                "created_at": strict_text(
                    item_created_at, field="item_catalog.created_at"
                ),
                "text": text,
                "truncated": False,
                "message_sequence": strict_non_negative_int(
                    message_sequence, field="reasoning_blocks.message_sequence"
                ),
                "content_block_index": content_block_index,
                "item_index": item_index,
                "carrier_type": strict_text(
                    carrier_type, field="reasoning_blocks.carrier_type"
                ),
                "signature_present": signature_value == 1,
            }
        )


def _finalize_projections(
    result: dict[str, dict[str, object]],
    discarded_activity_indices: dict[str, set[int]],
) -> None:
    """剔除 shadow 后排序并生成兼容统计。"""
    for turn_id, projection in result.items():
        activity_items = projection["activity_items"]
        if not isinstance(activity_items, list):
            raise TypeError("Turn activity_items projection 必须是列表")
        discarded_indices = discarded_activity_indices[turn_id]
        if discarded_indices:
            projection["activity_items"] = activity_items = [
                item
                for index, item in enumerate(activity_items)
                if index not in discarded_indices
            ]
        activity_items.sort(
            key=lambda item: (
                strict_non_negative_int(
                    item.get("logical_item_ordinal"), field="activity_item.logical_item_ordinal"
                ),
                strict_non_negative_int(
                    item.get("part_ordinal", 0), field="activity_item.part_ordinal"
                ),
            )
        )
        _finalize_activity_projection(projection)
