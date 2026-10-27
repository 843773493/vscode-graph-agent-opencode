"""MCP 等待的观察／投递边界与撤权；下游端口由 fixture 明确替换。"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.abstractions.mcp_connection_wait import McpWaitCheckpoint, McpWaitPayload
from app.agents.policy.tool_capability import ToolPolicyResolver
from app.core.background_task_contracts import (
    BackgroundTaskDescriptor,
    BackgroundTaskHandle,
)
from app.core.trace_middleware import get_current_gateway_id, get_current_request_id
from app.schemas.internal_v2.mcp import McpConnectionOperationDTO
from app.services.orchestration.mcp_wait.handler import McpConnectionWaitHandler


@pytest.fixture
def wait_case():
    operation = McpConnectionOperationDTO(
        operation_id="op", connection_id="connection", server_id="secure", action="connect",
        status="succeeded", stage="complete", created_at=time.time()-5, deadline=time.time()+60, updated_at=time.time(),
    )
    payload = McpWaitPayload(
        operation_id="op", connection_id="connection", deadline=operation.deadline,
        continuation="检查当前定义后继续", model_call_id="model", tool_call_id="call",
        origin_job_id="job", origin_execution_id="execution", gateway_id="gateway_0123456789abcdef0123456789abcdef", request_id="original-request",
    )
    descriptor = BackgroundTaskDescriptor(kind="mcp_connection_wait", version=1, payload=payload.model_dump(mode="json"))
    handle = BackgroundTaskHandle(
        task_id="bgt_wait", session_id="session", thread_id="thread", agent_id="default",
        descriptor=descriptor, checkpoint=McpWaitCheckpoint().model_dump(mode="json"),
        status="running", created_at=datetime.now(UTC), lease_id="lease",
    )
    connections = SimpleNamespace(
        wait_terminal=AsyncMock(return_value=operation), status=AsyncMock(return_value=operation),
        list_connections=Mock(return_value=[{"status": "ready"}]),
    )
    saver = SimpleNamespace(
        committed_tool_result_id=Mock(return_value="result"),
        get_canonical_item=Mock(return_value=SimpleNamespace(payload={
            "tool_outcome": "success", "content": json.dumps({"wait": {"task_id": handle.task_id}}),
        })),
        runtime_execution_outcome=Mock(return_value="completed"),
    )
    subscription = SimpleNamespace(get=AsyncMock())
    events = SimpleNamespace(subscribe=AsyncMock(return_value=subscription), unsubscribe=AsyncMock())
    config = SimpleNamespace(
        resolve_agent_tool_policy=Mock(return_value=SimpleNamespace(enabled_names={"connections.manage"})),
        get_tool_policy_resolver=Mock(return_value=ToolPolicyResolver(policy_defaults={}, policy_rules={}, restrictions={})),
    )
    selection = SimpleNamespace(snapshot=Mock(return_value=SimpleNamespace(execution_overrides={})))
    sessions = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(current_agent_id="default")))
    dispatcher = SimpleNamespace(submitted=Mock(return_value=None), submit=AsyncMock(), dispatch=AsyncMock())
    handler = McpConnectionWaitHandler(
        connections=connections, saver=saver, events=events, config=config, selection=selection, sessions=sessions,
    )
    handler.bind_dispatcher(dispatcher)
    return SimpleNamespace(**locals())


async def test_completion_checkpoint_and_mailbox_precede_dispatch(wait_case):
    case = wait_case
    first = await case.handler.step(case.handle)
    assert not first.completed and first.checkpoint["phase"] == "deliver"
    assert len(first.messages) == 1 and first.messages[0].message_id == "bgt_wait:completion"
    assert (first.messages[0].session_id, first.messages[0].thread_id) == ("session", "thread")
    case.dispatcher.submit.assert_not_called()
    case.handle.checkpoint = first.checkpoint

    async def submitted(*args, **kwargs):
        assert get_current_gateway_id() == "gateway_0123456789abcdef0123456789abcdef"
        assert get_current_request_id() == "original-request"
        assert kwargs["delivery_policy"] == "after_turn" and kwargs["idempotency_key"] == "bgt_wait"
        message = args[2]
        assert message.metadata["structured_prompt_kind"] == "mcp_connection_completed"
        assert "结果未知" in message.content
    case.dispatcher.submit.side_effect = submitted
    second = await case.handler.step(case.handle)
    assert second.completed and second.result["delivery"] == "submitted"
    assert get_current_gateway_id() != "gateway_0123456789abcdef0123456789abcdef" and get_current_request_id() != "original-request"
    case.connections.wait_terminal.assert_awaited_once()
    case.events.unsubscribe.assert_awaited_once()


@pytest.mark.parametrize("outcome", ["failure", "cancelled", "unknown"])
async def test_failed_original_result_never_registers_completion(wait_case, outcome):
    case = wait_case
    case.saver.get_canonical_item.return_value.payload["tool_outcome"] = outcome
    step = await case.handler.step(case.handle)
    assert step.completed and step.result["delivery"] == "origin_result_uncommitted"
    assert not step.messages
    case.connections.wait_terminal.assert_not_called()
    case.dispatcher.submit.assert_not_called()


async def test_subscribe_then_reread_original_commit_closes_lost_wakeup(wait_case):
    case = wait_case
    case.saver.committed_tool_result_id.return_value = None
    case.saver.runtime_execution_outcome.return_value = "unknown"

    async def subscribe(*args, **kwargs):
        case.saver.committed_tool_result_id.return_value = "result"
        return case.subscription
    case.events.subscribe.side_effect = subscribe
    step = await case.handler.step(case.handle)
    assert step.checkpoint["phase"] == "deliver"
    case.subscription.get.assert_not_called()


@pytest.mark.parametrize("reason", ["deadline", "failure", "disconnected", "cancelled_origin", "agent", "hard_permission", "selection"])
async def test_invalid_continuation_cannot_create_new_execution(wait_case, reason):
    case = wait_case
    first = await case.handler.step(case.handle)
    case.handle.checkpoint = first.checkpoint
    expected = {
        "deadline": "deadline_exceeded", "failure": "operation_failed", "disconnected": "connection_not_ready",
        "cancelled_origin": "origin_execution_invalid", "agent": "agent_changed",
        "hard_permission": "permission_revoked", "selection": "permission_revoked",
    }[reason]
    if reason == "deadline":
        case.handle.descriptor = case.descriptor.model_copy(update={"payload": {**case.descriptor.payload, "deadline": time.time()-1}})
    elif reason == "failure":
        case.connections.status.return_value = case.operation.model_copy(update={"status": "failed"})
    elif reason == "disconnected":
        case.connections.list_connections.return_value = [{"status": "failed"}]
    elif reason == "cancelled_origin":
        case.saver.runtime_execution_outcome.return_value = "cancelled"
    elif reason == "agent":
        case.sessions.get.return_value.current_agent_id = "other"
    elif reason == "hard_permission":
        case.config.get_tool_policy_resolver.return_value = ToolPolicyResolver(
            policy_defaults={}, policy_rules={}, restrictions={"execution_disabled": ["connections.manage"]},
        )
    else:
        case.selection.snapshot.return_value.execution_overrides = {"connections.manage": False}
    step = await case.handler.step(case.handle)
    assert step.completed and step.result["delivery"] == expected
    case.dispatcher.submit.assert_not_called()


async def test_accepted_dispatch_replays_existing_identity_after_crash(wait_case):
    case = wait_case
    first = await case.handler.step(case.handle)
    case.handle.checkpoint = first.checkpoint
    case.dispatcher.submitted.return_value = "original-durable-wakeup"
    # 旧投递已提交才过期；恢复只结算原准入，不创建新执行或重新连接。
    case.handle.descriptor = case.descriptor.model_copy(update={"payload": {**case.descriptor.payload, "deadline": time.time()-1}})
    step = await case.handler.step(case.handle)
    assert step.completed and step.result["delivery"] == "submitted"
    case.dispatcher.dispatch.assert_awaited_once_with("original-durable-wakeup")
    case.dispatcher.submit.assert_not_called()
    case.connections.status.assert_not_called()


async def test_cancel_observer_does_not_cancel_shared_operation(wait_case):
    case = wait_case
    entered = asyncio.Event()

    async def wait(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    case.connections.wait_terminal.side_effect = wait
    task = asyncio.create_task(case.handler.step(case.handle))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    case.connections.status.assert_not_called()
    case.dispatcher.submit.assert_not_called()
