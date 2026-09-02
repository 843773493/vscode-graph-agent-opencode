from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from app.agents.provider_api_mode import parse_provider_api_mode

ProviderCapability = str

TEXT_INPUT = "text_input"
IMAGE_INPUT = "image_input"
VIDEO_INPUT = "video_input"
AUDIO_INPUT = "audio_input"
PROMPT_CACHE_KEY = "prompt_cache_key"

SUPPORTED_PROVIDER_CAPABILITIES: frozenset[ProviderCapability] = frozenset(
    {
        TEXT_INPUT,
        IMAGE_INPUT,
        VIDEO_INPUT,
        AUDIO_INPUT,
        PROMPT_CACHE_KEY,
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
    """从结构化 api_mode 派生输入能力和请求特性。"""
    api_mode = parse_provider_api_mode(provider)
    capabilities: set[ProviderCapability] = {TEXT_INPUT}
    model_info = api_mode.model_info
    if model_info.supports_vision:
        capabilities.add(IMAGE_INPUT)
    if model_info.supports_video_input:
        capabilities.add(VIDEO_INPUT)
    if model_info.supports_audio_input:
        capabilities.add(AUDIO_INPUT)
    if api_mode.request_features.prompt_cache_key:
        capabilities.add(PROMPT_CACHE_KEY)
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
        metadata = part.get("metadata")
        if (
            isinstance(metadata, dict)
            and metadata.get("origin") == "generated"
            and metadata.get("kind") == "attachment_preview"
        ):
            # User attachment preview 是可选 rich block；没有 vision 能力时
            # UserContentBuilder 仍会发送 manifest 路径文本，由模型自行使用已有工具。
            continue
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
