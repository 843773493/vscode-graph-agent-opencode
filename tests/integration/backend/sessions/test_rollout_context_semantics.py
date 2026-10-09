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
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)

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
    checkpoint_id: str, messages: list[object], **channels: object
) -> dict[str, object]:
    checkpoint = empty_checkpoint()
    checkpoint["id"] = checkpoint_id
    checkpoint["channel_values"] = {"messages": messages, **channels}
    checkpoint["channel_versions"] = {
        channel: str(index + 1)
        for index, channel in enumerate(checkpoint["channel_values"])
    }
    checkpoint["updated_channels"] = list(checkpoint["channel_values"])
    return checkpoint


def test_context_view_filters_control_records_and_keeps_business_messages(
    context_saver: RolloutCheckpointSaver,
    accept_context_turn: Callable[[HumanMessage, str], ExecutionAdmission],
) -> None:
    saver = context_saver
    thread_id = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id
    user = HumanMessage(content="读取配置", id="user-1")
    internal_steering = HumanMessage(
        content="控制：工具返回后重新检查",
        id="steering-1",
        response_metadata={
            "internal": True,
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
    saver.admit_internal_execution(
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
            _checkpoint("checkpoint-2", second_messages, scratchpad={"job": "parent"}),
            {"source": "job", "step": 2},
            {"messages": "2", "scratchpad": "2"},
        )

    restored = saver.get_tuple(build_checkpoint_config(thread_id))
    assert restored is not None
    assert [
        message.id for message in restored.checkpoint["channel_values"]["messages"]
    ] == [
        "user-1",
        "steering-1",
        "assistant-tool-1",
        "tool-result-1",
        "assistant-final-1",
    ]
    assert (
        restored.checkpoint["channel_values"]["messages"][1].response_metadata[
            "internal"
        ]
        is True
    )

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
    with execution_admission_scope(admission, langgraph_thread_id=thread_id):
        saver.put(
            compact_config,
            _checkpoint(
                "checkpoint-compacted",
                [AIMessage(content="已压缩上下文", id="summary-1")],
                _summarization_event={"id": "summary-1"},
            ),
            {"source": "compaction", "step": 4},
            {"messages": "4", "_summarization_event": "4"},
        )
    compacted = saver.get_tuple(build_checkpoint_config(thread_id))
    assert compacted is not None
    assert [
        message.content
        for message in compacted.checkpoint["channel_values"]["messages"]
    ] == ["已压缩上下文"]
    with sqlite3.connect(root / "index.sqlite") as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM context_views WHERE view_kind = 'rewind' LIMIT 1"
            ).fetchone()
            is not None
        )
        compacted_view_kind = connection.execute(
            "SELECT v.view_kind FROM context_views v JOIN checkpoints c ON c.view_id = v.view_id WHERE c.checkpoint_id = 'checkpoint-compacted'"
        ).fetchone()[0]
        assert compacted_view_kind == "compaction"
        assert (
            connection.execute(
                "SELECT checkpoint_kind FROM checkpoints WHERE checkpoint_id = 'checkpoint-compacted'"
            ).fetchone()[0]
            == "compaction"
        )


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
        saver.put(
            first_config,
            _checkpoint(
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
            ),
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

    # v2 checkpoint 缺少 parent range 时必须明确拒绝，不能把损坏索引当成
    # 可兼容的旧路径继续投影。
    with sqlite3.connect(root / "index.sqlite") as connection:
        view_id = connection.execute(
            "SELECT context_view_id FROM checkpoint_channels WHERE checkpoint_id = 'checkpoint-tool-result' AND channel_name='messages'"
        ).fetchone()[0]
        connection.execute(
            "DELETE FROM context_view_ranges WHERE view_id = ? AND source_kind = 'view'",
            (view_id,),
        )
        connection.commit()
    with pytest.raises(FormatDispatchError, match="parent range"):
        saver.get_tuple(build_checkpoint_config(thread_id))


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

    # LangGraph continuation 的增量 checkpoint 只带新 HumanMessage；旧 view
    # 必须通过 parent range 继续提供整个并行 AI tool-call 声明组。
    continuation = HumanMessage(content="继续处理", id="user-continuation")
    admission = accept_context_turn(continuation, "turn-continuation")
    with execution_admission_scope(admission, langgraph_thread_id=thread_id):
        saver.put(
            parent_config,
            _checkpoint("parallel-continuation", [continuation]),
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

        # 损坏冻结 messages view，不能用独立 history selection 修补 channel。
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

    with pytest.raises(FormatDispatchError, match="parent range"):
        saver.get_tuple(build_checkpoint_config(thread_id))


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


def test_compaction_control_event_keeps_message_cutoff_identity(
    context_saver: RolloutCheckpointSaver,
    accept_context_turn: Callable[[HumanMessage, str], ExecutionAdmission],
) -> None:
    saver = context_saver
    thread_id = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id
    user = HumanMessage(
        content="问题", id="u1", response_metadata={"turn_id": "turn-1"}
    )
    admission = accept_context_turn(user, "turn-1")
    with execution_admission_scope(admission, langgraph_thread_id=thread_id):
        config = saver.put(
            build_checkpoint_config(thread_id),
            _checkpoint(
                "cp-1",
                [
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
                ],
            ),
            {"source": "cutoff-test"},
            {"messages": "1"},
        )
        saver.put(
            config,
            _checkpoint(
                "cp-2",
                [AIMessage(content="历史摘要", id="summary-1")],
                _summarization_event={"cutoff_message_id": "tool-1", "cutoff_index": 3},
            ),
            {"source": "compaction"},
            {"messages": "2", "_summarization_event": "2"},
        )
    root = saver._storage.storage.root(SESSION_ID, thread_id=thread_id)
    with sqlite3.connect(root / "index.sqlite") as connection:
        payload = connection.execute(
            "SELECT payload_json FROM control_events WHERE checkpoint_id = 'cp-2'"
        ).fetchone()[0]
    assert json.loads(payload)["cutoff_message_id"] == "tool-1"
    assert json.loads(payload)["cutoff_message_sequence"] == 3


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


def test_turn_anchor_walks_active_lineage_after_message_level_compaction(
    context_saver: RolloutCheckpointSaver,
    accept_context_turn: Callable[[HumanMessage, str], ExecutionAdmission],
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
        config = saver.put(
            build_checkpoint_config(thread_id),
            _checkpoint("cp-1", turn_messages),
            {"source": "anchor-test", "step": 1},
            {"messages": "1"},
        )
    second_user = HumanMessage(content="继续检查日志", id="turn-2-user")
    second_admission = accept_context_turn(second_user, "turn-2")
    with execution_admission_scope(second_admission, langgraph_thread_id=thread_id):
        config = saver.put(
            config,
            _checkpoint(
                "cp-2",
                [
                    *turn_messages,
                    second_user,
                    AIMessage(content="日志没有异常", id="turn-2-final"),
                ],
            ),
            {"source": "anchor-test", "step": 2},
            {"messages": "2"},
        )

    original = saver.resolve_turn_anchor(
        config,
        turn_id="turn-1",
        anchor_mode="inclusive",
    )
    assert original.view_id.startswith("view-")
    assert original.checkpoint_id == "cp-2"
    assert original.cutoff_message_sequence == original.last_message_sequence

    # 以 message 级边界压缩到 Turn-1 的中间，当前 view 不能伪造完整 Turn-1。
    with execution_admission_scope(second_admission, langgraph_thread_id=thread_id):
        config = saver.put(
            config,
            _checkpoint(
                "cp-3",
                [AIMessage(content="Turn-1 已压缩", id="summary-1"), turn_messages[-1]],
                _summarization_event={"cutoff_message_id": "turn-1-tool-result"},
            ),
            {"source": "compaction", "step": 3},
            {"messages": "3", "_summarization_event": "3"},
        )
        config = saver.put(
            config,
            _checkpoint(
                "cp-4",
                [AIMessage(content="Turn-1 压缩状态继续", id="summary-2")],
                _summarization_event={"cutoff_message_id": "turn-1-final"},
            ),
            {"source": "compaction", "step": 4},
            {"messages": "4", "_summarization_event": "4"},
        )

    resolved = saver.resolve_turn_anchor(
        config,
        turn_id="turn-1",
        anchor_mode="inclusive",
    )
    assert resolved.view_id == original.view_id
    assert resolved.checkpoint_id == "cp-2"
    assert [
        message.id
        for message in saver.materialize_turn_anchor(
            config,
            turn_id="turn-1",
            anchor_mode="inclusive",
        )[1]
    ] == [message.id for message in turn_messages]

    before = saver.resolve_turn_anchor(
        config,
        turn_id="turn-1",
        anchor_mode="before",
    )
    assert before.view_id == original.view_id
    assert before.cutoff_message_sequence == before.user_message_sequence - 1

    with sqlite3.connect(
        saver._storage.storage.index_path(SESSION_ID, thread_id=thread_id)
    ) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM context_view_turns WHERE view_id = (SELECT view_id FROM checkpoints WHERE checkpoint_id = 'cp-3') AND turn_id = 'turn-1'"
            ).fetchone()
            is None
        )
        assert (
            connection.execute(
                "SELECT 1 FROM context_view_turns WHERE turn_id = 'turn-1' LIMIT 1"
            ).fetchone()
            is not None
        )

    saver.rewind_to_turn(
        config,
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
                _checkpoint(f"cp-{index}", messages),
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
            _checkpoint("cp-2", [user, AIMessage(content="二", id="a1")]),
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
