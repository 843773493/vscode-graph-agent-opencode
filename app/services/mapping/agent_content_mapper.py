from __future__ import annotations

from typing import Any


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
        if part_type == "reasoning":
            reasoning = part.get("reasoning")
            if isinstance(reasoning, str):
                reasoning_parts.append(reasoning)
            else:
                reasoning_parts.append(extract_reasoning_summary(part.get("summary")))
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
