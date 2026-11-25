"""真实摘要候选必须完整覆盖并保留 canonical 多工具消息组。"""

from __future__ import annotations

import time

import pytest
from langchain_core.messages import (
    ToolMessage,
    messages_from_dict,
)

from app.agents.summarization.generation import CompactionGenerator, SummaryCallBudget
from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.compaction import ApplyCompactionCandidateIntent
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.prompting import validate_internal_message
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.infrastructure.rollout_context.activation.body import (
    ResourceActivationBodyStore,
)
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.orchestration.resource_activation import ResourceActivationCoordinator
from tests.integration.backend.sessions.rollout_context.test_derived_summary_request import (
    summary_model as summary_model,  # noqa: PLC0414 - 复用确定性 Provider cassette
)
from tests.integration.backend.sessions.test_checkpoint_message_groups import (
    group_case as group_case,  # noqa: PLC0414 - 复用真实会话与执行准入 fixture
)


@pytest.mark.asyncio
@pytest.mark.parametrize("retain_final", [False, True])
@pytest.mark.parametrize("explicit_tail_id", [False, True])
@pytest.mark.parametrize("summary_model", [
    ("basic-text", "chat_completions", "openai", "/chat/completions"),
], indirect=True)
async def test_compaction_preserves_all_parallel_tool_group_members(
    tmp_path, group_case, frozen_provider_prepare, retain_final: bool, explicit_tail_id: bool,
    summary_model,
) -> None:
    session_id, _, messages, checkpoint = group_case
    if not explicit_tail_id:
        messages[-1].id = None
    messages[1].tool_calls.append({
        "id": "call-2", "name": "search", "args": {"query": "group"}, "type": "tool_call",
    })
    messages.insert(3, ToolMessage(
        id="result-2", content="第二个工具结果", tool_call_id="call-2", name="search",
        response_metadata={"model_call_id": "model-call-1"},
    ))

    saver = RolloutCheckpointSaver(tmp_path)
    thread_id = saver._storage.storage.main_thread_id(session_id)
    owner = MutationIntentOwner(session_id, thread_id)
    config = build_checkpoint_config(thread_id)
    with session_thread_owner_scope(owner, langgraph_thread_id=thread_id):
        admission = saver.execution_admission(
            session_id, thread_id=thread_id, execution_id="execution-group",
        )
        with execution_admission_scope(admission, langgraph_thread_id=thread_id):
            config = saver.put(config, checkpoint, {"source": "group-before-compaction"}, {"messages": "1"})
            source_items = saver._storage.storage.read_items(session_id, thread_id)
            group = [item for item in source_items if item.message_group_id == "message-call-message-1"]
            assert [item.semantic_kind for item in group] == [
                "assistant_output", "reasoning", "assistant_output", "tool_call", "tool_call",
            ]
            assert [item.projection_group.ordinal for item in group] == list(range(5))
            assert all(item.projection_group.size == len(group) for item in group)
            results = [item for item in source_items if item.item_id in {"item-result-1", "item-result-2"}]
            assert len(results) == 2
            group_ids = {item.item_id for item in group}

            before = frozen_provider_prepare(saver, key="before-compaction-group")
            before_selected = [
                entry.ref.ref_id for entry in before.selection
                if entry.included and entry.ref.ref_type == "canonical_item"
            ]
            assert all(before_selected.count(item_id) == 1 for item_id in group_ids)
            assert before_selected.index(group[-1].item_id) < before_selected.index(results[0].item_id)
            assert before_selected.index(group[-1].item_id) < before_selected.index(results[1].item_id)

            final = next(item for item in source_items if item.projection_identity is not None
                         and item.projection_identity.projection_message_id == "final-1")
            candidate_items = [item for item in source_items if retain_final or item.item_id != final.item_id]
            candidate = saver._storage.compactions.freeze(
                session_id, thread_id, candidate_id=f"group-{retain_final}-{explicit_tail_id}",
                task_id=f"group-task-{retain_final}-{explicit_tail_id}",
                item_ids=[item.item_id for item in candidate_items],
                model_config_hash=sha256_jcs({"model": "basic-text"}),
            )
            model, controller, _protocol = summary_model
            activation = ResourceActivationCoordinator(
                registry=ResourceRegistry(), saver=saver,
                body_store=ResourceActivationBodyStore(saver._storage, saver._detail_store),
            )
            budget = SummaryCallBudget(time.time() + 60)
            result = await CompactionGenerator(saver=saver, model=model, activation=activation).generate(
                candidate, budget=budget,
            )
            assert result.coverage == candidate.refs
            assert controller.transport.hit_counts == {0: budget.calls_used}

            with saver._storage.storage.read_connection(session_id, thread_id) as connection:
                expected_view, = connection.execute(
                    "SELECT branch.head_view_id FROM checkpoint_namespace_state namespace "
                    "JOIN branches branch ON branch.branch_id=namespace.active_branch_id "
                    "WHERE namespace.checkpoint_ns='' AND branch.status='active'",
                ).fetchone()
            applied = saver.adopt_candidate(
                config, ApplyCompactionCandidateIntent(candidate, expected_view),
            )
            with RolloutCheckpointSaver(tmp_path) as cold:
                restored = cold.get_tuple(applied.config)
                assert restored is not None
                restored_messages = restored.checkpoint["channel_values"]["messages"]
                summary, = [message for message in restored_messages
                            if message.response_metadata.get("structured_prompt_kind") == "compaction_summary"]
                assert result.text in summary.content
                validate_internal_message(summary.content, summary.response_metadata)
                expected_tail = [] if retain_final else [final]
                expected_ids = [summary.id, *(item.projection_identity.projection_message_id for item in expected_tail)]
                assert [message.id for message in restored_messages] == expected_ids
                if expected_tail:
                    assert restored_messages[-1].content == messages[-1].content

                copied_items = cold._storage.storage.read_items(session_id, thread_id)
                copied_by_id = {item.item_id: item for item in copied_items}
                assert [copied_by_id[item.item_id].to_dict() for item in group] == [item.to_dict() for item in group]
                restored_group = [item for item in copied_items if item.message_group_id == group[0].message_group_id]
                assert [item.item_id for item in restored_group] == [item.item_id for item in group]
                projected_group = messages_from_dict([
                    LangChainMessageCodec().project_message(tuple(restored_group)),
                ])[0]
                assert projected_group.content == messages[1].content
                assert [call["id"] for call in projected_group.tool_calls] == ["call-1", "call-2"]
                assert [copied_by_id[item.item_id].to_dict() for item in results] == [item.to_dict() for item in results]
                history = cold._storage.compactions.read_history(session_id, thread_id)
                assert "文件正文" in history and "第二个工具结果" in history
                assert "保留完整工具证据" not in history
