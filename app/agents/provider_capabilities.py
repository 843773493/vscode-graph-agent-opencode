from __future__ import annotations

from collections.abc import Iterable
from typing import Any

ProviderCapability = str

TEXT_INPUT = "text_input"
IMAGE_INPUT = "image_input"
VIDEO_INPUT = "video_input"
AUDIO_INPUT = "audio_input"

SUPPORTED_PROVIDER_CAPABILITIES: frozenset[ProviderCapability] = frozenset(
    {
        TEXT_INPUT,
        IMAGE_INPUT,
        VIDEO_INPUT,
        AUDIO_INPUT,
    }
)

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


def parse_provider_capabilities(provider: dict[str, Any]) -> set[ProviderCapability]:
    """读取并严格校验 provider capabilities。"""
    raw_capabilities = provider.get("capabilities", [])
    capabilities: set[ProviderCapability] = {TEXT_INPUT}
    if not isinstance(raw_capabilities, list):
        raise TypeError("provider.capabilities 必须是字符串数组")

    for item in raw_capabilities:
        if not isinstance(item, str):
            raise TypeError("provider.capabilities 只能包含字符串")
        if item not in SUPPORTED_PROVIDER_CAPABILITIES:
            provider_id = provider.get("id") or provider.get("model") or "<unknown>"
            supported = ", ".join(sorted(SUPPORTED_PROVIDER_CAPABILITIES))
            raise ValueError(
                f"provider {provider_id!r} 包含不支持的 capability: {item!r}。"
                f"允许值: {supported}"
            )
        capabilities.add(item)
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


def detect_required_capabilities_from_messages(
    messages: Iterable[object],
) -> set[ProviderCapability]:
    """扫描完整模型消息上下文，包括工具返回的多模态 content blocks。"""
    required: set[ProviderCapability] = {TEXT_INPUT}
    for message in messages:
        if isinstance(message, dict):
            content = message.get("content")
            content_blocks = message.get("content_blocks")
        else:
            content = getattr(message, "content", None)
            content_blocks = getattr(message, "content_blocks", None)
        required.update(detect_required_capabilities(content))
        if content_blocks is not content:
            required.update(detect_required_capabilities(content_blocks))
    return required
