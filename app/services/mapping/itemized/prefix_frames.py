"""封存 prefix 与最终 Provider 消息的无 I/O 联校。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from itertools import groupby

from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
    SystemMessage,
    message_to_dict,
    messages_from_dict,
)

from app.domain.itemized.epoch.prefix_epoch import (
    AppendedItemRef,
    ProviderProfileItemFrame,
)
from app.domain.itemized.epoch.seal_binding import ContextSealBinding
from app.domain.itemized.epoch.source_protocol import (
    SourceReplacementIndex,
    parse_source_blocks,
)
from app.domain.itemized.hashing import canonical_json_bytes, contribution_content_hash
from app.domain.itemized.identity.refs import selection_ref_identity
from app.domain.itemized.plans.request import ContextRequestPlan
from app.domain.itemized.records import CanonicalItemRecord
from app.services.mapping.itemized.canonical_langchain import (
    project_canonical_items,
    project_summary_source,
    summary_retry_variants,
    summary_source_groups,
    validate_summary_source,
)
from app.services.mapping.itemized.selection import (
    resolve_selected_item,
    resolve_selected_request_body,
    validate_projection_selection,
)


def project_canonical_prefix_frames(
    items: Sequence[CanonicalItemRecord],
    *,
    provider_profile: str,
    start_ordinal: int,
) -> tuple[tuple[ProviderProfileItemFrame, ...], tuple[str, ...]]:
    """按 typed assistant 分组映射 frame；user/tool/source 保持独立。"""
    def group_key(item: CanonicalItemRecord) -> tuple[str, str]:
        if item.semantic_kind in {"assistant_output", "reasoning", "tool_call"}:
            return "assistant", item.message_group_id or item.item_id
        return "item", item.item_id

    frames: list[ProviderProfileItemFrame] = []
    selected_ids: list[str] = []
    for _key, values in groupby(items, key=group_key):
        group = tuple(values)
        losses: list[str] = []
        messages = project_canonical_items(
            group, include_runtime_notices=True, include_summaries=True,
            capability_losses=losses, preserve_order=True,
        )
        if losses:
            raise RuntimeError("provider-history-projection-unsupported: " + ",".join(losses))
        selected_ids.extend(item.item_id for item in group)
        for message in messages:
            frames.append(ProviderProfileItemFrame(
                start_ordinal + len(frames) + 1,
                provider_profile,
                {"human": "user", "ai": "assistant", "tool": "tool"}[message.type],
                "structured_content",
                {"item_ids": [item.item_id for item in group], "message": message_to_dict(message)},
                AppendedItemRef("canonical_item", group[0].item_id),
            ))
    return tuple(frames), tuple(selected_ids)


def project_sealed_prefix(
    plan: ContextRequestPlan,
    binding: ContextSealBinding,
    items: Mapping[tuple[str, str, str], CanonicalItemRecord],
    bodies: Mapping[str, object],
) -> list[BaseMessage]:
    """每一 wire frame 都必须对应同一 assembly 的真实 canonical/source selection。"""
    if binding.prefix.assembly_id != plan.assembly_id:
        raise ValueError("source-mismatch: prefix 与 sealed plan assembly 不一致")
    if any(entry.included and entry.ref.ref_type == "canonical_item"
           and entry.ref.thread_id != plan.thread_id for entry in plan.selection):
        raise ValueError("source-mismatch: stable prefix 不得跨 thread 拼接 canonical refs")
    entries = {
        selection_ref_identity(entry.ref): entry for entry in validate_projection_selection(plan.selection)
        if entry.included and entry.ref.ref_type != "tool_set"
    }
    contributions = {source.contribution_id: source for source in plan.contributions}
    canonical_entries = {selection_ref_identity(entry.ref): entry for entry in plan.selection if entry.ref.ref_type == "canonical_item"}
    used: set[tuple[str, ...]] = set()
    root = binding.root
    if root is not None:
        blocks = parse_source_blocks(root.content, expected_version=root.source_protocol) if root.source_protocol is not None else ()
        if blocks and len(blocks) != len(root.sources):
            raise ValueError("source-mismatch: compiled root 来源块数量不一致")
        root_bodies = []
        for index, provenance in enumerate(root.sources):
            matches = [
                entry for entry in entries.values()
                if entry.ref.ref_type == "request_only"
                and entry.ref.source_revision == provenance.revision
                and (source := contributions.get(entry.contribution_id)) is not None
                and source.source_binding is not None
                and source.source_binding.source_ref == provenance.source_identity
                and source.root_placement == "root_eligible"
                and source.contribution_kind == "prompt"
            ]
            if len(matches) != 1:
                raise ValueError("source-mismatch: compiled root 缺少唯一来源 selection")
            entry = matches[0]
            body = resolve_selected_request_body(plan, entry, bodies)
            if not isinstance(body, str) or contribution_content_hash("root_source", body) != provenance.content_hash:
                raise ValueError("source-mismatch: compiled root 正文与 provenance 不一致")
            if blocks:
                block = blocks[index]
                if (block.source_identity, block.source_kind, block.name, block.revision, block.body,
                    block.root_placement, block.representation) != (
                    provenance.source_identity, provenance.source_kind, provenance.name, provenance.revision,
                    body, "root_eligible", "full",
                ) or block.operation not in {"activate", "restore"}:
                    raise ValueError("source-mismatch: compiled root 来源块与 provenance 不一致")
            root_bodies.append(body)
            used.add(selection_ref_identity(entry.ref))
        if not blocks and "\n\n".join(root_bodies) != root.content:
            raise ValueError("source-mismatch: compiled root 正文与来源顺序不一致")
    replacements = SourceReplacementIndex()
    messages: list[BaseMessage] = []
    groups: dict[tuple[str, ...], tuple[list[dict[str, object]], int]] = {}
    for frame in binding.prefix.state.frames:
        if frame.source_protocol is not None:
            for block in parse_source_blocks(frame.content, expected_version=frame.source_protocol):
                replacements.record(block)
        ref = frame.item_ref
        if frame.role == "system":
            if root is None or frame.content != root.content:
                raise ValueError("source-mismatch: system frame 缺少精确 compiled root")
            messages.append(SystemMessage(content=root.content, id=ref.ref_id))
            continue
        if ref.ref_type == "source_item":
            entry = entries.get(("request_only", ref.ref_id, plan.thread_id))
            if entry is None or entry.ref.ref_type != "request_only":
                raise ValueError("source-mismatch: source frame 缺少独立 user selection")
            source = contributions.get(entry.contribution_id)
            if source is None or source.source_binding is None or (
                source.source_binding.source_ref != ref.source_identity
                or entry.ref.source_revision != ref.source_revision
            ):
                raise ValueError("source-mismatch: source frame identity/revision 不一致")
            body = resolve_selected_request_body(plan, entry, bodies)
            if frame.source_protocol is not None:
                blocks = parse_source_blocks(frame.content, expected_version=frame.source_protocol)
                if len(blocks) != 1:
                    raise ValueError("source-mismatch: 单来源 frame 必须封存一个来源块")
                block = blocks[0]
                if (block.source_identity, block.source_kind, block.revision,
                    block.source_tracking_at_seal, block.root_placement) != (
                    ref.source_identity, source.source_kind, ref.source_revision,
                    ref.source_tracking_at_seal, source.root_placement,
                ) or canonical_json_bytes(block.body) != canonical_json_bytes(body):
                    raise ValueError("source-mismatch: source 来源块与 sealed 正文或绑定不一致")
            elif canonical_json_bytes(body) != canonical_json_bytes(frame.content):
                raise ValueError("source-mismatch: source wire frame 与 sealed 正文不一致")
            if source.contribution_kind == "model_message":
                if plan.selection_policy != "summary_candidate" or not isinstance(body, Mapping):
                    raise ValueError("source-mismatch: model_message 只能属于摘要候选")
                projected = messages_from_dict([body["message"]])[0]
                if {"human": "user", "ai": "assistant", "tool": "tool"}.get(projected.type) != frame.role:
                    raise ValueError("source-mismatch: 摘要候选消息角色不一致")
                group = []
                for item_id in body["item_ids"]:
                    if ("canonical_item", item_id, plan.thread_id) not in items:
                        raise ValueError("source-mismatch: 摘要候选 canonical 来源已丢失")
                    selected = canonical_entries.get(("canonical_item", item_id, plan.thread_id))
                    if selected is None:
                        raise ValueError("source-mismatch: 摘要候选 canonical 来源没有封存")
                    group.append(resolve_selected_item(selected, items))
                ordinal, separator, phase = source.source_revision.partition(":")
                if not separator or not ordinal.isdecimal() or phase not in {"initial", "tool_free", "visible_text"}:
                    raise ValueError("source-mismatch: 摘要候选重试身份非法")
                retry_items = []
                for item_id in body["retry_source_item_ids"]:
                    selected = canonical_entries.get(("canonical_item", item_id, plan.thread_id))
                    if selected is None:
                        raise ValueError("source-mismatch: 摘要重试中段没有封存 canonical 来源")
                    retry_items.append(resolve_selected_item(selected, items))
                if retry_items and int(ordinal) == 0:
                    raise ValueError("source-mismatch: 初始候选不能声明重试变换来源")
                retry_messages = [project_summary_source(value) for value in summary_source_groups(retry_items).values()]
                variants = summary_retry_variants(retry_messages) if retry_items else {}
                validate_summary_source(projected, group, retry_variants=variants.get(projected.id, ()))
                messages.append(projected)
                used.add(("request_only", ref.ref_id, plan.thread_id))
                continue
            if frame.role != "user":
                raise ValueError("source-mismatch: 普通 source frame 必须为 user")
            wire_body = frame.content
            content = wire_body if isinstance(wire_body, (str, list)) else [wire_body]
            if frame.provider_profile.startswith("chat_completions:") and isinstance(content, list):
                for index, block in enumerate(content):
                    if isinstance(block, str):
                        continue
                    if (
                        not isinstance(block, Mapping)
                        or set(block) != {"type", "text"}
                        or not isinstance(block["type"], str)
                        or block["type"] not in {"text", "input_text", "output_text"}
                        or not isinstance(block["text"], str)
                    ):
                        raise ValueError(
                            "source-mismatch: Chat source frame 包含无法编码的 user text block: "
                            f"{ref.ref_id}[{index}]"
                        )
            messages.append(HumanMessage(
                content=content,
                id=ref.ref_id, response_metadata={"internal": True},
            ))
            used.add(("request_only", ref.ref_id, plan.thread_id))
            continue
        content = frame.content
        if not isinstance(content, Mapping) or set(content) != {"item_ids", "message"}:
            raise ValueError("source-mismatch: canonical wire frame manifest 非法")
        ids = content["item_ids"]
        if not isinstance(ids, list) or not ids or not all(isinstance(identity, str) for identity in ids):
            raise ValueError("source-mismatch: canonical wire frame 缺少 typed item refs")
        key = tuple(ids)
        if ref.ref_id != key[0]:
            raise ValueError("source-mismatch: canonical frame primary ref 不一致")
        if key not in groups:
            selected = []
            for item_id in key:
                entry = entries.get(("canonical_item", item_id, plan.thread_id))
                if entry is None or entry.ref.ref_type != "canonical_item":
                    raise ValueError("source-mismatch: canonical frame source 未被选入")
                selected.append(resolve_selected_item(entry, items))
            losses: list[str] = []
            expected = project_canonical_items(
                selected, include_runtime_notices=True, include_summaries=True,
                capability_losses=losses, preserve_order=True,
            )
            if losses:
                raise ValueError("provider-history-projection-unsupported: " + ",".join(losses))
            groups[key] = ([message_to_dict(message) for message in expected], 0)
        expected, index = groups[key]
        if index >= len(expected) or canonical_json_bytes(content["message"]) != canonical_json_bytes(expected[index]):
            raise ValueError("source-mismatch: canonical wire frame 与真实来源投影不一致")
        projected = messages_from_dict([expected[index]])[0]
        if {"human": "user", "ai": "assistant", "tool": "tool"}[projected.type] != frame.role:
            raise ValueError("source-mismatch: canonical frame wire role 不一致")
        messages.append(projected)
        groups[key] = expected, index + 1
        used.update(("canonical_item", item_id, plan.thread_id) for item_id in key)
    if any(index != len(expected) for expected, index in groups.values()) or used != set(entries):
        raise ValueError("source-mismatch: sealed selection 与最终 wire frames 未完整联校")
    return messages
