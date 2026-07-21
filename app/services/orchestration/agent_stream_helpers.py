from __future__ import annotations

import json
from typing import Any

from app.agents.tool_result_text import serialize_tool_value
from app.schemas.public_v2.message import AttachmentRef

CHAT_MODEL_EVENT_NAMES = {
    "ChatOpenAI",
    "BoxteamLiteLLMChatModel",
    "BoxteamOpenAIResponsesModel",
    "ChatLiteLLM",
    "ChatLiteLLMRouter",
}


def normalize_tool_args(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    return {"input": value}


def extract_tool_result_text(output: Any) -> str:
    content = getattr(output, "content", None)
    if content is not None:
        return serialize_tool_value(content)
    return serialize_tool_value(output)


def unwrap_json_string_tool_result(final_text: str, last_tool_result_text: str) -> str:
    if not last_tool_result_text:
        return final_text
    stripped = final_text.strip()
    candidates: list[str] = []
    if stripped.startswith('"') and stripped.endswith('"'):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, str):
            candidates.append(parsed)
    if stripped.startswith('\\"') and stripped.endswith('\\"') and len(stripped) >= 4:
        candidates.append(stripped[2:-2])

    for candidate in candidates:
        if candidate == last_tool_result_text:
            return candidate
        if candidate.startswith('"') and candidate.endswith('"') and len(candidate) >= 2:
            inner_candidate = candidate[1:-1]
            if inner_candidate == last_tool_result_text:
                return inner_candidate
    return final_text


def is_tracked_chat_model_event(name: str) -> bool:
    return name in CHAT_MODEL_EVENT_NAMES


def build_human_response_metadata(
    *,
    message_id: str,
    display_content: str,
    attachments: list[AttachmentRef],
    message_created_at: str,
    message_metadata: dict[str, object],
) -> dict[str, object]:
    metadata: dict[str, object] = {
        "display_content": display_content,
        "message_id": message_id,
        "created_at": message_created_at,
        "updated_at": message_created_at,
        "message_metadata": dict(message_metadata),
    }
    if attachments:
        metadata["attachments"] = [
            attachment.model_dump(mode="json", exclude={"data_url"})
            for attachment in attachments
        ]
    return metadata
