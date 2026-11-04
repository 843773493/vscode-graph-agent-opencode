"""真实 checkpoint 的默认 context 排序保持原顺序，集合构建不随成员数重复。"""

from __future__ import annotations

import builtins
import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.base import empty_checkpoint

from app.services.infrastructure.rollout_context.storage.catalog import view_membership
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.rollout_context.test_context_epoch_transition import (
    epoch_case as epoch_case,
    epoch_db as epoch_db,
)


@pytest.mark.parametrize("message_count", [32, 256])
def test_metadata_checkpoint_reuses_inherited_membership_set(
    epoch_case, monkeypatch, request, message_count,
):
    case = epoch_case
    messages = [case.user, *(AIMessage(content=f"原消息 {index}", id=f"order-{index}") for index in range(message_count))]
    case.checkpoint("order-parent", messages=messages)
    parent_view = case.connection.execute(
        "SELECT context_view_id FROM checkpoint_channels WHERE checkpoint_id=? AND channel_name='messages'",
        (case.config["configurable"]["checkpoint_id"],),
    ).fetchone()[0]
    query = (
        "SELECT item_id,logical_item_ordinal,source_kind,context_selected,context_ordinal "
        "FROM context_view_items WHERE view_id=? ORDER BY logical_item_ordinal"
    )
    expected_members = case.connection.execute(query, (parent_view,)).fetchall()
    assert len(expected_members) >= message_count
    canonical_path = case.saver._storage.storage.jsonl_path(case.owner.session_id, thread_id=case.owner.thread_id)
    canonical_before = canonical_path.read_bytes()
    set_builds = []

    def observe_set(values=()):
        if isinstance(values, list) and len(values) >= message_count:
            set_builds.append(len(values))
        return builtins.set(values)

    # 仅记录目标 owner 的集合构建；实际 Saver、SQLite、JSONL 和 view 发布均照常执行。
    with monkeypatch.context() as patch:
        patch.setattr(view_membership, "set", observe_set, raising=False)
        checkpoint = empty_checkpoint()
        checkpoint["id"] = "order-metadata-only"
        checkpoint["channel_values"] = {"counter": 1}
        checkpoint["channel_versions"] = {"counter": "order-counter-1"}
        checkpoint["updated_channels"] = ["counter"]
        with case.scope():
            case.config = case.saver.put(
                case.config, checkpoint, {"source": "loop", "step": 2}, checkpoint["channel_versions"],
            )

    target_view = case.connection.execute(
        "SELECT context_view_id FROM checkpoint_channels WHERE checkpoint_id=? AND channel_name='messages'",
        (case.config["configurable"]["checkpoint_id"],),
    ).fetchone()[0]
    assert target_view != parent_view
    assert case.connection.execute(query, (target_view,)).fetchall() == expected_members
    assert canonical_path.read_bytes() == canonical_before
    with case.scope():
        restored = case.saver.get_tuple(case.config)
    assert [message.id for message in restored.checkpoint["channel_values"]["messages"]] == [message.id for message in messages]

    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    measurement = {"messages": len(messages), "large_set_builds": len(set_builds), "set_elements": sum(set_builds)}
    (context.artifacts_dir / "context-order-work.json").write_text(json.dumps(measurement, indent=2), encoding="utf-8")
    print(f"context order work: {measurement}")
    assert 0 < len(set_builds) <= 8, measurement
