"""真实消息准入、Agent 与模型链路在队列领取/启动窗口硬退出后恢复。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from tests.integration.backend.sessions.test_session_communication_pipeline import (
    CommunicationCase,
    _completed,
)
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    communication_case as communication_case,  # noqa: PLC0414 — 显式注册共享 pytest fixture
)
from tests.support.api_helpers import create_session
from tests.support.e2e_control.harness import control_request


async def _send(case: CommunicationCase, session: str, text: str) -> dict[str, object]:
    response = await case.client.post(f"/api/v1/sessions/{session}/messages", json={
        "message": {"content": text},
        "run": {"mode": "single_agent", "agent_id": "default", "delivery_policy": "after_turn"},
    })
    assert response.status_code == 200, response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"]
    return response.json()["data"]


def _state(database: Path) -> dict[str, list[dict[str, object]]]:
    # 只读真实 owner 已提交的状态；不替产品构造 acceptance、checkpoint 或队列。
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        return {name: [dict(row) for row in connection.execute(query)] for name, query in {
            "acceptances": "SELECT * FROM turn_acceptances ORDER BY created_at,turn_id",
            "turn_identity": "SELECT session_id,thread_id,turn_id,turn_ordinal,source_branch_id,"
                             "root_input_item_id,accepted_ingress_id,acceptance_idempotency_key,"
                             "initial_execution_id,created_at FROM turn_records ORDER BY turn_ordinal",
            "executions": "SELECT execution_id,session_id,thread_id,turn_id,job_id,execution_origin,"
                          "admission_idempotency_key,admission_payload_hash,accepted_ingress_id,"
                          "attempt,execution_ordinal,created_at FROM executions ORDER BY job_id",
            "queue_identity": "SELECT checkpoint_ns,job_id,execution_id,admission_commit_id,input_json,"
                              "input_hash,created_at FROM execution_queue ORDER BY admission_commit_id",
            "roots": "SELECT item_id,item_sequence,turn_id,execution_id,content_hash FROM item_catalog "
                     "WHERE semantic_kind='user_input' ORDER BY item_sequence",
            "queue": "SELECT job_id,phase,claim_owner,claim_generation FROM execution_queue ORDER BY admission_commit_id",
            "outcomes": "SELECT job_id,outcome FROM executions ORDER BY job_id",
            "turns": "SELECT turn_id,status,final_item_id FROM turn_records ORDER BY turn_ordinal",
            "calls": "SELECT model_call_id,execution_id,outcome FROM model_calls ORDER BY created_at,model_call_id",
            "outputs": "SELECT item_id,execution_id,status,projection_manifest_json FROM item_catalog "
                       "WHERE semantic_kind='assistant_output' ORDER BY item_sequence",
        }.items()}


def _same_inputs(before: dict[str, list[dict[str, object]]], after: dict[str, list[dict[str, object]]]) -> None:
    for name in ("acceptances", "turn_identity", "executions", "queue_identity", "roots"):
        assert after[name] == before[name], name


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["claimed", "started"])
async def test_real_agent_recovers_original_fifo_after_queue_process_loss(
    communication_case: CommunicationCase, phase: str,
) -> None:
    case = communication_case
    control = case.replay.control
    session = await create_session(case.client, "执行队列进程恢复")
    calls = await case.plan(session, [
        {"content": "queue-head-result"}, {"content": "queue-middle-result"}, {"content": "queue-tail-result"},
    ])
    head_call, middle_call, tail_call = calls
    control.arm(head_call, "model.response_ready")
    control.arm(middle_call, "model.response_ready")
    barrier = "queue-first-generation"
    barrier_armed = False
    try:
        head = await _send(case, session, "queue-head-input")
        await asyncio.to_thread(control.acknowledged, head_call, "model.response_ready", 60)
        middle = await _send(case, session, "queue-middle-input")
        tail = await _send(case, session, "queue-tail-input")
        dispatches = [head, middle, tail]
        jobs = [dispatch["job_id"] for dispatch in dispatches]
        assert len(set(jobs)) == 3
        pending = await case.client.get(f"/api/v1/sessions/{session}/pending-requests")
        assert pending.status_code == 200, pending.text
        assert [row["job_id"] for row in pending.json()["data"]["requests"]] == jobs[1:]
        database = case.main_thread_node(session) / "rollout/index.sqlite"
        before = _state(database)
        assert [row["job_id"] for row in before["queue_identity"]] == jobs
        assert [row["phase"] for row in before["queue"]] == ["started", "queued", "queued"]
        assert len(before["acceptances"]) == len(before["executions"]) == len(before["roots"]) == 3
        for dispatch, turn in zip(dispatches, before["turn_identity"], strict=True):
            execution, = [row for row in before["executions"] if row["job_id"] == dispatch["job_id"]]
            assert (execution["turn_id"], turn["turn_id"], turn["thread_id"]) == (
                dispatch["turn_id"], dispatch["turn_id"], dispatch["thread_id"],
            )
            assert turn["initial_execution_id"] == execution["execution_id"]
        control.arm_match(barrier, "execution.queue." + phase, {
            "session_id": session, "thread_id": middle["thread_id"], "claim_generation": 1,
        })
        barrier_armed = True
        control.release(head_call, "model.response_ready")
        crash = await asyncio.to_thread(control.acknowledged_match, barrier)
        assert crash["operation_id"] == middle["job_id"]
        assert crash["pid"] == case.backend.process.pid
        at_crash = _state(database)
        _same_inputs(before, at_crash)
        assert [row["phase"] for row in at_crash["queue"]] == ["terminal", phase, "queued"]
        assert [row["claim_generation"] for row in at_crash["queue"]] == [1, 1, 0]
        assert [row["model_call_id"] for row in at_crash["calls"]] == [head_call]
        assert not (case.replay.artifacts / (middle_call + ".json")).exists()
        assert not (case.replay.artifacts / (tail_call + ".json")).exists()
        case.record("queue-before-crash", {"dispatches": dispatches, "state": at_crash, "ack": crash})

        # 屏障期间不向持有 dispatch lock 的 Backend 发 HTTP；由 pytest 直接硬杀进程。
        await case.hard_restart()
        await asyncio.to_thread(control.acknowledged, middle_call, "model.response_ready", 60)
        recovered = _state(database)
        _same_inputs(before, recovered)
        assert [row["phase"] for row in recovered["queue"]] == ["terminal", "started", "queued"]
        assert [row["claim_generation"] for row in recovered["queue"]] == [1, 2, 0]
        assert recovered["queue"][1]["claim_owner"] != at_crash["queue"][1]["claim_owner"]
        recovered_start, = [event for event in control.events
                            if event["operation_id"] == middle["job_id"]
                            and event["phase"] == "execution.queue.started"
                            and event["pid"] == case.backend.process.pid]
        assert recovered_start["details"]["claim_generation"] == 2
        assert recovered_start["details"]["execution_id"] == crash["details"]["execution_id"]
        assert not (case.replay.artifacts / (tail_call + ".json")).exists()
        case.record("queue-recovered-before-response", {"state": recovered, "ack": recovered_start})
        control.release(middle_call, "model.response_ready")
        # 已完成的 head 不会恢复为内存 Job；其终态沿下方 canonical owner 核验。
        for job in jobs[1:]:
            await _completed(case.client, job)
        finished = _state(database)
        _same_inputs(before, finished)
        assert [row["phase"] for row in finished["queue"]] == ["terminal"] * 3
        assert [row["outcome"] for row in finished["outcomes"]] == ["completed"] * 3
        assert [row["status"] for row in finished["turns"]] == ["completed"] * 3
        # 流 block、checkpoint 消息和替换后的 final_answer 可各有 canonical carrier。
        # 一次执行由模型身份和唯一最终逻辑结果证明，不能把合法 carrier 当作重复回答。
        final_outputs = [row for row in finished["outputs"]
                         if json.loads(row["projection_manifest_json"])["projection_identity"].get("phase") == "final_answer"]
        assert len(final_outputs) == 3
        assert [row["item_id"] for row in final_outputs] == [row["final_item_id"] for row in finished["turns"]]
        assert all(row["status"] == "completed" for row in finished["outputs"])
        assert {row["model_call_id"] for row in finished["calls"]} == set(calls)
        assert [row["outcome"] for row in finished["calls"]] == ["completed"] * 3
        assert [event["operation_id"] for event in control.events if event["phase"] == "model.response_ready"] == calls
        for index, call in enumerate(calls):
            request = case.request(call)
            expected_inputs = ["queue-head-input", "queue-middle-input", "queue-tail-input"]
            # request-only 来源也投影为 user；精确核对真实输入，并拒绝任何未来输入泄漏。
            inputs = [message["content"] for message in request["messages"]
                      if message["role"] == "user" and message["content"] in expected_inputs]
            assert inputs == expected_inputs[:index + 1]
            wire = json.dumps(request["messages"], ensure_ascii=False)
            assert all(text not in wire for text in expected_inputs[index + 1:])
            call_row, = [row for row in finished["calls"] if row["model_call_id"] == call]
            execution, = [row for row in finished["executions"] if row["job_id"] == jobs[index]]
            assert call_row["execution_id"] == execution["execution_id"]
            output, = [row for row in final_outputs if row["execution_id"] == execution["execution_id"]]
            assert output["item_id"] == finished["turns"][index]["final_item_id"]
            assert all(json.loads(row["projection_manifest_json"])["projection_identity"]["model_call_id"] == call
                       for row in finished["outputs"] if row["execution_id"] == execution["execution_id"])
        pending = await case.client.get(f"/api/v1/sessions/{session}/pending-requests")
        assert pending.status_code == 200, pending.text
        assert pending.json()["data"]["requests"] == []
        assert pending.json()["data"]["active_job_id"] is None

        # 第二次冷启动不得重新准入或重放任何已完成的模型调用。
        await case.hard_restart()
        assert _state(database) == finished
        history = await case.client.post(f"/api/v1/sessions/{session}/history", json={"direction": "head", "turns": 3})
        assert history.status_code == 200, history.text
        visible = history.json()["data"]["items"]
        assert [(turn["turn_id"], turn["job_id"], [message["content"] for message in turn["user_messages"]],
                 turn["final_response"]) for turn in visible] == [
            (dispatch["turn_id"], dispatch["job_id"], [f"queue-{name}-input"], f"queue-{name}-result")
            for dispatch, name in zip(dispatches, ("head", "middle", "tail"), strict=True)
        ]
        pending = await case.client.get(f"/api/v1/sessions/{session}/pending-requests")
        assert pending.status_code == 200, pending.text
        assert pending.json()["data"]["requests"] == []
        assert pending.json()["data"]["active_job_id"] is None
        assert [event["operation_id"] for event in control.events if event["phase"] == "model.response_ready"] == calls
        case.record("queue-terminal-after-second-restart", {"state": finished, "events": control.events})
    finally:
        control.release(head_call, "model.response_ready")
        control.release(middle_call, "model.response_ready")
        if barrier_armed:
            control_request(json.loads(control.environment["BOXTEAM_E2E_TEST_CONTROL"]), {
                "action": "release_match", "barrier_id": barrier,
            })
