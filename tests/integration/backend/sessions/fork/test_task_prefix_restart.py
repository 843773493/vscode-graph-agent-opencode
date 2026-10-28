"""真实模型 task 的前缀捕获、发布与初始绑定硬重启。"""

from __future__ import annotations

import asyncio
import json
import traceback
from contextlib import closing
from dataclasses import asdict

import pytest

from app.core.path_utils import get_session_path_resolver
from app.core.session_control_primitives import CONTROL_DATABASE_NAME
from app.core.session_control_store import SessionControlStore
from app.services.infrastructure.rollout_context.fork.source_capture import (
    read_captured_snapshot,
)
from app.services.orchestration.session_subagent_service import _derive_delegation_id
from tests.integration.backend.sessions.fork.test_prefix_restart import _record, _sql
from tests.integration.backend.sessions.fork.test_prefix_restart import (
    fork_deadlines as fork_deadlines,  # noqa: PLC0414 - 保留正式60秒监督器
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RestartRuntimeSourceCase,
    _job,
    _send,
    _tool_reply,
    _wait_until,
    _wire_text,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    restart_source_case as restart_source_case,  # noqa: PLC0414 - 复用真实进程及model_stream
)
from tests.support.api_helpers import create_session
from tests.support.e2e_control.runtime_ports import control_request
from tests.support.fork_deadline import ForkDeadlineRegistry


def _release_match(case: RestartRuntimeSourceCase, barrier: str) -> None:
    control_request(json.loads(case.control.environment["BOXTEAM_E2E_TEST_CONTROL"]), {
        "action": "release_match", "barrier_id": barrier,
    })


