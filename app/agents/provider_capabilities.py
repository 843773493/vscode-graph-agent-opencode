from __future__ import annotations

from collections.abc import Iterable
from typing import Any

ProviderCapability = str

TEXT_INPUT = "text_input"
IMAGE_INPUT = "image_input"
VIDEO_INPUT = "video_input"
AUDIO_INPUT = "audio_input"

CAPABILITY_ALIASES = {
    # TODO: 兼容历史配置名 vision，后续配置全部迁移到 image_input 后可移除。
    "vision": IMAGE_INPUT,
}

CONTENT_BLOCK_CAPABILITY_REQUIREMENTS: dict[str, set[ProviderCapability]] = {
    "image": {IMAGE_INPUT},
    "image_url": {IMAGE_INPUT},
    "video": {VIDEO_INPUT},
    "video_url": {VIDEO_INPUT},
    "input_video": {VIDEO_INPUT},
    "audio": {AUDIO_INPUT},
    "audio_url": {AUDIO_INPUT},
    "input_audio": {AUDIO_INPUT},
}


def normalize_provider_capabilities(provider: dict[str, Any]) -> set[ProviderCapability]:
    """读取 provider capabilities，并把历史别名归一到规范能力名。"""
    raw_capabilities = provider.get("capabilities", [])
    capabilities: set[ProviderCapability] = {TEXT_INPUT}
    if not isinstance(raw_capabilities, list):
        raise TypeError("provider.capabilities 必须是字符串数组")

    for item in raw_capabilities:
        if not isinstance(item, str):
            raise TypeError("provider.capabilities 只能包含字符串")
        capabilities.add(CAPABILITY_ALIASES.get(item, item))
    return capabilities


def detect_required_capabilities(content: object) -> set[ProviderCapability]:
    """根据消息 content blocks 推导当前请求需要的模型输入能力。"""
    required: set[ProviderCapability] = {TEXT_INPUT}
    if not isinstance(content, list):
        return required

    for part in content:
        if not isinstance(part, dict):
            continue
        block_type = part.get("type")
        if isinstance(block_type, str):
            required.update(CONTENT_BLOCK_CAPABILITY_REQUIREMENTS.get(block_type, set()))
    return required


def _format_capabilities(capabilities: Iterable[ProviderCapability]) -> str:
    return ", ".join(sorted(capabilities))


def select_providers_for_capabilities(
    providers: list[dict[str, Any]],
    required_capabilities: set[ProviderCapability],
) -> list[dict[str, Any]]:
    """按请求需要的输入能力筛选 provider，保留原始 fallback 顺序。"""
    required = set(required_capabilities)
    if not required:
        return providers

    selected: list[dict[str, Any]] = []
    provider_summaries: list[str] = []
    for provider in providers:
        provider_capabilities = normalize_provider_capabilities(provider)
        provider_id = str(provider.get("id") or provider.get("model") or "<unknown>")
        provider_summaries.append(
            f"{provider_id}({_format_capabilities(provider_capabilities)})"
        )
        if required.issubset(provider_capabilities):
            selected.append(provider)

    if selected:
        return selected

    raise RuntimeError(
        "当前消息需要模型输入能力 "
        f"[{_format_capabilities(required)}]，但当前 agent 的模型链路没有匹配的 provider。"
        f"已配置 provider: {', '.join(provider_summaries)}。"
        "请在支持该输入类型的 provider 上配置 capabilities，"
        "例如 image_input、video_input 或 audio_input，并将它加入该 agent 的 fallback_providers。"
    )
