"""真实消息与中断入口验证控制文件失败前后的执行边界。"""

from __future__ import annotations

import asyncio
import errno
import json
from pathlib import Path

import httpx
import pytest

from app import main
from app.core.path_utils import get_session_path_resolver
from app.services.infrastructure.message_stream_store import MessageStreamStore
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RuntimeSourceCase,
    _job,
    _send,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414 - 复用真实应用与唯一 model_stream 装具
)
from tests.support.api_helpers import create_session
from tests.support.sealed_request_audit import audit_sealed_requests


@pytest.mark.parametrize("failed_status", ["accepted", "consumed"])
async def test_interrupt_persistence_failure_preserves_original_execution_and_next_turn(
    source_case: RuntimeSourceCase,
    monkeypatch: pytest.MonkeyPatch,
    failed_status: str,
) -> None:
    case = source_case
    session = await create_session(case.client, f"中断持久化-{failed_status}")
    call, = await case.plan(session, [{"content": "取消后不应采纳的回复"}])
    case.control.arm(call, "model.response_ready")
    streams = case.container.message_stream_store
    # 注册由真实执行入口完成，不提前构造 scope、控制命令或执行状态。
    failures: list[OSError] = []
    replace = Path.replace

    try:
        accepted = await _send(case, session, "等待我主动取消这一轮。")
        thread = accepted["thread_id"]
        turn = accepted["turn_id"]
        job_id = accepted["job_id"]
        assert isinstance(thread, str) and isinstance(turn, str) and isinstance(job_id, str)
        await asyncio.to_thread(case.control.acknowledged, call, "model.response_ready", 20)
        writer = await streams.open_existing(session_id=session, thread_id=thread, turn_id=turn)
        registry = case.container.agent_execution_service.execution_scope_registry
        inbox = registry.get_inbox(writer.turn_stream_id)
        assert inbox is not None and inbox._state_path is not None
        state_path = inbox._state_path
        original = state_path.read_bytes() if state_path.exists() else None

        def fail_selected_publication(source: Path, target: str | Path) -> Path:
            if source == state_path.with_suffix(".tmp"):
                candidate = json.loads(source.read_text(encoding="utf-8"))
                if any(command["status"] == failed_status for command in candidate["commands"]):
                    error = PermissionError(errno.EACCES, "拒绝控制文件原子发布", str(state_path))
                    failures.append(error)
                    raise error
            return replace(source, target)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app, raise_app_exceptions=False),
            base_url=case.client.base_url, headers=case.client.headers,
        ) as client:
            with monkeypatch.context() as patch:
                patch.setattr(Path, "replace", fail_selected_publication)
                failed = await client.post(f"/api/v1/sessions/{session}/interrupt", params={"thread_id": thread})
            assert failed.status_code == 500, failed.text
            assert failed.headers["X-Request-ID"] == failed.json()["request_id"]
            assert len(failures) == 1
            assert str(state_path) not in failed.text
            if failed_status == "accepted":
                assert inbox.snapshot() == []
                assert (state_path.read_bytes() if state_path.exists() else None) == original
                before_retry = await streams.list_events(session_id=session, turn_stream_id=writer.turn_stream_id)
                assert not any(event["type"].startswith("interrupt.") for event in before_retry)
                running = await client.get(f"/api/v1/jobs/{accepted['job_id']}")
                assert running.status_code == 200 and running.json()["data"]["status"] == "running"
                retried = await client.post(f"/api/v1/sessions/{session}/interrupt", params={"thread_id": thread})
                assert retried.status_code == 200, retried.text
                assert retried.headers["X-Request-ID"] == retried.json()["request_id"]
                interrupt_id = retried.json()["data"]["interrupt_request_id"]
                assert [command.status for command in inbox.snapshot()] == ["consumed"]
            else:
                command, = inbox.snapshot()
                assert command.status == "accepted"
                interrupt_id = command.command_id
    finally:
        case.control.release(call, "model.response_ready")

    job = await _job(case, job_id, expected="cancelled")
    async with asyncio.timeout(5):
        while (await streams.get_state(writer.turn_stream_id))["stream_status"] != "interrupted":
            await asyncio.sleep(0.01)
    events = await streams.list_events(session_id=session, turn_stream_id=writer.turn_stream_id)
    requested, = [event for event in events if event["type"] == "interrupt.requested"]
    assert requested["event_id"] == interrupt_id
    assert requested["payload"]["interrupt_request_id"] == interrupt_id
    assert requested["thread_id"] == thread and requested["turn_id"] == turn
    assert events[-1]["type"] == "stream.interrupted"
    assert not any(event["type"] in {"interrupt.rejected", "stream.completed"} for event in events)
    assert not any("取消后不应采纳的回复" in json.dumps(event, ensure_ascii=False) for event in events)
    assert [(command.command_id, command.control_seq) for command in inbox.snapshot()] == [(interrupt_id, 1)]
    # 公开接口每次创建新 ID；原执行已停止时，重复点击不创建事件或重选执行。
    late = await case.client.post(f"/api/v1/sessions/{session}/interrupt", params={"thread_id": thread})
    assert late.status_code == 404, late.text
    assert await streams.list_events(session_id=session, turn_stream_id=writer.turn_stream_id) == events
    followup, = await case.plan(session, [{"content": "中断故障后同一线程继续完成。"}])
    resumed = await _send(case, session, "继续下一轮。")
    assert resumed["thread_id"] == thread and resumed["turn_id"] != turn
    resumed_job = resumed["job_id"]
    assert isinstance(resumed_job, str)
    await _job(case, resumed_job)
    assert await streams.list_events(session_id=session, turn_stream_id=writer.turn_stream_id) == events
    messages = case.request(followup)["messages"]
    assert isinstance(messages, list)
    assert messages[-1]["content"] == "继续下一轮。"
    resolver = get_session_path_resolver(case.workspace / ".boxteam/sessions")
    cold = MessageStreamStore(path_resolver=resolver)
    await cold.open_existing(session_id=session, thread_id=thread, turn_id=turn)
    assert await cold.list_events(session_id=session, turn_stream_id=writer.turn_stream_id) == events
    root = resolver.resolve_thread_node(session, thread) / "rollout"
    captures = {model_call: json.loads((case.model.artifacts / (model_call + ".json")).read_text())
                for model_call in case.planned_calls}
    proof = audit_sealed_requests(
        root / "index.sqlite", (resolver.resolve_session_node(session) / "logs/llm_requests").glob("*.json"), captures,
    )
    audited = proof["requests"]
    assert isinstance(audited, list) and len(audited) == 2
    case.write_actual(f"interrupt-persistence-{failed_status}", {
        "accepted": accepted, "cancelled_job": job, "interrupt_request_id": interrupt_id,
        "failed_http": failed.json(), "late_http": late.json(), "events": events, "resumed": resumed,
        "sealed_requests": proof,
    })
