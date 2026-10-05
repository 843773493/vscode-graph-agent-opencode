"""按已发布 typed 身份排除 provider context 中的 stream shadow。

两个 sink 保留各自的 canonical 事实；去重只消费 model-call、part 坐标和
replaces 关系，不解释 extensions，也不从消息 ID、正文或相邻记录推测归属。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.domain.itemized.enums import SemanticKind
from app.domain.itemized.identity.tool_call_identity import provider_tool_call_id
from app.domain.itemized.records import CanonicalItemRecord


def projection_message_group_id(item: CanonicalItemRecord) -> str:
    """投影按显式模型调用分组；没有调用归属的独立 item 保留物理分组。"""
    identity = item.projection_identity
    if identity is not None and identity.model_call_id is not None:
        return f"message-model-call:{identity.model_call_id}"
    return item.message_group_id or item.item_id


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


def _scope(item: CanonicalItemRecord) -> str | None:
    identity = item.projection_identity
    return identity.model_call_id if identity is not None else None


def _checkpoint_carrier(item: CanonicalItemRecord) -> bool:
    identity = item.projection_identity
    return identity is not None and identity.projection_message_id is not None


def _result_call_id(item: CanonicalItemRecord) -> str:
    evidence = item.tool_result_evidence
    raw_id = evidence.tool_call_id if evidence is not None else item.payload["tool_call_id"]
    return provider_tool_call_id(item.projection_identity, raw_id)


def _confirmed_result(item: CanonicalItemRecord) -> bool:
    evidence = item.tool_result_evidence
    return evidence.execution_confirmed if evidence is not None else item.payload["execution_confirmed"]


def superseded_stream_tool_group_item_ids(
    items: Sequence[CanonicalItemRecord],
) -> set[str]:
    """同一真实 model-call 的完整 checkpoint 工具组替代 stream 工具组。"""
    groups: dict[tuple[str | None, str], list[CanonicalItemRecord]] = {}
    for item in items:
        if item.semantic_kind in {SemanticKind.REASONING, SemanticKind.TOOL_CALL}:
            groups.setdefault((_scope(item), item.message_group_id or item.item_id), []).append(item)
    checkpoint: dict[tuple[str, frozenset[str]], list[CanonicalItemRecord]] = {}
    stream: dict[tuple[str, frozenset[str]], list[CanonicalItemRecord]] = {}
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

    superseded: set[str] = set()
    replaced_calls: set[tuple[str, str]] = set()
    for key in checkpoint.keys() & stream.keys():
        superseded.update(item.item_id for item in stream[key])
        replaced_calls.update((key[0], call_id) for call_id in key[1])
    confirmed_results = {
        (_scope(item), _result_call_id(item))
        for item in items
        if item.semantic_kind == SemanticKind.TOOL_RESULT
        and _checkpoint_carrier(item) and _confirmed_result(item)
    }
    for item in items:
        if item.semantic_kind != SemanticKind.TOOL_RESULT or _checkpoint_carrier(item):
            continue
        scope = _scope(item)
        call = (scope, _result_call_id(item))
        if scope is None or call not in replaced_calls:
            continue
        if call not in confirmed_results:
            raise ValueError(
                "provider projection 过滤将删除唯一的 tool_result carrier: "
                f"model_call_id={scope}, item_id={item.item_id}"
            )
        superseded.add(item.item_id)
    return superseded


def superseded_stream_content_item_ids(
    items: Sequence[CanonicalItemRecord],
) -> set[str]:
    """只按显式 part 三元坐标与替代目标排除旧 assistant carrier。"""
    parts: set[tuple[str, str, int]] = set()
    replaced_messages: set[str] = set()
    for item in items:
        identity = item.projection_identity
        relations = [
            relation for relation in item.provenance_relations
            if relation.relation == "replaces"
            and relation.target_kind == "projection_message"
        ]
        replaced_messages.update(relation.target_id for relation in relations)
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
    return {
        item.item_id for item in items
        if item.semantic_kind in {SemanticKind.ASSISTANT_OUTPUT, SemanticKind.REASONING}
        and item.projection_identity is not None
        and (
            (item.projection_identity.model_call_id,
             item.projection_identity.block_id,
             item.projection_identity.block_index) in parts
            or item.projection_identity.projection_message_id in replaced_messages
        )
    }


def superseded_stream_item_ids(items: Sequence[CanonicalItemRecord]) -> set[str]:
    """返回同一 selection 中必须排除的全部 shadow。"""
    return superseded_stream_tool_group_item_ids(items) | superseded_stream_content_item_ids(items)
