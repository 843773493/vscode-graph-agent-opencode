from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Send

from app.core.checkpoint_config import build_checkpoint_config
from app.core.path_utils import get_session_path_resolver
from app.domain.itemized.hashing import content_hash
from app.domain.itemized.records import CanonicalItemRecord
from app.prompting import internal_message_factory
from app.schemas.event import ModelTokenUsagePayload
from app.schemas.internal_v2.turn import TurnHistoryLoadRequest
from app.services.business.reasoning_checkpoint_service import (
    persist_standard_assistant_checkpoint,
    persist_user_message_checkpoint,
)
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.checkpoint.reader import (
    RolloutContextReader,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.storage.service import RolloutStorage
from app.services.infrastructure.rollout_history_reader import RolloutHistoryReader
from app.services.mapping.itemized.message_reasoning_merge import (
    merge_canonical_reasoning,
)
from tests.support.message_service import build_message_service

MESSAGE_TIME = datetime(2026, 7, 14, tzinfo=UTC)


@pytest.fixture
def accept_previous_message():
    """先建立真实已提交 root；空库不能伪造一个有 commit 的 checkpoint。"""
    def accept(saver: RolloutCheckpointSaver, session_id: str) -> HumanMessage:
        message = HumanMessage(
            id="msg_previous",
            content="上一条已接受的输入",
            response_metadata={
                "message_id": "msg_previous",
                "created_at": MESSAGE_TIME.isoformat(),
                "updated_at": MESSAGE_TIME.isoformat(),
                "message_metadata": {"turn_id": "turn_previous"},
            },
        )
        saver.accept_turn(
            session_id,
            accepted_ingress_id="msg_previous",
            acceptance_idempotency_key="message:msg_previous",
            payload=message.content,
            turn_id="turn_previous",
            root_item_id="item-msg_previous",
        )
        return message

    return accept


@pytest.mark.asyncio
async def test_persist_user_message_checkpoint_is_idempotent(
    tmp_path,
    session_bundle_factory,
    accept_previous_message,
):
    session_id = "ses_69da9815c55649f082b182e532a22367"
    session_bundle_factory(tmp_path, session_id)
    saver = RolloutCheckpointSaver(sessions_dir=tmp_path)
    previous_message = accept_previous_message(saver, session_id)
    config = build_checkpoint_config(session_id)
    await saver.aput(
        config,
        {
            "channel_values": {"messages": [previous_message]},
            "channel_versions": {"messages": "1"},
            "updated_channels": ["messages"],
            "id": "ckpt-user-input",
        },
        {"source": "test", "step": 1, "writes": {}},
        {"messages": "1"},
    )
    message = HumanMessage(
        content="失败后也必须可重试",
        response_metadata={
            "message_id": "msg_user_checkpoint",
            "created_at": MESSAGE_TIME.isoformat(),
            "updated_at": MESSAGE_TIME.isoformat(),
        },
    )

    assert persist_user_message_checkpoint(
        checkpointer=saver,
        session_id=session_id,
        message=message,
    ) is True
    assert persist_user_message_checkpoint(
        checkpointer=saver,
        session_id=session_id,
        message=message,
    ) is False
    latest = await saver.aget_tuple(config)
    assert latest is not None
    messages = latest.checkpoint["channel_values"]["messages"]
    assert [item.response_metadata["message_id"] for item in messages] == [
        "msg_previous", "msg_user_checkpoint"
    ]


def test_persist_user_message_checkpoint_preserves_internal_acceptance_metadata(
    tmp_path,
    session_bundle_factory,
):
    session_id = "ses_3754dc4e89eb4a668c294421ee2ae558"
    session_bundle_factory(tmp_path, session_id)
    saver = RolloutCheckpointSaver(sessions_dir=tmp_path)
    message = HumanMessage(
        content="<system_reminder>继续 Goal</system_reminder>",
        response_metadata={
            "message_id": "msg_internal_goal",
            "created_at": MESSAGE_TIME.isoformat(),
            "updated_at": MESSAGE_TIME.isoformat(),
            "message_metadata": {
                "internal": True,
                "turn_id": "job_internal_goal",
                "job_id": "job_internal_goal",
            },
        },
    )

    assert persist_user_message_checkpoint(
        checkpointer=saver,
        session_id=session_id,
        message=message,
    ) is False

    database_path = (
        get_session_path_resolver(tmp_path).resolve_session_node(session_id)
        / "rollout"
        / "index.sqlite"
    )
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT metadata_json FROM item_catalog WHERE item_id = ?",
            ("item-msg_internal_goal",),
        ).fetchone()
    assert row is not None
    assert json.loads(row[0])["message_metadata"]["internal"] is True

    page = RolloutHistoryReader(
        RolloutContextReader(
            RolloutStorage(tmp_path, message_codec=LangChainMessageCodec())
        )
    ).load(
        session_id,
        TurnHistoryLoadRequest(direction="tail", turns=1),
    )
    assert len(page.items) == 1
    assert page.items[0].user_messages == []


