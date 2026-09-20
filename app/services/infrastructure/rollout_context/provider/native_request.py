"""从 Saver sealed selection 直接生成 Responses 原生 request。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.domain.itemized.hashing import canonical_json_bytes
from app.domain.itemized.records import CanonicalItemRecord
from app.domain.itemized.refs import ToolSetRef
from app.domain.itemized.request_plan import ContextRequestPlan
from app.domain.itemized.tool_call_identity import provider_tool_call_id
from app.services.infrastructure.rollout_context.provider.toolset_request_bridge import (
    project_tool_set_ref,
)
from app.services.mapping.itemized.carrier_dedup import superseded_stream_item_ids
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
    items: Sequence[CanonicalItemRecord],
    request_only_content: Mapping[str, object],
) -> dict[str, object]:
    """selection 顺序和来源留在诊断中，request 只携带原生协议字段。"""
    if plan.plan_state != "sealed" or plan.assembly_id is None:
        raise ValueError("context plan 未 sealed，不能进入 projector")
    by_id = {item.item_id: item for item in items}
    if len(by_id) != len(items):
        raise ValueError("plan-order-integrity: canonical item registry 重复")
    # stream sink 与 checkpoint sink 都保存了同一次模型调用的 canonical 事实，
    # 但 provider wire 只能发送一个 carrier；否则历史会出现两份工具调用/输出。
    # 同一规则必须和 LangChain/history 投影保持一致。
    superseded_ids = superseded_stream_item_ids(items)
    inputs: list[dict[str, object]] = []
    tools: list[dict[str, object]] = []
    losses: list[str] = []
    wire_sources: list[dict[str, object]] = []
    for entry in validate_projection_selection(plan.selection):
        losses.extend(entry.loss)
        if not entry.included:
            if not entry.loss:
                losses.append(entry.omission_reason or "omitted")
            continue
        ref = entry.ref
        if isinstance(ref, ToolSetRef):
            tools.extend(project_tool_set_ref(ref, target_format="responses"))
            continue
        start = len(inputs)
        if ref.ref_type == "request_only":
            body = resolve_selected_request_body(plan, entry, request_only_content)
            inputs.append(
                {"role": "system", "content": _text_blocks(body, role="system")}
            )
        else:
            item = resolve_selected_item(entry, by_id)
            if item.item_id in superseded_ids:
                continue
            kind = item.semantic_kind
            if kind in {"user_input", "assistant_output"}:
                role = "user" if kind == "user_input" else "assistant"
                if role == "user":
                    projection = project_user_message_content(
                        item.payload,
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
                                item.metadata, raw_call_id
                            ),
                            "name": call["name"],
                            "arguments": canonical_json_bytes(
                                call.get("args", {})
                            ).decode("utf-8"),
                        }
                    )
            elif kind == "tool_result":
                payload = item.payload
                if not isinstance(payload, Mapping) or not payload.get("tool_call_id"):
                    raise ValueError(
                        f"source-mismatch: native tool_result: {item.item_id}"
                    )
                output = payload.get("content", payload)
                inputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": provider_tool_call_id(
                            item.metadata, str(payload["tool_call_id"])
                        ),
                        "output": output
                        if isinstance(output, str)
                        else canonical_json_bytes(output).decode("utf-8"),
                    }
                )
            elif kind == "runtime_notice":
                if item.wire_role != "user":
                    raise ValueError(
                        "source-mismatch: native runtime_notice 必须投影为 user: "
                        f"{item.item_id}"
                    )
                content = _text_blocks(
                    project_runtime_notice_content(item.payload),
                    role="user",
                )
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


def bind_native_request_payload(
    projection: Mapping[str, object], parameters: Mapping[str, object]
) -> dict[str, object]:
    """Provider 配置不得覆盖已冻结的 input；工具只取同一 sealed manifest。"""
    request = projection["request"]
    if not isinstance(request, Mapping):
        raise TypeError("source-mismatch: native request 必须是 object")
    forbidden = {"input", "messages", "instructions", "previous_response_id", "prompt"}
    if overridden := forbidden.intersection(parameters):
        raise ValueError(
            "source-mismatch: provider 参数覆盖 sealed context: "
            + ",".join(sorted(overridden))
        )
    # LangChain bind_tools 仍为 tool execution/callback 提供工具定义；实际 wire
    # 只使用 Saver ToolSetRef，不能再从 LangChain tools 反推一次选择。
    return {**parameters, **request}


__all__ = ["bind_native_request_payload", "project_native_request"]
