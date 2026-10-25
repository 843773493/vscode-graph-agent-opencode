from __future__ import annotations

from app.domain.itemized.execution_queue import ExecutionQueueInput
import json
import sqlite3
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Send

from app.agents.graph_binding import compute_capability_profile_hash
from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.core.path_utils import get_session_path_resolver, workspace_context
from app.core.session_control_store import SessionControlStore
from app.core.session_thread_context import session_thread_owner_scope
from app.core.thread_creation import ThreadCreationService
from app.core.workspace_identity import load_or_create_workspace_id
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.execution_admission import (
    InternalExecutionIntent,
    UserExecutionIdentity,
)
from app.domain.itemized.hashing import content_hash, sha256_jcs
from app.domain.itemized.identity.item_projection import (
    ItemProjectionIdentity,
    ProjectionGroup,
    ProjectionPartRef,
)
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.records import CanonicalItemRecord
from app.prompting import PromptSection, internal_message_factory
from app.schemas.event import ModelTokenUsagePayload
from app.schemas.internal_v2.common import MessageRole
from app.schemas.internal_v2.message import MessageDTO
from app.schemas.internal_v2.turn import TurnHistoryLoadRequest
from app.services.business.reasoning_checkpoint_service import (
    persist_standard_assistant_checkpoint,
    persist_user_message_checkpoint,
)
from app.services.infrastructure.rollout_context.checkpoint.codec.message_channel import (
    checkpoint_messages,
)
from app.services.infrastructure.rollout_context.checkpoint.codec.metadata import (
    producer_response_metadata,
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
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
)
from app.services.infrastructure.rollout_context.store import RolloutContextStore
from app.services.infrastructure.rollout_history_reader import RolloutHistoryReader
from app.services.mapping.itemized.canonical_langchain import project_canonical_items
from app.services.mapping.itemized.message_reasoning_merge import (
    merge_canonical_reasoning,
)
from app.services.mapping.itemized.provider_request import (
    project_runtime_notice_content,
)
from app.services.mapping.itemized.thread_seed import frozen_thread_seed
from app.services.orchestration.execution_step.model_call import StepModelCallAdapter
from tests.support.message_service import build_message_service

MESSAGE_TIME = datetime(2026, 7, 14, tzinfo=UTC)


def _accept_user_turn(
    saver: RolloutCheckpointSaver,
    *,
    session_id: str,
    thread_id: str,
    message_id: str,
    turn_id: str,
    job_id: str,
    payload: str,
):
    execution_id = f"execution-{turn_id}"
    saver.accept_turn(
        session_id,
        thread_id=thread_id,
        accepted_ingress_id=message_id,
        acceptance_idempotency_key=f"acceptance:{message_id}",
        job_id=job_id,
        admission_idempotency_key=f"admission:{message_id}",
        payload=payload,
        turn_id=turn_id,
        root_item_id=f"item-{message_id}",
        initial_execution_id=execution_id,
    )
    return saver.execution_admission(
        session_id,
        thread_id=thread_id,
        execution_id=execution_id,
    )


@pytest.fixture
def accept_previous_message():
    """先建立真实已提交 root；空库不能伪造一个有 commit 的 checkpoint。"""

    def accept(saver: RolloutCheckpointSaver, session_id: str, thread_id: str | None = None) -> HumanMessage:
        thread_id = thread_id or saver._storage.storage.main_thread_id(session_id)
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
            thread_id=thread_id,
            accepted_ingress_id="msg_previous",
            acceptance_idempotency_key="message:msg_previous",
            job_id="job_previous",
            admission_idempotency_key="execution-admission:msg_previous",
            payload=message.content,
            turn_id="turn_previous",
            root_item_id="item-msg_previous",
        )
        return message

    return accept


@pytest.fixture
def accept_runtime_input():
    def accept(saver, sessions_dir, session_id, thread_id, message, job_id, key):
        turn_id = message.response_metadata["message_metadata"]["turn_id"]
        binding = UserExecutionIdentity(job_id=job_id, turn_id=turn_id, admission_idempotency_key=key)
        workspace_root = sessions_dir.parent
        with workspace_context(
            load_or_create_workspace_id(workspace_root), workspace_root,
        ):
            queued_message = MessageDTO(
                message_id=message.response_metadata["message_id"], session_id=session_id, thread_id=thread_id,
                role=MessageRole.user, content=message.content, created_at=MESSAGE_TIME, updated_at=MESSAGE_TIME,
            )
            build_message_service(sessions_dir, checkpointer=saver).accept_user_message(queued_message, execution_identity=binding, queue_input=ExecutionQueueInput(
                execution_binding=binding, message_id=queued_message.message_id, agent_id="default",
                provider_id=None, delivery_policy="after_turn", gateway_id=None, request_id=None,
                created_at=queued_message.created_at.isoformat(),
            ))
        return saver.accepted_user_message(session_id, thread_id=thread_id, job_id=job_id, message_id=message.response_metadata["message_id"])
    return accept