@pytest.mark.asyncio
async def test_persist_user_message_checkpoint_discards_stale_execution_tasks(
    tmp_path,
    session_bundle_factory,
    accept_previous_message,
):
    session_id = "ses_7b2f2f84bc35456e8071c691db7f2ca8"
    session_bundle_factory(tmp_path, session_id)
    saver = RolloutCheckpointSaver(sessions_dir=tmp_path)
    previous_message = accept_previous_message(saver, session_id)
    config = build_checkpoint_config(session_id)
    stale_send = Send(
        "tools",
        {
            "name": "exec_command",
            "id": "call_stale",
            "args": {"cmd": "pwd"},
        },
    )
    await saver.aput(
        config,
        {
            "channel_values": {
                "messages": [previous_message],
                "__pregel_tasks": [stale_send],
            },
            "channel_versions": {"messages": "1", "__pregel_tasks": "2"},
            "updated_channels": ["__pregel_tasks"],
            "pending_sends": [stale_send],
            "id": "ckpt-stale-task",
        },
        {"source": "test", "step": 1, "writes": {}},
        {"messages": "1", "__pregel_tasks": "2"},
    )
    message = HumanMessage(
        content="失败后重新开始",
        response_metadata={
            "message_id": "msg_after_stale_task",
            "created_at": MESSAGE_TIME.isoformat(),
            "updated_at": MESSAGE_TIME.isoformat(),
        },
    )

    assert persist_user_message_checkpoint(
        checkpointer=saver,
        session_id=session_id,
        message=message,
    ) is True

    latest = await saver.aget_tuple(config)
    assert latest is not None
    checkpoint = latest.checkpoint
    assert "__pregel_tasks" not in checkpoint["channel_values"]
    assert "__pregel_tasks" not in checkpoint["channel_versions"]
    assert checkpoint["updated_channels"] == ["messages"]
    assert checkpoint["pending_sends"] == []
    assert [
        item.response_metadata["message_id"]
        for item in checkpoint["channel_values"]["messages"]
    ] == ["msg_previous", "msg_after_stale_task"]


