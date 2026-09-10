"""itemized v2 的无 I/O context projection owner。

本模块只消费 Saver 已提交的 ``ContextRequestPlan``、canonical item 和
request-only body。它不打开 rollout storage、不重新排序 registry，也不把
ToolSetRef 伪造成 message；provider 工具 wire 由 infrastructure bridge 负责。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.enums import SemanticKind
from app.domain.itemized.records import CanonicalItemRecord
from app.domain.itemized.refs import ContextRef, ToolSetRef
from app.domain.itemized.request_plan import ContextContribution, ContextRequestPlan
from app.services.mapping.itemized.selection import (
    resolve_selected_item,
    resolve_selected_request_body,
    validate_projection_selection,
)


def _message_id(item: CanonicalItemRecord) -> str:
    value = item.metadata.get("projection_message_id")
    return value if isinstance(value, str) and value else item.item_id


def _tool_calls(item: CanonicalItemRecord) -> list[dict[str, object]]:
    payload = item.payload if isinstance(item.payload, Mapping) else {}
    raw_calls = payload.get("tool_calls")
    calls = raw_calls if isinstance(raw_calls, list) else [payload]
    result: list[dict[str, object]] = []
    for call in calls:
        if not isinstance(call, Mapping):
            continue
        result.append(
            {
                "id": str(call.get("id") or call.get("tool_call_id") or item.item_id),
                "name": str(call.get("name") or "tool"),
                "args": dict(call.get("args") or {})
                if isinstance(call.get("args"), Mapping)
                else {"raw": call.get("args", "")},
                "type": "tool_call",
            }
        )
    return result


def _tool_call_ids(item: CanonicalItemRecord) -> set[str]:
    return {
        str(call["id"])
        for call in _tool_calls(item)
        if isinstance(call.get("id"), str) and call["id"]
    }


def _superseded_stream_tool_group_item_ids(
    items: Sequence[CanonicalItemRecord],
) -> set[str]:
    """用持久工具身份找出已被完整 checkpoint carrier 替代的 live 组。

    stream sink 与 checkpoint sink 都必须保存自己的 canonical 事实，但 provider
    projection 对同一次模型工具调用只能选择一个 carrier。checkpoint group 是
    LangChain 实际执行输入的完整镜像，live group 可能只有最后一个增量片段；
    因此同一 Turn 内工具调用 ID 集合完全相等时保留 checkpoint group，并排除
    live group。这里禁止按正文/hash 猜测，身份歧义必须直接失败。
    """
    groups: dict[tuple[str | None, str], list[CanonicalItemRecord]] = {}
    for item in items:
        if item.semantic_kind not in {
            SemanticKind.REASONING,
            SemanticKind.TOOL_CALL,
        }:
            continue
        group_id = item.message_group_id or item.item_id
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
            for call_id in _tool_call_ids(item)
        )
        if not tool_call_ids:
            continue
        is_checkpoint_group = any(
            item.metadata.get("execution_confirmed") is True
            and (
                isinstance(item.metadata.get("projection_group"), Mapping)
                or isinstance(item.metadata.get("projection_message_id"), str)
            )
            for item in group_items
        )
        registry = checkpoint_groups if is_checkpoint_group else stream_groups
        key = (turn_id, tool_call_ids)
        if key in registry:
            raise ValueError(
                "provider projection 的工具 carrier 身份不唯一: "
                f"turn_id={turn_id}, tool_call_ids={sorted(tool_call_ids)}"
            )
        registry[key] = group_items

    superseded_ids: set[str] = set()
    for key, checkpoint_items in checkpoint_groups.items():
        stream_items = stream_groups.get(key)
        if stream_items is None:
            continue
        if not checkpoint_items:
            raise AssertionError("checkpoint tool group 不得为空")
        superseded_ids.update(item.item_id for item in stream_items)
    return superseded_ids


def _item_content(item: CanonicalItemRecord) -> object:
    if item.semantic_kind != SemanticKind.REASONING:
        return item.payload
    block = {
        "type": "reasoning",
        "text": item.payload.get("content", "")
        if isinstance(item.payload, Mapping)
        else item.payload,
        "item_id": item.item_id,
    }
    return [block]


def _message_content(value: object) -> object:
    """把 canonical payload 转成 LangChain 接受的 content 形态。

    LangChain 的消息模型接受字符串或 block 数组，不接受裸 JSON object。
    canonical item 仍保留原始结构；这里只做临时 projection，不能反写
    canonical 或 SQLite 正文。
    """
    if isinstance(value, Mapping):
        return [dict(value)]
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, (str, list)):
        return value
    return str(value)


def project_canonical_items(
    items: Sequence[CanonicalItemRecord],
    *,
    include_runtime_notices: bool = False,
    include_summaries: bool = False,
    capability_losses: list[str] | None = None,
    preserve_order: bool = False,
) -> list[BaseMessage]:
    """按 canonical item 顺序生成 LangChain 临时 message view。"""
    messages: list[BaseMessage] = []
    message_groups: dict[str, int] = {}
    ordered = (
        items if preserve_order else sorted(items, key=lambda item: item.item_sequence)
    )
    superseded_stream_item_ids = _superseded_stream_tool_group_item_ids(ordered)
    for item in ordered:
        if item.item_id in superseded_stream_item_ids:
            continue
        if item.semantic_kind == SemanticKind.REASONING and item.payload_kind in {
            "opaque",
            "extension",
        }:
            if capability_losses is not None:
                capability_losses.append(
                    f"{item.item_id}:{item.semantic_kind}/{item.payload_kind}"
                )
            continue
        if item.semantic_kind == SemanticKind.USER_INPUT:
            messages.append(
                HumanMessage(
                    content=_message_content(item.payload), id=_message_id(item)
                )
            )
            continue
        if item.semantic_kind == SemanticKind.TOOL_RESULT:
            payload = item.payload if isinstance(item.payload, Mapping) else {}
            tool_status = (
                "error"
                if payload.get("tool_outcome") in {"failure", "cancelled"}
                else "success"
            )
            messages.append(
                ToolMessage(
                    content=_message_content(payload.get("content", item.payload)),
                    id=_message_id(item),
                    tool_call_id=str(payload.get("tool_call_id") or "unknown-call"),
                    name=str(payload.get("name") or "tool"),
                    status=tool_status,
                )
            )
            continue
        if item.semantic_kind == SemanticKind.RUNTIME_NOTICE:
            if not include_runtime_notices:
                continue
            messages.append(
                HumanMessage(
                    content=_message_content(item.payload),
                    id=_message_id(item),
                    response_metadata={
                        "internal": True,
                        "message_metadata": {
                            "turn_scope": item.turn_scope,
                            "item_id": item.item_id,
                        },
                    },
                )
            )
            continue
        if item.semantic_kind == SemanticKind.COMPACTION_SUMMARY:
            if not include_summaries:
                continue
            messages.append(
                HumanMessage(
                    content=_message_content(item.payload),
                    id=_message_id(item),
                    response_metadata={"internal": True, "compaction_summary": True},
                )
            )
            continue
        if item.semantic_kind == SemanticKind.TOOL_CALL:
            group = item.message_group_id or item.item_id
            calls = _tool_calls(item)
            current_index = message_groups.get(group)
            if (
                current_index == len(messages) - 1
                and current_index is not None
                and isinstance(messages[current_index], AIMessage)
            ):
                current = messages[current_index]
                messages[current_index] = current.model_copy(
                    update={"tool_calls": [*current.tool_calls, *calls]}
                )
            else:
                message_groups[group] = len(messages)
                messages.append(
                    AIMessage(content="", id=_message_id(item), tool_calls=calls)
                )
            continue
        if item.semantic_kind not in {
            SemanticKind.ASSISTANT_OUTPUT,
            SemanticKind.REASONING,
        }:
            if capability_losses is not None:
                capability_losses.append(
                    f"{item.item_id}:{item.semantic_kind}/{item.payload_kind}"
                )
            continue
        group = item.message_group_id or item.item_id
        content = _message_content(_item_content(item))
        current_index = message_groups.get(group)
        if (
            current_index != len(messages) - 1
            or current_index is None
            or not isinstance(messages[current_index], AIMessage)
        ):
            message_groups[group] = len(messages)
            messages.append(AIMessage(content=content, id=_message_id(item)))
            continue
        current = messages[current_index]
        current_content = current.content
        if isinstance(current_content, list):
            next_content: object = [
                *current_content,
                *(content if isinstance(content, list) else [content]),
            ]
        elif isinstance(content, str) and isinstance(current_content, str):
            next_content = current_content + content
        else:
            next_content = [
                current_content,
                *(content if isinstance(content, list) else [content]),
            ]
        messages[current_index] = current.model_copy(update={"content": next_content})
    return messages


def project_context_plan(
    plan: ContextRequestPlan,
    items: Iterable[CanonicalItemRecord],
    *,
    include_runtime_notices: bool = False,
    include_summaries: bool = False,
    capability_losses: list[str] | None = None,
    request_only_content: Mapping[str, object] | None = None,
    include_request_only: bool = True,
) -> list[BaseMessage]:
    """只按 Saver seal 的 selection 生成消息，缺失或 mismatch 立即失败。"""
    if plan.plan_state != "sealed" or plan.assembly_id is None:
        raise ValueError("context plan 未 sealed，不能进入 projector")
    item_values = tuple(items)
    by_id = {item.item_id: item for item in item_values}
    if len(by_id) != len(item_values):
        raise ValueError("canonical item registry 存在重复 item_id")
    ref_registry = {(ref.ref_type, ref.ref_id): ref for ref in plan.refs}
    contribution_registry = {
        contribution.contribution_id: contribution
        for contribution in plan.contributions
    }
    bodies = request_only_content or {}
    messages: list[BaseMessage] = []
    canonical_run: list[CanonicalItemRecord] = []
    system_run: list[object] = []
    system_metadata: list[dict[str, object]] = []

    def flush_system() -> None:
        if not system_run:
            return
        messages.append(
            SystemMessage(
                content=list(system_run),
                id="request-only-system-prompt",
                response_metadata={
                    "request_only": True,
                    "selection_count": len(system_metadata),
                    "selection": tuple(system_metadata),
                },
            )
        )
        system_run.clear()
        system_metadata.clear()

    def flush_canonical() -> None:
        if canonical_run:
            messages.extend(
                project_canonical_items(
                    tuple(canonical_run),
                    include_runtime_notices=include_runtime_notices,
                    include_summaries=include_summaries,
                    capability_losses=capability_losses,
                    preserve_order=True,
                )
            )
            canonical_run.clear()

    selected_items: list[CanonicalItemRecord] = []
    for entry in validate_projection_selection(plan.selection):
        if not entry.included or not isinstance(entry.ref, ContextRef):
            continue
        if entry.ref.ref_type != "canonical_item":
            continue
        item = by_id.get(entry.ref.ref_id)
        if item is None:
            raise ValueError(
                f"source-mismatch: context plan canonical ref 缺失: {entry.ref.ref_id}"
            )
        selected_items.append(item)
    seen: set[tuple[str, str]] = set()
    for entry in plan.selection:
        ref = entry.ref
        identity = (ref.ref_type, ref.ref_id)
        if identity in seen:
            raise ValueError(f"plan-order-integrity: selection 重复: {identity}")
        seen.add(identity)
        if entry.included and capability_losses is not None:
            capability_losses.extend(entry.loss)
        if not entry.included:
            flush_system()
            flush_canonical()
            if capability_losses is not None:
                capability_losses.extend(
                    entry.loss or (entry.omission_reason or "omitted",)
                )
            continue
        if isinstance(ref, ToolSetRef):
            flush_system()
            flush_canonical()
            if entry.availability != "available" or ref.availability != "available":
                raise ValueError(
                    "detail-unavailable: included ToolSetRef 不可用于 projection: "
                    f"{ref.ref_id}"
                )
            if ref.content_hash is None:
                raise ValueError(
                    "detail-unavailable: included ToolSetRef 没有可发送的 manifest: "
                    f"{ref.ref_id}"
                )
            continue
        if ref_registry.get(identity) != ref:
            raise ValueError(f"plan-order-integrity: selection ref 缺失: {ref.ref_id}")
        if ref.ref_type == "canonical_item":
            flush_system()
            item = resolve_selected_item(entry, by_id)
            canonical_run.append(item)
            continue
        if not include_request_only:
            # 默认 Web/history projection 不 materialize request-only 正文。
            # selection 仍已由同一个 sealed plan 校验并确定顺序；这里不能
            # 回退当前 middleware registry，也不能把缺正文伪造成空 system。
            flush_canonical()
            continue
        flush_canonical()
        body = resolve_selected_request_body(plan, entry, bodies)
        contribution = contribution_registry.get(entry.contribution_id)
        if entry.contribution_id is not None and contribution is None:
            raise ValueError(
                "plan-order-integrity: request-only contribution manifest 缺失: "
                f"{entry.contribution_id}"
            )
        system_run.extend(body if isinstance(body, list) else [body])
        entry_metadata = {
            "context_ref_id": ref.ref_id,
            "ref_type": ref.ref_type,
            "session_id": ref.session_id,
            "plan_id": ref.plan_id,
            "source_ref": ref.source_ref.to_dict()
            if isinstance(ref.source_ref, DetailRef)
            else ref.source_ref,
            "source_revision": ref.source_revision,
            "content_hash": ref.content_hash,
            "redacted_stable_digest": ref.redacted_stable_digest,
            "content_length": ref.content_length,
            "detail_ref": entry.detail_ref.to_dict() if entry.detail_ref else None,
            "request_only": True,
            "selection_kind": entry.selection_kind,
            "plan_ordinal": entry.plan_ordinal,
            "assembly_id": entry.assembly_id,
            "contribution_ordinal": entry.contribution_ordinal,
            "loss": entry.loss,
            "visibility": entry.visibility,
            "protection": entry.protection,
            "availability": entry.availability,
            "base_delta_role": entry.base_delta_role,
            "source_overlay_epoch": entry.source_overlay_epoch,
        }
        if contribution is not None:
            entry_metadata.update(
                {
                    "context_contribution_id": contribution.contribution_id,
                    "source_kind": contribution.source_kind,
                    "content_hash": contribution.content_hash,
                }
            )
        system_metadata.append(entry_metadata)
    flush_canonical()
    flush_system()
    return messages


def contribution_metadata(
    contributions: Iterable[ContextContribution],
) -> tuple[Mapping[str, object], ...]:
    return tuple(
        {
            "contribution_id": item.contribution_id,
            "source_kind": item.source_kind,
            "source_revision": item.source_revision,
            "content_hash": item.content_hash,
            "request_only": item.request_only,
        }
        for item in contributions
    )


__all__ = [
    "contribution_metadata",
    "project_canonical_items",
    "project_context_plan",
]