@pytest.mark.asyncio
async def test_persist_user_message_checkpoint_is_idempotent(
    tmp_path,
    session_bundle_factory,
    accept_previous_message,
    accept_runtime_input,
):
    session_id = "ses_019c0d94c86473198d801cbee5588f88"
    session_bundle_factory(tmp_path, session_id)
    saver = RolloutCheckpointSaver(sessions_dir=tmp_path)
    previous_message = accept_previous_message(saver, session_id)
    thread_id = saver._storage.storage.main_thread_id(session_id)
    config = build_checkpoint_config(thread_id)
    with session_thread_owner_scope(
        MutationIntentOwner(session_id, thread_id),
        langgraph_thread_id=thread_id,
    ):
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
            "message_metadata": {"turn_id": "turn_user_checkpoint"},
        },
    )

    message = accept_runtime_input(saver, tmp_path, session_id, thread_id, message, "job_user_checkpoint", "admission-job-user-checkpoint")

    with session_thread_owner_scope(
        MutationIntentOwner(session_id, thread_id),
        langgraph_thread_id=thread_id,
    ):
        assert (
            persist_user_message_checkpoint(
                checkpointer=saver,
                session_id=session_id,
                thread_id=thread_id,
                job_id="job_user_checkpoint",
                admission_idempotency_key="admission-job-user-checkpoint",
                message=message,
            )
            is True
        )
        assert (
            persist_user_message_checkpoint(
                checkpointer=saver,
                session_id=session_id,
                thread_id=thread_id,
                job_id="job_user_checkpoint",
                admission_idempotency_key="admission-job-user-checkpoint",
                message=message,
            )
            is False
        )
    execution_id = saver.execution_for_turn(
        session_id,
        thread_id=thread_id,
        turn_id="turn_user_checkpoint",
    )
    admission = saver.execution_admission(
        session_id,
        thread_id=thread_id,
        execution_id=execution_id,
    )
    assert admission.job_id == "job_user_checkpoint"
    assert admission.turn_id == "turn_user_checkpoint"
    assert admission.job_id != admission.turn_id
    assert admission.admission_idempotency_key == "admission-job-user-checkpoint"
    assert admission.admission_idempotency_key != "message:msg_user_checkpoint"
    with session_thread_owner_scope(
        MutationIntentOwner(session_id, thread_id),
        langgraph_thread_id=thread_id,
    ):
        latest = await saver.aget_tuple(config)
    assert latest is not None
    messages = checkpoint_messages(saver, latest)
    assert [item.response_metadata["message_id"] for item in messages] == [
        "msg_previous",
        "msg_user_checkpoint",
    ]


def test_persist_internal_execution_keeps_notice_outside_user_turn_history(
    tmp_path,
    session_bundle_factory,
):
    session_id = "ses_019c21595f0c70388c187be23ef92b9b"
    session_bundle_factory(tmp_path, session_id)
    saver = RolloutCheckpointSaver(sessions_dir=tmp_path)
    prepared = internal_message_factory.build(
        kind="generated_session_result",
        control="继续 Goal。",
        sections=(
            PromptSection("control_context", {"source": "goal_runtime"}),
            PromptSection("generated_session_result", "继续执行后续步骤。"),
        ),
        display_content="Goal 后续任务继续执行。",
    )
    message = HumanMessage(
        content=prepared.content,
        id="msg_internal_goal",
        response_metadata={
            "message_id": "msg_internal_goal",
            "created_at": MESSAGE_TIME.isoformat(),
            "updated_at": MESSAGE_TIME.isoformat(),
            "message_metadata": prepared.metadata,
        },
    )
    thread_id = get_session_path_resolver(tmp_path).main_thread_id(session_id)
    intent = InternalExecutionIntent(
        message_id="msg_internal_goal",
        execution_id="execution_internal_goal",
        admission_idempotency_key="internal-goal:resume-1",
    )
    saver.admit_internal_execution(session_id, thread_id=thread_id, job_id="job_internal_goal", intent=intent, message=message)
    message = saver.accepted_internal_message(session_id, thread_id=thread_id, job_id="job_internal_goal", message_id=intent.message_id, intent=intent)

    with session_thread_owner_scope(
        MutationIntentOwner(session_id, thread_id),
        langgraph_thread_id=thread_id,
    ):
        assert (
            persist_user_message_checkpoint(
                checkpointer=saver,
                session_id=session_id,
                thread_id=thread_id,
                job_id="job_internal_goal",
                admission_idempotency_key=intent.admission_idempotency_key,
                message=message,
                execution_intent=intent,
            )
            is False
        )

    admission = saver.execution_admission(
        session_id,
        thread_id=thread_id,
        execution_id=intent.execution_id,
    )
    database_path = saver._storage.storage.index_path(session_id, thread_id=thread_id)
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT turn_id, turn_scope, semantic_kind FROM item_catalog "
            "WHERE item_id = ?",
            ("item-msg_internal_goal",),
        ).fetchone()
    assert row is not None
    assert admission.execution_origin == "internal"
    assert admission.turn_id is None
    assert row[:2] == (None, "pending_next_turn")
    assert row[2] == "runtime_notice"
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turns").fetchone() == (0,)
    page = RolloutHistoryReader(
        RolloutContextReader(
            RolloutContextStore(tmp_path, message_codec=LangChainMessageCodec())
        )
    ).load(
        session_id,
        thread_id,
        TurnHistoryLoadRequest(direction="tail", turns=1),
    )
    assert page.items == []


