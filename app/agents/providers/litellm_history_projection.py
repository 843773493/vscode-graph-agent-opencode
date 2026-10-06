"""LiteLLM history/context message 到 provider request 的 projection owner。

历史消息的真实转换已迁到 `app/agents/providers/wire_finalize.py` 的单一实现；
本模块只保留输出侧 content 归一化，避免同一转换存在两份。
"""

from __future__ import annotations

import copy
from typing import Any


class LiteLLMHistoryProjectionMixin:
    @staticmethod
    def normalize_output_content(content: Any) -> Any:
        """把 LiteLLM 输出中的 thinking/output_text 等方言转为标准块。"""
        if content is None:
            return ""
        if isinstance(content, str):
            return [{"type": "text", "text": content}] if content else ""
        if not isinstance(content, list):
            return [{"type": "text", "text": str(content)}]

        normalized: list[dict[str, Any]] = []
        for block in content:
            if isinstance(block, str):
                if block:
                    normalized.append({"type": "text", "text": block})
                continue
            if not isinstance(block, dict):
                normalized.append({"type": "text", "text": str(block)})
                continue

            block_type = block.get("type")
            if block_type in {
                "reasoning",
                "reasoning_content",
                "reasoning_items",
                "thinking",
                "redacted_thinking",
            }:
                # LiteLLM 已经给出标准 block 时整体复制，不能在这里按字段
                # 白名单重建，否则 provider 新增的字段会在历史中丢失。
                normalized.append(copy.deepcopy(block))
                continue
            if block_type in {"text", "output_text"}:
                text = block.get("text")
                if isinstance(text, str):
                    normalized.append({"type": "text", "text": text})
                continue
            if block_type in {"tool_call", "tool_call_chunk"}:
                continue
            if isinstance(block_type, str):
                normalized.append(copy.deepcopy(block))
                continue

            fallback_text = block.get("text")
            if isinstance(fallback_text, str):
                normalized.append({"type": "text", "text": fallback_text})

        return normalized or ""


__all__ = ["LiteLLMHistoryProjectionMixin"]
