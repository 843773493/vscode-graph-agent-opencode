"""provider context 投影的 carrier 去重规则。

stream sink 与 checkpoint sink 都必须把各自的 canonical 事实写入 rollout，
但 provider context 对同一次模型调用只能发送一个 carrier，否则历史里会出现
两份工具调用/工具输出/思考正文。本模块只按持久身份（``model_call_id``、
``block_id``、``content_part_refs``、``supersedes_message_id``）判断替代关系，
禁止按正文或 hash 猜测等价性，也不做任何 I/O。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.domain.itemized.enums import SemanticKind
from app.domain.itemized.records import CanonicalItemRecord
from app.domain.itemized.tool_call_identity import provider_tool_call_id

_THREADING_KINDS = frozenset({SemanticKind.REASONING, SemanticKind.TOOL_CALL})
_ASSISTANT_CONTENT_KINDS = frozenset(
    {SemanticKind.ASSISTANT_OUTPUT, SemanticKind.REASONING}
)


def projection_message_group_id(item: CanonicalItemRecord) -> str:
    """返回 provider/history projection 使用的单次模型调用分组键。"""
    # TODO: 所有 rollout 都写入 model_call_id 后，可删除旧 message_group_id 回退。
    model_call_id = item.metadata.get("model_call_id")
    if isinstance(model_call_id, str) and model_call_id:
        return f"message-model-call:{model_call_id}"
    return item.message_group_id or item.item_id


def _raw_tool_call_ids(item: CanonicalItemRecord) -> set[str]:
    payload = item.payload if isinstance(item.payload, Mapping) else {}
    raw_calls = payload.get("tool_calls")
    calls = raw_calls if isinstance(raw_calls, list) else [payload]
    return {
        call_id
        for call in calls
        if isinstance(call, Mapping)
        for call_id in (call.get("id") or call.get("tool_call_id"),)
        if isinstance(call_id, str) and call_id
    }


def normalized_tool_call_ids(item: CanonicalItemRecord) -> set[str]:
    """返回 item 声明的 provider 工具调用身份集合。"""
    return {
        provider_tool_call_id(item.metadata, call_id)
        for call_id in _raw_tool_call_ids(item)
    }


def _is_confirmed_checkpoint_carrier(item: CanonicalItemRecord) -> bool:
    return item.metadata.get("execution_confirmed") is True and (
        isinstance(item.metadata.get("projection_group"), Mapping)
        or isinstance(item.metadata.get("projection_message_id"), str)
    )


def superseded_stream_tool_group_item_ids(
    items: Sequence[CanonicalItemRecord],
) -> set[str]:
    """找出已被完整 checkpoint carrier 替代的 live 工具调用组。

    stream sink 与 checkpoint sink 都必须保存自己的 canonical 事实，但 provider
    projection 对同一次模型工具调用只能选择一个 carrier。checkpoint group 是
    LangChain 实际执行输入的完整镜像，live group 可能只有最后一个增量片段；
    因此同一 Turn 内 provider 工具调用身份集合完全相等时保留 checkpoint
    group，并排除 live group。stream 的 scoped ID 会先按显式
    ``model_call_id`` 还原为 provider ID；这里禁止按正文/hash 猜测。
    """
    groups: dict[tuple[str | None, str], list[CanonicalItemRecord]] = {}
    for item in items:
        if item.semantic_kind not in _THREADING_KINDS:
            continue
        # TODO: 旧 rollout 迁移完成后，删除以下 message_group_id 兼容说明和回退。
        # 新数据的 stream group 已按 model_call_id 写入。读取旧数据时，
        # stream sink 曾经把同一 execution 内的多个 model call 共用一个
        # message_group_id；优先使用持久化的 model_call_id 拆回正确粒度，
        # 让已有 rollout 也能与各自的 checkpoint carrier 去重。
        group_id = projection_message_group_id(item)
        groups.setdefault((item.turn_id, group_id), []).append(item)

    checkpoint_groups: dict[
        tuple[str | None, frozenset[str]], list[CanonicalItemRecord]
    ] = {}
    stream_groups: dict[
        tuple[str | None, frozenset[str]], list[CanonicalItemRecord]
    ] = {}
    for (turn_id, _group_id), group_items in groups.items():
        tool_call_ids = frozenset(
            call_id
            for item in group_items
            if item.semantic_kind == SemanticKind.TOOL_CALL
            for call_id in normalized_tool_call_ids(item)
        )
        if not tool_call_ids:
            continue
        registry = (
            checkpoint_groups
            if any(_is_confirmed_checkpoint_carrier(item) for item in group_items)
            else stream_groups
        )
        key = (turn_id, tool_call_ids)
        if key in registry:
            raise ValueError(
                "provider projection 的工具 carrier 身份不唯一: "
                f"turn_id={turn_id}, tool_call_ids={sorted(tool_call_ids)}"
            )
        registry[key] = group_items

    superseded_ids: set[str] = set()
    superseded_stream_call_ids: set[tuple[str | None, str]] = set()
    for key, checkpoint_items in checkpoint_groups.items():
        stream_items = stream_groups.get(key)
        if stream_items is None:
            continue
        if not checkpoint_items:
            raise AssertionError("checkpoint tool group 不得为空")
        superseded_ids.update(item.item_id for item in stream_items)
        superseded_stream_call_ids.update(
            (item.turn_id, call_id)
            for item in stream_items
            if item.semantic_kind == SemanticKind.TOOL_CALL
            for call_id in normalized_tool_call_ids(item)
        )
    # stream sink 的 tool_result 不属于 assistant carrier group，但它和被替代
    # 的 stream tool_call 是同一个 canonical call。只过滤这组结果，保留
    # checkpoint 的完整 result carrier，避免历史中出现两份工具输出。
    for item in items:
        if item.semantic_kind != SemanticKind.TOOL_RESULT:
            continue
        if (
            item.metadata.get("execution_confirmed") is True
            and isinstance(item.metadata.get("projection_message_id"), str)
        ):
            # checkpoint 的结果是被保留的完整 carrier，不能和 stream shadow
            # 一起过滤；这里只处理没有 projection message 身份的实时结果。
            continue
        payload = item.payload if isinstance(item.payload, Mapping) else {}
        raw_call_id = payload.get("tool_call_id")
        if not isinstance(raw_call_id, str) or not raw_call_id:
            raw_call_id = item.metadata.get("tool_call_id")
        if not isinstance(raw_call_id, str) or not raw_call_id:
            continue
        if (item.turn_id, provider_tool_call_id(item.metadata, raw_call_id)) in (
            superseded_stream_call_ids
        ):
            superseded_ids.add(item.item_id)
    return superseded_ids


def superseded_stream_content_item_ids(
    items: Sequence[CanonicalItemRecord],
) -> set[str]:
    """按最终 assistant carrier 的持久 part 引用排除流式 shadow。

    provider stream 会先把模型调用的 reasoning/text 作为 canonical item 写入，
    checkpoint 在模型调用结束后又会把同一组 content parts 携带在最终
    assistant carrier 中写入。两者都必须保留给历史视图，但 provider context
    只能发送一次。这里使用 ``block_id`` 与 ``content_part_refs`` 的显式身份
    关系，不按正文或消息组猜测等价性。
    """
    referenced_part_ids: set[str] = set()
    superseded_projection_message_ids: set[str] = set()
    for item in items:
        refs = item.metadata.get("content_part_refs")
        if isinstance(refs, (list, tuple)):
            referenced_part_ids.update(
                ref_id
                for ref in refs
                if isinstance(ref, Mapping)
                for ref_id in (ref.get("id"),)
                if isinstance(ref_id, str) and ref_id
            )
        superseded = item.metadata.get("supersedes_message_id")
        if isinstance(superseded, str) and superseded:
            superseded_projection_message_ids.add(superseded)

    superseded_ids: set[str] = set()
    for item in items:
        if item.semantic_kind not in _ASSISTANT_CONTENT_KINDS:
            continue
        block_id = item.metadata.get("block_id")
        projection_message_id = item.metadata.get("projection_message_id")
        if (isinstance(block_id, str) and block_id in referenced_part_ids) or (
            isinstance(projection_message_id, str)
            and projection_message_id in superseded_projection_message_ids
        ):
            superseded_ids.add(item.item_id)
    return superseded_ids


def superseded_stream_item_ids(
    items: Sequence[CanonicalItemRecord],
) -> set[str]:
    """返回 provider context 必须跳过的全部 stream shadow item。"""
    return superseded_stream_tool_group_item_ids(
        items
    ) | superseded_stream_content_item_ids(items)
