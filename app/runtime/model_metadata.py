"""LiteLLM 公共目录到本项目模型能力的只读投影；不推导请求参数。"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy

from app.runtime.litellm_runtime import local_model_catalog

_CAPABILITIES = (
    "supports_vision", "supports_video_input", "supports_audio_input",
    "supports_audio_output", "supports_function_calling", "supports_reasoning",
)


def model_metadata(provider: str, model: str, *, owner_key: str | None = None) -> dict[str, object] | None:
    """服务商和名称共同匹配；同名其它服务商的能力不能串入。"""
    catalog = local_model_catalog(owner_key=owner_key)
    name = model.removeprefix(provider + "/")
    for key in dict.fromkeys((f"{provider}/{name}", name)):
        entry = catalog.get(key)
        if not isinstance(entry, Mapping) or entry.get("litellm_provider") != provider:
            continue
        if entry.get("mode") not in {"chat", "responses"}:
            continue
        info = {name: entry[name] for name in _CAPABILITIES if isinstance(entry.get(name), bool)}
        for field_name in ("max_input_tokens", "context_window"):
            capacity = entry.get(field_name)
            if isinstance(capacity, int) and not isinstance(capacity, bool) and capacity > 0:
                info[field_name] = capacity
        return info
    return None


def apply_model_metadata(options: Mapping[str, object], model: str | None, *, owner_key: str | None = None) -> dict[str, object]:
    """目录是最低优先级默认；显式 false、容量与回放声明均保留。"""
    result = deepcopy(dict(options))
    mode = result.get("api_mode")
    if not isinstance(mode, dict):
        return result
    info = model_metadata(str(result.get("custom_llm_provider", "")), model, owner_key=owner_key) if model else None
    explicit_info = mode.get("model_info", {})
    if not isinstance(explicit_info, Mapping):
        return result  # 由既有严格解析器报告输入错误。
    merged_info = {**(info or {}), **explicit_info}
    if "supports_reasoning" not in mode:
        reasoning: dict[str, object] = {}
        if merged_info.get("supports_reasoning") is True:
            protocol = mode.get("protocol")
            if protocol == "responses":
                reasoning = {"reasoning_items": {"summary": True, "encrypted_content": True}}
            elif protocol == "anthropic_messages":
                reasoning = {"thinking_blocks": {"thinking": True, "redacted_thinking": True}}
            elif protocol == "chat_completions":
                reasoning = {"reasoning_content": True}
        mode["supports_reasoning"] = reasoning
    elif info is not None and "supports_reasoning" not in explicit_info:
        # 显式回放声明覆盖目录的思考默认，空对象表示关闭。
        def enabled(value: object) -> bool:
            return value is True or isinstance(value, Mapping) and any(enabled(item) for item in value.values())

        merged_info["supports_reasoning"] = enabled(mode["supports_reasoning"])
    mode["model_info"] = merged_info
    return result
