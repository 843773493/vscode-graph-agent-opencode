"""真实工具响应已到达来源、尚未提交时的 Backend 硬退出恢复。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import cast

import pytest

from app.core.session_control_primitives import CONTROL_DATABASE_NAME
from app.core.session_control_store import SessionControlStore
from app.domain.itemized.identity.tool_call_identity import software_tool_operation_id
from app.services.business.session_context_resource import (
    session_resource_identity,
    session_resource_uri,
)
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    CommunicationCase,
    _completed,
    _tool,
)
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    communication_case as communication_case,  # noqa: PLC0414 — 显式注册共享 pytest fixture
)
from tests.support.api_helpers import create_session


async def _message(case: CommunicationCase, session_id: str, text: str) -> dict[str, object]:
    response = await case.client.post(f"/api/v1/sessions/{session_id}/messages", json={
        "message": {"content": text}, "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert response.status_code == 200, response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"]
    return response.json()["data"]


def _tool_results(database: Path, call_id: str) -> list[tuple[object, ...]]:
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        return connection.execute(
            "SELECT item_id,status,execution_id,producer_ref_json,projection_manifest_json "
            "FROM item_catalog WHERE semantic_kind='tool_result' AND tool_call_id=?",
            (call_id,),
        ).fetchall()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["read", "wait"])
async def test_hard_restart_replays_original_observation_and_one_tool_result(
    communication_case: CommunicationCase, operation: str,
) -> None:
    """产品入口生成历史；进程外屏障只暂停，不替产品填写恢复状态。"""
    case = communication_case
    source = await create_session(case.client, "观察恢复来源")
    target = await create_session(case.client, "观察恢复目标")
    await case.plan(target, [{"content": "冻结前的目标结果"}])
    target_dispatch = await _message(case, target, "生成冻结前历史")
    await _completed(case.client, target_dispatch["job_id"])
    if operation == "read":
        tool_name = "invoke_extension_tool"
        arguments = {"tool_name": "read_context", "arguments": {
            "resource_identity": str(session_resource_identity(case.backend.workspace_id, target)),
            "uri": session_resource_uri(case.backend.workspace_id, target),
            "scope": "workspace", "view": "messages",
        }}
    else:
        tool_name = "wait_for_session"
        arguments = {"target_session_id": target, "job_id": target_dispatch["job_id"]}
    provider_call = "observation-crash-" + operation
    first, final = await case.plan(source, [
        _tool(tool_name, arguments, provider_call), {"content": "原观察已恢复"},
    ])
    source_thread = case.planned_calls[first]["thread_id"]
    source_call = software_tool_operation_id(operation, source, source_thread, first, provider_call)
    scoped_call = first + ":tool-call:" + provider_call
    phase = "observation.source.response_received"
    case.replay.control.arm(source_call, phase)
    try:
        dispatch = await _message(case, source, "观察目标并保留原结果")
        await asyncio.to_thread(case.replay.control.acknowledged, source_call, phase, 60)
        source_node = await case.session_node(source)
        target_node = await case.session_node(target)
        database = case.main_thread_node(source) / "rollout/index.sqlite"
        with closing(SessionControlStore(source_node / CONTROL_DATABASE_NAME)) as store:
            original, = store.list_remote_observations()
            assert original.side == "source"
            envelope = original.request["envelope"]
            assert envelope["source_call_id"] == source_call
            assert envelope["source"]["session_id"] == source
            assert envelope["source"]["thread_id"] == source_thread
            source_page, = store.list_remote_pages(original.observation_id, side="source")
            assert source_page.result is source_page.target_result is None
        with closing(SessionControlStore(target_node / CONTROL_DATABASE_NAME)) as store:
            frozen = store.get_remote_observation(original.observation_id, side="target")
            target_page, = store.list_remote_pages(original.observation_id, side="target")
            assert frozen.snapshot is not None and target_page.result is not None
        assert _tool_results(database, scoped_call) == []
        assert not (case.replay.artifacts / (final + ".json")).exists()
        if operation == "read":
            assert "冻结前的目标结果" in json.dumps(target_page.result, ensure_ascii=False)
        else:
            assert target_page.result["status"] == "completed"
            assert target_page.result["observed"][0]["selector_id"] == target_dispatch["job_id"]
            assert envelope["query"]["deadline"]["timeout_seconds"] == 60

        # 冻结之后再通过真实消息推进目标，恢复不能重新读取最新历史或换 selector。
        await case.plan(target, [{"content": "冻结后不应混入的目标结果"}])
        newer = await _message(case, target, "生成冻结后历史")
        await _completed(case.client, newer["job_id"])
        await case.hard_restart()
    finally:
        case.replay.control.release(source_call, phase)

    recovered_job = await _completed(case.client, dispatch["job_id"])
    assert recovered_job["thread_id"] == source_thread
    with closing(SessionControlStore(source_node / CONTROL_DATABASE_NAME)) as store:
        recovered, = store.list_remote_observations()
        recovered_page, = store.list_remote_pages(original.observation_id, side="source")
        assert recovered == original
        assert recovered_page.request == source_page.request
        assert recovered_page.target_result == target_page.result
        assert recovered_page.result is not None
        for identity in ("source-observation|" + original.observation_id, "source-page|" + source_page.page_identity):
            assert store.find_lease_by_operation(identity).state == "completed"
    with closing(SessionControlStore(target_node / CONTROL_DATABASE_NAME)) as store:
        assert store.get_remote_observation(original.observation_id, side="target") == frozen
        assert store.list_remote_pages(original.observation_id, side="target") == (target_page,)
    request = case.request(final)
    result, = [message for message in request["messages"] if message["role"] == "tool"]
    assert result["tool_call_id"] == provider_call and result["name"] == tool_name
    assert json.loads(result["content"]) == recovered_page.result
    assert "冻结后不应混入的目标结果" not in result["content"]
    canonical, = _tool_results(database, scoped_call)
    assert canonical[1] == "completed"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        assert connection.execute(
            "SELECT execution_id FROM executions WHERE job_id=?", (dispatch["job_id"],),
        ).fetchall() == [(canonical[2],)]
    history = await case.client.post(f"/api/v1/sessions/{source}/history", json={})
    assert history.status_code == 200, history.text
    assert history.json()["data"]["thread_id"] == source_thread
    case.record("observation-hard-restart-" + operation, {
        "source_call_id": source_call, "source_before": asdict(original),
        "source_page_before": asdict(source_page), "source_page_after": asdict(recovered_page),
        "target_frozen": asdict(frozen), "target_page": asdict(target_page),
        "job": recovered_job, "canonical_result": canonical, "history": history.json(),
    })


@pytest.mark.asyncio
@pytest.mark.parametrize("communication_case", [{"barrier": True}], indirect=True)
@pytest.mark.parametrize("timeout_seconds,change_origin", [(60, False), (300, True)])
async def test_hard_restart_preserves_unexpired_wait_budget_and_baseline(
    communication_case: CommunicationCase, monkeypatch: pytest.MonkeyPatch,
    timeout_seconds: int, change_origin: bool,
) -> None:
    """预算剩余十秒时重启，原等待不能重新获得60/300秒，也不能纳入新Job。"""
    from app.services.business.communication.wait import MAX_WAIT_TIMEOUT_SECONDS

    case = communication_case
    source = await create_session(case.client, "等待预算恢复来源")
    target = await create_session(case.client, "等待预算恢复目标")
    await case.plan(target, [
        {"content": "冻结后新增任务完成"}, {"content": "原通信任务完成"},
    ])
    arguments = {"target_session_id": target}
    if timeout_seconds == 300:
        arguments["timeout_seconds"] = timeout_seconds
    _send, first, final = await case.plan(source, [
        _tool("send_message_to_session", {
            "target_session_id": target, "content": "保留待绑定通信", "kind": "result",
        }, "wait-budget-send"),
        _tool("wait_for_session", arguments, "wait-budget-source"),
        {"content": "等待按原预算结束"},
    ])
    source_thread = case.planned_calls[first]["thread_id"]
    source_call = software_tool_operation_id("wait", source, source_thread, first, "wait-budget-source")
    source_phase = "observation.target.baseline_frozen"
    case.replay.control.arm(source_call, source_phase)
    deadlines: list[dict[str, object]] = []
    dispatch_control = case.replay.control._dispatch

    def observe_timer(
        request: dict[str, object], *, disconnected: Callable[[], bool] | None = None,
    ) -> object:
        # 只观察既有外置timer的实际准入，不改变时钟、响应或业务存储。
        if request["action"] == "deadline":
            deadlines.append({key: request[key] for key in ("pid", "origin", "deadline_ns")})
        return dispatch_control(request, disconnected=disconnected)

    monkeypatch.setattr(case.replay.control, "_dispatch", observe_timer)
    communication_id = None
    restart = None
    target_phase = "communication.inbox.claimed"
    try:
        source_dispatch = await _message(case, source, "等待当前已有任务")
        await asyncio.to_thread(case.replay.control.acknowledged, source_call, source_phase, 60)
        target_node = await case.session_node(target)
        source_node = await case.session_node(source)
        with closing(SessionControlStore(target_node / CONTROL_DATABASE_NAME)) as store:
            frozen, = store.list_remote_observations()
            assert frozen.side == "target" and frozen.snapshot is not None
            page, = store.list_remote_pages(frozen.observation_id, side="target")
            assert page.result is None
            original_deadline = page.request["query"]["deadline"]
            assert original_deadline["timeout_seconds"] == timeout_seconds
            inbox, = store.list_target_accepted_communication_inboxes()
            assert inbox.job_id is inbox.execution_id is None
            communication_id = inbox.communication_id
            assert frozen.snapshot["identities"] == [{"kind": "communication", "id": communication_id}]
        # 初次在既有fixture的binder屏障暂停；重启由原claim phase挡住同一持久inbox。
        case.replay.control.arm(communication_id, target_phase)
        # 只用真实消息入口排队；这笔新工作不能进入原wait的冻结集合。
        future = await _message(case, target, "冻结后排队的新目标任务")
        await _completed(case.client, future["job_id"])
        assert future["job_id"] not in json.dumps(frozen.snapshot)
        case.replay.control.advance_wait(timeout_seconds - 10, change_origin=change_origin)
        # startup会先恢复inbox；并行等待启动，让pytest可在它准入前推进原wait预算。
        restart = asyncio.create_task(case.hard_restart())
        await asyncio.to_thread(case.replay.control.acknowledged, communication_id, target_phase, 60)
        recovery_pid = next(event["pid"] for event in reversed(case.replay.control.events)
                            if event["operation_id"] == communication_id and event["phase"] == target_phase)
        assert recovery_pid != case.backend.process.pid
        resumed_clock = cast(dict[str, object], dispatch_control({"action": "clock"}))
        wait_limit_ns = cast(int, resumed_clock["wait_ns"]) + MAX_WAIT_TIMEOUT_SECONDS * 1_000_000_000
        case.replay.control.release(source_call, source_phase)
        async with asyncio.timeout(30):
            # 同端口还有一天后的观察记录GC；按最长合法wait预算筛选，重置为60/300秒仍会被捕获。
            while not (resumed_timers := [
                row for row in deadlines
                if row["pid"] == recovery_pid and row["deadline_ns"] <= wait_limit_ns
            ]):
                await asyncio.sleep(0.01)
        expected_ns = 10_000_000_000 if change_origin else timeout_seconds * 1_000_000_000
        assert all(row["deadline_ns"] == expected_ns for row in resumed_timers)
        assert all((row["origin"] != original_deadline["monotonic_origin_id"]) == change_origin for row in resumed_timers)
        case.replay.control.advance_wait(9)
        with closing(SessionControlStore(source_node / CONTROL_DATABASE_NAME)) as store:
            source_before, = store.list_remote_observations()
            assert source_before.request["envelope"]["query"]["deadline"] == original_deadline
            pending, = store.list_remote_pages(frozen.observation_id, side="source")
            assert pending.result is None
        assert not (case.replay.artifacts / (final + ".json")).exists()
        case.replay.control.advance_wait(1)
        async with asyncio.timeout(10):
            while True:
                with closing(SessionControlStore(target_node / CONTROL_DATABASE_NAME)) as store:
                    target_page, = store.list_remote_pages(frozen.observation_id, side="target")
                    waiting_inbox = store.get_communication_inbox(communication_id)
                if target_page.result is not None:
                    break
                await asyncio.sleep(0.01)
        assert target_page.result["status"] == "timed_out"
        assert waiting_inbox.job_id is waiting_inbox.execution_id is None
        case.replay.control.release(communication_id, target_phase)
        await restart
        assert case.backend.process.pid == recovery_pid
        recovered_job = await _completed(case.client, source_dispatch["job_id"])
        with closing(SessionControlStore(source_node / CONTROL_DATABASE_NAME)) as store:
            completed, = store.list_remote_pages(frozen.observation_id, side="source")
            result = completed.result
            assert result["status"] == "timed_out"
            assert result["baseline_revision"] == frozen.snapshot["baseline_revision"]
            assert [row["selector_id"] for row in result["observed"]] == [communication_id]
            assert result["observed"][0]["state"] == "pending"
            assert completed.request["envelope"]["query"]["deadline"] == original_deadline
        with closing(SessionControlStore(target_node / CONTROL_DATABASE_NAME)) as store:
            after = store.get_remote_observation(frozen.observation_id, side="target")
            assert after.snapshot == frozen.snapshot and after.result == result
            target_page, = store.list_remote_pages(frozen.observation_id, side="target")
            assert target_page.request == page.request and target_page.result == result
        tool_result, = [message for message in case.request(final)["messages"] if message["role"] == "tool" and message["name"] == "wait_for_session"]
        assert tool_result["tool_call_id"] == "wait-budget-source"
        assert json.loads(tool_result["content"]) == result
        canonical, = _tool_results(case.main_thread_node(source) / "rollout/index.sqlite", first + ":tool-call:wait-budget-source")
        assert canonical[1] == "completed"
        case.record("wait-unexpired-restart-" + str(timeout_seconds), {
            "deadline": original_deadline, "source_call_id": source_call,
            "timers": resumed_timers, "baseline": asdict(frozen), "result": result,
            "job": recovered_job, "canonical_result": canonical, "excluded_job_id": future["job_id"],
            "inbox_pending": asdict(waiting_inbox),
        })
    finally:
        case.record("wait-restart-timer-requests-" + str(timeout_seconds), deadlines)
        case.replay.control.release(source_call, source_phase)
        if communication_id is not None:
            case.replay.control.release(communication_id, target_phase)
        if restart is not None:
            await restart
    async with asyncio.timeout(60):
        while True:
            with closing(SessionControlStore(target_node / CONTROL_DATABASE_NAME)) as store:
                bound = store.get_communication_inbox(communication_id)
            if bound.state == "execution_bound":
                break
            await asyncio.sleep(0.05)
    await _completed(case.client, bound.job_id)
