"""真实消息/回退 API 在可控业务边界竞争，历史由 Agent/model_stream 生成。"""

from __future__ import annotations

import asyncio
import json

import pytest

from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RuntimeSourceCase,
    _job,
    _send,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414 - 复用生产装配与 model_stream
)
from tests.support.api_helpers import create_session


async def _replay(case: RuntimeSourceCase, session: str, message: str):
    return await case.client.post(f"/api/v1/sessions/{session}/messages/{message}/replay", json={
        "action": "regenerate", "acknowledge_context_only": True,
    })


async def test_message_waits_until_rewind_and_replacement_are_admitted(
    source_case: RuntimeSourceCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = source_case
    session = await create_session(case.client, "回退与新输入的原子准入")
    await case.plan(session, [{"content": "将被重生成的回答"}])
    initial = await _send(case, session, "原始输入")
    await _job(case, str(initial["job_id"]))
    replay_call, successor_call = await case.plan(session, [
        {"content": "重生成后的回答"}, {"content": "后继输入的回答"},
    ])
    idle_checked = asyncio.Event()
    release = asyncio.Event()
    message_prepared = asyncio.Event()
    replay_service = case.container.session_turn_replay_service
    original_check = replay_service._assert_thread_idle
    original_create = case.container.message_service.create

    async def check(owner):
        await original_check(owner)
        idle_checked.set()
        await release.wait()

    async def create(*args, **kwargs):
        message = await original_create(*args, **kwargs)
        if message.content == "并发后继输入":
            message_prepared.set()
        return message

    monkeypatch.setattr(replay_service, "_assert_thread_idle", check)
    monkeypatch.setattr(case.container.message_service, "create", create)
    replay = asyncio.create_task(_replay(case, session, str(initial["message_id"])))
    send = None
    try:
        await asyncio.wait_for(idle_checked.wait(), 10)
        send = asyncio.create_task(_send(case, session, "并发后继输入"))
        await asyncio.wait_for(message_prepared.wait(), 10)
        done, _ = await asyncio.wait({send}, timeout=0.3)
        assert not done, "回退持有准入时，新输入不得提交 acceptance 或登记 Job"
        assert case.call_count(session, str(initial["thread_id"])) == 1
    finally:
        release.set()
        results = await asyncio.gather(replay, *([send] if send is not None else []), return_exceptions=True)
    replay_response = results[0]
    assert not isinstance(replay_response, BaseException), replay_response
    assert replay_response.status_code == 200, replay_response.text
    accepted = replay_response.json()["data"]
    successor = results[1]
    assert isinstance(successor, dict), successor
    await _job(case, accepted["job_id"])
    await _job(case, str(successor["job_id"]))
    replay_wire = json.dumps(case.request(replay_call)["messages"], ensure_ascii=False)
    successor_wire = json.dumps(case.request(successor_call)["messages"], ensure_ascii=False)
    assert "并发后继输入" not in replay_wire
    assert replay_wire.count("原始输入") == 1
    assert all(text in successor_wire for text in ("原始输入", "重生成后的回答", "并发后继输入"))
    assert "将被重生成的回答" not in successor_wire
    case.write_actual("replay-first", {"initial": initial, "replayed": accepted, "successor": successor})


async def test_replay_waits_for_acceptance_to_job_registration_then_rejects_busy_thread(
    source_case: RuntimeSourceCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = source_case
    session = await create_session(case.client, "消息 acceptance 与 Job 登记的临界区")
    await case.plan(session, [{"content": "保留的原回答"}])
    initial = await _send(case, session, "保留的原输入")
    await _job(case, str(initial["job_id"]))
    call, = await case.plan(session, [{"content": "并发输入完成"}])
    case.control.arm(call, "model.response_ready")
    accepted = asyncio.Event()
    release = asyncio.Event()
    replay_arrived = asyncio.Event()
    idle_checked = asyncio.Event()
    jobs = case.container.job_service
    replay_service = case.container.session_turn_replay_service
    original_start = jobs.start_job
    original_resolve = case.container.session_service.resolve_main_thread
    original_check = replay_service._assert_thread_idle
    replay = None

    async def start(session_id, message, **kwargs):
        if message == "先接受的并发输入":
            accepted.set()
            await release.wait()
        return await original_start(session_id, message, **kwargs)

    async def resolve(session_id):
        result = await original_resolve(session_id)
        if asyncio.current_task() is replay:
            replay_arrived.set()
        return result

    async def check(owner):
        idle_checked.set()
        await original_check(owner)

    monkeypatch.setattr(jobs, "start_job", start)
    monkeypatch.setattr(case.container.session_service, "resolve_main_thread", resolve)
    monkeypatch.setattr(replay_service, "_assert_thread_idle", check)
    send = asyncio.create_task(_send(case, session, "先接受的并发输入"))
    try:
        await asyncio.wait_for(accepted.wait(), 10)
        replay = asyncio.create_task(_replay(case, session, str(initial["message_id"])))
        await asyncio.wait_for(replay_arrived.wait(), 10)
        done, _ = await asyncio.wait({replay}, timeout=0.3)
        assert not done and not idle_checked.is_set(), "acceptance 未完成 Job 登记时，回退不能检查或修改上下文"
        release.set()
        response = await asyncio.wait_for(replay, 10)
        assert response.status_code == 409, response.text
        assert "主线程仍有运行中任务" in response.text
        successor = await send
        assert str(successor["job_id"]) in response.text
        await asyncio.to_thread(case.control.acknowledged, call, "model.response_ready")
    finally:
        release.set()
        case.control.release(call, "model.response_ready")
        await asyncio.gather(send, *([replay] if replay is not None else []), return_exceptions=True)
    await _job(case, str(successor["job_id"]))
    wire = json.dumps(case.request(call)["messages"], ensure_ascii=False)
    assert all(text in wire for text in ("保留的原输入", "保留的原回答", "先接受的并发输入"))
    assert case.call_count(session, str(initial["thread_id"])) == 2
    case.write_actual("acceptance-first", {"initial": initial, "successor": successor, "rejection": response.json()})


@pytest.mark.parametrize("source_case", [{"terminal": True, "browser": True, "managed_gateway": True}], indirect=True)
async def test_main_manual_compaction_allows_child_input_while_main_input_waits(
    source_case: RuntimeSourceCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = source_case
    session = await create_session(case.client, "main 摘要期间 child 仍可发送消息")
    await case.plan(session, [{"content": "双方共享的原回答"}])
    initial = await _send(case, session, "双方共享的原输入")
    await _job(case, str(initial["job_id"]))
    main = str(initial["thread_id"])
    response = await case.client.get(f"/api/v1/sessions/{session}/fork-source-point")
    assert response.status_code == 200, response.text
    point = response.json()["data"]
    response = await case.client.post(f"/api/v1/sessions/{session}/fork-context", json={
        "idempotency_key": "manual-compaction-child", "source_uri": point["source_uri"],
        "source_view_id": point["source_view_id"], "source_view_revision": point["source_view_revision"],
        "anchor_item_id": point["anchor_item_id"], "target_container_uri": point["child_container_uri"],
        "mode": "view_prefix_fork", "creation_kind": "new_child_thread",
    })
    assert response.status_code == 200, response.text
    child = response.json()["data"]["thread_id"]
    child_call, = await case.plan(session, [{"content": "child 在 main 摘要期间完成"}], thread_id=child)
    summary_call, main_call = await case.plan(session, [
        {"content": "共享问答"}, {"content": "main 等摘要退出后完成"},
    ], thread_id=main)
    loading = asyncio.Event()
    release = asyncio.Event()
    main_prepared = asyncio.Event()
    store = case.container.context_compaction_service._checkpoint_store
    original_load = store.load
    original_create = case.container.message_service.create

    async def load(owner):
        checkpoint = await original_load(owner)
        loading.set()
        await release.wait()
        return checkpoint

    async def create(*args, **kwargs):
        message = await original_create(*args, **kwargs)
        if message.content == "main 等待摘要的输入":
            main_prepared.set()
        return message

    monkeypatch.setattr(store, "load", load)
    monkeypatch.setattr(case.container.message_service, "create", create)
    compact = asyncio.create_task(case.client.post(f"/api/v1/sessions/{session}/compact"))
    send = None
    try:
        await asyncio.wait_for(loading.wait(), 10)
        child_response = await asyncio.wait_for(case.client.post(
            f"/api/v1/sessions/{session}/threads/{child}/messages", json={
                "message": {"content": "child 无需等待 main 摘要"},
                "run": {"mode": "single_agent", "agent_id": "default"},
            },
        ), 10)
        assert child_response.status_code == 200, child_response.text
        child_accepted = child_response.json()["data"]
        await _job(case, child_accepted["job_id"])
        send = asyncio.create_task(_send(case, session, "main 等待摘要的输入"))
        await asyncio.wait_for(main_prepared.wait(), 10)
        done, _ = await asyncio.wait({send}, timeout=0.3)
        assert not done and not compact.done()
        assert case.call_count(session, main) == 1
    finally:
        release.set()
        results = await asyncio.gather(compact, *([send] if send is not None else []), return_exceptions=True)
    compact_response = results[0]
    assert not isinstance(compact_response, BaseException), compact_response
    assert compact_response.status_code == 200, compact_response.text
    assert compact_response.json()["data"]["status"] == "scheduled"
    successor = results[1]
    assert isinstance(successor, dict), successor
    await _job(case, str(successor["job_id"]))
    child_wire = json.dumps(case.request(child_call)["messages"], ensure_ascii=False)
    main_wire = json.dumps(case.request(main_call)["messages"], ensure_ascii=False)
    assert "child 无需等待 main 摘要" in child_wire and "main 等待摘要的输入" not in child_wire
    assert "main 等待摘要的输入" in main_wire and "child 无需等待 main 摘要" not in main_wire
    summary_wire = json.dumps(case.request(summary_call)["messages"], ensure_ascii=False)
    assert "Return plain text only" in summary_wire and "child 无需等待 main 摘要" not in summary_wire
    assert case.call_count(session, main) == 3 and case.call_count(session, child) == 1
    case.write_actual("manual-compaction", {
        "initial": initial, "child": child_accepted, "main": successor,
        "compaction": compact_response.json(),
    })
