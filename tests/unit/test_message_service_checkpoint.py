"""MessageService 与 checkpoint 集成单元测试。"""
from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.core.checkpoint_saver import FileSystemCheckpointSaver
from app.schemas.public_v2.message import AttachmentRef
from app.services.business.message_service import MessageService


MESSAGE_TIME = "2026-07-14T00:00:00+00:00"


def _visible_metadata(message_id: str, **extra: object) -> dict[str, object]:
    return {
        "message_id": message_id,
        "created_at": MESSAGE_TIME,
        "updated_at": MESSAGE_TIME,
        **extra,
    }


@pytest.mark.asyncio
async def test_message_service_loads_history_from_checkpoint(tmp_path):
    saver = FileSystemCheckpointSaver(base_dir=tmp_path)
    config = {"configurable": {"thread_id": "sess1", "checkpoint_ns": ""}}
    checkpoint = {
        "channel_values": {
            "messages": [
                HumanMessage(
                    content="hi",
                    response_metadata=_visible_metadata("msg_user"),
                ),
                AIMessage(
                    content="hello",
                    response_metadata=_visible_metadata(
                        "msg_assistant",
                        phase="text",
                    ),
                ),
            ],
        },
        "channel_versions": {"messages": 1},
        "updated_channels": ["messages"],
        "id": "ckpt-1",
    }
    await saver.aput(
        config,
        checkpoint,
        {"source": "test", "step": 1, "writes": {}},
        {"messages": 1},
    )

    service = MessageService(checkpointer=saver)
    messages = await service.list(session_id="sess1", limit=10)

    assert len(messages.items) == 2
    assert messages.items[0].role.value == "user"
    assert messages.items[0].content == "hi"
    assert messages.items[1].role.value == "assistant"
    assert messages.items[1].content == "hello"
    assert messages.items[1].metadata.get("phase") == "text"


@pytest.mark.asyncio
async def test_message_service_returns_empty_when_no_checkpoint(tmp_path):
    saver = FileSystemCheckpointSaver(base_dir=tmp_path)
    service = MessageService(checkpointer=saver)
    messages = await service.list(session_id="sess_noexist", limit=10)
    assert messages.items == []


@pytest.mark.asyncio
async def test_message_service_rejects_visible_message_without_persisted_identity(
    tmp_path,
):
    saver = FileSystemCheckpointSaver(base_dir=tmp_path)
    config = {
        "configurable": {"thread_id": "sess_invalid_message", "checkpoint_ns": ""}
    }
    checkpoint = {
        "channel_values": {"messages": [HumanMessage(content="hi")]},
        "channel_versions": {"messages": 1},
        "updated_channels": ["messages"],
        "id": "ckpt-invalid-message",
    }
    await saver.aput(
        config,
        checkpoint,
        {"source": "test", "step": 1, "writes": {}},
        {"messages": 1},
    )

    with pytest.raises(RuntimeError, match="缺少持久化 message_id"):
        await MessageService(checkpointer=saver).list(
            session_id="sess_invalid_message",
            limit=10,
        )


@pytest.mark.asyncio
async def test_agent_context_state_applies_summarization_event(tmp_path):
    saver = FileSystemCheckpointSaver(base_dir=tmp_path)
    config = {"configurable": {"thread_id": "sess_compacted", "checkpoint_ns": ""}}
    checkpoint = {
        "channel_values": {
            "messages": [
                HumanMessage(content="旧问题"),
                AIMessage(content="旧回答"),
                HumanMessage(content="保留问题"),
                AIMessage(content="保留回答"),
            ],
            "_summarization_event": {
                "cutoff_index": 2,
                "summary_message": HumanMessage(
                    content="旧上下文摘要",
                    additional_kwargs={"lc_source": "summarization"},
                ),
                "file_path": "/conversation_history/sess_compacted.md",
            },
        },
        "channel_versions": {"messages": 1, "_summarization_event": 1},
        "updated_channels": ["_summarization_event"],
        "id": "ckpt-compacted",
    }
    await saver.aput(
        config,
        checkpoint,
        {"source": "test", "step": 1, "writes": {}},
        {"messages": 1, "_summarization_event": 1},
    )

    state = await MessageService(checkpointer=saver).get_agent_context_state(
        "sess_compacted"
    )

    assert state["checkpoint_id"] == "ckpt-compacted"
    assert state["raw_message_count"] == 4
    assert state["compacted"] is True
    assert state["compaction_cutoff"] == 2
    assert state["history_file_path"] == "/conversation_history/sess_compacted.md"
    assert [record["content"] for record in state["records"]] == [
        "旧上下文摘要",
        "保留问题",
        "保留回答",
    ]


