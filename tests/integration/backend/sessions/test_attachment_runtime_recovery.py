"""真实 HTTP、model_stream 和 Backend 硬退出验证目录代次与 FIFO 恢复。"""

from __future__ import annotations

import asyncio
import json
import sqlite3

from tests.integration.backend.sessions.test_attachment_directory_entry import upload
from tests.integration.backend.sessions.test_execution_queue_product_recovery import (
    _same_inputs,
    _state,
)
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    CommunicationCase,
    _completed,
)
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    communication_case as communication_case,  # noqa: PLC0414
)
from tests.support.api_helpers import create_session
from tests.support.e2e_control.harness import control_request


def directory_state(case: CommunicationCase) -> tuple[str, str, str | None, str | None]:
    path = case.workspace / ".boxteam/attachments/catalog.sqlite"
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
        row, = connection.execute(
            "SELECT a.binding_token,a.state,d.unattached_since,d.gc_after "
            "FROM attachment_directory_associations a JOIN attachment_directories d USING(attachment_id)",
        )
        return row


async def test_hard_restart_preserves_queued_attachment_and_releases_only_stale_runtime(communication_case: CommunicationCase) -> None:
    case = communication_case
    session = await create_session(case.client, "附件目录硬退出恢复")
    head_call, tail_call = await case.plan(session, [{"content": "附件已接受"}, {"content": "队列已恢复"}])
    control = case.replay.control
    control.arm(head_call, "model.response_ready")
    control.arm(tail_call, "model.response_ready")
    response = await case.client.post(f"/api/v1/sessions/{session}/messages", json={
        "message": {"content": "带目录输入", "attachments": [upload(b"restart original").model_dump(mode="json")]},
        "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert response.status_code == 200, response.text
    head = response.json()["data"]
    barrier = "attachment-queue-before-new-runtime"
    try:
        await asyncio.to_thread(control.acknowledged, head_call, "model.response_ready", 60)
        old_token, state, start, deadline = directory_state(case)
        assert old_token.startswith("runtime:") and state == "active" and start is deadline is None
        response = await case.client.post(f"/api/v1/sessions/{session}/messages", json={
            "message": {"content": "继续处理上轮附件"},
            "run": {"mode": "single_agent", "agent_id": "default", "delivery_policy": "after_turn"},
        })
        assert response.status_code == 200, response.text
        tail = response.json()["data"]
        control.arm_match(barrier, "execution.queue.claimed", {
            "session_id": session, "thread_id": tail["thread_id"], "claim_generation": 1,
        })
        control.release(head_call, "model.response_ready")
        await asyncio.to_thread(control.acknowledged_match, barrier)
        database = case.main_thread_node(session) / "rollout/index.sqlite"
        before = _state(database)
        assert [row["phase"] for row in before["queue"]] == ["terminal", "claimed"]
        assert directory_state(case) == (old_token, "active", None, None)
        case.record("attachment-before-hard-exit", {"directory": directory_state(case), "state": before})
        await case.hard_restart()
        await asyncio.to_thread(control.acknowledged, tail_call, "model.response_ready", 60)
        new_token, state, start, deadline = directory_state(case)
        assert new_token != old_token and new_token.startswith("runtime:")
        assert state == "active" and start is deadline is None
        _same_inputs(before, _state(database))
        control.release(tail_call, "model.response_ready")
        await _completed(case.client, tail["job_id"])
        finished = _state(database)
        assert [row["outcome"] for row in finished["outcomes"]] == ["completed", "completed"]
        assert len(finished["calls"]) == 2
        await case.hard_restart()
        released = directory_state(case)
        assert released[0] == new_token and released[1] == "released"
        assert released[2] is not None and released[3] is not None
        assert _state(database) == finished
        case.record("attachment-after-cold-reconcile", {"directory": released, "state": finished, "head": head, "tail": tail})
        await case.hard_restart()
        assert directory_state(case) == released
        assert _state(database) == finished
    finally:
        control.release(head_call, "model.response_ready")
        control.release(tail_call, "model.response_ready")
        control_request(json.loads(control.environment["BOXTEAM_E2E_TEST_CONTROL"]), {
            "action": "release_match", "barrier_id": barrier,
        })
