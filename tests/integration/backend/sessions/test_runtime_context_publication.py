"""仅发布上下文经真实 Session、执行及模型准备入口验证，不伪造检查点。"""

from __future__ import annotations

import asyncio
import hashlib
import traceback
from collections.abc import Iterator
from contextlib import closing
from dataclasses import asdict
from unittest.mock import patch

import pytest

from app.abstractions.internal_message import PreparedInternalMessage
from app.core.session_control_primitives import CONTROL_DATABASE_NAME
from app.core.session_control_store import SessionControlStore
from app.prompting import internal_message_factory
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.orchestration.runtime_source_dispatcher import RuntimeSourceDispatcher
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RuntimeSourceCase,
    _job,
    _send,
    _tool_reply,
    _wire_text,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414 - fixture 按当前测试文件隔离
)
from tests.support.api_helpers import create_session
from tests.support.fork_deadline import ForkDeadlineRegistry


def _notice(marker: str) -> PreparedInternalMessage:
    return internal_message_factory.build(
        kind="checkpoint_reminder", control=marker,
        metadata={"source": "persistent_task"},
    )


@pytest.mark.asyncio
async def test_idle_publication_is_durable_without_execution_and_consumed_by_real_user(
    source_case: RuntimeSourceCase,
) -> None:
    case = source_case
    session_id = await create_session(case.client, "仅发布服务状态")
    thread_id = await case.container.session_service.resolve_main_thread(session_id)
    orchestrator = case.container.session_orchestrator
    message = _notice("context-only-idle-private：服务已经就绪。")
    arguments = {"thread_id": thread_id, "source_id": "task:service-ready", "idempotency_key": "idle-ready"}

    receipt = await orchestrator.publish_runtime_context(session_id, message, **arguments)
    assert await orchestrator.publish_runtime_context(session_id, message, **arguments) == receipt
    assert (receipt.session_id, receipt.thread_id, receipt.source_id) == (session_id, thread_id, arguments["source_id"])
    for table in ("executions", "turn_records", "model_calls", "runtime_execution_wakeups", "context_source_control_states"):
        assert case.sql(session_id, thread_id, f"SELECT COUNT(*) FROM {table}") == [(0,)]
    assert await case.container.job_service.list(session_id=session_id) == []
    assert case.call_count(session_id, thread_id) == 0
    assert case.sql(session_id, thread_id,
        "SELECT item_id,turn_id,turn_scope,execution_id FROM item_catalog") == [(receipt.item_id, None, "ambient", None)]
    assert case.sql(session_id, thread_id,
        "SELECT item_id,source_kind,context_selected FROM context_view_items") == [(receipt.item_id, "canonical", 1)]
    with RolloutCheckpointSaver(case.container.checkpointer._storage.storage.sessions_dir) as cold:
        (stored,) = cold._storage.storage.read_items(session_id, thread_id, item_ids=(receipt.item_id,))
        assert stored.payload == message.content
        assert stored.source_manifest is not None and stored.source_manifest.source_revision == receipt.revision
    with pytest.raises(RuntimeError, match="preimage 冲突"):
        await orchestrator.publish_runtime_context(session_id, _notice("同一事件不允许更换正文。"), **arguments)
    with pytest.raises(RuntimeError, match="preimage 冲突"):
        await orchestrator.submit_runtime_event(
            session_id, message, thread_id=thread_id,
            source_id=arguments["source_id"], idempotency_key=arguments["idempotency_key"],
        )

    first_call, second_call = await case.plan(session_id, [{"content": "首次消费状态。"}, {"content": "继续沿用状态。"}])
    first = await _send(case, session_id, "用户首次请求，请读取已发布状态。")
    assert isinstance(first["job_id"], str)
    await _job(case, first["job_id"])
    assert _wire_text(case.request(first_call)["messages"]).count("context-only-idle-private") == 1
    assembly_id, = case.sql(session_id, thread_id, "SELECT assembly_id FROM context_assemblies")[0]
    frozen = case.container.checkpointer.get_context_assembly(session_id, thread_id, assembly_id=assembly_id).to_dict()
    assert await orchestrator.publish_runtime_context(session_id, message, **arguments) == receipt
    second = await _send(case, session_id, "用户再次请求。")
    assert isinstance(second["job_id"], str)
    await _job(case, second["job_id"])
    assert _wire_text(case.request(second_call)["messages"]).count("context-only-idle-private") == 1
    assert case.container.checkpointer.get_context_assembly(session_id, thread_id, assembly_id=assembly_id).to_dict() == frozen
    assert case.sql(session_id, thread_id, "SELECT COUNT(*) FROM turn_records") == [(2,)]
    assert case.sql(session_id, thread_id, "SELECT COUNT(*) FROM executions WHERE execution_origin='internal'") == [(0,)]
    assert case.sql(session_id, thread_id, "SELECT COUNT(*) FROM runtime_execution_wakeups") == [(0,)]
    assert case.sql(session_id, thread_id, "SELECT COUNT(*) FROM item_catalog WHERE item_id=?", (receipt.item_id,)) == [(1,)]
    assert len(case.requests()) == 2
    case.write_actual("idle-context-only", {"receipt": asdict(receipt), "first_assembly": frozen})


