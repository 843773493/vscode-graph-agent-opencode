"""真实 checkpoint 只物化目标 membership；完整审计检查历史 range 图。"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.base import empty_checkpoint

from app.core.sqlite_state import utc_now_text
from app.domain.itemized.errors import FormatDispatchError
from tests.integration.backend.sessions.rollout_context.test_context_epoch_transition import (
    epoch_case as epoch_case,  # noqa: PLC0414 - 按当前测试文件隔离真实存储
)
from tests.integration.backend.sessions.rollout_context.test_context_epoch_transition import (
    epoch_db as epoch_db,  # noqa: PLC0414 - 注入真实 SQLite/JSONL owner
)


def _put_counter(case, step):
    checkpoint = empty_checkpoint()
    checkpoint["id"] = f"range-counter-{step}"
    checkpoint["channel_values"] = {"counter": step}
    checkpoint["channel_versions"] = {"counter": str(step)}
    checkpoint["updated_channels"] = ["counter"]
    with case.scope():
        case.config = case.saver.put(
            case.config, checkpoint, {"source": "loop", "step": step}, checkpoint["channel_versions"],
        )


def test_saver_restores_only_target_membership_as_checkpoint_chain_grows(epoch_case):
    case = epoch_case
    messages = [case.user, *(AIMessage(id=f"range-message-{index}", content=f"冻结消息 {index}") for index in range(32))]
    case.checkpoint("range-messages", messages=messages)
    with case.scope():
        original_messages = case.saver.get_tuple(case.config).checkpoint["channel_values"]["messages"]
    storage = case.saver._storage.storage
    canonical = storage.jsonl_path(case.owner.session_id, thread_id=case.owner.thread_id)
    original_bytes = canonical.read_bytes()
    for step in range(1, 25):
        _put_counter(case, step)
        if step not in (4, 24):
            continue
        with case.scope():
            restored = case.saver.get_tuple(case.config)
        assert restored.checkpoint["channel_values"]["messages"] == original_messages
        assert restored.checkpoint["channel_values"]["counter"] == step
    assert canonical.read_bytes() == original_bytes


@pytest.fixture
def range_case(epoch_case):
    case = epoch_case
    _put_counter(case, 1)
    view = case.connection.execute(
        "SELECT context_view_id FROM checkpoint_channels WHERE checkpoint_id=? AND channel_name='messages'",
        (case.config["configurable"]["checkpoint_id"],),
    ).fetchone()[0]
    parent = case.connection.execute("SELECT parent_view_id FROM context_views WHERE view_id=?", (view,)).fetchone()[0]
    assert parent is not None
    return case, view, parent


@pytest.mark.parametrize("corruption,error", [
    ("cycle", "引用形成循环"),
    ("missing_view", "context view 不存在"),
    ("reverse", "range 不能反向"),
    ("source_kind", "source_kind 非法"),
    ("parent_range", "缺少显式 parent range"),
])
def test_saver_defers_ancestor_range_corruption_to_full_validation(range_case, corruption, error):
    case, view, parent = range_case
    with case.scope():
        original_checkpoint = case.saver.get_tuple(case.config).checkpoint
    canonical = case.saver._storage.storage.jsonl_path(case.owner.session_id, thread_id=case.owner.thread_id)
    original_bytes = canonical.read_bytes()
    with case.connection:
        if corruption in {"cycle", "missing_view"}:
            case.connection.execute(
                "UPDATE context_view_ranges SET source_kind='view',source_view_id=?,"
                "start_message_sequence=NULL,end_message_sequence=NULL WHERE view_id=?",
                (view if corruption == "cycle" else "missing-view", parent),
            )
        elif corruption == "reverse":
            case.connection.execute(
                "UPDATE context_view_ranges SET start_message_sequence=3,end_message_sequence=1 WHERE view_id=?",
                (parent,),
            )
        elif corruption == "source_kind":
            case.connection.execute("UPDATE context_view_ranges SET source_kind='broken' WHERE view_id=?", (parent,))
        else:
            case.connection.execute("DELETE FROM context_view_ranges WHERE view_id=? AND source_kind='view'", (view,))
    original_database = tuple(case.connection.iterdump())
    error_type = FormatDispatchError if corruption == "parent_range" else RuntimeError
    with case.scope():
        assert case.saver.get_tuple(case.config).checkpoint == original_checkpoint
    with pytest.raises(error_type, match=error):
        case.saver._storage.storage.validate_index(case.owner.session_id, case.owner.thread_id)
    assert canonical.read_bytes() == original_bytes
    assert tuple(case.connection.iterdump()) == original_database


def test_deep_owner_created_range_chain_does_not_recurse_in_python(epoch_case):
    case = epoch_case
    storage = case.saver._storage.storage
    branch = case.connection.execute("SELECT branch_id FROM context_views WHERE view_id=?", (case.active_view(),)).fetchone()[0]
    view = None
    with case.connection:
        for _ in range(1024):
            view = storage._create_view(
                case.connection, branch, view, (), utc_now_text(), view_kind="checkpoint_messages",
            )
    assert case.saver._storage.checkpoints.materialize_messages(
        case.owner.session_id, case.owner.thread_id, "", 0, context_ranges=((view, 0, 0),),
    ) == []
    with storage.validate_index(case.owner.session_id, case.owner.thread_id):
        pass


def test_shared_ranges_are_validated_and_later_cycles_still_fail(epoch_case):
    case = epoch_case
    storage = case.saver._storage.storage
    base = case.connection.execute(
        "SELECT context_view_id FROM checkpoint_channels WHERE checkpoint_id=? AND channel_name='messages'",
        (case.config["configurable"]["checkpoint_id"],),
    ).fetchone()[0]
    branch = case.connection.execute("SELECT branch_id FROM context_views WHERE view_id=?", (base,)).fetchone()[0]
    sequence = case.connection.execute("SELECT message_sequence FROM messages WHERE message_id=?", (case.user.id,)).fetchone()[0]
    with case.connection:
        left, right = [storage._create_view(
            case.connection, branch, base, (), utc_now_text(), view_kind="checkpoint_messages",
        ) for _ in range(2)]
        view = storage._create_view(
            case.connection, branch, left, (sequence,), utc_now_text(), view_kind="checkpoint_messages",
        )
        # 只构造合法的共享 range 图；消息、membership 与执行事实均来自真实 owner。
        case.connection.execute(
            "INSERT INTO context_view_ranges(view_id,range_index,source_kind,source_view_id) VALUES (?,2,'view',?)",
            (view, right),
        )
    messages = case.saver._storage.checkpoints.materialize_messages(
        case.owner.session_id, case.owner.thread_id, "", sequence, context_ranges=((view, 0, sequence),),
    )
    assert [(message.id, message.content) for message in messages] == [(case.user.id, case.user.content)]
    with storage.validate_index(case.owner.session_id, case.owner.thread_id):
        pass
    with case.connection:
        case.connection.execute(
            "INSERT INTO context_view_ranges(view_id,range_index,source_kind,source_view_id) VALUES (?,1,'view',?)",
            (right, view),
        )
    original_database = tuple(case.connection.iterdump())
    canonical = storage.jsonl_path(case.owner.session_id, thread_id=case.owner.thread_id)
    original_bytes = canonical.read_bytes()
    assert case.saver._storage.checkpoints.materialize_messages(
        case.owner.session_id, case.owner.thread_id, "", sequence, context_ranges=((view, 0, sequence),),
    ) == messages
    with pytest.raises(RuntimeError, match="引用形成循环"):
        storage.validate_index(case.owner.session_id, case.owner.thread_id)
    assert tuple(case.connection.iterdump()) == original_database
    assert canonical.read_bytes() == original_bytes


def test_full_checkpoint_restores_all_messages_as_history_grows(epoch_case):
    case = epoch_case
    messages = [case.user]
    for step in range(1, 13):
        messages.extend(AIMessage(id=f"full-{step}-{index}", content=f"消息 {step}-{index}") for index in range(8))
        case.checkpoint(f"full-{step}", messages=messages)
        if step not in (4, 12):
            continue
        # 经真实 Saver 入口重复读取，冻结 membership 必须保留完整消息及顺序。
        for _ in range(2):
            with case.scope():
                restored = case.saver.get_tuple(case.config)
            assert [(message.id, message.content) for message in restored.checkpoint["channel_values"]["messages"]] == [
                (message.id, message.content) for message in messages
            ]
    with case.saver._storage.storage.validate_index(case.owner.session_id, case.owner.thread_id):
        pass
