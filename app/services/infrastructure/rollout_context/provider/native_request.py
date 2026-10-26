"""从 Saver sealed selection 直接生成 Responses 原生 request。"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from app.domain.itemized.epoch.seal_binding import ContextSealBinding
from app.domain.itemized.hashing import canonical_json_bytes
from app.domain.itemized.identity.refs import (
    ToolSetRef,
    ref_identity,
    selection_ref_identity,
)
from app.domain.itemized.identity.tool_call_identity import provider_tool_call_id
from app.domain.itemized.plans.request import ContextRequestPlan
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.provider.toolset_request_bridge import (
    project_tool_set_ref,
)
from app.services.mapping.itemized.carrier_dedup import superseded_item_ids
from app.services.mapping.itemized.prefix_frames import project_sealed_prefix
from app.services.mapping.itemized.provider_request import (
    project_runtime_notice_content,
    project_user_message_content,
)
from app.services.mapping.itemized.selection import (
    resolve_selected_item,
    resolve_selected_request_body,
    validate_projection_selection,
)

_NON_TEXT_BLOCK_TYPES = frozenset(
    {
        "reasoning",
        "reasoning_content",
        "reasoning_items",
        "thinking",
        "redacted_thinking",
    }
)


def _summary_message_inputs(body: object) -> list[dict[str, object]]:
    """候选正文来自同一封存 frame；直接按其声明角色编码 Responses。"""
    if not isinstance(body, Mapping) or set(body) != {"message", "item_ids", "retry_source_item_ids"}:
        raise ValueError("source-mismatch: 摘要候选缺少精确 message manifest")
    message = body["message"]
    if not isinstance(message, Mapping) or not isinstance(message.get("data"), Mapping):
        raise TypeError("source-mismatch: 摘要候选 message 非法")
    data = message["data"]
    role = {"human": "user", "ai": "assistant", "tool": "tool"}.get(message.get("type"))
    if role is None:
        raise ValueError("source-mismatch: 摘要候选 message role 非法")
    if role == "tool":
        call_id = data.get("tool_call_id")
        if not isinstance(call_id, str) or not call_id:
            raise ValueError("source-mismatch: 摘要候选 tool result 缺少 call_id")
        output = data.get("content")
        return [{"type": "function_call_output", "call_id": call_id,
                 "output": output if isinstance(output, str) else canonical_json_bytes(output).decode("utf-8")}]
    if role == "user":
        projection = project_user_message_content(data.get("content"), target_format="responses", image_input=True)
        if projection["diagnostics"]:
            raise ValueError("source-mismatch: 摘要候选 user content 无法编码")
        content = projection["content"]
        content = _text_blocks(content, role=role) if isinstance(content, str) else content
    else:
        content = _text_blocks(data.get("content"), role=role)
    inputs = [{"role": role, "content": content}] if content else []
    if role == "assistant":
        for call in data.get("tool_calls", []):
            if not isinstance(call, Mapping) or not call.get("id") or not call.get("name"):
                raise ValueError("source-mismatch: 摘要候选 tool call identity 非法")
            inputs.append({"type": "function_call", "call_id": call["id"], "name": call["name"],
                           "arguments": canonical_json_bytes(call["args"]).decode("utf-8")})
    return inputs


def _text_blocks(body: object, *, role: str) -> list[dict[str, object]]:
    block_type = "output_text" if role == "assistant" else "input_text"
    if isinstance(body, str):
        return [{"type": block_type, "text": body}]
    blocks = body if isinstance(body, (list, tuple)) else [body]
    result: list[dict[str, object]] = []
    for block in blocks:
        if isinstance(block, str):
            result.append({"type": block_type, "text": block})
        elif (
            role == "assistant"
            and isinstance(block, Mapping)
            and block.get("type") in _NON_TEXT_BLOCK_TYPES
        ):
            # Responses 的 reasoning 扩展不是 message content text block。
            # 它们是否可回放由 provider 能力决定；native projector 没有能力
            # 上下文时不能把扩展伪装成普通文本，也不能因此阻断可见正文重放。
            continue
        elif (
            isinstance(block, Mapping)
            and block.get("type")
            in {
                "text",
                "input_text",
                "output_text",
            }
            and isinstance(block.get("text"), str)
        ):
            result.append({"type": block_type, "text": block["text"]})
        else:
            raise ValueError(
                "source-mismatch: native request source 包含无法编码的 text block"
            )
    return result


def project_native_request(
    plan: ContextRequestPlan,
    items: Mapping[tuple[str, str, str], CanonicalItemRecord],
    request_only_content: Mapping[str, object],
    *, seal_binding: ContextSealBinding | None = None,
    attachment_projector: Callable[[object], object] | None = None,
) -> dict[str, object]:
    """selection 顺序和来源留在诊断中，request 只携带原生协议字段。"""
    if plan.plan_state != "sealed" or plan.assembly_id is None:
        raise ValueError("context plan 未 sealed，不能进入 projector")
    if seal_binding is not None:
        project_sealed_prefix(plan, seal_binding, items, request_only_content)
    # stream sink 与 checkpoint sink 都保存了同一次模型调用的 canonical 事实，
    # 但 provider wire 只能发送一个 carrier；否则历史会出现两份工具调用/输出。
    # 同一规则必须和 LangChain/history 投影保持一致。
    by_thread: dict[str, list[CanonicalItemRecord]] = {}
    for (_ref_type, _item_id, thread_id), item in items.items():
        by_thread.setdefault(thread_id, []).append(item)
    superseded = {
        ("canonical_item", item_id, thread_id)
        for thread_id, values in by_thread.items()
        for item_id in superseded_item_ids(values)
    }
    inputs: list[dict[str, object]] = []
    tools: list[dict[str, object]] = []
    losses: list[str] = []
    wire_sources: list[dict[str, object]] = []
    entries = list(validate_projection_selection(plan.selection))
    frame_by_ref = {}
    root_source_refs: set[tuple[str, ...]] = set()
    if seal_binding is not None:
        for frame in seal_binding.prefix.state.frames:
            if frame.role != "system":
                ids = frame.content.get("item_ids", [frame.item_ref.ref_id]) if isinstance(frame.content, Mapping) and frame.item_ref.ref_type == "canonical_item" else [frame.item_ref.ref_id]
                for ref_id in ids:
                    frame_by_ref[("canonical_item" if frame.item_ref.ref_type == "canonical_item" else "request_only", ref_id, plan.thread_id)] = frame
        root_source_refs = {
            selection_ref_identity(entry.ref) for entry in entries
            if entry.included and entry.ref.ref_type != "tool_set"
            and selection_ref_identity(entry.ref) not in frame_by_ref
        }
        if seal_binding.root is not None:
            inputs.append({"role": "system", "content": _text_blocks(seal_binding.root.content, role="system")})
        entries.sort(key=lambda entry: (
            frame_by_ref[selection_ref_identity(entry.ref)].ordinal if selection_ref_identity(entry.ref) in frame_by_ref else -1
        ))
    for entry in entries:
        losses.extend(entry.loss)
        if not entry.included:
            if not entry.loss:
                losses.append(entry.omission_reason or "omitted")
            continue
        ref = entry.ref
        if isinstance(ref, ToolSetRef):
            tools.extend(project_tool_set_ref(ref, target_format="responses"))
            continue
        if selection_ref_identity(ref) in root_source_refs:
            continue
        start = len(inputs)
        if ref.ref_type == "request_only":
            body = resolve_selected_request_body(plan, entry, request_only_content)
            source = (
                next(
                    source
                    for source in plan.contributions
                    if source.contribution_id == entry.contribution_id
                )
                if entry.contribution_id is not None
                else None
            )
            if source is not None and source.contribution_kind == "model_message":
                if plan.selection_policy != "summary_candidate":
                    raise ValueError("source-mismatch: model_message 只能属于摘要候选")
                inputs.extend(_summary_message_inputs(body))
            else:
                role = "user" if seal_binding is not None else "system"
                content = _text_blocks(body, role=role)
                # 原始正文仍须能编码；已封存来源只发送 frame 中确定的模型正文。
                if seal_binding is not None:
                    content = _text_blocks(frame_by_ref[selection_ref_identity(ref)].content, role=role)
                inputs.append({"role": role, "content": content})
        else:
            item = resolve_selected_item(entry, items)
            if ref_identity(ref) in superseded:
                continue
            kind = item.semantic_kind
            if kind in {"user_input", "assistant_output"}:
                role = "user" if kind == "user_input" else "assistant"
                if role == "user":
                    projection = project_user_message_content(
                        attachment_projector(item.payload) if attachment_projector is not None else item.payload,
                        target_format="responses",
                        image_input=True,
                    )
                    if projection["diagnostics"]:
                        raise ValueError(
                            f"source-mismatch: native user content: {item.item_id}"
                        )
                    content = projection["content"]
                    if isinstance(content, str):
                        content = _text_blocks(content, role=role)
                else:
                    content = _text_blocks(item.payload, role=role)
                if content:
                    inputs.append({"role": role, "content": content})
            elif kind == "tool_call":
                payload = item.payload
                if not isinstance(payload, Mapping):
                    raise ValueError(
                        f"source-mismatch: native tool_call: {item.item_id}"
                    )
                calls = payload.get("tool_calls", [payload])
                for call in calls:
                    raw_call_id = call.get("id") or call.get("tool_call_id")
                    if not isinstance(raw_call_id, str) or not call.get("name"):
                        raise ValueError(
                            f"source-mismatch: native tool identity: {item.item_id}"
                        )
                    # wire 只认 provider 原始 ID；stream 的 model-call scope
                    # 前缀必须在这里还原，否则 provider 会拒绝超长 call_id。
                    inputs.append(
                        {
                            "type": "function_call",
                            "call_id": provider_tool_call_id(
                                item.projection_identity, raw_call_id
                            ),
                            "name": call["name"],
                            "arguments": canonical_json_bytes(
                                call.get("args", {})
                            ).decode("utf-8"),
                        }
                    )
            elif kind == "tool_result":
                payload = item.payload
                if item.tool_result_evidence is None and (not isinstance(payload, Mapping) or not payload.get("tool_call_id")):
                    raise ValueError(
                        f"source-mismatch: native tool_result: {item.item_id}"
                    )
                raw_call_id = item.tool_result_evidence.tool_call_id if item.tool_result_evidence is not None else payload["tool_call_id"]
                output = payload.get("content", payload) if isinstance(payload, Mapping) else payload
                inputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": provider_tool_call_id(
                            item.projection_identity, raw_call_id
                        ),
                        "output": output
                        if isinstance(output, str)
                        else canonical_json_bytes(output).decode("utf-8"),
                    }
                )
            elif kind == "compaction_summary":
                inputs.append({"role": "user", "content": _text_blocks(item.payload["summary"], role="user")})
            elif kind == "runtime_notice":
                if item.wire_role != "user":
                    raise ValueError(
                        "source-mismatch: native runtime_notice 必须投影为 user: "
                        f"{item.item_id}"
                    )
                payload = attachment_projector(item.payload) if attachment_projector is not None else item.payload
                projection = project_user_message_content(
                    project_runtime_notice_content(payload),
                    target_format="responses", image_input=True,
                )
                if projection["diagnostics"]:
                    raise ValueError(f"source-mismatch: native runtime notice content: {item.item_id}")
                content = projection["content"]
                if isinstance(content, str):
                    content = _text_blocks(content, role="user")
                if content:
                    inputs.append({"role": "user", "content": content})
            else:
                # Responses 对受保护 reasoning/扩展不承诺跨 provider 回放。
                # 不复制密文或伪造空正文；明确记录未发送的来源与能力损失。
                losses.append(f"{item.item_id}:{kind}/{item.payload_kind}")
        wire_sources.extend(
            {
                "input_index": index,
                "plan_ordinal": entry.plan_ordinal,
                "ref_id": ref.ref_id,
            }
            for index in range(start, len(inputs))
        )
    return {
        "request": {"input": inputs, "tools": tools},
        "assembly_id": plan.assembly_id,
        "session_id": plan.session_id,
        "plan_hash": plan.plan_hash(),
        "selection": [entry.to_dict() for entry in plan.selection],
        "wire_sources": wire_sources,
        "losses": losses,
    }


__all__ = ["project_native_request"]