@pytest.mark.asyncio
@pytest.mark.parametrize("source_case", [{"managed_gateway": True}], indirect=True)
async def test_busy_publication_waits_for_next_model_preparation_without_new_job(
    source_case: RuntimeSourceCase,
) -> None:
    case = source_case
    session_id = await create_session(case.client, "运行中发布上下文")
    thread_id = await case.container.session_service.resolve_main_thread(session_id)
    first_call, second_call = await case.plan(session_id, [
        _tool_reply("read_file", {"path": "README.md"}, "call_context_only_read"),
        {"content": "读文件后消费新状态。"},
    ])
    case.control.arm(first_call, "model.response_ready")
    accepted = await _send(case, session_id, "读取 README，然后检查服务状态。")
    assert isinstance(accepted["job_id"], str)
    try:
        await asyncio.to_thread(case.control.acknowledged, first_call, "model.response_ready", 30)
        before = case.request(first_call)
        assert "context-only-busy-private" not in _wire_text(before)
        receipt = await case.container.session_orchestrator.publish_runtime_context(
            session_id, _notice("context-only-busy-private：服务刚刚上线。"),
            thread_id=thread_id, source_id="task:busy-service", idempotency_key="busy-ready",
        )
        assert case.request(first_call) == before
        assert case.sql(session_id, thread_id, "SELECT COUNT(*) FROM executions") == [(1,)]
        assert case.sql(session_id, thread_id, "SELECT COUNT(*) FROM runtime_execution_wakeups") == [(0,)]
        assert case.call_count(session_id, thread_id) == 1
    finally:
        case.control.release(first_call, "model.response_ready")
    await _job(case, accepted["job_id"])
    wire = case.request(second_call)
    assert _wire_text(wire["messages"]).count("context-only-busy-private") == 1
    messages = wire["messages"]
    assert isinstance(messages, list)
    tool_results = [message for message in messages if message["role"] == "tool"]
    assert len(tool_results) == 1 and tool_results[0]["tool_call_id"] == "call_context_only_read"
    read_result = _wire_text(tool_results[0]["content"])
    read_bytes = (case.workspace / "README.md").read_bytes()
    assert read_bytes.decode().splitlines()[0] in read_result
    assert "sha256:" + hashlib.sha256(read_bytes).hexdigest() in read_result
    assert case.sql(session_id, thread_id,
        "SELECT execution_origin,turn_id FROM executions") == [("user_turn", accepted["turn_id"])]
    assert case.sql(session_id, thread_id, "SELECT COUNT(*) FROM model_calls") == [(2,)]
    assert len(case.requests()) == 2
    case.write_actual("busy-context-only", {"receipt": asdict(receipt), "first_request": before, "second_request": wire})