@pytest.mark.asyncio
async def test_message_service_dedupes_visible_messages_by_message_id(tmp_path):
    saver = FileSystemCheckpointSaver(base_dir=tmp_path)
    config = {"configurable": {"thread_id": "sess_dedupe", "checkpoint_ns": ""}}
    user_message = HumanMessage(
        content="请调用 test_tool_2",
        response_metadata=_visible_metadata("msg_user_001"),
    )
    checkpoint = {
        "channel_values": {
            "messages": [
                user_message,
                user_message.model_copy(update={"id": "langchain-copy-id"}),
                AIMessage(
                    content="4568",
                    response_metadata=_visible_metadata("msg_assistant_001"),
                ),
            ],
        },
        "channel_versions": {"messages": 1},
        "updated_channels": ["messages"],
        "id": "ckpt-dedupe",
    }
    await saver.aput(
        config,
        checkpoint,
        {"source": "test", "step": 1, "writes": {}},
        {"messages": 1},
    )

    service = MessageService(checkpointer=saver)
    messages = await service.list(session_id="sess_dedupe", limit=10)

    assert [(item.role.value, item.message_id) for item in messages.items] == [
        ("user", "msg_user_001"),
        ("assistant", "msg_assistant_001"),
    ]


@pytest.mark.asyncio
async def test_agent_state_dedupes_consecutive_duplicate_records(tmp_path):
    saver = FileSystemCheckpointSaver(base_dir=tmp_path)
    config = {"configurable": {"thread_id": "sess_state_dedupe", "checkpoint_ns": ""}}
    user_message = HumanMessage(
        content="请调用 test_tool_2",
        response_metadata=_visible_metadata("msg_user_001"),
    )
    checkpoint = {
        "channel_values": {
            "messages": [
                user_message,
                user_message.model_copy(update={"id": "langchain-copy-id"}),
                AIMessage(
                    content="4568",
                    response_metadata=_visible_metadata("msg_assistant_001"),
                ),
            ],
        },
        "channel_versions": {"messages": 1},
        "updated_channels": ["messages"],
        "id": "ckpt-state-dedupe",
    }
    await saver.aput(
        config,
        checkpoint,
        {"source": "test", "step": 1, "writes": {}},
        {"messages": 1},
    )

    service = MessageService(checkpointer=saver)
    state_snapshot = await service.get_agent_state_messages("sess_state_dedupe")
    records = [json.loads(line) for line in state_snapshot.jsonl.splitlines()]

    assert state_snapshot.message_count == 2
    assert [record["role"] for record in records] == ["user", "assistant"]
    assert records[0]["response_metadata"]["message_id"] == "msg_user_001"


@pytest.mark.asyncio
async def test_message_service_extracts_responses_api_reasoning_blocks(tmp_path):
    """验证 Responses API 路径下产生的 reasoning 块被正确提取到 metadata。"""
    saver = FileSystemCheckpointSaver(base_dir=tmp_path)
    config = {"configurable": {"thread_id": "sess_reasoning", "checkpoint_ns": ""}}
    reasoning_msg = AIMessage(
        content=[
            {
                "type": "reasoning",
                "id": "rs_abc123",
                "summary": [{"type": "summary_text", "text": "先思考一下"}],
            },
            {"type": "output_text", "text": "最终回答"},
        ],
        response_metadata=_visible_metadata("msg_001"),
    )
    checkpoint = {
        "channel_values": {
            "messages": [
                HumanMessage(
                    content="hi",
                    response_metadata=_visible_metadata("msg_user"),
                ),
                reasoning_msg,
            ]
        },
        "channel_versions": {"messages": 1},
        "updated_channels": ["messages"],
        "id": "ckpt-r",
    }
    await saver.aput(
        config,
        checkpoint,
        {"source": "test", "step": 1, "writes": {}},
        {"messages": 1},
    )

    service = MessageService(checkpointer=saver)
    messages = await service.list(session_id="sess_reasoning", limit=10)

    assert len(messages.items) == 2
    assistant = messages.items[1]
    assert assistant.content == "最终回答"
    assert assistant.metadata.get("reasoning_id") == "rs_abc123"
    content_blocks = assistant.metadata.get("content_blocks")
    assert isinstance(content_blocks, list)
    assert content_blocks == [
        {"type": "reasoning", "reasoning": "先思考一下", "id": "rs_abc123"},
        {"type": "text", "text": "最终回答"},
    ]


