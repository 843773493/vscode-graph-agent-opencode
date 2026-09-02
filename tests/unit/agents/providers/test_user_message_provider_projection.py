from copy import deepcopy

from langchain_core.messages import HumanMessage

from app.agents.providers.anthropic_messages import BoxteamAnthropicMessagesModel
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


def test_anthropic_projects_user_image_as_standard_image_url():
    message = _message()
    model = BoxteamAnthropicMessagesModel(
        model="claude-test",
        api_key="test-key",
        base_url="https://example.com",
        provider_id="anthropic-test",
        image_input_replay=True,
    )

    projected = model._project_messages([message])

    assert projected[0].content[0] == {"type": "text", "text": "请看图片"}
    assert projected[0].content[2] == {
        "type": "image_url",
        "image_url": {"url": "data:image/webp;base64,preview"},
    }