@pytest.fixture
def fork_deadlines(source_case: RuntimeSourceCase) -> Iterator[ForkDeadlineRegistry]:
    registry = ForkDeadlineRegistry(source_case.artifacts_dir)
    try:
        yield registry
    finally:
        registry.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("source_committed", [False, True])
async def test_interrupted_publication_recovers_from_canonical_fact_without_replaying(
    source_case: RuntimeSourceCase, source_committed: bool,
) -> None:
    case = source_case
    session_id = await create_session(case.client, "仅发布的结算中断")
    thread_id = await case.container.session_service.resolve_main_thread(session_id)
    message = _notice("context-only-recovery-private：原始事件。")
    arguments = {"thread_id": thread_id, "source_id": "task:recovery", "idempotency_key": "recovery"}
    dispatcher = case.container.runtime_source_dispatcher
    resolver = case.container.session_service.path_resolver
    database = resolver.resolve_session_node(session_id) / CONTROL_DATABASE_NAME
    # 故障只截断真实提交边界；不手工填 item、checkpoint 或 lease。
    target = patch.object(SessionControlStore, "settle_lease", side_effect=RuntimeError("publication settlement fault")) if source_committed else patch.object(
        case.container.checkpointer, "consume_mutation_intent", side_effect=RuntimeError("publication source fault"),
    )
    with target, pytest.raises(RuntimeError, match="publication .* fault"):
        await case.container.session_orchestrator.publish_runtime_context(session_id, message, **arguments)
    with closing(SessionControlStore(database)) as control:
        (interrupted,) = control.list_non_terminal_leases()
        assert interrupted.state == "settling"
    # 全新 Saver 和 dispatcher 只使用磁盘事实，调用实际启动恢复入口。
    with RolloutCheckpointSaver(case.container.checkpointer._storage.storage.sessions_dir) as cold:
        recovered = RuntimeSourceDispatcher(saver=cold, session_service=case.container.session_service,
                                            job_service=case.container.job_service)
        await recovered.start()
        await recovered.start()
    with closing(SessionControlStore(database)) as control:
        lease = control.get_lease(interrupted.lease_id)
        assert lease.state == ("completed" if source_committed else "failed")
        assert lease.fencing_token > interrupted.fencing_token
        assert control.list_non_terminal_leases() == ()
    if source_committed:
        receipt = await case.container.session_orchestrator.publish_runtime_context(session_id, message, **arguments)
        assert case.sql(session_id, thread_id, "SELECT item_id FROM item_catalog") == [(receipt.item_id,)]
    else:
        with pytest.raises(RuntimeError, match="生命周期准入已失效"):
            await dispatcher.publish_context(session_id, thread_id, message,
                                             source_id=arguments["source_id"], idempotency_key=arguments["idempotency_key"])
    assert await case.container.job_service.list(session_id=session_id) == []
    call, = await case.plan(session_id, [{"content": "真实用户继续。"}])
    accepted = await _send(case, session_id, "读取恢复后的上下文。")
    assert isinstance(accepted["job_id"], str)
    await _job(case, accepted["job_id"])
    assert _wire_text(case.request(call)["messages"]).count("context-only-recovery-private") == int(source_committed)
    assert case.sql(session_id, thread_id, "SELECT COUNT(*) FROM runtime_execution_wakeups") == [(0,)]
    assert case.sql(session_id, thread_id, "SELECT COUNT(*) FROM executions WHERE execution_origin='internal'") == [(0,)]
    case.write_actual("publication-recovery", {"source_committed": source_committed,
                                              "interrupted": asdict(interrupted), "settled": asdict(lease)})


