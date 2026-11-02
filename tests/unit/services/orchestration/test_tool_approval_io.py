"""审批控制写入让出事件循环时，原等待取消不能改写写盘结果。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from langgraph.types import Interrupt, StateSnapshot

from app.core.turn_execution_scope import AgentControlCommand, AgentControlInbox
from app.schemas.internal_v2.tool_approval import ToolApprovalSubmitRequest
from app.services.orchestration.execution_step.approval import ToolApprovalService


class ControlledInbox(AgentControlInbox):
    def __init__(
        self, turn_stream_id: str, *, session_id: str | None = None, state_path: Path | None = None,
    ) -> None:
        super().__init__(turn_stream_id, session_id=session_id, state_path=state_path)
        self.entered, self.finish = asyncio.Event(), asyncio.Event()
        self.accept_entered, self.accept_finish = asyncio.Event(), asyncio.Event()
        self.block_accept = False
        self.failure: OSError | None = None

    async def accept(
        self, *, command_id: str, kind: str, idempotency_key: str,
        payload: dict[str, object] | None = None,
    ) -> AgentControlCommand:
        if self.block_accept:
            self.accept_entered.set()
            await self.accept_finish.wait()
        return await super().accept(
            command_id=command_id, kind=kind, idempotency_key=idempotency_key, payload=payload,
        )

    async def mark(self, command_id: str, status: str) -> AgentControlCommand:
        self.entered.set()
        await self.finish.wait()
        if self.failure is not None:
            raise self.failure
        return await super().mark(command_id, status)


@pytest.fixture
async def approval_inbox() -> AsyncIterator[ControlledInbox]:
    inbox = await ControlledInbox.open("stream", session_id="session")
    assert isinstance(inbox, ControlledInbox)
    try:
        yield inbox
    finally:
        inbox.finish.set()
        inbox.accept_finish.set()
        await inbox.close()


@pytest.fixture
def approval_snapshot() -> StateSnapshot:
    return StateSnapshot(
        values={"messages": [AIMessage(content="", tool_calls=[{
            "name": "ls", "args": {"path": "."}, "id": "call", "type": "tool_call",
        }])]},
        next=(), config={"configurable": {"checkpoint_id": "checkpoint"}}, metadata=None,
        created_at=None, parent_config=None, tasks=(), interrupts=(Interrupt({
            "action_requests": [{"name": "ls", "args": {"path": "."}, "description": "列表"}],
            "review_configs": [{"action_name": "ls", "allowed_decisions": ["approve"]}],
        }, id="approval"),),
    )


@pytest.fixture
def approval_request() -> ToolApprovalSubmitRequest:
    return ToolApprovalSubmitRequest.model_validate({
        "command_id": "command", "checkpoint_id": "checkpoint", "responses": [{
            "approval_id": "approval", "decisions": [{"tool_call_id": "call", "type": "approve"}],
        }],
    })


@pytest.mark.parametrize("cancel_wait", [False, True])
@pytest.mark.parametrize("fail_write", [False, True])
async def test_approval_settlement_preserves_original_result_when_waiter_finishes(
    approval_inbox: ControlledInbox, approval_snapshot: StateSnapshot,
    approval_request: ToolApprovalSubmitRequest,
    cancel_wait: bool, fail_write: bool,
) -> None:
    inbox, service = approval_inbox, ToolApprovalService()
    waiting = asyncio.create_task(service.wait(
        approval_snapshot, job_id="job", session_id="session", thread_id="thread", inbox=inbox,
    ))
    await asyncio.sleep(0)
    accepted = await service.submit("job", approval_request)
    assert accepted.status == "accepted"
    command = await inbox.next()
    process = asyncio.create_task(service.process(command))
    await asyncio.wait_for(inbox.entered.wait(), 1)
    if fail_write:
        inbox.failure = OSError("审批决定写盘失败")
    if cancel_wait:
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
    inbox.finish.set()
    try:
        if fail_write:
            with pytest.raises(OSError) as caught:
                await process
            assert caught.value is inbox.failure
            assert inbox.get(command.command_id) == command
            if not cancel_wait:
                with pytest.raises(OSError) as waiting_error:
                    await waiting
                assert waiting_error.value is inbox.failure
        else:
            assert await process == {"status": "consumed"}
            stored = inbox.get(command.command_id)
            assert stored is not None and stored.status == "consumed"
            if not cancel_wait:
                resume = await waiting
                assert resume.resume == {"approval": {"decisions": [{"type": "approve"}]}}
    finally:
        inbox.finish.set()
        waiting.cancel()
        await asyncio.gather(waiting, process, return_exceptions=True)


async def test_concurrent_approval_submission_rechecks_original_checkpoint_after_write_wait(
    approval_inbox: ControlledInbox, approval_snapshot: StateSnapshot,
    approval_request: ToolApprovalSubmitRequest,
) -> None:
    inbox, service = approval_inbox, ToolApprovalService()
    inbox.block_accept = True
    waiting = asyncio.create_task(service.wait(
        approval_snapshot, job_id="job", session_id="session", thread_id="thread", inbox=inbox,
    ))
    await asyncio.sleep(0)
    first = asyncio.create_task(service.submit("job", approval_request))
    await asyncio.wait_for(inbox.accept_entered.wait(), 1)
    second = asyncio.create_task(service.submit("job", approval_request.model_copy(update={"command_id": "other"})))
    try:
        await asyncio.sleep(0)
        assert not second.done()
        assert inbox.snapshot() == []
        inbox.accept_finish.set()
        accepted = await first
        assert accepted.status == "accepted"
        with pytest.raises(ValueError, match="原检查点已接受另一份审批决定"):
            await second
        assert [command.command_id for command in inbox.snapshot()] == ["command"]
        assert (await inbox.next()).command_id == "command"
        assert inbox._pending.empty()
    finally:
        inbox.accept_finish.set()
        inbox.finish.set()
        waiting.cancel()
        await asyncio.gather(waiting, first, second, return_exceptions=True)
