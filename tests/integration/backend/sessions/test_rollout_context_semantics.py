from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.base import empty_checkpoint

from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.errors import FormatDispatchError
from app.domain.itemized.execution_admission import (
    ExecutionAdmission,
    InternalExecutionIntent,
)
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.prompting.factory import internal_message_factory
from app.services.infrastructure.rollout_context.checkpoint.codec.message_channel import (
    checkpoint_messages,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.support.rollout.compaction_application import (
    generate_and_adopt_compaction_candidate,
)
from tests.support.rollout.fixture_output import commit_fixture_output

SESSION_ID = "ses_019c205391bb7a4b81b6728df70f63e6"


@pytest.fixture
def context_saver(
    tmp_path: Path, session_bundle_factory
) -> Iterator[RolloutCheckpointSaver]:
    """context/checkpoint 的消费者统一使用真实 main thread 与显式 owner。"""
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    with RolloutCheckpointSaver(sessions_dir) as saver:
        owner = saver.forks.source.resolve_main_thread_owner(SESSION_ID)
        assert owner.thread_id != SESSION_ID
        with session_thread_owner_scope(
            MutationIntentOwner(owner.session_id, owner.thread_id),
            langgraph_thread_id=owner.thread_id,
        ):
            yield saver


@pytest.fixture
def accept_context_turn(
    context_saver: RolloutCheckpointSaver,
) -> Callable[[HumanMessage, str], ExecutionAdmission]:
    """先走真实 acceptance，再给 checkpoint writer 注入该执行的 admission。"""
    saver = context_saver
    thread_id = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id

    def accept(message: HumanMessage, turn_id: str) -> ExecutionAdmission:
        codec = saver._storage.storage._codec()
        message_id = codec.message_id(message, 0)
        execution_id = f"execution-{turn_id}"
        root = codec.items_for_message(
            message,
            item_sequence=1,
            message_id=message_id,
            turn_id=turn_id,
            execution_id=execution_id,
            timestamp="2026-10-05T00:00:00+00:00",
        )[0]
        saver.accept_turn(
            SESSION_ID,
            thread_id=thread_id,
            accepted_ingress_id=message_id,
            acceptance_idempotency_key=f"anchor-acceptance-{turn_id}",
            job_id=f"anchor-job-{turn_id}",
            admission_idempotency_key=f"anchor-admission-{turn_id}",
            payload=root.payload,
            payload_kind=root.payload_kind,
            turn_id=turn_id,
            root_item_id=root.item_id,
            initial_execution_id=execution_id,
            extensions=root.extensions,
            provenance_relations=root.provenance_relations,
        )
        return saver.execution_admission(
            SESSION_ID,
            thread_id=thread_id,
            execution_id=execution_id,
        )

    return accept


def _checkpoint(
    checkpoint_id: str, messages: list[object], *, version: str = "1", **channels: object
) -> dict[str, object]:
    checkpoint = empty_checkpoint()
    checkpoint["id"] = checkpoint_id
    checkpoint["channel_values"] = {"messages": messages, **channels}
    # 版本随 checkpoint 推进，与消息条数和字段位置无关。
    checkpoint["channel_versions"] = {channel: version for channel in checkpoint["channel_values"]}
    checkpoint["updated_channels"] = list(checkpoint["channel_values"])
    return checkpoint


def _assert_message_projection(actual: list[object], expected: list[object]) -> None:
    """比较冷读会变化的投影之外的消息身份、顺序、正文和工具配对。"""
    assert len(actual) == len(expected)
    seen_actual_tool_calls: set[str] = set()
    seen_expected_tool_calls: set[str] = set()
    for actual_message, expected_message in zip(actual, expected, strict=True):
        assert type(actual_message) is type(expected_message)
        assert actual_message.id == expected_message.id
        assert actual_message.content == expected_message.content
        if isinstance(actual_message, AIMessage) and isinstance(expected_message, AIMessage):
            actual_calls = [
                (call["id"], call["name"], call["args"])
                for call in actual_message.tool_calls
            ]
            expected_calls = [
                (call["id"], call["name"], call["args"])
                for call in expected_message.tool_calls
            ]
            assert actual_calls == expected_calls
            seen_actual_tool_calls.update(call[0] for call in actual_calls)
            seen_expected_tool_calls.update(call[0] for call in expected_calls)
        if isinstance(actual_message, ToolMessage) and isinstance(expected_message, ToolMessage):
            assert actual_message.tool_call_id == expected_message.tool_call_id
            assert actual_message.tool_call_id in seen_actual_tool_calls
            assert expected_message.tool_call_id in seen_expected_tool_calls


def _active_context_items(
    saver: RolloutCheckpointSaver, session_id: str, thread_id: str,
):
    with saver._context_reader.open_snapshot(session_id, thread_id, "") as snapshot:
        return saver._storage.checkpoints.committed_context_items(
            snapshot, predecessor=None, include_pending_notices=False,
        )


def _items_through_message(items, message_id: str):
    positions = [
        index for index, item in enumerate(items)
        if item.projection_identity is not None
        and item.projection_identity.projection_message_id == message_id
    ]
    assert positions, f"活动上下文中缺少消息 {message_id} 的 canonical item"
    return tuple(items[:positions[-1] + 1])


@pytest.mark.asyncio
async def test_context_view_filters_control_records_and_keeps_business_messages(
    context_saver: RolloutCheckpointSaver,
    accept_context_turn: Callable[[HumanMessage, str], ExecutionAdmission],
    compaction_summary_model,
) -> None:
    saver = context_saver
    thread_id = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id
    user = HumanMessage(content="读取配置", id="user-1")
    prompt = internal_message_factory.build(kind="checkpoint_reminder", control="工具返回后重新检查")
    internal_steering = HumanMessage(
        content=prompt.content,
        id="steering-1",
        response_metadata={
            "message_metadata": prompt.metadata,
            "source": "terminal_steering",
            "message_id": "steering-1",
            "created_at": "2026-10-05T00:00:00+00:00",
        },
    )
    tool_call = AIMessage(
        content="",
        id="assistant-tool-1",
        tool_calls=[
            {"name": "read_file", "args": {"path": "config.json"}, "id": "call-1"}
        ],
    )
    tool_result = ToolMessage(
        content='{"ok":true}',
        id="tool-result-1",
        tool_call_id="call-1",
        name="read_file",
        response_metadata={"source": "subagent"},
    )
    admission = accept_context_turn(user, "turn-config")
    internal_admission = saver.admit_internal_execution(
        SESSION_ID,
        thread_id=thread_id,
        job_id="job-steering",
        intent=InternalExecutionIntent(
            "steering-1", "execution-steering", "admission-steering"
        ),
        message=internal_steering,
    )
    first_messages = [user, internal_steering, tool_call, tool_result]
    with execution_admission_scope(admission, langgraph_thread_id=thread_id):
        first_config = saver.put(
            build_checkpoint_config(thread_id),
            _checkpoint("checkpoint-1", first_messages, scratchpad={"job": "parent"}),
            {"source": "job", "step": 1},
            {"messages": "1", "scratchpad": "1"},
        )
    second_messages = [
        *first_messages,
        AIMessage(content="配置正确", id="assistant-final-1"),
    ]
    with execution_admission_scope(admission, langgraph_thread_id=thread_id):
        saver.put(
            first_config,
            _checkpoint("checkpoint-2", second_messages, version="2", scratchpad={"job": "parent"}),
            {"source": "job", "step": 2},
            {"messages": "2", "scratchpad": "2"},
        )

    # 冷查询保留冻结历史；执行隔离必须由真实 admission 确定。
    with execution_admission_scope(admission, langgraph_thread_id=thread_id):
        restored = saver.get_tuple(build_checkpoint_config(thread_id))
    assert restored is not None
    assert [
        message.id for message in restored.checkpoint["channel_values"]["messages"]
    ] == [
        "user-1",
        "assistant-tool-1",
        "tool-result-1",
        "assistant-final-1",
    ]
    # 用户 execution 的 acceptance 已先冻结，后来的 notice 只由其自身 admission 消费。
    with execution_admission_scope(internal_admission, langgraph_thread_id=thread_id):
        internal_checkpoint = saver.get_tuple(build_checkpoint_config(thread_id))
    assert internal_checkpoint is not None
    internal_messages = internal_checkpoint.checkpoint["channel_values"]["messages"]
    assert [message.content for message in internal_messages if message.id == "steering-1"] == [
        internal_steering.content
    ]

    root = saver._storage.storage.root(SESSION_ID, thread_id=thread_id)
    records = [
        json.loads(line)
        for line in (root / "rollout.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert {record["wire_role"] for record in records} <= {
        "user",
        "assistant",
        "tool",
    }
    assert all(record["record_type"] == "item" for record in records)
    assert all("kind" not in record for record in records)

    rewind_config = saver.rewind(
        build_checkpoint_config(thread_id),
        checkpoint_id="checkpoint-1",
        source_anchor="user-1",
    )
    with execution_admission_scope(admission, langgraph_thread_id=thread_id):
        saver.put(
            rewind_config,
            _checkpoint(
                "checkpoint-replay",
                [
                    *first_messages,
                    AIMessage(content="重放后的回答", id="assistant-final-2"),
                ],
                version="3",
                scratchpad={"job": "parent"},
            ),
            {"source": "replay", "step": 3},
            {"messages": "3", "scratchpad": "3"},
        )
    current = saver.get_tuple(build_checkpoint_config(thread_id))
    assert current is not None
    assert [
        message.content for message in current.checkpoint["channel_values"]["messages"]
    ][-1] == "重放后的回答"
    assert len(list(root.glob("segment-*.jsonl"))) == 0

    compact_config = current.config
    raw_messages = current.checkpoint["channel_values"]["messages"]
    source_items = _active_context_items(saver, SESSION_ID, thread_id)
    frozen, summary, compacted = await generate_and_adopt_compaction_candidate(
        saver,
        session_id=SESSION_ID,
        thread_id=thread_id,
        checkpoint_config=compact_config,
        admission=admission,
        model=compaction_summary_model,
        candidate_id="context-view-control-filter",
        task_id="context-view-control-filter-task",
    )
    source_message_ids = tuple(
        item.projection_identity.projection_message_id
        for item in source_items
        if item.projection_identity is not None
    )
    assert source_message_ids == (
        "user-1", "assistant-tool-1", "tool-result-1", "assistant-final-2",
    )
    assert tuple(ref.ref_id for ref in frozen.refs) == tuple(item.item_id for item in source_items)
    compacted_messages = compacted.checkpoint["channel_values"]["messages"]
    summary_message, = compacted_messages
    assert summary_message.id == "summary-context-view-control-filter"
    assert summary_message.response_metadata["compaction_summary"] is True
    assert summary.text in summary_message.content
    assert summary_message.id not in {message.id for message in raw_messages}
    with sqlite3.connect(root / "index.sqlite") as connection:
        assert connection.execute(
            "SELECT 1 FROM context_views WHERE view_kind = 'rewind' LIMIT 1"
        ).fetchone() is not None
        (applied_view_id,) = connection.execute(
            "SELECT applied_view_id FROM context_compaction_candidates WHERE candidate_id = ?",
            ("context-view-control-filter",),
        ).fetchone()
        assert applied_view_id is not None
        (compacted_view_kind,) = connection.execute(
            "SELECT view_kind FROM context_views WHERE view_id = ?",
            (applied_view_id,),
        ).fetchone()
        assert compacted_view_kind == "compaction"
        (checkpoint_kind,) = connection.execute(
            "SELECT checkpoint_kind FROM checkpoints WHERE view_id = ?",
            (applied_view_id,),
        ).fetchone()
        assert checkpoint_kind == "compaction"


def test_incremental_checkpoint_keeps_tool_call_with_later_tool_result(
    context_saver: RolloutCheckpointSaver,
    accept_context_turn: Callable[[HumanMessage, str], ExecutionAdmission],
) -> None:
    """增量 checkpoint 不能只恢复 ToolMessage 而丢掉其 assistant call。"""
    saver = context_saver
    thread_id = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id
    user = HumanMessage(
        content="读取 README",
        id="user-tool-call",
        response_metadata={"turn_id": "turn-tool-call"},
    )
    admission = accept_context_turn(user, "turn-tool-call")
    with execution_admission_scope(admission, langgraph_thread_id=thread_id):
        first_config = saver.put(
            build_checkpoint_config(thread_id),
            _checkpoint(
                "checkpoint-tool-call",
                [
                    user,
                    AIMessage(
                        content="",
                        id="assistant-tool-call",
                        response_metadata={"turn_id": "turn-tool-call"},
                        tool_calls=[
                            {
                                "name": "read_file",
                                "args": {"path": "README.md"},
                                "id": "call-tool-call",
                                "type": "tool_call",
                            }
                        ],
                    ),
                ],
            ),
            {"source": "incremental-tool-test", "step": 1},
            {"messages": "1"},
        )
        result_checkpoint = _checkpoint(
            "checkpoint-tool-result",
            [
                ToolMessage(
                    content="README 内容",
                    id="tool-result",
                    tool_call_id="call-tool-call",
                    name="read_file",
                    response_metadata={"turn_id": "turn-tool-call"},
                )
            ],
            version="2",
        )
        saver.put(
            first_config,
            result_checkpoint,
            {"source": "incremental-tool-test", "step": 2},
            {"messages": "2"},
        )

    root = saver._storage.storage.root(SESSION_ID, thread_id=thread_id)
    with sqlite3.connect(root / "index.sqlite") as connection:
        view_id = connection.execute(
            "SELECT view_id FROM checkpoints WHERE checkpoint_id = 'checkpoint-tool-result'"
        ).fetchone()[0]
        assert (
            connection.execute(
                "SELECT source_kind FROM context_view_ranges WHERE view_id = ? ORDER BY range_index LIMIT 1",
                (view_id,),
            ).fetchone()[0]
            == "view"
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM context_view_turns WHERE view_id = ? AND turn_id = 'turn-tool-call'",
                (view_id,),
            ).fetchone()[0]
            == 1
        )

    restored = saver.get_tuple(build_checkpoint_config(thread_id))
    assert restored is not None
    assert [
        message.id for message in restored.checkpoint["channel_values"]["messages"]
    ] == [
        "user-tool-call",
        "assistant-tool-call",
        "tool-result",
    ]
    restored_call = restored.checkpoint["channel_values"]["messages"][1]
    restored_result = restored.checkpoint["channel_values"]["messages"][2]
    assert isinstance(restored_call, AIMessage)
    assert isinstance(restored_result, ToolMessage)
    assert restored_call.tool_calls[0]["id"] == restored_result.tool_call_id

    # range 是来源证据，普通恢复只读冻结 membership；完整校验仍须报告损坏。
    with sqlite3.connect(root / "index.sqlite") as connection:
        view_id = connection.execute(
            "SELECT context_view_id FROM checkpoint_channels WHERE checkpoint_id = 'checkpoint-tool-result' AND channel_name='messages'"
        ).fetchone()[0]
        connection.execute(
            "DELETE FROM context_view_ranges WHERE view_id = ? AND source_kind = 'view'",
            (view_id,),
        )
        connection.commit()
    assert saver.get_tuple(build_checkpoint_config(thread_id)).checkpoint == restored.checkpoint
    with pytest.raises(FormatDispatchError, match="parent range"):
        saver._storage.storage.validate_index(SESSION_ID, thread_id)


def test_parallel_tool_continuation_restores_all_call_declarations(
    context_saver: RolloutCheckpointSaver,
    accept_context_turn: Callable[[HumanMessage, str], ExecutionAdmission],
) -> None:
    """并行工具组之后的 continuation 不能只携带 ToolMessage 结果。"""
    saver = context_saver
    thread_id = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id
    parallel_calls = [
        {
            "name": "read_file",
            "args": {"path": f"file-{index}.txt"},
            "id": f"call-parallel-{index}",
            "type": "tool_call",
        }
        for index in range(4)
    ]
    parent_messages = [
        HumanMessage(content="并行读取文件", id="user-parallel"),
        AIMessage(
            content="",
            id="assistant-parallel",
            tool_calls=parallel_calls,
        ),
        *[
            ToolMessage(
                content=f"结果 {index}",
                id=f"result-parallel-{index}",
                name="read_file",
                tool_call_id=f"call-parallel-{index}",
            )
            for index in range(4)
        ],
    ]
    parent_admission = accept_context_turn(parent_messages[0], "turn-parallel")
    with execution_admission_scope(parent_admission, langgraph_thread_id=thread_id):
        parent_config = saver.put(
            build_checkpoint_config(thread_id),
            _checkpoint("parallel-parent", parent_messages),
            {"source": "parallel-continuation-test", "step": 1},
            {"messages": "1"},
        )

    # LangGraph continuation 只带新 HumanMessage；冻结 membership 必须保留整个并行工具组。
    continuation = HumanMessage(content="继续处理", id="user-continuation")
    admission = accept_context_turn(continuation, "turn-continuation")
    continuation_checkpoint = _checkpoint("parallel-continuation", [continuation], version="2")
    with execution_admission_scope(admission, langgraph_thread_id=thread_id):
        saver.put(
            parent_config,
            continuation_checkpoint,
            {"source": "parallel-continuation-test", "step": 2},
            {"messages": "2"},
        )

    restored = saver.get_tuple(build_checkpoint_config(thread_id))
    assert restored is not None
    restored_messages = restored.checkpoint["channel_values"]["messages"]
    assert [message.id for message in restored_messages] == [
        "user-parallel",
        "assistant-parallel",
        "result-parallel-0",
        "result-parallel-1",
        "result-parallel-2",
        "result-parallel-3",
        "user-continuation",
    ]
    assert isinstance(restored_messages[1], AIMessage)
    assert {call["id"] for call in restored_messages[1].tool_calls} == {
        f"call-parallel-{index}" for index in range(4)
    }

    root = saver._storage.storage.root(SESSION_ID, thread_id=thread_id)
    with sqlite3.connect(root / "index.sqlite") as connection:
        child_view = connection.execute(
            "SELECT view_id FROM checkpoints WHERE checkpoint_id = 'parallel-continuation'"
        ).fetchone()[0]
        parent_view = connection.execute(
            "SELECT parent_view_id FROM context_views WHERE view_id = ?",
            (child_view,),
        ).fetchone()[0]
        assert (
            connection.execute(
                "SELECT source_kind FROM context_view_ranges WHERE view_id = ? ORDER BY range_index LIMIT 1",
                (child_view,),
            ).fetchone()[0]
            == "view"
        )

        # 损坏 range 来源证据不改变冻结 messages membership，交由完整校验报告。
        child_view = connection.execute(
            "SELECT context_view_id FROM checkpoint_channels WHERE checkpoint_id='parallel-continuation' AND channel_name='messages'"
        ).fetchone()[0]
        parent_view = connection.execute(
            "SELECT parent_view_id FROM context_views WHERE view_id=?",
            (child_view,),
        ).fetchone()[0]
        connection.execute(
            "DELETE FROM context_view_ranges WHERE view_id = ? AND source_kind = 'view'",
            (child_view,),
        )
        connection.execute(
            "DELETE FROM context_view_ranges WHERE view_id = ?",
            (parent_view,),
        )
        connection.executemany(
            "INSERT INTO context_view_ranges(view_id, range_index, source_kind, start_message_sequence, end_message_sequence, message_start_sequence, message_end_sequence, range_ordinal, logical_start_turn_ordinal, logical_end_turn_ordinal) VALUES (?, ?, 'messages', ?, ?, ?, ?, ?, NULL, NULL)",
            [
                (parent_view, 0, 1, 1, 1, 1, 0),
                (parent_view, 1, 3, 6, 3, 6, 1),
            ],
        )
        connection.commit()

    assert saver.get_tuple(build_checkpoint_config(thread_id)).checkpoint["channel_values"]["messages"] == restored_messages
    with pytest.raises(FormatDispatchError, match="parent range"):
        saver._storage.storage.validate_index(SESSION_ID, thread_id)


def test_context_view_validation_accepts_single_message_range(
    context_saver: RolloutCheckpointSaver,
    accept_context_turn: Callable[[HumanMessage, str], ExecutionAdmission],
) -> None:
    """单消息 view range 的起止序号相同，校验时仍应视为完整范围。"""
    saver = context_saver
    thread_id = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id
    user = HumanMessage(content="单条消息", id="single")
    admission = accept_context_turn(user, "turn-single")
    with execution_admission_scope(admission, langgraph_thread_id=thread_id):
        saver.put(
            build_checkpoint_config(thread_id),
            _checkpoint("single-message", [user]),
            {"source": "single-message-range"},
            {"messages": "1"},
        )

    root = saver._storage.storage.root(SESSION_ID, thread_id=thread_id)
    with sqlite3.connect(root / "index.sqlite") as connection:
        view_id = connection.execute(
            "SELECT view_id FROM checkpoints WHERE checkpoint_id = 'single-message'"
        ).fetchone()[0]

    saver._storage.checkpoints.validate_context_view_chain(thread_id, "", str(view_id))


@pytest.mark.asyncio
async def test_candidate_compaction_keeps_canonical_tool_range_identity(
    context_saver: RolloutCheckpointSaver,
    accept_context_turn: Callable[[HumanMessage, str], ExecutionAdmission],
    compaction_summary_model,
) -> None:
    saver = context_saver
    thread_id = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id
    user = HumanMessage(
        content="问题", id="u1", response_metadata={"turn_id": "turn-1"}
    )
    messages = [
        user,
        AIMessage(
            content="工具请求",
            id="a1",
            tool_calls=[
                {
                    "name": "read_file",
                    "args": {"path": "config.json"},
                    "id": "call-1",
                },
            ],
        ),
        ToolMessage(content="结果", id="tool-1", tool_call_id="call-1"),
    ]
    admission = accept_context_turn(user, "turn-1")
    with execution_admission_scope(admission, langgraph_thread_id=thread_id):
        config = saver.put(
            build_checkpoint_config(thread_id),
            _checkpoint("cp-1", messages),
            {"source": "cutoff-test"},
            {"messages": "1"},
        )
    source_items = _active_context_items(saver, SESSION_ID, thread_id)
    result_frozen, result, compacted = await generate_and_adopt_compaction_candidate(
        saver,
        session_id=SESSION_ID,
        thread_id=thread_id,
        checkpoint_config=config,
        admission=admission,
        model=compaction_summary_model,
        candidate_id="tool-range-identity",
        task_id="tool-range-identity-task",
    )
    assert result.coverage == result_frozen.refs
    assert tuple(ref.ref_id for ref in result_frozen.refs) == tuple(item.item_id for item in source_items)
    tool_call = next(item for item in source_items if item.projection_identity is not None
                     and item.projection_identity.projection_message_id == "a1")
    tool_result = next(item for item in source_items if item.projection_identity is not None
                       and item.projection_identity.projection_message_id == "tool-1")
    assert tool_call.item_id in {ref.ref_id for ref in result_frozen.refs}
    assert tool_result.item_id in {ref.ref_id for ref in result_frozen.refs}
    frozen_ids = tuple(ref.ref_id for ref in result_frozen.refs)
    assert frozen_ids.index(tool_call.item_id) < frozen_ids.index(tool_result.item_id)
    compacted_messages = compacted.checkpoint["channel_values"]["messages"]
    summary_message, = compacted_messages
    assert summary_message.id == "summary-tool-range-identity"
    assert summary_message.response_metadata["compaction_summary"] is True
    assert result.text in summary_message.content
    root = saver._storage.storage.root(SESSION_ID, thread_id=thread_id)
    with sqlite3.connect(root / "index.sqlite") as connection:
        (summary_item_id,) = connection.execute(
            "SELECT applied_summary_item_id FROM context_compaction_candidates WHERE candidate_id=?",
            (result_frozen.candidate_id,),
        ).fetchone()
        provenance = connection.execute(
            "SELECT json_extract(target_ref,'$.target_id') FROM item_relations "
            "WHERE canonical_item_id=? AND relation='derived_from' ORDER BY canonical_relation_ordinal",
            (summary_item_id,),
        ).fetchall()
    assert tuple(row[0] for row in provenance) == (
        *frozen_ids,
        *(ref.ref_id for ref in result.output_refs),
    )
    assert "结果" in saver._storage.compactions.read_history(SESSION_ID, thread_id)


def test_turn_anchor_reports_unreachable_when_no_complete_view_remains(
    context_saver: RolloutCheckpointSaver,
    accept_context_turn: Callable[[HumanMessage, str], ExecutionAdmission],
) -> None:
    saver = context_saver
    thread_id = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id
    user = HumanMessage(
        content="不可达问题",
        id="unreachable-user",
        response_metadata={"turn_id": "turn-unreachable"},
    )
    admission = accept_context_turn(user, "turn-unreachable")
    with execution_admission_scope(admission, langgraph_thread_id=thread_id):
        saver.put(
            build_checkpoint_config(thread_id),
            _checkpoint(
                "cp-1", [user, AIMessage(content="不可达回答", id="unreachable-answer")]
            ),
            {"source": "unreachable-test"},
            {"messages": "1"},
        )
    with sqlite3.connect(
        saver._storage.storage.index_path(SESSION_ID, thread_id=thread_id)
    ) as connection:
        connection.execute(
            "DELETE FROM context_view_turns WHERE turn_id = 'turn-unreachable'"
        )

    with pytest.raises(KeyError, match="不包含可恢复的完整 Turn"):
        saver.resolve_turn_anchor(
            build_checkpoint_config(thread_id),
            turn_id="turn-unreachable",
        )


@pytest.mark.asyncio
async def test_turn_anchor_walks_active_lineage_after_message_level_compaction(
    context_saver: RolloutCheckpointSaver,
    accept_context_turn: Callable[[HumanMessage, str], ExecutionAdmission],
    frozen_provider_prepare,
    compaction_summary_model,
) -> None:
    saver = context_saver
    thread_id = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id
    turn_messages = [
        HumanMessage(
            content="检查部署",
            id="turn-1-user",
            response_metadata={"turn_id": "turn-1"},
        ),
        AIMessage(
            content="我先检查配置",
            id="turn-1-tool-call",
            tool_calls=[
                {"name": "read_file", "args": {"path": "config.json"}, "id": "call-1"}
            ],
        ),
        ToolMessage(
            content='{"status":"ok"}',
            id="turn-1-tool-result",
            tool_call_id="call-1",
        ),
        AIMessage(content="部署配置正常", id="turn-1-final"),
    ]
    first_admission = accept_context_turn(turn_messages[0], "turn-1")
    with execution_admission_scope(first_admission, langgraph_thread_id=thread_id):
        frozen_provider_prepare(saver, key="turn-anchor:turn-1")
        commit_fixture_output(saver, messages=turn_messages)
        config = saver.put(
            build_checkpoint_config(thread_id),
            _checkpoint("cp-1", turn_messages),
            {"source": "anchor-test", "step": 1},
            {"messages": "1"},
        )
    cp1_anchor = saver.resolve_turn_anchor(
        config,
        turn_id="turn-1",
        anchor_mode="inclusive",
    )
    assert cp1_anchor.checkpoint_id == "cp-1"
    second_user = HumanMessage(content="继续检查日志", id="turn-2-user")
    second_admission = accept_context_turn(second_user, "turn-2")
    compacted_messages = [
        *turn_messages,
        second_user,
        AIMessage(content="日志没有异常", id="turn-2-final"),
    ]
    with execution_admission_scope(second_admission, langgraph_thread_id=thread_id):
        config = saver.put(
            config,
            _checkpoint("cp-2", compacted_messages, version="2"),
            {"source": "anchor-test", "step": 2},
            {"messages": "2"},
        )

    cp2_anchor = saver.resolve_turn_anchor(
        config,
        turn_id="turn-1",
        anchor_mode="inclusive",
    )
    assert cp2_anchor.view_id.startswith("view-")
    assert cp2_anchor.checkpoint_id == "cp-2"
    assert cp2_anchor.cutoff_message_sequence == cp2_anchor.last_message_sequence

    first_range = _items_through_message(
        _active_context_items(saver, SESSION_ID, thread_id), "turn-1-tool-result",
    )
    cp3_frozen, cp3_result, cp3_applied = await generate_and_adopt_compaction_candidate(
        saver,
        session_id=SESSION_ID,
        thread_id=thread_id,
        checkpoint_config=config,
        admission=second_admission,
        model=compaction_summary_model,
        candidate_id="turn-anchor-prefix",
        task_id="turn-anchor-prefix-task",
        item_ids=tuple(item.item_id for item in first_range),
    )
    assert cp3_result.coverage == cp3_frozen.refs
    config = cp3_applied.config
    cp3_anchor = saver.resolve_turn_anchor(
        config,
        turn_id="turn-1",
        anchor_mode="inclusive",
    )
    assert cp3_anchor.checkpoint_id == cp3_applied.checkpoint["id"]

    second_range = _items_through_message(
        _active_context_items(saver, SESSION_ID, thread_id), "turn-1-final",
    )
    cp4_frozen, cp4_result, cp4_applied = await generate_and_adopt_compaction_candidate(
        saver,
        session_id=SESSION_ID,
        thread_id=thread_id,
        checkpoint_config=config,
        admission=second_admission,
        model=compaction_summary_model,
        candidate_id="turn-anchor-final",
        task_id="turn-anchor-final-task",
        item_ids=tuple(item.item_id for item in second_range),
        predecessor_candidate_id=cp3_frozen.candidate_id,
    )
    assert cp4_frozen.predecessor is not None
    assert cp4_frozen.predecessor.candidate_id == cp3_frozen.candidate_id
    assert cp4_frozen.refs[:len(cp3_frozen.refs)] == cp3_frozen.refs
    assert cp4_result.coverage == cp4_frozen.refs
    config = cp4_applied.config

    with session_thread_owner_scope(
        MutationIntentOwner(SESSION_ID, thread_id), langgraph_thread_id=thread_id,
    ), execution_admission_scope(second_admission, langgraph_thread_id=thread_id):
        compacted = saver.get_tuple(config)
    assert compacted is not None
    current_messages = checkpoint_messages(saver, compacted)
    assert [message.id for message in current_messages] == [
        "summary-turn-anchor-final", "turn-2-user", "turn-2-final",
    ]
    assert all(message.id != "summary-turn-anchor-prefix" for message in current_messages)
    summary_message = current_messages[0]
    assert summary_message.response_metadata["compaction_summary"] is True
    assert cp4_result.text in summary_message.content
    resolved = saver.resolve_turn_anchor(
        config,
        turn_id="turn-1",
        anchor_mode="inclusive",
    )
    assert resolved.checkpoint_id == cp4_applied.checkpoint["id"]
    assert resolved.view_id != cp1_anchor.view_id
    assert resolved.view_id != cp2_anchor.view_id
    assert resolved.view_id != cp3_anchor.view_id
    _assert_message_projection(
        saver.materialize_turn_anchor(
            config,
            turn_id="turn-1",
            anchor_mode="inclusive",
        )[1],
        turn_messages,
    )

    before = saver.resolve_turn_anchor(
        config,
        turn_id="turn-1",
        anchor_mode="before",
    )
    assert before.view_id == resolved.view_id
    assert before.cutoff_message_sequence == before.user_message_sequence - 1

    with sqlite3.connect(
        saver._storage.storage.index_path(SESSION_ID, thread_id=thread_id)
    ) as connection:
        applied_checkpoints = connection.execute(
            "SELECT candidate.candidate_id, checkpoint.checkpoint_id, checkpoint.checkpoint_kind "
            "FROM context_compaction_candidates candidate JOIN checkpoints checkpoint "
            "ON checkpoint.view_id=candidate.applied_view_id WHERE candidate.candidate_id IN (?, ?) "
            "ORDER BY candidate.candidate_id",
            (cp3_frozen.candidate_id, cp4_frozen.candidate_id),
        ).fetchall()
    assert {candidate_id: (checkpoint_id, kind)
            for candidate_id, checkpoint_id, kind in applied_checkpoints} == {
        cp3_frozen.candidate_id: (cp3_applied.checkpoint["id"], "compaction"),
        cp4_frozen.candidate_id: (cp4_applied.checkpoint["id"], "compaction"),
    }

    sibling_config = saver.rewind(
        config,
        checkpoint_id="cp-2",
        source_anchor="turn-1-final",
        anchor_mode="inclusive",
    )
    sibling_anchor = saver.resolve_turn_anchor(
        sibling_config,
        turn_id="turn-1",
        anchor_mode="inclusive",
    )
    assert sibling_anchor.checkpoint_id == "cp-2"
    assert sibling_anchor.view_id != cp2_anchor.view_id
    _assert_message_projection(
        saver.materialize_turn_anchor(
            sibling_config,
            turn_id="turn-1",
            anchor_mode="inclusive",
        )[1],
        turn_messages,
    )

    partial_config = saver.rewind(
        sibling_config,
        checkpoint_id="cp-2",
        source_anchor="turn-1-user",
        anchor_mode="inclusive",
    )
    partial = saver.get_tuple(build_checkpoint_config(thread_id))
    assert partial is not None
    partial_messages = partial.checkpoint["channel_values"]["messages"]
    assert [message.id for message in partial_messages] == ["turn-1-user"]
    selected = saver.resolve_turn_anchor(
        partial_config,
        turn_id="turn-1",
        anchor_mode="inclusive",
    )
    assert selected.view_id == cp2_anchor.view_id
    assert selected.checkpoint_id == "cp-2"
    assert selected.view_id != sibling_anchor.view_id
    assert selected.view_id != cp1_anchor.view_id

    saver.rewind_to_turn(
        partial_config,
        turn_id="turn-1",
        anchor_mode="before",
    )
    active = saver.get_tuple(build_checkpoint_config(thread_id))
    assert active is not None
    assert active.checkpoint["channel_values"]["messages"] == []


def test_logical_pruning_marks_unreferenced_checkpoints_without_touching_jsonl(
    context_saver: RolloutCheckpointSaver,
    accept_context_turn: Callable[[HumanMessage, str], ExecutionAdmission],
) -> None:
    saver = context_saver
    thread_id = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id
    config = build_checkpoint_config(thread_id)
    messages: list[object] = []
    for index in range(3):
        user = HumanMessage(content=f"问题 {index}", id=f"user-{index}")
        answer = AIMessage(content=f"回答 {index}", id=f"answer-{index}")
        messages = [*messages, user, answer]
        admission = accept_context_turn(user, f"turn-{index}")
        with execution_admission_scope(admission, langgraph_thread_id=thread_id):
            config = saver.put(
                config,
                _checkpoint(f"cp-{index}", messages, version=str(index)),
                {"source": "pruning-test", "step": index},
                {"messages": str(index)},
            )

    plan = saver.plan_pruning(
        thread_id,
        retain_checkpoint_ids=("cp-0",),
    )
    assert {candidate.checkpoint_id for candidate in plan.candidates} == {"cp-1"}
    assert all(candidate.view_id.startswith("view-") for candidate in plan.candidates)

    rollout_path = saver._storage.storage.jsonl_path(SESSION_ID, thread_id=thread_id)
    original_jsonl = rollout_path.read_bytes()
    assert saver.execute_pruning(thread_id, plan) == ("cp-1",)
    assert rollout_path.read_bytes() == original_jsonl
    assert [
        item.checkpoint["id"] for item in saver.list(build_checkpoint_config(thread_id))
    ] == [
        "cp-2",
        "cp-0",
    ]
    assert saver.get_tuple(build_checkpoint_config(thread_id)) is not None


def test_context_view_jump_validation_rejects_cycle_or_wrong_ancestor(
    context_saver: RolloutCheckpointSaver,
    accept_context_turn: Callable[[HumanMessage, str], ExecutionAdmission],
) -> None:
    saver = context_saver
    thread_id = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id
    user = HumanMessage(content="一", id="u1")
    admission = accept_context_turn(user, "turn-jump")
    with execution_admission_scope(admission, langgraph_thread_id=thread_id):
        config = saver.put(
            build_checkpoint_config(thread_id),
            _checkpoint("cp-1", [user]),
            {"source": "jump-test"},
            {"messages": "1"},
        )
        saver.put(
            config,
            _checkpoint("cp-2", [user, AIMessage(content="二", id="a1")], version="2"),
            {"source": "jump-test"},
            {"messages": "2"},
        )
    root = saver._storage.storage.root(SESSION_ID, thread_id=thread_id)
    with sqlite3.connect(root / "index.sqlite") as connection:
        view_id, parent_view_id = connection.execute(
            "SELECT view_id, parent_view_id FROM context_views WHERE parent_view_id IS NOT NULL LIMIT 1"
        ).fetchone()
        assert parent_view_id is not None
        connection.execute(
            "UPDATE context_view_jumps SET ancestor_view_id = ? WHERE view_id = ? AND jump_level = 0",
            (view_id, view_id),
        )

    with pytest.raises(RuntimeError, match="jump 非法"):
        saver._storage.checkpoints.validate_context_view_chain(thread_id, "", view_id)


def test_pruning_preserves_all_canonical_bytes_and_item_offsets(
    context_saver: RolloutCheckpointSaver,
    accept_context_turn: Callable[[HumanMessage, str], ExecutionAdmission],
) -> None:
    saver = context_saver
    thread_id = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id
    first_user = HumanMessage(content="保留的问题", id="u1")
    first_messages = [first_user, AIMessage(content="保留的回答", id="a1")]
    first_admission = accept_context_turn(first_user, "turn-retained")
    with execution_admission_scope(first_admission, langgraph_thread_id=thread_id):
        first_config = saver.put(
            build_checkpoint_config(thread_id),
            _checkpoint("cp-1", first_messages),
            {"source": "compaction-test", "step": 1},
            {"messages": "1"},
        )
    second_user = HumanMessage(content="待回收的问题", id="u2")
    second_admission = accept_context_turn(second_user, "turn-discarded")
    with execution_admission_scope(second_admission, langgraph_thread_id=thread_id):
        second_config = saver.put(
            first_config,
            _checkpoint(
                "cp-2",
                [
                    *first_messages,
                    second_user,
                    AIMessage(content="待回收的回答", id="a2"),
                ],
                version="2",
            ),
            {"source": "compaction-test", "step": 2},
            {"messages": "2"},
        )
    rewind_config = saver.rewind(
        second_config,
        checkpoint_id="cp-1",
        source_anchor="u1",
    )
    third_user = HumanMessage(content="新分支的问题", id="u3")
    third_admission = accept_context_turn(third_user, "turn-new-branch")
    with execution_admission_scope(third_admission, langgraph_thread_id=thread_id):
        saver.put(
            rewind_config,
            _checkpoint(
                "cp-3",
                [
                    *first_messages,
                    third_user,
                    AIMessage(content="新分支的回答", id="a3"),
                ],
                version="3",
            ),
            {"source": "compaction-test", "step": 3},
            {"messages": "3"},
        )
    root = saver._storage.storage.root(SESSION_ID, thread_id=thread_id)
    rollout_path = root / "rollout.jsonl"
    before = rollout_path.read_bytes()
    with sqlite3.connect(root / "index.sqlite") as connection:
        catalog_before = connection.execute(
            "SELECT item_id, item_sequence, jsonl_offset, jsonl_length FROM item_catalog ORDER BY item_sequence"
        ).fetchall()
    plan = saver.plan_pruning(thread_id, retain_checkpoint_ids=("cp-1",))
    assert {candidate.checkpoint_id for candidate in plan.candidates} == {"cp-2"}
    saver.execute_pruning(thread_id, plan)
    assert rollout_path.read_bytes() == before
    assert not hasattr(saver, "compact_jsonl_offline")
    assert not list(root.glob(".*compaction-*"))

    current = saver.get_tuple(build_checkpoint_config(thread_id))
    assert current is not None
    assert [
        message.id for message in current.checkpoint["channel_values"]["messages"]
    ] == [
        "u1",
        "a1",
        "u3",
        "a3",
    ]
    with sqlite3.connect(root / "index.sqlite") as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 6
        assert (
            connection.execute(
                "SELECT item_id, item_sequence, jsonl_offset, jsonl_length FROM item_catalog ORDER BY item_sequence"
            ).fetchall()
            == catalog_before
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM control_events WHERE control_kind = 'offline_compaction'"
            ).fetchone()[0]
            == 0
        )