@pytest.mark.parametrize("restart_source_case", [{"native_services": True, "managed_gateway": True}], indirect=True)
@pytest.mark.parametrize("phase", [
    "copy.source.before_captured_cas", "copy.target.published", "thread.initial.admitted_before_bound",
], ids=["capture", "published", "admitted"])
async def test_task_prefix_hard_restart_reuses_frozen_owner_and_initial_job(
    restart_source_case: RestartRuntimeSourceCase, fork_deadlines: ForkDeadlineRegistry, phase: str,
) -> None:
    case = restart_source_case
    session = await create_session(case.client, "task 前缀硬重启")
    await case.plan(session, [{"content": "task-restart-frozen-answer"}, {"content": "task-restart-tail-answer"}])
    first = await _send(case, session, "task-restart-frozen-question")
    thread = (await _job(case, first["job_id"]))["thread_id"]
    item, = _sql(case, session, thread,
        "SELECT turn.final_item_id FROM turn_records turn JOIN executions execution "
        "ON execution.execution_id=turn.initial_execution_id WHERE execution.job_id=?", (first["job_id"],))[0]
    tail = await _send(case, session, "task-restart-tail-question")
    await _job(case, tail["job_id"])
    call_id = "call_task_restart_prefix"
    model_call, *_ = await case.plan(session, [
        _tool_reply("task", {"description": "task-restart-private-seed", "context": {
            "mode": "view_prefix_fork", "item_id": item,
        }}, call_id),
        {"content": "task-restart-queued-answer"}, {"content": "task-restart-report-ack"},
    ])
    case.control.arm(model_call, "model.response_ready")
    parent = await _send(case, session, "task-restart-active-question")
    await asyncio.to_thread(case.control.acknowledged, model_call, "model.response_ready")
    key = _derive_delegation_id(parent_session_id=session, parent_job_id=parent["job_id"],
                                parent_tool_call_id=call_id, subagent_type="general-purpose")
    capture_phase = "copy.source.before_captured_cas"
    case.control.arm(key, capture_phase)
    if phase == "copy.target.published":
        case.control.arm(key, phase)
    if phase == "thread.initial.admitted_before_bound":
        case.control.arm_match("task-admission", phase, {"session_id": session})
    case.control.arm_match("task-child", "thread.runtime.acquired", {
        "session_id": session, "execution_origin": "internal",
    })
    resolver = get_session_path_resolver(case.workspace / ".boxteam/sessions")
    session_dir = resolver.resolve_session_node(session)
    deadline = fork_deadlines.for_operation(target=thread, operation_key=key)
    case.control.release(model_call, "model.response_ready")
    try:
        await deadline.wait(asyncio.to_thread(case.control.acknowledged, key, capture_phase), "task私有快照落盘")
        original = _record(case, session, key, True)
        child = original["child_thread_id"]
        assert original["state"] == "preparing"
        assert json.loads(original["request_intent_json"])["source_point"] is None
        capture = resolver.catalog_store.get_fork_source_capture(creation_key=key)
        frozen_snapshot, marker = read_captured_snapshot(resolver.sessions_root, capture)
        frozen = frozen_snapshot.source_point
        assert frozen.anchor_item_id == item
        expected = _sql(case, session, thread,
            "SELECT item_id FROM context_view_items WHERE view_id=? AND visible=1 AND context_selected=1 "
            "AND context_ordinal<=(SELECT context_ordinal FROM context_view_items WHERE view_id=? AND item_id=?) "
            "ORDER BY context_ordinal", (frozen.source_view_id, frozen.source_view_id, item))
        visible = await deadline.wait(case.client.get(f"/api/v1/sessions/{session}/child-threads"), "未发布child不可见")
        assert visible.status_code == 200 and visible.json()["data"]["items"] == [], visible.text
        # 真实消息入口先提交queued root；不能由测试拼接候选view或历史。
        queued = await deadline.wait(_send(case, session, "task-restart-queued-question"), "冻结后追加排队输入")
        latest_response = await deadline.wait(case.client.get(f"/api/v1/sessions/{session}/fork-source-point", params={
            "thread_id": thread, "item_id": item,
        }), "核对冻结后的候选")
        assert latest_response.status_code == 200, latest_response.text
        latest = latest_response.json()["data"]
        case.write_actual("task-prefix-later-candidate", {"frozen": frozen.model_dump(), "latest": latest, "queued": queued})
        assert latest["source_view_revision"] > frozen.source_view_revision
        if phase != capture_phase:
            case.control.release(key, capture_phase)
            if phase == "copy.target.published":
                await deadline.wait(asyncio.to_thread(case.control.acknowledged, key, phase), "task发布后中断")
            else:
                admitted = await deadline.wait(asyncio.to_thread(case.control.acknowledged_match, "task-admission"), "task准入后绑定前中断")
                assert admitted["details"]["thread_id"] == child
        before = _record(case, session, key, True)
        assert before["state"] == ("preparing" if phase == capture_phase else "published")
        if phase == "thread.initial.admitted_before_bound":
            before_binding = case.snapshot(session_dir)
            assert before_binding["intent"]["state"] == "pending"
            assert _sql(case, session, child,
                "SELECT execution_id,job_id FROM executions WHERE thread_id=?", (child,)) == [
                    (before_binding["intent"]["execution_binding_id"], before_binding["intent"]["job_id"]),
                ]
            assert _sql(case, session, child,
                "SELECT model.model_call_id FROM model_calls model JOIN executions execution USING(execution_id) "
                "WHERE execution.thread_id=?", (child,)) == []
        case.write_actual("task-prefix-before-kill", {
            "phase": phase, "record": before, "capture": asdict(capture), "marker": marker,
            "binding": before_binding if phase == "thread.initial.admitted_before_bound" else None,
        })

        def release_stopped() -> None:
            if phase == "thread.initial.admitted_before_bound":
                _release_match(case, "task-admission")
            else:
                case.control.release(key, phase)

        await deadline.wait(case.restart(hard=True, while_stopped=release_stopped), "task后端硬重启")
        event = await deadline.wait(asyncio.to_thread(case.control.acknowledged_match, "task-child"), "原child恢复准入")
        assert event["details"]["thread_id"] == child
        deadline.complete()
    except Exception:
        case.write_actual("task-prefix-restart-failure", {"traceback": traceback.format_exc()})
        raise
    finally:
        case.control.release(key, capture_phase)
        if phase == "copy.target.published":
            case.control.release(key, phase)
        if phase == "thread.initial.admitted_before_bound":
            _release_match(case, "task-admission")

    try:
        recovered = _record(case, session, key, True)
        assert recovered["state"] == "published"
        immutable = set(original) - {"state", "record_updated_at", "fork_completion_json"}
        assert {name: recovered[name] for name in immutable} == {name: original[name] for name in immutable}
        restored = resolver.catalog_store.get_fork_source_capture(creation_key=key)
        assert restored.state == "materialized"
        assert (restored.snapshot_id, restored.preimage_hash, restored.marker_sha256) == (capture.snapshot_id, capture.preimage_hash, marker)
        assert restored.captured_point == frozen
        inherited = _sql(case, session, child,
            "SELECT member.item_id FROM checkpoint_namespace_state namespace JOIN branches branch "
            "ON branch.branch_id=namespace.active_branch_id JOIN context_view_items member ON member.view_id=branch.head_view_id "
            "WHERE namespace.checkpoint_ns='' AND member.visible=1 AND member.context_selected=1 ORDER BY member.context_ordinal")
        assert inherited == expected
        materializations = _sql(case, session, child,
            "SELECT materialization_id,source_session_id,source_thread_id,source_view_id,status FROM fork_materializations")
        assert materializations == [(restored.materialization_id, session, thread, frozen.source_view_id, "committed")]
        bound = case.snapshot(session_dir)
        intent = bound["intent"]
        assert intent["state"] == "bound" and intent["creation_idempotency_key"] == key
        assert (intent["thread_id"], intent["job_id"]) == (child, event["details"]["job_id"])
        if phase == "thread.initial.admitted_before_bound":
            assert intent == {**before_binding["intent"], "state": "bound"}
            assert bound["child"]["admissions"] == before_binding["child"]["admissions"]
            assert bound["child"]["admission_commits"] == before_binding["child"]["admission_commits"]
        with closing(SessionControlStore(session_dir / CONTROL_DATABASE_NAME)) as control:
            assert control.connection.execute("SELECT COUNT(*) FROM thread_creation_records").fetchone()[0] == 1
            assert control.connection.execute("SELECT COUNT(*) FROM collaboration_members").fetchone()[0] == 1
            member = control.get_collaboration_member(key)
            assert (member.coordinator_thread_id, member.child_thread_id, member.state) == (thread, child, "published")
        child_call, = await case.plan(session, [{"content": "task-restart-child-result"}], thread_id=child)
    finally:
        _release_match(case, "task-child")
    await _job(case, intent["job_id"])
    await _job(case, queued["job_id"])
    await _wait_until(lambda: bool(_sql(case, session, thread,
        "SELECT execution_id FROM executions WHERE execution_origin='internal' AND outcome='completed'")))
    wire = _wire_text(case.request(child_call)["messages"])
    for required in ("task-restart-frozen-question", "task-restart-frozen-answer", "task-restart-private-seed", session, thread, key):
        assert required in wire, required
    for excluded in ("task-restart-tail-", "task-restart-active-", "task-restart-queued-", call_id):
        assert excluded not in wire, excluded
    completed = case.snapshot(session_dir)
    assert _sql(case, session, child,
        "SELECT execution_id,job_id,admission_idempotency_key,outcome FROM executions WHERE thread_id=?", (child,)) == [
            (intent["execution_binding_id"], intent["job_id"], intent["admission_idempotency_key"], "completed"),
        ]
    assert case.call_count(session, child) == 1
    calls = case.call_count(session, thread)
    await case.restart(hard=False)
    second = case.snapshot(session_dir)
    case.write_actual("task-prefix-restart-snapshots", {"completed": completed, "second": second})
    assert second["intent"] == completed["intent"] and second["child"] == completed["child"]
    assert {name: rows for name, rows in second["parent"].items() if name != "wakeups"} == {
        name: rows for name, rows in completed["parent"].items() if name != "wakeups"
    }
    before_wakeup, = completed["parent"]["wakeups"]
    after_wakeup, = second["parent"]["wakeups"]
    assert after_wakeup[:6] == before_wakeup[:6] and after_wakeup[5] == "dispatched"
    # 启动对账会把原dispatch的排队/运行回执推进到已完成，身份保持不变。
    before_receipt, after_receipt = json.loads(before_wakeup[6]), json.loads(after_wakeup[6])
    for field in ("message_id", "job_id", "thread_id", "turn_id"):
        assert after_receipt[field] == before_receipt[field]
    assert after_receipt["status"] == after_receipt["dispatch"]["job_status"] == "completed"
    assert after_receipt["dispatch"]["job_id"] == after_wakeup[3]
    assert after_receipt["dispatch"]["thread_id"] == thread
    assert (case.call_count(session, child), case.call_count(session, thread)) == (1, calls)
    assert _record(case, session, key, True) == recovered
    case.write_actual("task-prefix-after-second-restart", {
        "record": recovered, "capture": asdict(restored), "expected": expected, "inherited": inherited,
        "snapshot": second, "child_call": child_call, "backend_pids": [backend.process.pid for backend in case.backends],
    })