@pytest.mark.asyncio
@pytest.mark.parametrize("reopened", [False, True])
async def test_persist_user_message_checkpoint_discards_stale_execution_tasks(
    tmp_path,
    session_bundle_factory,
    accept_previous_message,
    accept_runtime_input,
    reopened,
):
    session_id = "ses_019bddf6d61b70acb52e1541440e0c6c"
    session_bundle_factory(tmp_path, session_id)
    saver = RolloutCheckpointSaver(sessions_dir=tmp_path)
    previous_message = accept_previous_message(saver, session_id)
    thread_id = saver._storage.storage.main_thread_id(session_id)
    config = build_checkpoint_config(thread_id)
    stale_send = Send(
        "tools",
        {
            "name": "exec_command",
            "id": "call_stale",
            "args": {"cmd": "pwd"},
        },
    )
    with session_thread_owner_scope(
        MutationIntentOwner(session_id, thread_id),
        langgraph_thread_id=thread_id,
    ):
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
            "message_metadata": {"turn_id": "turn_after_stale_task"},
        },
    )
    message = accept_runtime_input(saver, tmp_path, session_id, thread_id, message, "job_after_stale_task", "admission-job-after-stale-task")
    if reopened:
        # 新 Saver 从磁盘读取已接受 root 与旧 checkpoint 控制态。
        saver = RolloutCheckpointSaver(sessions_dir=tmp_path)

    with session_thread_owner_scope(
        MutationIntentOwner(session_id, thread_id),
        langgraph_thread_id=thread_id,
    ):
        assert (
            persist_user_message_checkpoint(
                checkpointer=saver,
                session_id=session_id,
                thread_id=thread_id,
                job_id="job_after_stale_task",
                admission_idempotency_key="admission-job-after-stale-task",
                message=message,
            )
            is True
        )
        assert persist_user_message_checkpoint(
            checkpointer=saver,
            session_id=session_id,
            thread_id=thread_id,
            job_id="job_after_stale_task",
            admission_idempotency_key="admission-job-after-stale-task",
            message=message,
        ) is False

    with session_thread_owner_scope(
        MutationIntentOwner(session_id, thread_id),
        langgraph_thread_id=thread_id,
    ):
        latest = await saver.aget_tuple(config)
    assert latest is not None
    checkpoint = latest.checkpoint
    assert "__pregel_tasks" not in checkpoint["channel_values"]
    assert "__pregel_tasks" not in checkpoint["channel_versions"]
    assert checkpoint["updated_channels"] == ["messages"]
    assert checkpoint["pending_sends"] == []
    assert [
        item.response_metadata["message_id"]
        for item in checkpoint_messages(saver, latest)
    ] == ["msg_previous", "msg_after_stale_task"]


