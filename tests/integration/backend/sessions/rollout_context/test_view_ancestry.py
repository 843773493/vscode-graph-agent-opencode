"""真实 view owner 的父链与 jump 不扫描无关分支，损坏仍明确失败。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.sqlite_state import utc_now_text
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.rollout_context.test_context_epoch_transition import (
    epoch_case as epoch_case,
    epoch_db as epoch_db,
)


def test_view_ancestry_work_is_independent_of_unrelated_views(epoch_case, monkeypatch, request):
    case = epoch_case
    storage = case.saver._storage.storage
    parent = case.active_view()
    branch = case.connection.execute(
        "SELECT branch_id FROM context_views WHERE view_id=?", (parent,),
    ).fetchone()[0]
    canonical = storage.jsonl_path(case.owner.session_id, thread_id=case.owner.thread_id)
    original_bytes = canonical.read_bytes()
    original_parent = case.connection.execute("SELECT * FROM context_views WHERE view_id=?", (parent,)).fetchone()
    with case.scope():
        original_messages = case.saver.get_tuple(case.config).checkpoint["channel_values"]["messages"]
    measurements = []
    steps = 0

    def progress():
        nonlocal steps
        steps += 1
        return 0

    connect = storage._connect

    def observe(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_progress_handler(progress, 1)
        return connection

    previous = 0
    for count in (64, 2048):
        # 通过实际 owner 创建空历史分支，保持目标祖先链和 canonical 历史不变。
        with case.connection:
            for _ in range(count - previous):
                storage._create_view(case.connection, branch, None, (), utc_now_text(), view_kind="history_replay")
        previous = count
        measurement = {"unrelated_views": count}
        with monkeypatch.context() as patch, case.scope():
            patch.setattr(storage, "_connect", observe)
            steps = 0
            case.saver._storage.checkpoints.validate_context_view_chain(case.owner.thread_id, "", parent)
            measurement["validation_steps"] = steps
        for kind in ("active", "history_replay"):
            with case.connection:
                case.connection.set_progress_handler(progress, 1)
                steps = 0
                try:
                    child = storage._create_view(
                        case.connection, branch, parent, (), utc_now_text(), view_kind=kind,
                    )
                finally:
                    case.connection.set_progress_handler(None, 0)
                measurement[kind + "_steps"] = steps
            assert case.connection.execute(
                "SELECT ancestor_view_id,ancestor_depth FROM context_view_jumps WHERE view_id=? AND jump_level=0",
                (child,),
            ).fetchone() == (parent, 1)
        measurements.append(measurement)

    assert canonical.read_bytes() == original_bytes
    assert case.connection.execute("SELECT * FROM context_views WHERE view_id=?", (parent,)).fetchone() == original_parent
    with case.scope():
        restored = case.saver.get_tuple(case.config)
    assert restored.checkpoint["channel_values"]["messages"] == original_messages
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    (context.artifacts_dir / "ancestry-work.json").write_text(json.dumps(measurements, indent=2), encoding="utf-8")
    print(f"view ancestry work: {measurements}")
    for key in ("validation_steps", "active_steps", "history_replay_steps"):
        assert measurements[1][key] <= measurements[0][key] + 200, measurements


def test_view_jumps_match_parent_chain_at_power_boundaries(epoch_case):
    case = epoch_case
    storage = case.saver._storage.storage
    branch = case.connection.execute("SELECT branch_id FROM context_views WHERE view_id=?", (case.active_view(),)).fetchone()[0]
    chain = []
    for depth in range(18):
        with case.connection:
            view = storage._create_view(
                case.connection, branch, chain[-1] if chain else None, (), utc_now_text(), view_kind="history_replay",
            )
        chain.append(view)
        expected = [(level, chain[-1 - (1 << level)], 1 << level) for level in range(depth.bit_length())]
        assert case.connection.execute(
            "SELECT jump_level,ancestor_view_id,ancestor_depth FROM context_view_jumps WHERE view_id=? ORDER BY jump_level",
            (view,),
        ).fetchall() == expected
        with case.scope():
            case.saver._storage.checkpoints.validate_context_view_chain(case.owner.thread_id, "", view)


@pytest.mark.parametrize("corruption,error", [
    ("cycle", "父链成环"),
    ("missing_parent", "引用不存在的 parent"),
    ("wrong_jump", "jump 非法"),
    ("huge_jump", "jump 越界"),
])
def test_view_ancestry_corruption_fails_without_rewriting_history(epoch_case, corruption, error):
    case = epoch_case
    storage = case.saver._storage.storage
    view = case.active_view()
    canonical = storage.jsonl_path(case.owner.session_id, thread_id=case.owner.thread_id)
    original_bytes = canonical.read_bytes()
    assert case.connection.execute("SELECT parent_view_id FROM context_views WHERE view_id=?", (view,)).fetchone()[0]
    with case.connection:
        if corruption in {"cycle", "missing_parent"}:
            case.connection.execute(
                "UPDATE context_views SET parent_view_id=? WHERE view_id=?",
                (view if corruption == "cycle" else "missing-parent", view),
            )
        elif corruption == "wrong_jump":
            case.connection.execute(
                "UPDATE context_view_jumps SET ancestor_view_id=? WHERE view_id=? AND jump_level=0", (view, view),
            )
        else:
            case.connection.execute(
                "INSERT INTO context_view_jumps VALUES (?,1000000000,?,1)", (view, view),
            )
    original_database = tuple(case.connection.iterdump())
    with case.scope(), pytest.raises(RuntimeError, match=error):
        case.saver._storage.checkpoints.validate_context_view_chain(case.owner.thread_id, "", view)
    assert canonical.read_bytes() == original_bytes
    assert tuple(case.connection.iterdump()) == original_database
