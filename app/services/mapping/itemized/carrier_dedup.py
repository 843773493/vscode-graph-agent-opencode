"""provider context 投影的 carrier 去重规则。

stream sink 与 checkpoint sink 都必须把各自的 canonical 事实写入 rollout，
但 provider context 对同一次模型调用只能发送一个 carrier，否则历史里会出现
两份工具调用/工具输出/思考正文。本模块只按持久身份（``model_call_id``、
``block_id``、``content_part_refs``、``supersedes_message_id``）判断替代关系，
禁止按正文或 hash 猜测等价性，也不做任何 I/O。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast

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


def _explicit_carrier_scope(item: CanonicalItemRecord) -> str | None:
    """返回一次模型响应的显式 carrier scope。

    provider 的原始 ``tool_call_id`` 只在单个 assistant carrier 内有作用域，
    不能拿它作为整个 Turn 的唯一键；部分 provider 会在后续模型请求重新使用
    同一个原始 ID。实时 stream 已保存 ``model_call_id``，checkpoint 的
    LangChain message identity 以 ``lc_run--<model_call_id>`` 保存。两者都
    指向同一个模型调用时才允许互相替代。
    """
    model_call_id = item.metadata.get("model_call_id")
    if isinstance(model_call_id, str) and model_call_id:
        return f"model-call:{model_call_id}"
    projection_message_id = item.metadata.get("projection_message_id")
    if (
        isinstance(projection_message_id, str)
        and projection_message_id
        and projection_message_id.startswith("lc_run--")
    ):
        # LangChain checkpoint message 的 producer identity 是唯一的模型调用
        # 载体；只有带有明确 lc_run 前缀时才可还原为 model call。普通
        # projection message ID 不能冒充跨 sink 的 provenance。
        return f"model-call:{projection_message_id.removeprefix('lc_run--')}"
    return None


def _fallback_carrier_scope(item: CanonicalItemRecord) -> str:
    """为没有 provenance 的旧 carrier 返回不参与跨组配对的 scope。"""
    if item.message_group_id:
        return f"message-group:{item.message_group_id}"
    return f"item:{item.item_id}"


def _tool_result_call_ids(item: CanonicalItemRecord) -> set[str]:
    """返回 tool_result 声明的 provider 工具调用身份集合。

    result 正文优先携带 ``tool_call_id``；旧数据可能只把它写在 metadata。
    """
    payload = item.payload if isinstance(item.payload, Mapping) else {}
    raw_call_id = payload.get("tool_call_id")
    if not isinstance(raw_call_id, str) or not raw_call_id:
        raw_call_id = item.metadata.get("tool_call_id")
    if not isinstance(raw_call_id, str) or not raw_call_id:
        return set()
    return {provider_tool_call_id(item.metadata, raw_call_id)}


def _projection_model_call_id(item: CanonicalItemRecord) -> str | None:
    """从 checkpoint carrier 的显式 projection identity 解析 model call。"""
    model_call_id = item.metadata.get("model_call_id")
    if isinstance(model_call_id, str) and model_call_id:
        return model_call_id
    return _model_call_id_from_projection_message_id(
        item.metadata.get("projection_message_id")
    )


def _model_call_id_from_projection_message_id(value: object) -> str | None:
    """从 LangChain checkpoint message 的稳定 producer ID 解析调用范围。"""
    if isinstance(value, str) and value.startswith("lc_run--"):
        model_call_id = value.removeprefix("lc_run--")
        return model_call_id or None
    return None


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
        # checkpoint 与 stream 即使属于同一 model call，也必须保留为两个
        # 物理 carrier group，后面才能明确选择 checkpoint 并过滤 stream
        # shadow。model_call_id 只作为配对 scope，不能拿来合并两个 source。
        group_id = (
            item.message_group_id
            if _is_confirmed_checkpoint_carrier(item)
            else projection_message_group_id(item)
        )
        if not isinstance(group_id, str) or not group_id:
            group_id = item.item_id
        groups.setdefault((item.turn_id, group_id), []).append(item)

    group_entries: list[dict[str, object]] = []
    for (turn_id, group_id), group_items in groups.items():
        tool_call_ids = frozenset(
            call_id
            for item in group_items
            if item.semantic_kind == SemanticKind.TOOL_CALL
            for call_id in normalized_tool_call_ids(item)
        )
        if not tool_call_ids:
            continue
        explicit_scopes = {
            scope
            for item in group_items
            if (scope := _explicit_carrier_scope(item)) is not None
        }
        if len(explicit_scopes) > 1:
            raise ValueError(
                "provider projection 的工具 carrier 缺少唯一 model-call scope: "
                f"turn_id={turn_id}, scopes={sorted(explicit_scopes)}, "
                f"tool_call_ids={sorted(tool_call_ids)}"
            )
        group_entries.append(
            {
                "turn_id": turn_id,
                "group_id": group_id,
                "items": group_items,
                "tool_call_ids": tool_call_ids,
                "is_checkpoint": any(
                    _is_confirmed_checkpoint_carrier(item)
                    for item in group_items
                ),
                "explicit_scope": next(iter(explicit_scopes), None),
            }
        )

    # 先解析一次 Turn 内的 provenance。原始 provider tool_call_id 只在单次
    # assistant carrier 内有作用域；不能因两个不同 model call 复用了同一个
    # 原始 ID 就误判为重复。旧数据若没有 model_call_id，只在双方各自唯一时
    # 建立显式配对；否则必须报歧义，禁止按正文或时间猜测。
    entries_by_identity: dict[
        tuple[str | None, frozenset[str]], list[dict[str, object]]
    ] = {}
    for entry in group_entries:
        key = (entry["turn_id"], entry["tool_call_ids"])
        entries_by_identity.setdefault(key, []).append(entry)

    for (turn_id, tool_call_ids), entries in entries_by_identity.items():
        explicit_scopes = {
            scope
            for entry in entries
            if (scope := entry["explicit_scope"]) is not None
        }
        unresolved = [
            entry for entry in entries if entry["explicit_scope"] is None
        ]
        if len(explicit_scopes) > 1 and unresolved:
            raise ValueError(
                "provider projection 的工具 carrier 缺少可验证的 model-call scope: "
                f"turn_id={turn_id}, scopes={sorted(explicit_scopes)}, "
                f"tool_call_ids={sorted(tool_call_ids)}"
            )
        if len(explicit_scopes) == 1:
            scope = next(iter(explicit_scopes))
            for entry in unresolved:
                entry["resolved_scope"] = scope
        elif len(unresolved) == 2 and sum(
            bool(entry["is_checkpoint"]) for entry in unresolved
        ) == 1:
            # 仅有一组 live 与一组 checkpoint 时，tool-call identity 本身
            # 足以建立旧数据的唯一配对；这不是正文去重，也不会跨多个调用
            # 猜测归属。
            synthetic_scope = (
                "unscoped-tool-carrier:"
                f"{turn_id}:{','.join(sorted(tool_call_ids))}"
            )
            for entry in unresolved:
                entry["resolved_scope"] = synthetic_scope
        elif unresolved:
            if len(unresolved) > 1:
                raise ValueError(
                    "provider projection 的工具 carrier 缺少唯一 model-call scope: "
                    f"turn_id={turn_id}, tool_call_ids={sorted(tool_call_ids)}"
                )
            unresolved[0]["resolved_scope"] = _fallback_carrier_scope(
                cast(CanonicalItemRecord, unresolved[0]["items"][0])
            )
        for entry in entries:
            scope = entry.get("resolved_scope") or entry["explicit_scope"]
            if not isinstance(scope, str) or not scope:
                raise RuntimeError("工具 carrier scope 解析为空")
            entry["resolved_scope"] = scope

    checkpoint_groups: dict[
        tuple[str | None, str, frozenset[str]], list[CanonicalItemRecord]
    ] = {}
    stream_groups: dict[
        tuple[str | None, str, frozenset[str]], list[CanonicalItemRecord]
    ] = {}
    resolved_item_scopes: dict[str, str] = {}
    for entry in group_entries:
        turn_id = entry["turn_id"]
        group_id = entry["group_id"]
        group_items = entry["items"]
        tool_call_ids = entry["tool_call_ids"]
        scope = entry["resolved_scope"]
        if not isinstance(group_id, str) or not isinstance(scope, str):
            raise TypeError("工具 carrier group/scope identity 非法")
        if not isinstance(group_items, list) or not isinstance(
            tool_call_ids, frozenset
        ):
            raise TypeError("工具 carrier group 解析结果非法")
        registry = (
            checkpoint_groups
            if entry["is_checkpoint"] is True
            else stream_groups
        )
        key = (turn_id, scope, tool_call_ids)
        if key in registry:
            raise ValueError(
                "provider projection 的工具 carrier 身份不唯一: "
                f"turn_id={turn_id}, scope={scope}, "
                f"tool_call_ids={sorted(tool_call_ids)}"
            )
        registry[key] = group_items
        for item in group_items:
            resolved_item_scopes[item.item_id] = scope

    superseded_ids: set[str] = set()
    superseded_stream_call_ids: set[tuple[str | None, str, str]] = set()
    for key, checkpoint_items in checkpoint_groups.items():
        stream_items = stream_groups.get(key)
        if stream_items is None:
            continue
        if not checkpoint_items:
            raise AssertionError("checkpoint tool group 不得为空")
        superseded_ids.update(item.item_id for item in stream_items)
        superseded_stream_call_ids.update(
            (
                item.turn_id,
                resolved_item_scopes.get(
                    item.item_id, _explicit_carrier_scope(item)
                    or _fallback_carrier_scope(item)
                ),
                call_id,
            )
            for item in stream_items
            if item.semantic_kind == SemanticKind.TOOL_CALL
            for call_id in normalized_tool_call_ids(item)
        )
    # stream sink 的 tool_result 不属于 assistant carrier group，但它和被替代
    # 的 stream tool_call 是同一个 canonical call。只过滤这组结果，保留
    # checkpoint 的完整 result carrier，避免历史中出现两份工具输出。
    confirmed_result_call_ids = {
        (item.turn_id, call_id)
        for item in items
        if item.semantic_kind == SemanticKind.TOOL_RESULT
        and item.metadata.get("execution_confirmed") is True
        and isinstance(item.metadata.get("projection_message_id"), str)
        for call_id in _tool_result_call_ids(item)
    }
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
        for call_id in _tool_result_call_ids(item):
            if (
                item.turn_id,
                _explicit_carrier_scope(item) or _fallback_carrier_scope(item),
                call_id,
            ) not in superseded_stream_call_ids:
                continue
            if (item.turn_id, call_id) not in confirmed_result_call_ids:
                # 本次过滤会删掉该调用唯一的 tool_result，且 selection 里
                # 没有任何带投影身份的替代 carrier——产出必然缺失尾部
                # ToolMessage 的非法 wire。禁止静默失败，必须显式报错。
                raise ValueError(
                    "provider projection 过滤将删除唯一的 tool_result carrier: "
                    f"turn_id={item.turn_id}, tool_call_id={call_id}, "
                    f"item_id={item.item_id}"
                )
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
    legacy_referenced_part_ids: set[str] = set()
    scoped_referenced_part_ids: set[tuple[str, str, int | None]] = set()
    superseded_projection_message_ids: set[str] = set()
    projection_model_call_ids: dict[str, set[str]] = {}
    for item in items:
        projection_message_id = item.metadata.get("projection_message_id")
        model_call_id = _projection_model_call_id(item)
        if (
            isinstance(projection_message_id, str)
            and projection_message_id
            and isinstance(model_call_id, str)
        ):
            projection_model_call_ids.setdefault(projection_message_id, set()).add(
                model_call_id
            )
    for item in items:
        refs = item.metadata.get("content_part_refs")
        if isinstance(refs, (list, tuple)):
            carrier_model_call_ids: set[str] = set()
            direct_model_call_id = _projection_model_call_id(item)
            if direct_model_call_id is not None:
                carrier_model_call_ids.add(direct_model_call_id)
            superseded = item.metadata.get("supersedes_message_id")
            if isinstance(superseded, str) and superseded:
                # provider projection 可能只读取最终 carrier 和 stream shadow，
                # 不会把被替代的 checkpoint item 一并放进 selection。此时不能
                # 依赖被替代 item 的 metadata 建 scope；``lc_run--`` 本身就是
                # checkpoint producer 的显式稳定身份。
                superseded_model_call_id = _model_call_id_from_projection_message_id(
                    superseded
                )
                if superseded_model_call_id is not None:
                    carrier_model_call_ids.add(superseded_model_call_id)
                carrier_model_call_ids.update(
                    projection_model_call_ids.get(superseded, set())
                )
            for ref in refs:
                if not isinstance(ref, Mapping):
                    continue
                ref_id = ref.get("id")
                if not isinstance(ref_id, str) or not ref_id:
                    continue
                raw_index = ref.get("index")
                ref_index = (
                    raw_index
                    if isinstance(raw_index, int) and not isinstance(raw_index, bool)
                    else None
                )
                if len(carrier_model_call_ids) == 1:
                    scoped_referenced_part_ids.add(
                        (next(iter(carrier_model_call_ids)), ref_id, ref_index)
                    )
                elif not carrier_model_call_ids:
                    # 没有 model-call scope 时不能猜测同名 provider part 属于
                    # 哪次调用；仅保留旧的无 scope carrier 精确匹配分支。
                    legacy_referenced_part_ids.add(ref_id)
        superseded = item.metadata.get("supersedes_message_id")
        if isinstance(superseded, str) and superseded:
            superseded_projection_message_ids.add(superseded)

    superseded_ids: set[str] = set()
    for item in items:
        if item.semantic_kind not in _ASSISTANT_CONTENT_KINDS:
            continue
        block_id = item.metadata.get("block_id")
        projection_message_id = item.metadata.get("projection_message_id")
        model_call_id = item.metadata.get("model_call_id")
        exact_scoped_match = False
        if isinstance(block_id, str) and isinstance(model_call_id, str):
            # stream canonical item 的 block_id 是
            # ``<model_call_id>:block:<provider_part_id>``，而最终 checkpoint
            # carrier 保存的是该 provider part 的局部 id。这里按生成该身份
            # 的明确格式匹配；不能用 endswith/正文/hash 猜测，否则不同模型
            # 调用复用 provider part id 时会把另一条 assistant 也删掉。
            item_block_index = item.metadata.get("block_index")
            exact_scoped_match = any(
                scoped_model_call_id == model_call_id
                and block_id == f"{scoped_model_call_id}:block:{part_id}"
                and (
                    ref_index is None
                    or ref_index == item_block_index
                )
                for scoped_model_call_id, part_id, ref_index in scoped_referenced_part_ids
            )
        # TODO: 旧 rollout 迁移完成后，删除没有 model_call_id 的无 scope
        # provider carrier 兼容分支；新数据必须使用 scoped block identity。
        legacy_exact_match = (
            isinstance(block_id, str)
            and block_id in legacy_referenced_part_ids
            and not isinstance(model_call_id, str)
        )
        if (exact_scoped_match or legacy_exact_match) or (
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