@pytest.mark.asyncio
async def test_persist_standard_assistant_checkpoint_rewrites_latest_message(
    tmp_path,
    session_bundle_factory,
):
    session_id = "ses_68b6d4ac434b450b8327ae2775447777"
    session_bundle_factory(tmp_path, session_id)
    reasoning_text = "用户只要求回复 OK。"
    final_text = "OK"
    saver = RolloutCheckpointSaver(sessions_dir=tmp_path)
    config = build_checkpoint_config(session_id)
    checkpoint = {
        "channel_values": {
            "messages": [
                HumanMessage(
                    content="只回复 OK",
                    response_metadata={
                        "message_id": "msg_user",
                        "created_at": MESSAGE_TIME.isoformat(),
                        "updated_at": MESSAGE_TIME.isoformat(),
                    },
                ),
                    AIMessage(
                        content=final_text,
                        name="default",
                        response_metadata={
                            "message_id": "msg_intermediate",
                            "created_at": MESSAGE_TIME.isoformat(),
                            "updated_at": MESSAGE_TIME.isoformat(),
                        },
                    ),
            ],
        },
        "channel_versions": {"messages": "1"},
        "updated_channels": ["messages"],
        "id": "ckpt-mixed",
    }
    await saver.aput(
        config,
        checkpoint,
        {"source": "test", "step": 1, "writes": {}},
        {"messages": "1"},
    )

    changed = persist_standard_assistant_checkpoint(
        checkpointer=saver,
        session_id=session_id,
        content_blocks=(
            {
                "type": "reasoning",
                "reasoning": reasoning_text,
                "id": "part_reasoning",
                "index": 0,
            },
            {
                "type": "text",
                "text": final_text,
                "id": "part_answer",
                "index": 1,
            },
        ),
        final_text=final_text,
        message_id="msg_assistant",
        message_created_at=MESSAGE_TIME,
        token_usage=ModelTokenUsagePayload(
            input_tokens=100,
            output_tokens=20,
            total_tokens=120,
            cache_read_input_tokens=80,
            model_calls=2,
            reported_model_calls=2,
        ),
    )

    assert changed is True
    plan = saver.compose_committed_context_plan(
        session_id,
        plan_id="plan-standard-assistant",
        include_pending_notices=False,
    )
    canonical_ref_ids = [ref.ref_id for ref in plan.refs]
    assert "item-msg_intermediate" not in canonical_ref_ids
    assert "item-msg_assistant" in canonical_ref_ids
    latest = await saver.aget_tuple(config)
    assert latest is not None
    messages = latest.checkpoint["channel_values"]["messages"]
    assistant = messages[-1]
    assert isinstance(assistant, AIMessage)
    assert assistant.response_metadata["phase"] == "final_answer"
    assert assistant.id == "msg_assistant"
    assert assistant.response_metadata["message_id"] == "msg_assistant"
    assert assistant.response_metadata["supersedes_message_id"] == "msg_intermediate"
    assert isinstance(assistant.response_metadata["created_at"], str)
    assert assistant.response_metadata["updated_at"] == assistant.response_metadata[
        "created_at"
    ]
    assert assistant.tool_calls == []
    assert assistant.content == [
        {
            "type": "reasoning_content",
            "reasoning_content": reasoning_text,
        },
        {"type": "text", "text": final_text},
    ]

    messages_page = await build_message_service(tmp_path, checkpointer=saver).list(session_id, limit=10)
    assert messages_page.items[-1].content == final_text
    assert messages_page.items[-1].metadata["token_usage"] == {
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 120,
        "cache_read_input_tokens": 80,
        "model_calls": 2,
        "reported_model_calls": 2,
    }

    message_service = build_message_service(tmp_path, checkpointer=saver)
    state_snapshot = await message_service.get_agent_state_messages(session_id)
    records = [json.loads(line) for line in state_snapshot.jsonl.splitlines()]
    state_assistant = records[-1]
    assert state_assistant["role"] == "assistant"
    assert state_assistant["response_metadata"]["phase"] == "final_answer"
    # design 5.2：诊断快照保留 reasoning/text carrier，展示文本由历史投影提供。
    assert state_assistant["content"] == [
        {"type": "reasoning", "reasoning": reasoning_text},
        {"type": "text", "text": final_text},
    ]


