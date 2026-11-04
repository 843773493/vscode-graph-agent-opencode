"""真实 checkpoint 只物化目标 membership，仍检查关联 range 的结构损坏。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.base import empty_checkpoint

from app.core.sqlite_state import utc_now_text
from app.domain.itemized.errors import FormatDispatchError
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.rollout_context.test_context_epoch_transition import (
    epoch_case as epoch_case,
    epoch_db as epoch_db,
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


def test_saver_restores_only_target_membership_as_checkpoint_chain_grows(epoch_case, monkeypatch, request):
    case = epoch_case
    messages = [case.user, *(AIMessage(id=f"range-message-{index}", content=f"冻结消息 {index}") for index in range(32))]
    case.checkpoint("range-messages", messages=messages)
    with case.scope():
        original_messages = case.saver.get_tuple(case.config).checkpoint["channel_values"]["messages"]
    storage = case.saver._storage.storage
    canonical = storage.jsonl_path(case.owner.session_id, thread_id=case.owner.thread_id)
    original_bytes = canonical.read_bytes()
    connect = storage._connect
    statements = []
    steps = 0

    def progress():
        nonlocal steps
        steps += 1
        return 0

    def observe(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        connection.set_progress_handler(progress, 1)
        return connection

    measurements = []
    for step in range(1, 25):
        _put_counter(case, step)
        if step not in (4, 24):
            continue
        steps = 0
        statements.clear()
        with case.scope(), monkeypatch.context() as patch:
            patch.setattr(storage, "_connect", observe)
            restored = case.saver.get_tuple(case.config)
        assert restored.checkpoint["channel_values"]["messages"] == original_messages
        assert restored.checkpoint["channel_values"]["counter"] == step
        measurements.append({
            "metadata_checkpoints": step,
            "membership_queries": sum("SELECT member.item_id, item.semantic_kind" in sql for sql in statements),
            "span_queries": sum("SELECT COUNT(*) FROM messages WHERE message_sequence BETWEEN" in sql for sql in statements),
            "sqlite_vm_steps": steps,
        })
    assert canonical.read_bytes() == original_bytes
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    (context.artifacts_dir / "sequence-work.json").write_text(json.dumps(measurements, indent=2), encoding="utf-8")
    print(f"view sequence work: {measurements}")
    assert measurements[0]["membership_queries"] == measurements[1]["membership_queries"] == 1, measurements


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
    ("gap", "不存在完整消息范围"),
    ("source_kind", "source_kind 非法"),
    ("parent_range", "缺少显式 parent range"),
])
def test_saver_rechecks_ancestor_range_corruption_on_every_read(range_case, corruption, error):
    case, view, parent = range_case
    with case.scope():
        assert case.saver.get_tuple(case.config) is not None
    canonical = case.saver._storage.storage.jsonl_path(case.owner.session_id, thread_id=case.owner.thread_id)
    original_bytes = canonical.read_bytes()
    with case.connection:
        if corruption in {"cycle", "missing_view"}:
            case.connection.execute(
                "UPDATE context_view_ranges SET source_kind='view',source_view_id=?,"
                "start_message_sequence=NULL,end_message_sequence=NULL WHERE view_id=?",
                (view if corruption == "cycle" else "missing-view", parent),
            )
        elif corruption in {"reverse", "gap"}:
            case.connection.execute(
                "UPDATE context_view_ranges SET start_message_sequence=?,end_message_sequence=? WHERE view_id=?",
                (3 if corruption == "reverse" else 1, 1 if corruption == "reverse" else 999999, parent),
            )
        elif corruption == "source_kind":
            case.connection.execute("UPDATE context_view_ranges SET source_kind='broken' WHERE view_id=?", (parent,))
        else:
            case.connection.execute("DELETE FROM context_view_ranges WHERE view_id=? AND source_kind='view'", (view,))
    original_database = tuple(case.connection.iterdump())
    error_type = FormatDispatchError if corruption == "parent_range" else RuntimeError
    with case.scope(), pytest.raises(error_type, match=error):
        case.saver.get_tuple(case.config)
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


def test_shared_ranges_are_checked_once_and_later_cycles_still_fail(epoch_case, monkeypatch):
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
    statements = []
    connect = storage._connect

    def observe(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    with monkeypatch.context() as patch:
        patch.setattr(storage, "_connect", observe)
        messages = case.saver._storage.checkpoints.materialize_messages(
            case.owner.session_id, case.owner.thread_id, "", sequence, context_ranges=((view, 0, sequence),),
        )
    assert [(message.id, message.content) for message in messages] == [(case.user.id, case.user.content)]
    assert sum("SELECT view_kind, parent_view_id FROM context_views" in sql for sql in statements) == 4
    assert sum("SELECT COUNT(*) FROM messages WHERE message_sequence BETWEEN" in sql for sql in statements) == 1
    with case.connection:
        case.connection.execute(
            "INSERT INTO context_view_ranges(view_id,range_index,source_kind,source_view_id) VALUES (?,1,'view',?)",
            (right, view),
        )
    original_database = tuple(case.connection.iterdump())
    canonical = storage.jsonl_path(case.owner.session_id, thread_id=case.owner.thread_id)
    original_bytes = canonical.read_bytes()
    with pytest.raises(RuntimeError, match="引用形成循环"):
        case.saver._storage.checkpoints.materialize_messages(
            case.owner.session_id, case.owner.thread_id, "", sequence, context_ranges=((view, 0, sequence),),
        )
    assert tuple(case.connection.iterdump()) == original_database
    assert canonical.read_bytes() == original_bytes
