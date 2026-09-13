from copy import deepcopy

from langchain_core.messages import HumanMessage, ToolMessage

from app.agents.providers.litellm_chat import BoxteamLiteLLMChatModel
from app.agents.providers.openai_responses import BoxteamOpenAIResponsesModel


def _message() -> HumanMessage:
    return HumanMessage(
        content=[
            {"type": "text", "text": "请看图片"},
            {
                "type": "text",
                "text": "<attachment path='.boxteam/sessions/s/attachments/a.png'>",
                "metadata": {
                    "origin": "generated",
                    "kind": "attachment_manifest",
                    "schema_version": 1,
                    "file_id": "boxteam-session://s/attachments/a.png",
                },
            },
            {
                "type": "image_url",
                "image_url": {"url": "data:image/webp;base64,preview"},
                "metadata": {
                    "origin": "generated",
                    "kind": "attachment_preview",
                    "schema_version": 1,
                    "file_id": "boxteam-session://s/attachments/a.png",
                },
            },
        ],
        response_metadata={
            "attachments": [
                {
                    "file_id": "boxteam-session://s/attachments/a.png",
                    "name": "a.png",
                    "content_type": "image/png",
                }
            ]
        },
    )


def test_chat_completions_projects_user_blocks_without_mutating_source():
    message = _message()
    source = deepcopy(message.content)
    model = BoxteamLiteLLMChatModel(
        model="openai/test-model",
        api_key="test-key",
        api_base="https://example.com/v1",
        provider_id="chat-test",
        image_input_replay=True,
    )

    projected = model._convert_messages_to_dicts([message])[0]

    assert projected["role"] == "user"
    assert projected["content"][1]["text"].startswith("<attachment")
    assert projected["content"][2]["type"] == "image_url"
    assert all("metadata" not in block for block in projected["content"])
    assert message.content == source


def test_responses_projects_input_text_and_input_image():
    message = _message()
    model = BoxteamOpenAIResponsesModel(
        model="openai/test-model",
        api_key="test-key",
        api_base="https://example.com/v1",
        provider_id="responses-test",
        image_input_replay=True,
    )

    projected = model._history_messages([message])

    assert projected[0].content[0] == {"type": "input_text", "text": "请看图片"}
    assert projected[0].content[1]["type"] == "input_text"
    assert projected[0].content[2] == {
        "type": "input_image",
        "image_url": "data:image/webp;base64,preview",
    }


def test_anthropic_messages_uses_litellm_chat_projection():
    message = _message()
    model = BoxteamLiteLLMChatModel(
        model="claude-test",
        api_key="test-key",
        api_base="https://example.com",
        custom_llm_provider="anthropic",
        provider_id="anthropic-test",
        image_input_replay=True,
    )

    projected = model._convert_messages_to_dicts([message])[0]

    assert projected["role"] == "user"
    assert projected["content"][0] == {"type": "text", "text": "请看图片"}
    assert projected["content"][2] == {
        "type": "image_url",
        "image_url": {"url": "data:image/webp;base64,preview"},
    }


def _image_tool_message() -> ToolMessage:
    return ToolMessage(
        content=[
            {"type": "text", "text": "读取完成"},
            {"type": "image", "base64": "eA==", "mime_type": "image/png"},
        ],
        tool_call_id="read-file-call",
        name="read_file",
        additional_kwargs={"read_file_path": "verification.png"},
    )


def test_chat_completions_without_vision_replays_tool_image_as_text():
    message = _image_tool_message()
    model = BoxteamLiteLLMChatModel(
        model="openai/test-model",
        api_key="test-key",
        api_base="https://example.com/v1",
        provider_id="text-only",
        image_input_replay=False,
    )

    projected = model._convert_messages_to_dicts([message])[0]

    assert projected["role"] == "tool"
    assert projected["tool_call_id"] == "read-file-call"
    assert projected["content"][0] == {"type": "text", "text": "读取完成"}
    assert projected["content"][1]["type"] == "text"
    assert "verification.png" in projected["content"][1]["text"]
    assert all(
        block["type"] not in {"image", "image_url", "input_image"}
        for block in projected["content"]
    )
    # 会话记录中的原始图片 block 不被就地修改
    assert message.content[1]["base64"] == "eA=="


def test_chat_completions_with_vision_keeps_tool_image_block():
    message = _image_tool_message()
    model = BoxteamLiteLLMChatModel(
        model="openai/test-model",
        api_key="test-key",
        api_base="https://example.com/v1",
        provider_id="vision",
        image_input_replay=True,
    )

    projected = model._convert_messages_to_dicts([message])[0]

    assert projected["content"] == [
        {"type": "text", "text": "读取完成"},
        {"type": "image", "base64": "eA==", "mime_type": "image/png"},
    ]