@pytest.mark.asyncio
async def test_persist_checkpoint_keeps_encrypted_response_reasoning(
    tmp_path,
    session_bundle_factory,
):
    session_id = "ses_d19bb63df7864d998bb7993f182b7426"
    session_bundle_factory(tmp_path, session_id)
    saver = RolloutCheckpointSaver(sessions_dir=tmp_path)
    config = build_checkpoint_config(session_id)
    await saver.aput(
        config,
        {
            "channel_values": {
                "messages": [HumanMessage(content="问题"), AIMessage(content="回答")]
            },
            "channel_versions": {"messages": "1"},
            "updated_channels": ["messages"],
            "id": "ckpt-encrypted",
        },
        {"source": "test", "step": 1, "writes": {}},
        {"messages": "1"},
    )
    reasoning_item = {
        "type": "reasoning",
        "id": "rs_item_001",
        "status": "completed",
        "content": [],
        "encrypted_content": "encrypted-reasoning",
        "summary": [],
    }

    assert persist_standard_assistant_checkpoint(
        checkpointer=saver,
        session_id=session_id,
        content_blocks=(
            reasoning_item,
            {"type": "text", "text": "回答"},
        ),
        final_text="回答",
        message_id="msg_assistant",
        message_created_at=MESSAGE_TIME,
    )
    latest = await saver.aget_tuple(config)
    assert latest is not None
    assistant = latest.checkpoint["channel_values"]["messages"][-1]
    assert assistant.content[0] == {
        "type": "reasoning_items",
        "reasoning_items": [reasoning_item],
    }
    assert assistant.content[1] == {"type": "text", "text": "回答"}
    assert assistant.additional_kwargs == {}


@pytest.mark.asyncio
async def test_persist_checkpoint_preserves_existing_system_reminder_in_agent_state(
    tmp_path,
    session_bundle_factory,
):
    session_id = "ses_718399a9bca34ffb8b970cc05f8bafca"
    session_bundle_factory(tmp_path, session_id)
    first_reasoning = "先调用工具。"
    final_reasoning = "只回复工具 stdout。"
    final_text = "OK"
    reminder = "以下工具调用已完成，请在生成回复时参考其结果。"
    tool_call = {
        "name": "python_exec",
        "args": {"code": "print('OK')"},
        "id": "call_1",
        "type": "tool_call",
    }
    user_message = HumanMessage(
        content="调用工具",
        response_metadata={
            "message_id": "msg_user",
            "created_at": MESSAGE_TIME.isoformat(),
            "updated_at": MESSAGE_TIME.isoformat(),
        },
    )
    tool_call_message = AIMessage(
        content=[{"type": "reasoning", "reasoning": first_reasoning}],
        tool_calls=[tool_call],
        name="default",
        response_metadata={
            "phase": "commentary",
            "message_id": "msg_tool_call",
            "created_at": MESSAGE_TIME.isoformat(),
            "updated_at": MESSAGE_TIME.isoformat(),
        },
    )
    tool_message = ToolMessage(
        content='{"stdout":"OK\\n"}',
        tool_call_id="call_1",
        name="python_exec",
    )
    final_message = AIMessage(
        content=final_text,
        name="default",
        response_metadata={
            "message_id": "msg_intermediate_final",
            "created_at": MESSAGE_TIME.isoformat(),
            "updated_at": MESSAGE_TIME.isoformat(),
        },
    )

    saver = RolloutCheckpointSaver(sessions_dir=tmp_path)
    config = build_checkpoint_config(session_id)
    prepared_reminder = internal_message_factory.build(
        kind="checkpoint_reminder",
        control=reminder,
    )
    checkpoint = {
        "channel_values": {
            "messages": [
                user_message,
                tool_call_message,
                tool_message,
                HumanMessage(
                    content=prepared_reminder.content,
                    response_metadata=prepared_reminder.metadata,
                ),
                final_message,
            ],
        },
        "channel_versions": {"messages": "1"},
        "updated_channels": ["messages"],
        "id": "ckpt-reminder",
    }
    await saver.aput(
        config,
        checkpoint,
        {"source": "test", "step": 1, "writes": {}},
        {"messages": "1"},
    )

    changed = persist_standard_assistant_checkpoint(
        checkpointer=saver,
        session_id=session_id,
        content_blocks=(
            {
                "type": "reasoning",
                "reasoning": final_reasoning,
                "id": "part_final_reasoning",
                "index": 0,
            },
            {
                "type": "text",
                "text": final_text,
                "id": "part_final_answer",
                "index": 1,
            },
        ),
        final_text=final_text,
        message_id="msg_assistant",
        message_created_at=MESSAGE_TIME,
    )

    assert changed is True
    state_snapshot = await build_message_service(tmp_path, checkpointer=saver).get_agent_state_messages(
        session_id
    )
    records = [json.loads(line) for line in state_snapshot.jsonl.splitlines()]
    assert len(records) == 6
    assert records[3]["role"] == "user"
    assert "<system_reminder>" in records[3]["content"]
    assert reminder in records[3]["content"]
    assert records[-1]["content"] == [
        {"type": "reasoning", "reasoning": final_reasoning},
        {"type": "text", "text": final_text},
    ]
    assert first_reasoning not in json.dumps(records[-1], ensure_ascii=False)

    visible_messages = await build_message_service(tmp_path, checkpointer=saver).list(
        session_id,
        limit=10,
    )
    assert all("<system_reminder>" not in item.content for item in visible_messages.items)


