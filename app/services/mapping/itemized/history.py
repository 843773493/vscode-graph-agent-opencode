"""历史/展示投影的无 I/O入口。"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence

from langchain_core.messages import BaseMessage

from app.domain.itemized.plans.request import ContextRequestPlan
from app.domain.itemized.records import CanonicalItemRecord
from app.services.mapping.itemized.canonical_langchain import (
    project_canonical_items,
    project_reasoning_carrier,
)
from app.services.mapping.itemized.langchain import project_context_plan
from app.services.mapping.itemized.provider_history import (
    canonical_reasoning_projection,
    public_message_record,
)


def compaction_history_fields(item: CanonicalItemRecord) -> Iterator[tuple[str, object]]:
    """版本2原文叶字段；安全转换先裁决可见内容，存储层只建立字节位置。"""
    if item.semantic_kind == "compaction_summary":
        raise ValueError("compaction-history-source-mismatch: 摘要必须先按原覆盖展开")
    if item.semantic_kind == "reasoning":
        value = canonical_reasoning_projection(item)
        carrier = project_reasoning_carrier(item)
        reasoning_parts: list[str] = []
        summary_parts: list[str] = []
        blocks = carrier.get("reasoning_items", []) if carrier is not None and carrier["type"] == "reasoning_items" else [carrier]
        for block in blocks:
            if not isinstance(block, Mapping):
                continue
            kind = block.get("type")
            if kind == "reasoning":
                direct = block.get("reasoning")
                if isinstance(direct, str):
                    reasoning_parts.append(direct)
                else:
                    reasoning_parts.extend(part["text"] for part in block.get("content", [])
                                           if isinstance(part, Mapping) and part.get("type") in {"reasoning_text", "text"}
                                           and isinstance(part.get("text"), str))
                summary = block.get("summary")
                if isinstance(summary, list):
                    summary_parts.extend(part if isinstance(part, str) else part["text"] for part in summary
                                         if isinstance(part, str) or isinstance(part, Mapping) and isinstance(part.get("text"), str))
                elif summary is not None:
                    summary_parts.append(str(summary))
            elif kind in {"thinking", "reasoning_content"}:
                text = (block.get("thinking") or block.get("text")) if kind == "thinking" else block.get("reasoning_content")
                if isinstance(text, str):
                    reasoning_parts.append(text)
        if ("".join(reasoning_parts) != (value["reasoning_text"] or "")
                or "".join(summary_parts) != (value["summary_text"] or "")):
            raise ValueError("compaction-history-source-mismatch: 原始文本片段与安全 reasoning 投影不一致")
        value["reasoning_text"], value["summary_text"] = reasoning_parts or None, summary_parts or None
    else:
        messages = project_history_items((item,), include_runtime_notices=True)
        value = [public_message_record({"role": message.type, "content": message.content,
                  **({"tool_calls": message.tool_calls} if getattr(message, "tool_calls", None) else {})})
                 for message in messages]
    def fields(node: object, path: str) -> Iterator[tuple[str, object]]:
        if isinstance(node, Mapping) and node:
            for key, child in sorted(node.items()):
                segment = f".{key}" if key.isidentifier() else f"[{json.dumps(key, ensure_ascii=False)}]"
                yield from fields(child, path + segment)
        elif isinstance(node, (list, tuple)) and node:
            for index, child in enumerate(node):
                yield from fields(child, f"{path}[{index}]")
        else:
            yield path, node

    yield from fields(value, "$")


def project_history_items(
    items: Sequence[CanonicalItemRecord],
    *,
    include_runtime_notices: bool = False,
    include_summaries: bool = False,
    capability_losses: list[str] | None = None,
) -> list[BaseMessage]:
    """历史只消费调用方给出的有序 canonical item，不访问 storage。"""
    return project_canonical_items(
        items,
        include_runtime_notices=include_runtime_notices,
        include_summaries=include_summaries,
        capability_losses=capability_losses,
        preserve_order=True,
    )


def project_history_plan(
    plan: ContextRequestPlan,
    items: Mapping[tuple[str, str, str], CanonicalItemRecord],
    *,
    include_runtime_notices: bool = False,
    include_summaries: bool = False,
    capability_losses: list[str] | None = None,
) -> list[BaseMessage]:
    """消费与 LangChain/provider 相同的 sealed selection 生成历史消息。

    history 不发送 request-only 正文，也不把 ToolSetRef 变成 message；它只
    materialize plan 中已经选择的 canonical item。selection 校验和 item
    顺序仍由 ``project_context_plan`` 统一执行，避免 history 自己重新排序。
    """
    return project_context_plan(
        plan,
        items,
        include_runtime_notices=include_runtime_notices,
        include_summaries=include_summaries,
        capability_losses=capability_losses,
        include_request_only=False,
    )


__all__ = ["project_history_items", "project_history_plan"]