@pytest.mark.asyncio
@pytest.mark.parametrize("source_case", [{"managed_gateway": True, "terminal": True, "browser": True}], indirect=True)
async def test_context_only_fork_keeps_consumed_source_without_importing_later_observation(
    source_case: RuntimeSourceCase, fork_deadlines: ForkDeadlineRegistry,
) -> None:
    case = source_case
    session_id = await create_session(case.client, "fork 仅发布的事件")
    main_thread = await case.container.session_service.resolve_main_thread(session_id)
    orchestrator = case.container.session_orchestrator
    receipt = await orchestrator.publish_runtime_context(
        session_id, _notice("context-only-fork-included：已在历史请求消费。"),
        thread_id=main_thread, source_id="task:fork-source", idempotency_key="fork-included",
    )
    await case.plan(session_id, [{"content": "完成公开来源历史。"}])
    first = await _send(case, session_id, "生成真实可 fork 的历史。")
    assert isinstance(first["job_id"], str)
    await _job(case, first["job_id"])
    excluded = await orchestrator.publish_runtime_context(
        session_id, _notice("context-only-fork-excluded：晚于完成锚点。"),
        thread_id=main_thread, source_id="task:fork-source", idempotency_key="fork-excluded",
    )
    deadline = fork_deadlines.for_operation(session_id, "context-only-fork")
    try:
        response = await deadline.wait(case.client.get(f"/api/v1/sessions/{session_id}/fork-source-point", params={
            "thread_id": main_thread, "mode": "context_fork",
        }), "context-only source point")
        case.write_actual("fork-source-point", {"status": response.status_code, "body": response.text})
        assert response.status_code == 200, response.text
        point = response.json()["data"]
        response = await deadline.wait(case.client.post(f"/api/v1/sessions/{session_id}/fork-context", json={
            "idempotency_key": "context-only-fork", "source_uri": point["source_uri"],
            "source_view_id": point["source_view_id"], "source_view_revision": point["source_view_revision"],
            "anchor_item_id": point["anchor_item_id"], "mode": "context_fork",
            "creation_kind": "new_child_thread", "target_container_uri": point["child_container_uri"],
        }), "context-only public fork")
    except Exception:
        case.write_actual("fork-error", {"traceback": traceback.format_exc()})
        raise
    finally:
        # 请求已返回错误也结束观察，避免 teardown 看门狗掩盖原始失败；
        # 已超时的 deadline 保持 active，由进程外 watchdog 按原期限回收。
        deadline.complete()
    assert response.status_code == 200, response.text
    assert response.json()["data"]["session"]["session_id"] == session_id
    child = response.json()["data"]["thread_id"]
    assert child != main_thread
    assert case.sql(session_id, child, "SELECT COUNT(*) FROM item_catalog WHERE item_id=?", (excluded.item_id,)) == [(0,)]
    saver = case.container.checkpointer
    (source,) = saver._storage.storage.read_items(session_id, main_thread, item_ids=(receipt.item_id,))
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold:
        (copied,) = cold._storage.storage.read_items(session_id, child, item_ids=(receipt.item_id,))
        assert (copied.payload, copied.producer_ref, copied.source_manifest, copied.projection_identity) == (
            source.payload, source.producer_ref, source.source_manifest, source.projection_identity,
        )
    child_call, = await case.plan(session_id, [{"content": "child 独立消费历史来源。"}], thread_id=child)
    response = await case.client.post(f"/api/v1/sessions/{session_id}/threads/{child}/messages", json={
        "message": {"content": "继续 fork 历史。"}, "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert response.status_code == 200, response.text
    await _job(case, response.json()["data"]["job_id"])
    wire = _wire_text(case.request(child_call)["messages"])
    assert wire.count("context-only-fork-included") == 1
    assert "context-only-fork-excluded" not in wire
    assert case.sql(session_id, main_thread, "SELECT COUNT(*) FROM executions") == [(1,)]
    assert case.sql(session_id, child, "SELECT COUNT(*) FROM runtime_execution_wakeups") == [(0,)]
    case.write_actual("fork-context-only", {"included": asdict(receipt), "excluded": asdict(excluded), "child": child})
