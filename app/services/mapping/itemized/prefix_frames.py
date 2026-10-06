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
from app.domain.itemized.hashing import canonical_json_bytes, contribution_content_hash
from app.domain.itemized.records import CanonicalItemRecord
from app.domain.itemized.request_plan import ContextRequestPlan
from app.services.mapping.itemized.canonical_langchain import project_canonical_items
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
    items: Sequence[CanonicalItemRecord],
    bodies: Mapping[str, object],
) -> list[BaseMessage]:
    """每一 wire frame 都必须对应同一 assembly 的真实 canonical/source selection。"""
    if binding.prefix.assembly_id != plan.assembly_id:
        raise ValueError("source-mismatch: prefix 与 sealed plan assembly 不一致")
    entries = {
        entry.ref.ref_id: entry for entry in validate_projection_selection(plan.selection)
        if entry.included and entry.ref.ref_type != "tool_set"
    }
    contributions = {source.contribution_id: source for source in plan.contributions}
    by_id = {item.item_id: item for item in items}
    used: set[str] = set()
    root = binding.root
    if root is not None:
        root_bodies = []
        for provenance in root.sources:
            matches = [
                entry for entry in entries.values()
                if entry.ref.ref_type == "request_only"
                and entry.ref.source_revision == provenance.revision
                and (source := contributions.get(entry.contribution_id)) is not None
                and source.source_binding is not None
                and source.source_binding.source_ref == provenance.source_identity
                and source.root_placement == "root_eligible"
            ]
            if len(matches) != 1:
                raise ValueError("source-mismatch: compiled root 缺少唯一来源 selection")
            entry = matches[0]
            body = resolve_selected_request_body(plan, entry, bodies)
            if not isinstance(body, str) or contribution_content_hash("root_source", body) != provenance.content_hash:
                raise ValueError("source-mismatch: compiled root 正文与 provenance 不一致")
            root_bodies.append(body)
            used.add(entry.ref.ref_id)
        if "\n\n".join(root_bodies) != root.content:
            raise ValueError("source-mismatch: compiled root 正文与来源顺序不一致")
    messages: list[BaseMessage] = []
    groups: dict[tuple[str, ...], tuple[list[dict[str, object]], int]] = {}
    for frame in binding.prefix.state.frames:
        ref = frame.item_ref
        if frame.role == "system":
            if root is None or frame.content != root.content:
                raise ValueError("source-mismatch: system frame 缺少精确 compiled root")
            messages.append(SystemMessage(content=root.content, id=ref.ref_id))
            continue
        if ref.ref_type == "source_item":
            entry = entries.get(ref.ref_id)
            if entry is None or entry.ref.ref_type != "request_only" or frame.role != "user":
                raise ValueError("source-mismatch: source frame 缺少独立 user selection")
            source = contributions.get(entry.contribution_id)
            if source is None or source.source_binding is None or (
                source.source_binding.source_ref != ref.source_identity
                or entry.ref.source_revision != ref.source_revision
            ):
                raise ValueError("source-mismatch: source frame identity/revision 不一致")
            body = resolve_selected_request_body(plan, entry, bodies)
            if canonical_json_bytes(body) != canonical_json_bytes(frame.content):
                raise ValueError("source-mismatch: source wire frame 与 sealed 正文不一致")
            content = body if isinstance(body, (str, list)) else [body]
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
            used.add(ref.ref_id)
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
                entry = entries.get(item_id)
                if entry is None or entry.ref.ref_type != "canonical_item":
                    raise ValueError("source-mismatch: canonical frame source 未被选入")
                selected.append(resolve_selected_item(entry, by_id))
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
        used.update(key)
    if any(index != len(expected) for expected, index in groups.values()) or used != set(entries):
        raise ValueError("source-mismatch: sealed selection 与最终 wire frames 未完整联校")
    return messages
