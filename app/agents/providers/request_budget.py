"""估算最终 wire 的实际输入；不重新组装消息、不读取媒体或调用 Provider。"""

from __future__ import annotations

import json
from collections.abc import Mapping

from langchain_core.messages import SystemMessage
from langchain_core.messages.utils import count_tokens_approximately

from app.domain.context_compaction import InputTokenEstimate

# 三种协议实际携带模型输入的字段；model、stream、输出长度等不是输入正文。
_INPUT_FIELDS = frozenset({
    "messages", "input", "instructions", "system", "tools", "tool_choice",
    "response_format", "output_format", "text",
})
_OPAQUE_BLOCKS = frozenset({
    "image", "image_url", "input_image", "input_audio", "audio", "video",
    "input_video", "file", "input_file", "item_reference",
    "redacted_thinking",
})


def estimate_wire_input(wire_request: Mapping[str, object]) -> InputTokenEstimate:
    """复用 LangChain 字符估算，完整保留工具参数和 schema；媒体无依据则 unknown。

    不把远端 URL、base64、加密 reasoning 当普通文本 token，也不为计数下载资源。
    此值是跨协议文本估算，包含 JSON framing，不冒充模型专属 tokenizer 的精确计数。
    """
    unknown: set[str] = set()

    def visible(value: object) -> object:
        if isinstance(value, list):
            return [visible(item) for item in value]
        if not isinstance(value, Mapping):
            return value
        kind = value.get("type")
        if kind == "document":
            source = value.get("source")
            if isinstance(source, Mapping) and source.get("type") in {"text", "content"}:
                # Anthropic 的文本／内容文档已经在最终正文中；嵌套图片仍单独标未知。
                return {**value, "source": {key: visible(item) if key == "content" else item
                                           for key, item in source.items()}}
            unknown.add("document")
            # PDF／远端文档正文未知，标题和 context 仍是真实可见输入。
            return {key: item for key, item in value.items() if key != "source"}
        if isinstance(kind, str) and kind in _OPAQUE_BLOCKS:
            unknown.add(kind)
            return None
        # Responses reasoning 可同时带可见摘要与不可解释的加密载荷。
        if kind == "reasoning" and value.get("encrypted_content"):
            unknown.add("encrypted_reasoning")
        # thinking 签名只用于验真；加密载荷不能按编码字符估算语义 token。
        excluded = {"encrypted_content"} if kind == "reasoning" else {"signature"} if kind == "thinking" else set()
        # 只递归协议正文，不把工具参数／schema 中恰好名为 type 的业务值当媒体。
        return {key: visible(item) if key in {"content", "summary", "output"} else item
                for key, item in value.items() if key not in excluded}

    body = {key: (visible(value) if key in {"messages", "input", "system"} else value)
            for key, value in wire_request.items() if key in _INPUT_FIELDS}
    output_config = wire_request.get("output_config")
    if isinstance(output_config, Mapping) and "format" in output_config:
        body["output_config"] = {"format": output_config["format"]}
    tokens = count_tokens_approximately([
        SystemMessage(content=json.dumps(body, ensure_ascii=False, separators=(",", ":"))),
    ])
    return InputTokenEstimate(
        known_tokens=tokens, unknown_parts=tuple(sorted(unknown)),
        source="langchain-approximate-final-wire:v1",
    )