@pytest.mark.asyncio
async def test_agent_state_renders_standard_reasoning_tool_call_message(tmp_path):
    """工具调用前的 reasoning 应来自标准 content block。"""
    saver = FileSystemCheckpointSaver(base_dir=tmp_path)
    config = {"configurable": {"thread_id": "sess_tool_reasoning", "checkpoint_ns": ""}}
    reasoning_text = "用户想查看当前系统时间。"
    tool_call = {
        "name": "python_exec",
        "args": {"code": "print('ok')"},
        "id": "call_001",
        "type": "tool_call",
    }
    reasoning_msg = AIMessage(
        content=[{"type": "reasoning", "reasoning": reasoning_text}],
        tool_calls=[tool_call],
        response_metadata={"phase": "commentary"},
        name="default",
    )
    checkpoint = {
        "channel_values": {
            "messages": [
                HumanMessage(
                    content="现在几点",
                    response_metadata=_visible_metadata("msg_user"),
                ),
                reasoning_msg,
            ]
        },
        "channel_versions": {"messages": 1},
        "updated_channels": ["messages"],
        "id": "ckpt-tool-reasoning",
    }
    await saver.aput(
        config,
        checkpoint,
        {"source": "test", "step": 1, "writes": {}},
        {"messages": 1},
    )

    service = MessageService(checkpointer=saver)
    messages = await service.list(session_id="sess_tool_reasoning", limit=10)
    assert len(messages.items) == 1
    assert messages.items[0].role.value == "user"

    state_snapshot = await service.get_agent_state_messages("sess_tool_reasoning")
    records = [json.loads(line) for line in state_snapshot.jsonl.splitlines()]
    state_assistant = records[1]
    assert state_assistant["content"] == [
        {"type": "reasoning", "reasoning": reasoning_text}
    ]
    assert state_assistant["tool_calls"] == [tool_call]
    assert state_assistant["response_metadata"]["phase"] == "commentary"
    assert "additional_kwargs" not in state_assistant


@pytest.mark.asyncio
async def test_message_service_hides_empty_assistant_tool_call_messages(tmp_path):
    saver = FileSystemCheckpointSaver(base_dir=tmp_path)
    config = {"configurable": {"thread_id": "sess_tool_hidden", "checkpoint_ns": ""}}
    tool_call_message = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "test_tool_2",
                "args": {},
                "id": "call_001",
                "type": "tool_call",
            }
        ],
        response_metadata={"phase": "commentary"},
    )
    final_message = AIMessage(
        content=[
            {"type": "reasoning", "reasoning": "工具返回 4568。"},
            {"type": "text", "text": "4568"},
        ],
        response_metadata=_visible_metadata(
            "msg_assistant",
            phase="final_answer",
        ),
    )
    checkpoint = {
        "channel_values": {
            "messages": [
                HumanMessage(
                    content="请调用 test_tool_2",
                    response_metadata=_visible_metadata("msg_user"),
                ),
                tool_call_message,
                final_message,
            ]
        },
        "channel_versions": {"messages": 1},
        "updated_channels": ["messages"],
        "id": "ckpt-tool-hidden",
    }
    await saver.aput(
        config,
        checkpoint,
        {"source": "test", "step": 1, "writes": {}},
        {"messages": 1},
    )

    service = MessageService(checkpointer=saver)
    messages = await service.list(session_id="sess_tool_hidden", limit=10)

    assert [message.role.value for message in messages.items] == ["user", "assistant"]
    assert messages.items[-1].content == "4568"


