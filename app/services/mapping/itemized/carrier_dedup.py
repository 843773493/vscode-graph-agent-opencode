"""按已发布 typed 身份排除 provider context 中的 stream shadow。

两个 sink 保留各自的 canonical 事实；去重只消费 model-call、part 坐标和
replaces 关系，不解释 extensions，也不从消息 ID、正文或相邻记录推测归属。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.domain.itemized.enums import SemanticKind
from app.domain.itemized.identity.tool_call_identity import provider_tool_call_id
from app.domain.itemized.records import CanonicalItemRecord


def projection_message_group_id(item: CanonicalItemRecord) -> tuple[str | None, str | None, str]:
    """只按显式消息归属成组，ModelCall 不替代原消息身份。"""
    identity = item.projection_identity
    return (item.turn_id, identity.model_call_id if identity else None,
            item.message_group_id or item.item_id)


def normalized_tool_call_ids(item: CanonicalItemRecord) -> set[str]:
    """读取 payload 声明的调用身份，并移除同一 typed scope 的前缀。"""
    payload = item.payload if isinstance(item.payload, Mapping) else {}
    calls = payload.get("tool_calls", [payload])
    return {
        provider_tool_call_id(item.projection_identity, call_id)
        for call in calls
        if isinstance(call, Mapping)
        for call_id in (call.get("id") or call.get("tool_call_id"),)
        if isinstance(call_id, str) and call_id
    }


_ToolCarrierScope = tuple[str | None, str]


def _scope(item: CanonicalItemRecord) -> _ToolCarrierScope | None:
    identity = item.projection_identity
    return (item.turn_id, identity.model_call_id) if identity is not None and identity.model_call_id is not None else None


def _checkpoint_carrier(item: CanonicalItemRecord) -> bool:
    identity = item.projection_identity
    return identity is not None and identity.projection_message_id is not None


def _result_call_id(item: CanonicalItemRecord) -> str:
    evidence = item.tool_result_evidence
    raw_id = evidence.tool_call_id if evidence is not None else item.payload["tool_call_id"]
    return provider_tool_call_id(item.projection_identity, raw_id)


def superseded_tool_group_item_ids(
    items: Sequence[CanonicalItemRecord],
) -> set[str]:
    """同 Turn/model-call 的工具组整体择一，保留声明先于真实 terminal 的顺序。"""
    groups: dict[tuple[_ToolCarrierScope | None, str], list[CanonicalItemRecord]] = {}
    for item in items:
        if item.semantic_kind in {SemanticKind.ASSISTANT_OUTPUT, SemanticKind.REASONING, SemanticKind.TOOL_CALL}:
            groups.setdefault((_scope(item), item.message_group_id or item.item_id), []).append(item)
    checkpoint: dict[tuple[_ToolCarrierScope, frozenset[str]], list[CanonicalItemRecord]] = {}
    stream: dict[tuple[_ToolCarrierScope, frozenset[str]], list[CanonicalItemRecord]] = {}
    for group in groups.values():
        call_ids = frozenset(
            call_id
            for item in group if item.semantic_kind == SemanticKind.TOOL_CALL
            for call_id in normalized_tool_call_ids(item)
        )
        if not call_ids:
            continue
        scopes = {_scope(item) for item in group}
        if len(scopes) != 1:
            raise ValueError("provider 工具 carrier 的 typed model-call scope 不一致")
        scope = next(iter(scopes))
        if scope is None:
            # 独立导入记录没有真实 model-call，不能与其它 sink 配对。
            continue
        registry = checkpoint if any(_checkpoint_carrier(item) for item in group) else stream
        key = (scope, call_ids)
        if key in registry:
            raise ValueError(f"provider 工具 carrier 身份不唯一: {key}")
        registry[key] = group

    positions = {item.item_id: index for index, item in enumerate(items)}
    results = [item for item in items if item.semantic_kind == SemanticKind.TOOL_RESULT and item.status == "completed"]
    checkpoint_results = {(_scope(item), _result_call_id(item)): positions[item.item_id]
                          for item in results if _checkpoint_carrier(item)}
    stream_results = {(_scope(item), _result_call_id(item))
                      for item in results if not _checkpoint_carrier(item)}
    superseded: set[str] = set()
    prefer_checkpoint: set[tuple[_ToolCarrierScope, str]] = set()
    prefer_stream: set[tuple[_ToolCarrierScope, str]] = set()
    for key, checkpoint_group in checkpoint.items():
        calls = {(key[0], call_id) for call_id in key[1]}
        last_call = max(positions[item.item_id] for item in checkpoint_group)
        if all(checkpoint_results.get(call, -1) > last_call for call in calls):
            prefer_checkpoint.update(calls)
        elif key in stream:
            # 尚未闭合的 checkpoint shadow 不替代原声明；不能把较晚的
            # call 挪到结果之前，也不能为配对补造工具结果。
            superseded.update(item.item_id for item in checkpoint_group)
            prefer_stream.update(calls)
    # 前次装配可能只留下并行 stream 组的一部分；完整 checkpoint
    # 声明及全部结果已闭合时，同 scope 的剩余子集仍是重复 carrier。
    for (scope, call_ids), group in stream.items():
        if all((scope, call_id) in prefer_checkpoint for call_id in call_ids):
            superseded.update(item.item_id for item in group)
    for item in results:
        call = (_scope(item), _result_call_id(item))
        if (
            not _checkpoint_carrier(item) and call in prefer_checkpoint
            or _checkpoint_carrier(item) and call in prefer_stream and call in stream_results
        ):
            superseded.add(item.item_id)
    return superseded


def superseded_stream_content_item_ids(
    items: Sequence[CanonicalItemRecord],
) -> set[str]:
    """按显式来源排除旧流式块及原消息组，不保留组内思考重影。"""
    parts: set[tuple[str, str, int]] = set()
    replaced_messages: set[tuple[str | None, str | None, str]] = set()
    for item in items:
        group = item.projection_group
        if group is not None and group.ordinal != group.size - 1:
            continue
        identity = item.projection_identity
        relations = [
            relation for relation in item.provenance_relations
            if relation.relation == "replaces"
            and relation.target_kind == "projection_message"
        ]
        replaced_messages.update(
            (item.turn_id, relation.target_model_call_id or (identity.model_call_id if identity else None),
             relation.target_id)
            for relation in relations
        )
        if identity is None:
            continue
        scopes = {relation.target_model_call_id for relation in relations
                  if relation.target_model_call_id is not None}
        if identity.model_call_id is not None:
            scopes.add(identity.model_call_id)
        if identity.content_part_refs and len(scopes) != 1:
            raise ValueError("最终 content part carrier 缺少唯一 typed model-call scope")
        for scope in scopes:
            parts.update((scope, f"{scope}:block:{ref.id}", ref.index)
                         for ref in identity.content_part_refs)
    replaced_groups = {
        projection_message_group_id(item)
        for item in items
        if item.projection_identity is not None
        and (item.turn_id, item.projection_identity.model_call_id,
             item.projection_identity.projection_message_id) in replaced_messages
    }
    return {
        item.item_id for item in items
        if item.semantic_kind in {SemanticKind.ASSISTANT_OUTPUT, SemanticKind.REASONING}
        and item.projection_identity is not None
        and (
            (item.projection_identity.model_call_id,
             item.projection_identity.block_id,
             item.projection_identity.block_index) in parts
            or projection_message_group_id(item) in replaced_groups
        )
    }


def superseded_canonical_message_item_ids(items: Sequence[CanonicalItemRecord]) -> set[str]:
    """显式正文版本先择一，之后才合并实时流与 checkpoint carrier。"""
    by_id = {item.item_id: item for item in items}
    replaced: dict[str, str] = {}
    groups: set[str] = set()
    for item in items:
        group = item.projection_group
        if group is not None and group.ordinal != group.size - 1:
            continue
        relations = [relation for relation in item.provenance_relations
                     if relation.relation == "replaces" and relation.target_kind == "canonical_item"]
        if len(relations) > 1:
            raise ValueError(f"canonical message revision 来源不唯一: {item.item_id}")
        for relation in relations:
            if relation.target_id in replaced and replaced[relation.target_id] != item.item_id:
                raise ValueError(f"canonical message revision 存在分叉: {relation.target_id}")
            replaced[relation.target_id] = item.item_id
            previous = by_id.get(relation.target_id)
            if previous is None:
                # 当前 selection 可以不含源正文；归属与来源存在性由 storage owner 核验。
                continue
            old_identity, new_identity = previous.projection_identity, item.projection_identity
            if (previous.item_sequence >= item.item_sequence
                    or old_identity is None or new_identity is None
                    or old_identity.projection_message_id is None
                    or old_identity.projection_message_id != new_identity.projection_message_id):
                raise ValueError(f"canonical message revision 身份/顺序不一致: {item.item_id}")
            if previous.message_group_id is not None:
                groups.add(previous.message_group_id)
    return set(replaced) & set(by_id) | {
        item.item_id for item in items if item.message_group_id in groups
    }


def superseded_item_ids(items: Sequence[CanonicalItemRecord]) -> set[str]:
    """先按精确版本排除旧正文，再择一 stream/checkpoint shadow。"""
    replaced = superseded_canonical_message_item_ids(items)
    selected = tuple(item for item in items if item.item_id not in replaced)
    return replaced | superseded_tool_group_item_ids(selected) | superseded_stream_content_item_ids(selected)


def select_item_carriers(items: Sequence[CanonicalItemRecord]) -> tuple[CanonicalItemRecord, ...]:
    """保留实际上下文载体及原有顺序，物理历史中的 shadow 仍保持不变。"""
    superseded = superseded_item_ids(items)
    return tuple(item for item in items if item.item_id not in superseded)