@pytest.fixture
def reasoning_items():
    """输入顺序来自已提交 selection；物理 sequence 故意与它不同。"""
    def make(*indices: int) -> list[CanonicalItemRecord]:
        items = [
            CanonicalItemRecord.create(
                item_sequence=10 - position,
                item_id=f"reasoning-{index}",
                semantic_kind="reasoning",
                payload_kind="text",
                status="completed",
                producer_ref={
                    "producer_kind": "provider",
                    "producer_id": "test-model",
                    "invocation_id": "model-call-1",
                },
                payload=f"思考 {index}",
                created_at=MESSAGE_TIME.isoformat(),
                metadata={"block_id": f"reasoning-{index}", "block_index": index},
                turn_id="turn-1",
                turn_scope="turn_member",
            )
            for position, index in enumerate(indices)
        ]
        items.append(CanonicalItemRecord.create(
            item_sequence=11,
            item_id="tool-call-item",
            semantic_kind="tool_call",
            payload_kind="tool_call",
            status="completed",
            producer_ref={
                "producer_kind": "provider",
                "producer_id": "test-model",
                "invocation_id": "model-call-1",
            },
            payload={"tool_calls": [{"id": "call-1", "name": "read", "args": {}}]},
            created_at=MESSAGE_TIME.isoformat(),
            turn_id="turn-1",
            turn_scope="turn_member",
        ))
        return items

    return make


def test_agent_state_reasoning_merge_preserves_selection_order(reasoning_items):
    items = reasoning_items(0, 2)
    message = AIMessage(
        content="",
        tool_calls=[{"id": "call-1", "name": "read", "args": {}}],
    )
    result = merge_canonical_reasoning([message], items)
    assert [block["id"] for block in result[0].content] == [
        "reasoning-0", "reasoning-2",
    ]
    assert message.content == ""
    assert [item.item_sequence for item in items] == [10, 9, 11]
    assert result[0].tool_calls == message.tool_calls


def test_agent_state_reasoning_merge_preserves_text_and_protected_carrier(reasoning_items):
    protected = {
        "type": "reasoning", "id": "reasoning-0", "index": 0,
        "reasoning": "思考 0", "signature": "protected-signature",
        "extras": {"opaque": "不得丢失"},
    }
    text = {"type": "text", "text": "调用前说明", "index": 1, "id": "text-1"}
    message = AIMessage(
        content=[protected, text],
        tool_calls=[{"id": "call-1", "name": "read", "args": {}}],
    )
    items = reasoning_items(0, 2)
    original_items = [item.to_dict() for item in items]
    result = merge_canonical_reasoning([message], items)
    assert result[0].content == [protected, text, {
        "type": "reasoning", "reasoning": "思考 2", "id": "reasoning-2", "index": 2,
    }]
    assert message.content == [protected, text]
    assert [item.to_dict() for item in items] == original_items
    assert merge_canonical_reasoning(result, items)[0].content == result[0].content


def test_agent_state_reasoning_merge_reuses_matching_provider_carrier(reasoning_items):
    message = AIMessage(
        content=[
            {
                "type": "reasoning_content",
                "reasoning_content": "思考 0",
            }
        ],
        tool_calls=[{"id": "call-1", "name": "read", "args": {}}],
    )
    result = merge_canonical_reasoning([message], reasoning_items(0))
    assert result[0].content == message.content
    assert result[0].response_metadata["reasoning_source"] == "canonical_item_stream"