@pytest.mark.asyncio
async def test_message_service_preserves_image_blocks_in_agent_state(tmp_path):
    """多模态用户消息应在 Agent State 中保留 image_url 块。"""
    saver = FileSystemCheckpointSaver(base_dir=tmp_path)
    config = {"configurable": {"thread_id": "sess_image", "checkpoint_ns": ""}}
    image_block = {
        "type": "image_url",
        "image_url": {"url": "data:image/jpeg;base64,abc123"},
    }
    user_message = HumanMessage(
        content=[
            {"type": "text", "text": "请描述图片"},
            image_block,
        ],
        response_metadata={
            **_visible_metadata("msg_image"),
            "attachments": [
                AttachmentRef(
                    file_id="assets/test.jpg",
                    name="test.jpg",
                    content_type="image/jpeg",
                ).model_dump(mode="json")
            ],
        },
    )
    checkpoint = {
        "channel_values": {"messages": [user_message]},
        "channel_versions": {"messages": 1},
        "updated_channels": ["messages"],
        "id": "ckpt-image",
    }
    await saver.aput(
        config,
        checkpoint,
        {"source": "test", "step": 1, "writes": {}},
        {"messages": 1},
    )

    service = MessageService(checkpointer=saver)
    messages = await service.list(session_id="sess_image", limit=10)
    assert messages.items[0].content == "请描述图片"
    assert messages.items[0].attachments[0].file_id == "assets/test.jpg"

    state_snapshot = await service.get_agent_state_messages("sess_image")
    records = [json.loads(line) for line in state_snapshot.jsonl.splitlines()]
    assert records[0]["content"] == [
        {"type": "text", "text": "请描述图片"},
        image_block,
    ]
    assert records[0]["response_metadata"]["attachments"][0]["file_id"] == "assets/test.jpg"


@pytest.mark.asyncio
async def test_message_service_uses_display_content_for_user_message_text(tmp_path):
    """用户消息列表应显示原始输入，不把内部附件处理提示混成用户正文。"""
    saver = FileSystemCheckpointSaver(base_dir=tmp_path)
    config = {"configurable": {"thread_id": "sess_video_display", "checkpoint_ns": ""}}
    user_message = HumanMessage(
        content=[
            {"type": "text", "text": "请按时间顺序说明这个视频。"},
            {
                "type": "text",
                "text": "视频附件 demo.mp4 已抽取为 3 个按时间顺序排列的关键帧。",
            },
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,abc123"}},
        ],
        response_metadata={
            **_visible_metadata("msg_video"),
            "display_content": "请按时间顺序说明这个视频。",
            "attachments": [
                AttachmentRef(
                    file_id="assets/demo.mp4",
                    name="demo.mp4",
                    content_type="video/mp4",
                ).model_dump(mode="json")
            ],
        },
    )
    checkpoint = {
        "channel_values": {"messages": [user_message]},
        "channel_versions": {"messages": 1},
        "updated_channels": ["messages"],
        "id": "ckpt-video-display",
    }
    await saver.aput(
        config,
        checkpoint,
        {"source": "test", "step": 1, "writes": {}},
        {"messages": 1},
    )

    service = MessageService(checkpointer=saver)
    messages = await service.list(session_id="sess_video_display", limit=10)
    assert messages.items[0].content == "请按时间顺序说明这个视频。"
    assert "已抽取为" not in messages.items[0].content

    state_snapshot = await service.get_agent_state_messages("sess_video_display")
    records = [json.loads(line) for line in state_snapshot.jsonl.splitlines()]
    assert "display_content" not in records[0].get("response_metadata", {})


@pytest.mark.asyncio
async def test_message_service_refusal_block(tmp_path):
    """验证 refusal 块被识别并标记。"""
    saver = FileSystemCheckpointSaver(base_dir=tmp_path)
    config = {"configurable": {"thread_id": "sess_refusal", "checkpoint_ns": ""}}
    msg = AIMessage(
        content=[{"type": "refusal", "refusal": "我拒绝回答"}],
        response_metadata=_visible_metadata("msg_assistant"),
    )
    checkpoint = {
        "channel_values": {
            "messages": [
                HumanMessage(
                    content="hi",
                    response_metadata=_visible_metadata("msg_user"),
                ),
                msg,
            ]
        },
        "channel_versions": {"messages": 1},
        "updated_channels": ["messages"],
        "id": "ckpt-rf",
    }
    await saver.aput(
        config,
        checkpoint,
        {"source": "test", "step": 1, "writes": {}},
        {"messages": 1},
    )

    service = MessageService(checkpointer=saver)
    messages = await service.list(session_id="sess_refusal", limit=10)

    assert messages.items[1].content == "[拒绝]我拒绝回答"
