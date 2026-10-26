from copy import deepcopy

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

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
                    "file_id": "boxteam://workspace/019d1b4a-1111-7000-8000-000000000001/resources/session/ses_043a718774c572bd8a25adbeb1bfcd5c/attachments/att_7f071235805fbf58a2524f77bff76e13",
                },
            },
            {
                "type": "image_url",
                "image_url": {"url": "data:image/webp;base64,preview"},
                "metadata": {
                    "origin": "generated",
                    "kind": "attachment_preview",
                    "schema_version": 1,
                    "file_id": "boxteam://workspace/019d1b4a-1111-7000-8000-000000000001/resources/session/ses_043a718774c572bd8a25adbeb1bfcd5c/attachments/att_7f071235805fbf58a2524f77bff76e13",
                },
            },
        ],
        response_metadata={
            "attachments": [
                {
                    "file_id": "boxteam://workspace/019d1b4a-1111-7000-8000-000000000001/resources/session/ses_043a718774c572bd8a25adbeb1bfcd5c/attachments/att_7f071235805fbf58a2524f77bff76e13",
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

    projected, media = model._convert_messages_to_dicts([message])

    assert projected["content"] == [
        {"type": "text", "text": "读取完成"},
    ]
    assert media["role"] == "user"
    assert media["content"][1] == {"type": "image_url", "image_url": {"url": "data:image/png;base64,eA=="}}
    assert "read-file-call" in media["content"][0]["text"]


@pytest.fixture
def multimodal_tool_result():
    return ToolMessage(
        content=[
            {"type": "text", "text": "完整回执"},
            {"type": "image", "base64": "eA==", "mime_type": "image/png"},
            {"type": "audio", "base64": "YXVkaW8=", "mime_type": "audio/wav"},
            {"type": "file", "url": "https://example.invalid/report", "mime_type": "text/plain"},
        ],
        artifact={"structured_content": {"receipt": 3}},
        tool_call_id="media-call", name="media", status="error",
    )


@pytest.mark.parametrize("as_dict", [False, True])
def test_chat_tool_results_preserve_media_in_actual_wire(multimodal_tool_result, as_dict):
    message = multimodal_tool_result
    source = deepcopy(message)
    model = BoxteamLiteLLMChatModel(
        model="openai/test-model", api_key="test-key", api_base="https://example.com/v1",
        image_input_replay=True,
    )
    value = {"role": "tool", "content": message.content, "tool_call_id": message.tool_call_id} if as_dict else message
    projected, media = model._convert_messages_to_dicts([value])
    assert projected["tool_call_id"] == message.tool_call_id
    assert projected["content"] == [
        {"type": "text", "text": "完整回执"},
        {"type": "text", "text": "[资源引用：https://example.invalid/report，类型：text/plain]"},
    ]
    assert media["role"] == "user" and "media-call" in media["content"][0]["text"]
    assert media["content"][1:] == [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,eA=="}},
        {"type": "input_audio", "input_audio": {"data": "YXVkaW8=", "format": "wav"}},
    ]
    assert message == source


def test_responses_reports_each_media_type_and_keeps_resource_link(multimodal_tool_result):
    message = multimodal_tool_result
    source = deepcopy(message)
    model = BoxteamOpenAIResponsesModel(
        model="openai/test-model", api_key="test-key", api_base="https://example.com/v1",
    )
    assistant = AIMessage(content="", tool_calls=[{"name": "media", "args": {}, "id": message.tool_call_id}])
    projected = model._history_messages([assistant, message])[-1]
    assert isinstance(projected.content, str)
    assert "完整回执" in projected.content
    assert "1 个图片媒体" in projected.content and "1 个音频媒体" in projected.content
    assert "https://example.invalid/report" in projected.content and "text/plain" in projected.content
    assert projected.artifact == message.artifact
    assert message == source
