"""封存 prefix 与最终 Provider 消息的无 I/O 联校。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from itertools import groupby, pairwise

from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
    SystemMessage,
    message_to_dict,
    messages_from_dict,
)

from app.domain.itemized.compaction import CanonicalTextRange, summary_output_text
from app.domain.itemized.dispatch_ref import SealedContextDispatchRef
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
from app.domain.itemized.identity.detail_ref import DetailRef
from app.domain.itemized.identity.refs import selection_ref_identity
from app.domain.itemized.plans.request import ContextRequestPlan
from app.domain.itemized.records import CanonicalItemRecord
from app.domain.itemized.selection import ContextSelectionEntry
from app.domain.itemized.serde.registry import parse_context_ref
from app.services.mapping.itemized.canonical_langchain import (
    project_canonical_items,
    project_derived_summary_message,
)
from app.services.mapping.itemized.carrier_dedup import projection_message_group_id
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
    def group_key(item: CanonicalItemRecord) -> tuple[str, str | None, str | None, str]:
        if item.semantic_kind in {"assistant_output", "reasoning", "tool_call"}:
            return "assistant", *projection_message_group_id(item)
        return "item", None, None, item.item_id

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
                (AppendedItemRef("canonical_item", group[0].item_id),),
            ))
    return tuple(frames), tuple(selected_ids)


def project_text_range(item: CanonicalItemRecord, part: CanonicalTextRange) -> BaseMessage:
    """先按安全 canonical 合同投影，再切纯文本；不拆工具、媒体或受保护载荷。"""
    from app.domain.itemized.identity.refs import ContextRef

    if (part.ref != ContextRef.canonical_item(item, session_id=part.ref.session_id, thread_id=part.ref.thread_id)
            or item.semantic_kind not in {"user_input", "assistant_output"}
            or item.payload_kind != "text" or part.ref.protection != "public"):
        raise ValueError("compaction-text-range-invalid: 只能分片公开的独立纯文本 Item")
    messages = project_canonical_items((item,), include_runtime_notices=True, include_summaries=True, preserve_order=True)
    if len(messages) != 1 or not isinstance(messages[0].content, str) or len(messages[0].content) != part.text_length:
        raise ValueError("compaction-text-range-invalid: 安全投影与冻结文本长度不一致")
    return messages[0].model_copy(update={"content": messages[0].content[part.start:part.end]})


def project_sealed_prefix(
    plan: ContextRequestPlan,
    binding: ContextSealBinding,
    items: Mapping[tuple[str, str, str], CanonicalItemRecord],
    bodies: Mapping[str, object],
) -> list[BaseMessage]:
    """每一 wire frame 都必须对应同一 assembly 的真实 canonical/source selection。"""
    if binding.prefix.assembly_id != plan.assembly_id:
        raise ValueError("source-mismatch: prefix 与 sealed plan assembly 不一致")
    if plan.selection_policy != "derived_request" and any(entry.included and entry.ref.ref_type == "canonical_item"
           and entry.ref.thread_id != plan.thread_id for entry in plan.selection):
        raise ValueError("source-mismatch: stable prefix 不得跨 thread 拼接 canonical refs")
    entries = {
        selection_ref_identity(entry.ref): entry for entry in validate_projection_selection(plan.selection)
        if entry.included and entry.ref.ref_type != "tool_set"
    }
    contributions = {source.contribution_id: source for source in plan.contributions}
    canonical_entries = {selection_ref_identity(entry.ref): entry for entry in plan.selection if entry.ref.ref_type == "canonical_item"}
    canonical_by_id: dict[str, ContextSelectionEntry] = {}
    for entry in entries.values():
        if entry.ref.ref_type == "canonical_item":
            if entry.ref.ref_id in canonical_by_id:
                raise ValueError("source-mismatch: canonical frame item owner 不唯一")
            canonical_by_id[entry.ref.ref_id] = entry
    used: set[tuple[str, ...]] = set()
    root = binding.root
    if root is not None:
        blocks = parse_source_blocks(root.content, expected_version=root.source_protocol) if root.source_protocol is not None else ()
        if blocks and len(blocks) != len(root.sources):
            raise ValueError("source-mismatch: compiled root 来源块数量不一致")
        # root 来源按身份与 revision 一次分组；保留重复项，仍要求唯一 selection。
        root_entries: dict[tuple[str | None, str], list[ContextSelectionEntry]] = {}
        for entry in entries.values():
            source = contributions.get(entry.contribution_id)
            if (entry.ref.ref_type == "request_only" and source is not None
                and source.source_binding is not None
                and source.root_placement == "root_eligible" and source.contribution_kind == "prompt"):
                root_entries.setdefault((source.source_binding.source_ref, entry.ref.source_revision), []).append(entry)
        root_bodies = []
        for index, provenance in enumerate(root.sources):
            matches = root_entries.get((provenance.source_identity, provenance.revision), ())
            if len(matches) != 1:
                raise ValueError("source-mismatch: compiled root 缺少唯一来源 selection")
            entry = matches[0]
            body = resolve_selected_request_body(contributions, entry, bodies)
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
        ref = frame.item_refs[0]
        if frame.role == "system":
            if root is None or frame.content != root.content:
                raise ValueError("source-mismatch: system frame 缺少精确 compiled root")
            messages.append(SystemMessage(content=root.content, id=ref.ref_id,
                                          response_metadata={"context_source_protocol": frame.source_protocol}))
            continue
        if ref.ref_type == "source_item":
            blocks = parse_source_blocks(frame.content, expected_version=frame.source_protocol) if frame.source_protocol is not None else ()
            if blocks and len(blocks) != len(frame.item_refs):
                raise ValueError("source-mismatch: 来源块与有序 item 引用数量不一致")
            validated = []
            for index, member in enumerate(frame.item_refs):
                entry = entries.get(("request_only", member.ref_id, plan.thread_id))
                if entry is None or entry.ref.ref_type != "request_only":
                    raise ValueError("source-mismatch: source frame 缺少独立 user selection")
                source = contributions.get(entry.contribution_id)
                if source is None or source.source_binding is None or (
                    source.source_binding.source_ref != member.source_identity
                    or entry.ref.source_revision != member.source_revision
                ):
                    raise ValueError("source-mismatch: source frame identity/revision 不一致")
                body = resolve_selected_request_body(contributions, entry, bodies)
                if blocks:
                    block = blocks[index]
                    if (block.source_identity, block.source_kind, block.revision,
                        block.source_tracking_at_seal, block.root_placement) != (
                        member.source_identity, source.source_kind, member.source_revision,
                        member.source_tracking_at_seal, source.root_placement,
                    ) or canonical_json_bytes(block.body) != canonical_json_bytes(body):
                        raise ValueError("source-mismatch: source 来源块与 sealed 正文或绑定不一致")
                elif canonical_json_bytes(body) != canonical_json_bytes(frame.content):
                    raise ValueError("source-mismatch: source wire frame 与 sealed 正文不一致")
                used.add(("request_only", member.ref_id, plan.thread_id))
                validated.append((source, body))
            source, body = validated[0]
            if source.contribution_kind == "model_message":
                if plan.selection_policy != "derived_request" or not isinstance(body, Mapping):
                    raise ValueError("source-mismatch: model_message 只能属于摘要候选")
                projected = messages_from_dict([body["message"]])[0]
                if {"human": "user", "ai": "assistant", "tool": "tool"}.get(projected.type) != frame.role:
                    raise ValueError("source-mismatch: 摘要候选消息角色不一致")
                if set(body) != {"message", "result_ref", "output_refs", "dispatch_ref", "coverage"}:
                    raise ValueError("source-mismatch: 合并摘要缺少精确结果 provenance")
                result_ref = DetailRef.from_dict(body["result_ref"])
                dispatch_ref = SealedContextDispatchRef.from_dict(body["dispatch_ref"])
                result_ref.require_owner(plan.session_id, plan.thread_id)
                output_refs = tuple(parse_context_ref(value) for value in body["output_refs"])
                if not output_refs:
                    raise ValueError("source-mismatch: 合并摘要缺少原 canonical outputs")
                outputs = []
                for output_ref in output_refs:
                    output_entry = canonical_entries.get(selection_ref_identity(output_ref))
                    if output_entry is None or output_entry.ref != output_ref:
                        raise ValueError("source-mismatch: 合并摘要没有封存原 canonical output")
                    output = resolve_selected_item(output_entry, items)
                    identity = output.projection_identity
                    if identity is None or identity.model_call_id != dispatch_ref.callback_model_call_id:
                        raise ValueError("source-mismatch: 合并摘要原 output/model-call 不一致")
                    outputs.append(output)
                if (any(left.item_sequence >= right.item_sequence for left, right in pairwise(outputs))
                    or not isinstance(projected, HumanMessage)
                    or message_to_dict(projected) != message_to_dict(project_derived_summary_message(
                        "".join(summary_output_text(output) for output in outputs), message_id=ref.ref_id,
                    ))):
                    raise ValueError("source-mismatch: 合并摘要正文与 canonical output 不一致")
                for value in body["coverage"]:
                    covered = parse_context_ref(value)
                    selected = canonical_entries.get(selection_ref_identity(covered))
                    if selected is None or selected.ref != covered:
                        raise ValueError("source-mismatch: 合并摘要缺少冻结 coverage")
                    resolve_selected_item(selected, items)
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
                id=ref.ref_id, response_metadata={"internal": True, "context_source_protocol": frame.source_protocol},
            ))
            used.add(("request_only", ref.ref_id, plan.thread_id))
            continue
        content = frame.content
        if not isinstance(content, Mapping) or set(content) not in ({"item_ids", "message"}, {"item_ids", "message", "text_range"}):
            raise ValueError("source-mismatch: canonical wire frame manifest 非法")
        ids = content["item_ids"]
        if not isinstance(ids, list) or not ids or not all(isinstance(identity, str) for identity in ids):
            raise ValueError("source-mismatch: canonical wire frame 缺少 typed item refs")
        key = tuple(ids)
        if ref.ref_id != key[0]:
            raise ValueError("source-mismatch: canonical frame primary ref 不一致")
        if "text_range" in content:
            part = CanonicalTextRange.from_dict(content["text_range"])
            entry = canonical_by_id.get(part.ref.ref_id)
            if (plan.selection_policy != "derived_request" or key != (part.ref.ref_id,)
                    or entry is None or entry.ref != part.ref or key in groups):
                raise ValueError("source-mismatch: 文本分片必须对应唯一的派生原件 selection")
            projected = project_text_range(resolve_selected_item(entry, items), part)
            if (message_to_dict(projected) != content["message"]
                    or {"human": "user", "ai": "assistant"}[projected.type] != frame.role):
                raise ValueError("source-mismatch: 文本分片与实际 canonical 正文不一致")
            messages.append(projected)
            groups[key] = ([content["message"]], 1)
            used.add(selection_ref_identity(entry.ref))
            continue
        if key not in groups:
            selected = []
            for item_id in key:
                entry = canonical_by_id.get(item_id)
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
        used.update(selection_ref_identity(canonical_by_id[item_id].ref) for item_id in key)
    if any(index != len(expected) for expected, index in groups.values()) or used != set(entries):
        raise ValueError("source-mismatch: sealed selection 与最终 wire frames 未完整联校")
    return messages