def test_agent_state_reasoning_merge_deduplicates_stream_and_checkpoint_items(
    reasoning_items,
):
    items = reasoning_items(0)
    items.insert(
        1,
        replace(
            items[0],
            item_id="reasoning-0-checkpoint",
            metadata={
                "execution_confirmed": True,
                "projection_group": {"content_form": "list", "ordinal": 0, "size": 2},
            },
        ),
    )
    message = AIMessage(
        content=[
            {
                "type": "reasoning_content",
                "reasoning_content": "思考 0",
            }
        ],
        tool_calls=[{"id": "call-1", "name": "read", "args": {}}],
    )
    result = merge_canonical_reasoning([message], items)
    assert result[0].content == message.content


def test_agent_state_reasoning_merge_excludes_final_carrier_parts_from_tool_message(
    reasoning_items,
):
    items = reasoning_items(0)
    post_tool = replace(
        items[0],
        item_id="reasoning-after-tool",
        item_sequence=8,
        payload="工具返回后的 reasoning",
        content_hash=content_hash("text", "工具返回后的 reasoning"),
        metadata={"block_id": "part-after-tool", "block_index": 0},
    )
    items.insert(1, post_tool)
    items.append(
        CanonicalItemRecord.create(
            item_sequence=12,
            item_id="final-assistant",
            semantic_kind="assistant_output",
            payload_kind="structured_content",
            status="completed",
            producer_ref={
                "producer_kind": "provider",
                "producer_id": "final-assistant",
                "invocation_id": "turn-1",
            },
            payload=[
                {"reasoning_content": "工具返回后的 reasoning", "type": "reasoning_content"},
                {"text": "最终答复", "type": "text"},
            ],
            metadata={
                "phase": "final_answer",
                "content_part_refs": [
                    {"id": "part-after-tool", "index": 0},
                    {"id": "part-final-text", "index": 1},
                ],
            },
            turn_id="turn-1",
            turn_scope="turn_member",
            wire_role="assistant",
        )
    )
    message = AIMessage(
        content=[
            {
                "type": "reasoning_content",
                "reasoning_content": "思考 0",
            }
        ],
        tool_calls=[{"id": "call-1", "name": "read", "args": {}}],
    )

    result = merge_canonical_reasoning([message], items)

    assert result[0].content == message.content
    assert result[0].response_metadata["reasoning_source"] == "canonical_item_stream"


@pytest.mark.parametrize("indices", [(0, 2), (2, 0)])
def test_agent_state_reasoning_merge_keeps_ambiguous_part_order_explicit(
    reasoning_items,
    indices,
):
    message = AIMessage(
        content="缺少 content-part 顺序的正文",
        tool_calls=[{"id": "call-1", "name": "read", "args": {}}],
    )
    result = merge_canonical_reasoning([message], reasoning_items(*indices))
    assert result[0].content == message.content
    assert result[0].response_metadata["reasoning_merge_status"] == (
        "skipped_ambiguous_order"
    )
    assert result[0].response_metadata["reasoning_merge_reason"] == (
        "content_part_index_missing"
    )
    assert message.content == result[0].content


def test_agent_state_reasoning_merge_keeps_conflicting_part_order_explicit(
    reasoning_items,
):
    message = AIMessage(
        content=[{"type": "text", "text": "调用前说明", "index": 1}],
        tool_calls=[{"id": "call-1", "name": "read", "args": {}}],
    )
    result = merge_canonical_reasoning([message], reasoning_items(2, 0))
    assert result[0].content == message.content
    assert result[0].response_metadata["reasoning_merge_status"] == (
        "skipped_ambiguous_order"
    )
    assert result[0].response_metadata["reasoning_merge_reason"] == (
        "content_part_order_conflict"
    )
    assert message.content == result[0].content
