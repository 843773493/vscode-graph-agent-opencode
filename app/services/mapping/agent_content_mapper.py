from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class AgentStreamContentPart:
    part_id: str
    index: int
    kind: Literal["reasoning", "markdown"]
    block_type: Literal[
        "reasoning",
        "reasoning_content",
        "reasoning_items",
        "thinking",
        "redacted_thinking",
        "text",
        "refusal",
    ]
    text: str
    extras: dict[str, Any] | None = None
    payload: dict[str, Any] | None = None


def extract_reasoning_summary(summary: Any) -> str:
    if summary is None:
        return ""
    if isinstance(summary, str):
        return summary
    if not isinstance(summary, list):
        return str(summary)

    parts: list[str] = []
    for entry in summary:
        if isinstance(entry, str):
            parts.append(entry)
            continue
        if not isinstance(entry, dict):
            continue
        text = entry.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def _reasoning_block_text(block_type: object, block: dict[str, Any]) -> str:
    if block_type == "reasoning_content":
        value = block.get("reasoning_content")
        return value if isinstance(value, str) else ""
    if block_type == "reasoning_items":
        items = block.get("reasoning_items")
        if not isinstance(items, list):
            return ""
        return "".join(
            extract_reasoning_summary(item.get("summary"))
            for item in items
            if isinstance(item, dict)
        )
    if block_type == "redacted_thinking":
        return ""
    if block_type == "thinking":
        value = block.get("thinking") or block.get("text")
        return value if isinstance(value, str) else ""
    if block_type == "reasoning":
        value = block.get("reasoning")
        if isinstance(value, str):
            return value
        raw_content = block.get("content")
        if isinstance(raw_content, list):
            return "".join(
                item.get("text", "")
                for item in raw_content
                if isinstance(item, dict)
                and item.get("type") in {"reasoning_text", "text"}
                and isinstance(item.get("text"), str)
            )
        return extract_reasoning_summary(block.get("summary"))
    return ""


def split_agent_content(content: Any) -> tuple[str, str]:
    """把 LangChain content blocks 拆成 reasoning 文本和用户可见文本。"""
    if content is None:
        return "", ""
    if isinstance(content, str):
        return "", content
    if not isinstance(content, list):
        text = str(content)
        return "", text

    reasoning_parts: list[str] = []
    text_parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            text_parts.append(part)
            continue
        if not isinstance(part, dict):
            text_parts.append(str(part))
            continue

        part_type = part.get("type")
        if part_type in {
            "reasoning",
            "reasoning_content",
            "reasoning_items",
            "thinking",
            "redacted_thinking",
        }:
            reasoning_parts.append(_reasoning_block_text(part_type, part))
            continue

        if part_type in ("text", "output_text"):
            text = part.get("text")
            if isinstance(text, str):
                text_parts.append(text)
            continue

        if part_type == "refusal":
            refusal = part.get("refusal")
            if isinstance(refusal, str):
                text_parts.append(f"[拒绝]{refusal}")
            continue

        fallback = part.get("text")
        if isinstance(fallback, str):
            text_parts.append(fallback)

    reasoning_text = "".join(reasoning_parts)
    visible_text = "".join(text_parts)
    return reasoning_text, visible_text


def extract_agent_stream_content_parts(content: Any) -> list[AgentStreamContentPart]:
    """读取带权威身份的模型流 content blocks。"""
    if content in (None, ""):
        return []
    if not isinstance(content, list):
        raise TypeError(
            "模型流 content 必须是带 id/index 的 LangChain content blocks，"
            f"实际类型: {type(content).__name__}"
        )

    result: list[AgentStreamContentPart] = []
    for position, block in enumerate(content):
        if not isinstance(block, dict):
            raise TypeError(
                f"模型流 content[{position}] 必须是 dict，实际类型: {type(block).__name__}"
            )
        block_type = block.get("type")
        if block_type in {"tool_call", "tool_call_chunk"}:
            continue
        if block_type in {
            "reasoning",
            "reasoning_content",
            "reasoning_items",
            "thinking",
            "redacted_thinking",
        }:
            text = _reasoning_block_text(block_type, block)
            kind: Literal["reasoning", "markdown"] = "reasoning"
        elif block_type in {"text", "output_text"}:
            text = block.get("text")
            kind = "markdown"
            block_type = "text"
        elif block_type == "refusal":
            text = block.get("refusal")
            kind = "markdown"
        else:
            raise ValueError(
                f"模型流 content[{position}] 含未知 block type: {block_type!r}"
            )

        if not isinstance(text, str):
            raise TypeError(
                f"模型流 content[{position}] 的文本字段必须是 str，实际值: {text!r}"
            )

        part_id = block.get("id")
        if not isinstance(part_id, str) or not part_id:
            raise ValueError(f"模型流 content[{position}] 缺少权威 part id")
        index = block.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ValueError(
                f"模型流 content[{position}] 缺少有效 block index: {index!r}"
            )
        result.append(
            AgentStreamContentPart(
                part_id=part_id,
                index=index,
                kind=kind,
                block_type=block_type,
                text=text,
                extras=(
                    dict(block["extras"])
                    if isinstance(block.get("extras"), dict)
                    else None
                ),
                payload=dict(block),
            )
        )
    return result