@pytest.fixture
async def seed_execution_intent(tmp_path, session_bundle_factory):
    session_id = "ses_019c21595f0c70388c187be23ef92b9b"
    session_dir = session_bundle_factory(tmp_path, session_id)
    resolver = get_session_path_resolver(tmp_path)
    profile = {"thread_role": "child", "goal_enabled": False}
    with closing(SessionControlStore(session_dir / "session-control.sqlite")) as control:
        child = await ThreadCreationService(
            store=resolver.catalog_store, control_store=control, sessions_root=tmp_path,
            workspace_id=resolver.workspace_id, compute_capability_profile_hash=compute_capability_profile_hash,
        ).create(
            idempotency_key="creation-seed-reference", session_id=session_id, thread_id=None, delegation_id=None,
            initial_state="running", artifact_manifest={}, session_metadata={
                "graph_binding": {"graph_id": "deep-agent", "graph_revision": 1, "graph_schema_hash": "sha256:" + "a" * 64,
                                  "capability_profile_hash": compute_capability_profile_hash(capability_profile=profile)},
                "capability_profile": profile, "task_seed": {"description": "冻结任务", "gateway_id": None, "request_id": None},
                "task_reference": {"parent_session_id": session_id, "parent_thread_id": resolver.main_thread_id(session_id)},
            },
        )
        frozen = frozen_thread_seed(control.get_thread_creation_record("creation-seed-reference"))
    return child.child_thread_id, InternalExecutionIntent(
        message_id="msg_seed_reference",
        execution_id="execution-seed-reference",
        admission_idempotency_key="admission-seed-reference",
        input_reference_json=json.dumps(frozen.reference),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_tasks", [False, True])
async def test_internal_seed_checkpoint_keeps_graph_messages_and_cleans_stale_tasks(
    tmp_path, session_bundle_factory, accept_previous_message, seed_execution_intent, stale_tasks,
):
    session_id = "ses_019c21595f0c70388c187be23ef92b9b"
    saver = RolloutCheckpointSaver(sessions_dir=tmp_path)
    thread_id, seed_execution_intent = seed_execution_intent
    previous_message = accept_previous_message(saver, session_id, thread_id)
    saver.admit_internal_execution(session_id, thread_id=thread_id, job_id="job-seed-reference", intent=seed_execution_intent, message=None)
    config = build_checkpoint_config(thread_id)
    channels = {"messages": [previous_message]}
    versions = {"messages": "1"}
    sends = [Send("tools", {"id": "call_seed_stale", "name": "exec_command", "args": {}})]
    if stale_tasks:
        channels["__pregel_tasks"] = sends
        versions["__pregel_tasks"] = "2"
    with session_thread_owner_scope(
        MutationIntentOwner(session_id, thread_id), langgraph_thread_id=thread_id,
    ):
        await saver.aput(
            config,
            {
                "channel_values": channels,
                "channel_versions": versions,
                "updated_channels": list(channels),
                "pending_sends": sends if stale_tasks else [],
                "id": "ckpt-seed-reference",
            },
            {"source": "test", "step": 1, "writes": {}},
            versions,
        )
        arguments = {
            "checkpointer": saver, "session_id": session_id, "thread_id": thread_id,
            "job_id": "job-seed-reference",
            "admission_idempotency_key": seed_execution_intent.admission_idempotency_key,
            "message": None, "execution_intent": seed_execution_intent,
        }
        assert persist_user_message_checkpoint(**arguments) is stale_tasks
        assert persist_user_message_checkpoint(**arguments) is False
        latest = await saver.aget_tuple(config)
    messages = checkpoint_messages(saver, latest)
    assert len(messages) == 1
    assert messages[0].response_metadata["message_id"] == "msg_previous"
    assert latest.checkpoint["pending_sends"] == []
    assert "__pregel_tasks" not in latest.checkpoint["channel_values"]
    assert "__pregel_tasks" not in latest.checkpoint["channel_versions"]
    admission = saver.execution_admission(
        session_id, thread_id=thread_id, execution_id=seed_execution_intent.execution_id,
    )
    assert (admission.session_id, admission.thread_id) == (session_id, thread_id)
    assert admission.execution_origin == "internal" and admission.turn_id is None
    assert admission.job_id == "job-seed-reference"
    assert [item.item_id for item in saver._storage.storage.read_items(session_id, thread_id)] == ["item-msg_previous"]
    with sqlite3.connect(saver._storage.storage.index_path(session_id, thread_id=thread_id)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turn_records").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM item_catalog").fetchone() == (1,)


@pytest.mark.asyncio
async def test_persist_standard_assistant_checkpoint_rewrites_latest_message(
    tmp_path,
    session_bundle_factory,
):
    session_id = "ses_019c3e8835447c678e07a55a9cc127fc"
    session_bundle_factory(tmp_path, session_id)
    reasoning_text = "用户只要求回复 OK。"
    final_text = "OK"
    saver = RolloutCheckpointSaver(sessions_dir=tmp_path)
    thread_id = saver._storage.storage.main_thread_id(session_id)
    turn_id = "turn-standard-assistant"
    admission = _accept_user_turn(
        saver,
        session_id=session_id,
        thread_id=thread_id,
        message_id="msg_user",
        turn_id=turn_id,
        job_id="job-standard-assistant",
        payload="只回复 OK",
    )
    config = build_checkpoint_config(thread_id)
    checkpoint = {
        "channel_values": {
            "messages": [
                HumanMessage(
                    content="只回复 OK",
                    response_metadata={
                        "message_id": "msg_user",
                        "created_at": MESSAGE_TIME.isoformat(),
                        "updated_at": MESSAGE_TIME.isoformat(),
                        "message_metadata": {"turn_id": turn_id},
                    },
                ),
                AIMessage(
                    content=final_text,
                    name="default",
                    response_metadata={
                        "message_id": "msg_intermediate",
                        "model_call_id": "model-call-final",
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
    with (
        session_thread_owner_scope(
            MutationIntentOwner(session_id, thread_id),
            langgraph_thread_id=thread_id,
        ),
        execution_admission_scope(admission, langgraph_thread_id=thread_id),
    ):
        await saver.aput(
            config,
            checkpoint,
            {"source": "test", "step": 1, "writes": {}},
            {"messages": "1"},
        )

    with (
        session_thread_owner_scope(
            MutationIntentOwner(session_id, thread_id),
            langgraph_thread_id=thread_id,
        ),
        execution_admission_scope(admission, langgraph_thread_id=thread_id),
    ):
        changed = persist_standard_assistant_checkpoint(
            checkpointer=saver,
            session_id=session_id,
            thread_id=thread_id,
            turn_id=turn_id,
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
        thread_id=thread_id,
        plan_id="plan-standard-assistant",
        tool_set=ToolSetSnapshot(selection_revision=sha256_jcs({}), tools=()),
        include_pending_notices=False,
    )
    canonical_ref_ids = [ref.ref_id for ref in plan.refs]
    assert "item-msg_intermediate" not in canonical_ref_ids
    assert "item-msg_assistant" in canonical_ref_ids
    with session_thread_owner_scope(
        MutationIntentOwner(session_id, thread_id),
        langgraph_thread_id=thread_id,
    ):
        latest = await saver.aget_tuple(config)
    assert latest is not None
    messages = checkpoint_messages(saver, latest)
    assistant = messages[-1]
    assert isinstance(assistant, AIMessage)
    assert assistant.response_metadata["phase"] == "final_answer"
    assert assistant.id == "msg_assistant"
    assert assistant.response_metadata["message_id"] == "msg_assistant"
    assert assistant.response_metadata["provenance_relations"][-1] == {
        "relation": "replaces",
        "target_kind": "projection_message",
        "target_id": "msg_intermediate",
        "target_model_call_id": "model-call-final",
    }
    assert isinstance(assistant.response_metadata["created_at"], str)
    assert (
        assistant.response_metadata["updated_at"]
        == assistant.response_metadata["created_at"]
    )
    assert assistant.tool_calls == []
    assert assistant.content == [
        {
            "type": "reasoning_content",
            "reasoning_content": reasoning_text,
        },
        {"type": "text", "text": final_text},
    ]

    messages_page = await build_message_service(tmp_path, checkpointer=saver).list(
        session_id, limit=10
    )
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
@pytest.mark.parametrize("tamper", ["model_call_id", "provenance_relations", "extensions"])
async def test_final_before_persist_convergence_replays_without_typed_provenance_conflict(
    tmp_path,
    session_bundle_factory,
    frozen_provider_activation,
    frozen_provider_wire,
    tamper: str,
) -> None:
    session_id = "ses_019e8f046abe7813889184829fac0231"
    session_bundle_factory(tmp_path, session_id)
    saver = RolloutCheckpointSaver(sessions_dir=tmp_path)
    thread_id = saver._storage.storage.main_thread_id(session_id)
    turn_id = "turn-final-provenance"
    job_id = "job-final-provenance"
    model_call_id = str(uuid4())
    source_message_id = f"lc_run--{model_call_id}"
    final_message_id = "msg_final_provenance"
    admission = _accept_user_turn(
        saver,
        session_id=session_id,
        thread_id=thread_id,
        message_id="msg_provenance_user",
        turn_id=turn_id,
        job_id=job_id,
        payload="请回答",
    )
    source_relation = {
        "relation": "derived_from",
        "target_kind": "projection_message",
        "target_id": "msg_provider_source",
        "target_model_call_id": model_call_id,
    }
    extension = {
        "extension_version": "1",
        "protection": "public",
        "value": {"provider_fact": "不可丢弃的原始事实"},
    }
    config = build_checkpoint_config(thread_id)
    with (
        session_thread_owner_scope(
            MutationIntentOwner(session_id, thread_id),
            langgraph_thread_id=thread_id,
        ),
        execution_admission_scope(admission, langgraph_thread_id=thread_id),
    ):
        saver.put(
            config,
            {
                "channel_values": {
                    "messages": [
                        HumanMessage(
                            id="msg_provenance_user",
                            content="请回答",
                            response_metadata={"message_metadata": {"turn_id": turn_id}},
                        ),
                        AIMessage(
                            id=source_message_id,
                            content="最终回答",
                            response_metadata={
                                "context_revision": "context-final-provenance",
                                "provenance_relations": [source_relation],
                                "extensions": {"example.provider_fact": extension},
                            },
                        ),
                    ]
                },
                "channel_versions": {"messages": "1"},
                "updated_channels": ["messages"],
                "id": "ckpt-provenance-source",
            },
            {"source": "test", "step": 1, "writes": {}},
            {"messages": "1"},
        )
        adapter = StepModelCallAdapter(
            checkpointer=saver,
            session_id=session_id,
            thread_id=thread_id,
            job_id=job_id,
            checkpoint_ns="",
        )
        plan_creation_idempotency_key = f"final-provenance:{model_call_id}"
        manager = ContextSourceManager(
            owner=ContextSourceOwnerKey(session_id, thread_id),
            control_state_port=saver,
        )
        selected = ToolSetSnapshot(selection_revision=sha256_jcs({}), tools=())
        saver.prepare_context_for_provider(
            session_id,
            thread_id,
            turn_id=turn_id,
            execution_id=admission.execution_id,
            plan_creation_idempotency_key=plan_creation_idempotency_key,
            seal_idempotency_key=f"final-provenance-seal:{model_call_id}",
            context_source_manager=manager,
            tool_set_supplier=lambda: selected,
            **frozen_provider_wire("reasoning-checkpoint", "chat_completions"),
            **frozen_provider_activation(
                saver,
                plan_creation_idempotency_key=plan_creation_idempotency_key,
                model_call_id=model_call_id,
            ),
        )
        await adapter.register(model_call_id, 1, "reasoning-checkpoint")
        assert persist_standard_assistant_checkpoint(
            checkpointer=saver,
            session_id=session_id,
            thread_id=thread_id,
            turn_id=turn_id,
            content_blocks=(
                {"type": "text", "text": "最终回答", "id": "part_answer", "index": 0},
            ),
            final_text="最终回答",
            message_id=final_message_id,
            message_created_at=MESSAGE_TIME,
            token_usage=ModelTokenUsagePayload(
                input_tokens=11,
                output_tokens=7,
                total_tokens=18,
                model_calls=1,
                reported_model_calls=1,
            ),
            preserve_content_part_refs=True,
            before_persist=adapter.converge_final_message,
        )
        await adapter.converge_final_checkpoint(final_message_id)
        stored_items = saver._storage.storage.read_items(session_id, thread_id)
        by_id = {item.item_id: item for item in stored_items}
        final_item = by_id[f"item-{final_message_id}"]
        assert by_id[f"item-{source_message_id}"].projection_identity.model_call_id == (
            final_item.projection_identity.model_call_id
        ) == model_call_id
        assert final_item.projection_identity.phase == "final_answer"
        assert final_item.projection_identity.content_part_refs[0].id == "part_answer"
        assert final_item.source_manifest.source_revision == "context-final-provenance"
        assert final_item.extensions["example.provider_fact"] == extension
        assert final_item.extensions["boxteam.checkpoint.annotations"]["value"][
            "token_usage"
        ]["total_tokens"] == 18
        assert [relation.to_dict() for relation in final_item.provenance_relations] == [
            source_relation,
            {
                "relation": "replaces",
                "target_kind": "projection_message",
                "target_id": source_message_id,
                "target_model_call_id": model_call_id,
            },
        ]
        assert final_item.turn_id == turn_id
        assert final_item.producer_ref["invocation_id"] == admission.execution_id
        canonical_before_replay = [item.to_dict() for item in stored_items]
        latest = saver.get_tuple(config)
        assert latest is not None
        replay_checkpoint = {**latest.checkpoint, "id": "ckpt-provenance-replay"}
        saver.put(
            latest.config,
            replay_checkpoint,
            {"source": "test", "step": 2, "writes": {}},
            {},
        )
        assert [
            item.to_dict() for item in saver._storage.storage.read_items(session_id, thread_id)
        ] == canonical_before_replay
        replayed = saver.get_tuple(config)
        assert replayed is not None
        checkpoint_before_failure = deepcopy(replayed.checkpoint)
        config_before_failure = deepcopy(replayed.config)
        metadata_before_failure = deepcopy(replayed.metadata)
        replayed_messages = checkpoint_messages(saver, replayed)
        final_message = replayed_messages[-1]
        assert isinstance(final_message, AIMessage)
        assert final_message.id == final_message_id
        response_metadata = producer_response_metadata(final_message.response_metadata)
        if tamper == "model_call_id":
            response_metadata["model_call_id"] = "model-conflicting"
        elif tamper == "provenance_relations":
            response_metadata["provenance_relations"][-1]["target_id"] = "msg-conflicting"
        else:
            response_metadata["extensions"]["example.provider_fact"]["value"][
                "provider_fact"
            ] = "篡改后的事实"
        conflicting = final_message.model_copy(
            update={"response_metadata": response_metadata}
        )
        conflicting_checkpoint = {
            **replayed.checkpoint,
            "id": "ckpt-provenance-conflicting",
            "channel_values": {
                **replayed.checkpoint["channel_values"],
                "messages": [*replayed_messages[:-1], conflicting],
            },
        }
        jsonl_path = saver._storage.storage.jsonl_path(session_id, "", thread_id=thread_id)
        jsonl_before_failure = jsonl_path.read_bytes()

        with pytest.raises(ValueError, match="typed provenance/extensions 不一致"):
            saver.put(
                replayed.config,
                conflicting_checkpoint,
                {"source": "test", "step": 3, "writes": {}},
                {},
            )

        assert jsonl_path.read_bytes() == jsonl_before_failure
        assert [
            item.to_dict() for item in saver._storage.storage.read_items(session_id, thread_id)
        ] == canonical_before_replay
        latest_after_failure = saver.get_tuple(config)
        assert latest_after_failure is not None
        assert latest_after_failure.checkpoint["id"] == replayed.checkpoint["id"]
        assert latest_after_failure.checkpoint == checkpoint_before_failure
        assert latest_after_failure.config == config_before_failure
        assert latest_after_failure.metadata == metadata_before_failure


@pytest.mark.asyncio
async def test_persist_checkpoint_keeps_encrypted_response_reasoning(
    tmp_path,
    session_bundle_factory,
):
    session_id = "ses_019bfa27d86b7e5e8bef9be8c2075691"
    session_bundle_factory(tmp_path, session_id)
    saver = RolloutCheckpointSaver(sessions_dir=tmp_path)
    thread_id = saver._storage.storage.main_thread_id(session_id)
    turn_id = "turn-encrypted-reasoning"
    admission = _accept_user_turn(
        saver,
        session_id=session_id,
        thread_id=thread_id,
        message_id="msg-encrypted-user",
        turn_id=turn_id,
        job_id="job-encrypted-reasoning",
        payload="问题",
    )
    config = build_checkpoint_config(thread_id)
    with (
        session_thread_owner_scope(
            MutationIntentOwner(session_id, thread_id),
            langgraph_thread_id=thread_id,
        ),
        execution_admission_scope(admission, langgraph_thread_id=thread_id),
    ):
        await saver.aput(
            config,
            {
                "channel_values": {
                    "messages": [
                        HumanMessage(
                            content="问题",
                            id="msg-encrypted-user",
                            response_metadata={
                                "message_metadata": {"turn_id": turn_id}
                            },
                        ),
                        AIMessage(content="回答"),
                    ]
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

    with (
        session_thread_owner_scope(
            MutationIntentOwner(session_id, thread_id),
            langgraph_thread_id=thread_id,
        ),
        execution_admission_scope(admission, langgraph_thread_id=thread_id),
    ):
        assert persist_standard_assistant_checkpoint(
            checkpointer=saver,
            session_id=session_id,
            thread_id=thread_id,
            turn_id=turn_id,
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
    assistant = checkpoint_messages(saver, latest)[-1]
    assert assistant.content[0] == {
        "type": "reasoning_items",
        "reasoning_items": [reasoning_item],
    }
    assert assistant.content[1] == {"type": "text", "text": "回答"}
    assert assistant.additional_kwargs == {}


@pytest.mark.asyncio
async def test_persist_checkpoint_preserves_existing_system_reminder_source(
    tmp_path,
    session_bundle_factory,
):
    session_id = "ses_019c22f837b87aaebcccebe44d920fb6"
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
            "message_metadata": {"turn_id": "turn-reminder-assistant"},
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
    thread_id = saver._storage.storage.main_thread_id(session_id)
    turn_id = "turn-reminder-assistant"
    user_admission = _accept_user_turn(
        saver,
        session_id=session_id,
        thread_id=thread_id,
        message_id="msg_user",
        turn_id=turn_id,
        job_id="job-reminder-assistant",
        payload="调用工具",
    )
    config = build_checkpoint_config(thread_id)
    prepared_reminder = internal_message_factory.build(
        kind="checkpoint_reminder",
        control=reminder,
    )
    reminder_message_id = "msg_checkpoint_reminder"
    reminder_message = HumanMessage(
        content=prepared_reminder.content,
        id=reminder_message_id,
        response_metadata={
            "message_id": reminder_message_id,
            "created_at": MESSAGE_TIME.isoformat(),
            "updated_at": MESSAGE_TIME.isoformat(),
            "message_metadata": prepared_reminder.metadata,
        },
    )
    reminder_intent = InternalExecutionIntent(message_id=reminder_message_id, execution_id="execution-checkpoint-reminder", admission_idempotency_key="admission-checkpoint-reminder")
    saver.admit_internal_execution(session_id, thread_id=thread_id, job_id="job-checkpoint-reminder", intent=reminder_intent, message=reminder_message)
    reminder_message = saver.accepted_internal_message(session_id, thread_id=thread_id, job_id="job-checkpoint-reminder", message_id=reminder_message_id, intent=reminder_intent)
    with (
        session_thread_owner_scope(
            MutationIntentOwner(session_id, thread_id),
            langgraph_thread_id=thread_id,
        ),
        execution_admission_scope(user_admission, langgraph_thread_id=thread_id),
    ):
        await saver.aput(
            config,
            {
                "channel_values": {
                    "messages": [user_message, tool_call_message, tool_message],
                },
                "channel_versions": {"messages": "1"},
                "updated_channels": ["messages"],
                "id": "ckpt-reminder-turn",
            },
            {"source": "test", "step": 1, "writes": {}},
            {"messages": "1"},
        )
        assert persist_user_message_checkpoint(
            checkpointer=saver,
            session_id=session_id,
            thread_id=thread_id,
            job_id="job-checkpoint-reminder",
            admission_idempotency_key="admission-checkpoint-reminder",
            message=reminder_message,
            execution_intent=reminder_intent,
        ) is False
    reminder_item = saver.get_canonical_item(
        session_id,
        thread_id=thread_id,
        item_id=f"item-{reminder_message_id}",
    )
    assert reminder_item is not None
    assert reminder_item.semantic_kind == "runtime_notice"
    assert reminder_item.turn_id is None and reminder_item.turn_scope == "pending_next_turn"
    assert reminder_item.wire_role == "user"
    assert reminder_item.producer_ref == {
        "producer_kind": "runtime",
        "producer_id": reminder_message_id,
        "invocation_id": "execution-checkpoint-reminder",
    }
    assert reminder_item.payload == prepared_reminder.content
    reminder_admission = saver.execution_admission(
        session_id,
        thread_id=thread_id,
        execution_id="execution-checkpoint-reminder",
    )
    assert reminder_admission.execution_origin == "internal"
    assert reminder_admission.turn_id is None
    assert (reminder_admission.session_id, reminder_admission.thread_id) == (session_id, thread_id)

    with session_thread_owner_scope(
        MutationIntentOwner(session_id, thread_id),
        langgraph_thread_id=thread_id,
    ):
        reminder_tuple = await saver.aget_tuple(config)
    assert reminder_tuple is not None
    checkpoint = reminder_tuple.checkpoint.copy()
    channel_values = dict(checkpoint["channel_values"])
    checkpoint_messages = channel_values.get("messages")
    assert isinstance(checkpoint_messages, list)
    channel_values["messages"] = [*checkpoint_messages, final_message]
    checkpoint["channel_values"] = channel_values
    checkpoint["id"] = "ckpt-reminder-final"
    channel_versions = dict(checkpoint["channel_versions"])
    messages_version = saver.get_next_version(channel_versions.get("messages"), None)
    channel_versions["messages"] = messages_version
    checkpoint["channel_versions"] = channel_versions
    with (
        session_thread_owner_scope(
            MutationIntentOwner(session_id, thread_id),
            langgraph_thread_id=thread_id,
        ),
        execution_admission_scope(user_admission, langgraph_thread_id=thread_id),
    ):
        await saver.aput(
            reminder_tuple.config,
            checkpoint,
            {"source": "test", "step": 2, "writes": {}},
            {"messages": messages_version},
        )

    with (
        session_thread_owner_scope(
            MutationIntentOwner(session_id, thread_id),
            langgraph_thread_id=thread_id,
        ),
        execution_admission_scope(user_admission, langgraph_thread_id=thread_id),
    ):
        changed = persist_standard_assistant_checkpoint(
            checkpointer=saver,
            session_id=session_id,
            thread_id=thread_id,
            turn_id=turn_id,
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
    state_snapshot = await build_message_service(
        tmp_path, checkpointer=saver
    ).get_agent_state_messages(session_id)
    records = [json.loads(line) for line in state_snapshot.jsonl.splitlines()]
    assert [record["role"] for record in records] == ["user", "assistant", "tool", "assistant"]
    assert all("<system_reminder>" not in json.dumps(record, ensure_ascii=False) for record in records)
    assert records[-1]["content"] == [
        {"type": "reasoning", "reasoning": final_reasoning},
        {"type": "text", "text": final_text},
    ]
    assert first_reasoning not in json.dumps(records[-1], ensure_ascii=False)
    assert saver.get_canonical_item(
        session_id,
        thread_id=thread_id,
        item_id=reminder_item.item_id,
    ) == reminder_item
    assert project_runtime_notice_content(reminder_item.payload) == prepared_reminder.content
    (request_notice,) = project_canonical_items((reminder_item,), include_runtime_notices=True)
    assert isinstance(request_notice, HumanMessage)
    assert request_notice.content == prepared_reminder.content
    assert project_canonical_items((reminder_item,)) == []

    visible_messages = await build_message_service(tmp_path, checkpointer=saver).list(
        session_id,
        limit=10,
    )
    assert all(
        "<system_reminder>" not in item.content for item in visible_messages.items
    )


@pytest.fixture
def reasoning_items_factory():
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
                message_group_id="message-model-call-1",
                projection_identity=ItemProjectionIdentity(
                    model_call_id="model-call-1",
                    block_id=f"model-call-1:block:reasoning-{index}",
                    block_index=index,
                ),
                turn_id="turn-1",
                turn_scope="turn_member",
            )
            for position, index in enumerate(indices)
        ]
        items.append(
            CanonicalItemRecord.create(
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
                projection_identity=ItemProjectionIdentity(
                    model_call_id="model-call-1"
                ),
                turn_id="turn-1",
                turn_scope="turn_member",
            )
        )
        return items

    return make


def test_agent_state_reasoning_merge_preserves_selection_order(reasoning_items_factory):
    items = reasoning_items_factory(0, 2)
    message = AIMessage(
        response_metadata={"model_call_id": "model-call-1"},
        content="",
        tool_calls=[{"id": "call-1", "name": "read", "args": {}}],
    )
    result = merge_canonical_reasoning([message], items)
    assert [block["id"] for block in result[0].content] == [
        "model-call-1:block:reasoning-0",
        "model-call-1:block:reasoning-2",
    ]
    assert message.content == ""
    assert [item.item_sequence for item in items] == [10, 9, 11]
    assert result[0].tool_calls == message.tool_calls


def test_agent_state_reasoning_merge_preserves_text_and_protected_carrier(
    reasoning_items_factory,
):
    protected = {
        "type": "reasoning",
        "id": "reasoning-0",
        "index": 0,
        "reasoning": "思考 0",
        "signature": "protected-signature",
        "extras": {"opaque": "不得丢失"},
    }
    text = {"type": "text", "text": "调用前说明", "index": 1, "id": "text-1"}
    message = AIMessage(
        response_metadata={"model_call_id": "model-call-1"},
        content=[protected, text],
        tool_calls=[{"id": "call-1", "name": "read", "args": {}}],
    )
    items = reasoning_items_factory(0, 2)
    original_items = [item.to_dict() for item in items]
    result = merge_canonical_reasoning([message], items)
    assert result[0].content == [
        protected,
        text,
        {
            "type": "reasoning",
            "reasoning": "思考 2",
            "id": "model-call-1:block:reasoning-2",
            "index": 2,
        },
    ]
    assert message.content == [protected, text]
    assert [item.to_dict() for item in items] == original_items
    assert merge_canonical_reasoning(result, items)[0].content == result[0].content


def test_agent_state_reasoning_merge_reports_unidentified_provider_carrier(
    reasoning_items_factory,
):
    message = AIMessage(
        response_metadata={"model_call_id": "model-call-1"},
        content=[
            {
                "type": "reasoning_content",
                "reasoning_content": "思考 0",
            }
        ],
        tool_calls=[{"id": "call-1", "name": "read", "args": {}}],
    )
    result = merge_canonical_reasoning([message], reasoning_items_factory(0))
    assert result[0].content == message.content
    assert result[0].response_metadata["reasoning_source"] == "canonical_item_stream"
    assert (
        result[0].response_metadata["reasoning_merge_reason"]
        == "content_part_index_missing"
    )


def test_agent_state_reasoning_merge_deduplicates_stream_and_checkpoint_items(
    reasoning_items_factory,
):
    items = reasoning_items_factory(0)
    items.insert(
        1,
        replace(
            items[0],
            item_id="reasoning-0-checkpoint",
            projection_group=ProjectionGroup.from_dict(
                {"content_form": "list", "ordinal": 0, "size": 2}
            ),
        ),
    )
    message = AIMessage(
        response_metadata={"model_call_id": "model-call-1"},
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
    reasoning_items_factory,
):
    items = reasoning_items_factory(0)
    post_tool = replace(
        items[0],
        item_id="reasoning-after-tool",
        item_sequence=8,
        payload="工具返回后的 reasoning",
        content_hash=content_hash("text", "工具返回后的 reasoning"),
        projection_identity=ItemProjectionIdentity(
            model_call_id="model-call-2",
            block_id="model-call-2:block:part-after-tool",
            block_index=0,
        ),
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
                {
                    "reasoning_content": "工具返回后的 reasoning",
                    "type": "reasoning_content",
                },
                {"text": "最终答复", "type": "text"},
            ],
            projection_identity=ItemProjectionIdentity(
                model_call_id="model-call-2",
                phase="final_answer",
                content_part_refs=tuple(
                    ProjectionPartRef.from_dict(ref)
                    for ref in [
                        {"id": "part-after-tool", "index": 0},
                        {"id": "part-final-text", "index": 1},
                    ]
                ),
            ),
            turn_id="turn-1",
            turn_scope="turn_member",
            wire_role="assistant",
        )
    )
    message = AIMessage(
        response_metadata={"model_call_id": "model-call-1"},
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
    reasoning_items_factory,
    indices,
):
    message = AIMessage(
        response_metadata={"model_call_id": "model-call-1"},
        content="缺少 content-part 顺序的正文",
        tool_calls=[{"id": "call-1", "name": "read", "args": {}}],
    )
    result = merge_canonical_reasoning([message], reasoning_items_factory(*indices))
    assert result[0].content == message.content
    assert result[0].response_metadata["reasoning_merge_status"] == (
        "skipped_ambiguous_order"
    )
    assert result[0].response_metadata["reasoning_merge_reason"] == (
        "content_part_index_missing"
    )
    assert message.content == result[0].content


def test_agent_state_reasoning_merge_keeps_conflicting_part_order_explicit(
    reasoning_items_factory,
):
    message = AIMessage(
        response_metadata={"model_call_id": "model-call-1"},
        content=[{"type": "text", "text": "调用前说明", "index": 1}],
        tool_calls=[{"id": "call-1", "name": "read", "args": {}}],
    )
    result = merge_canonical_reasoning([message], reasoning_items_factory(2, 0))
    assert result[0].content == message.content
    assert result[0].response_metadata["reasoning_merge_status"] == (
        "skipped_ambiguous_order"
    )
    assert result[0].response_metadata["reasoning_merge_reason"] == (
        "content_part_order_conflict"
    )
    assert message.content == result[0].content
