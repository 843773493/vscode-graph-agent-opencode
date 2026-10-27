"""以原图检查点为审批来源，沿既有控制 inbox 投递决定。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal, cast

from langchain.agents.middleware.human_in_the_loop import HITLRequest
from langchain_core.messages import AIMessage
from langgraph.types import Command, StateSnapshot
from pydantic import TypeAdapter

from app.core.turn_execution_scope import AgentControlCommand, AgentControlInbox
from app.schemas.internal_v2.tool_approval import (
    ToolApprovalActionDTO,
    ToolApprovalCommandDTO,
    ToolApprovalDTO,
    ToolApprovalSubmitRequest,
)
from app.services.orchestration.activity_runtime import current_activity_runtime

_REQUEST = TypeAdapter(HITLRequest)
LiteralCommandStatus = Literal["accepted", "consumed", "rejected"]


def checkpoint_approvals(
    snapshot: StateSnapshot, *, job_id: str, session_id: str, thread_id: str,
) -> list[ToolApprovalDTO]:
    checkpoint_id = snapshot.config.get("configurable", {}).get("checkpoint_id")
    if not isinstance(checkpoint_id, str) or not checkpoint_id:
        raise RuntimeError("工具审批缺少原检查点身份")
    messages = snapshot.values.get("messages", [])
    assistant = next((message for message in reversed(messages) if isinstance(message, AIMessage)), None)
    if assistant is None:
        raise RuntimeError("工具审批检查点缺少原模型工具调用")
    calls = assistant.tool_calls
    matched: set[str] = set()
    approvals: list[ToolApprovalDTO] = []
    for interrupt in snapshot.interrupts:
        request = _REQUEST.validate_python(interrupt.value)
        actions = request["action_requests"]
        reviews = request["review_configs"]
        if not actions or len(actions) != len(reviews):
            raise RuntimeError("工具审批请求与 review config 数量不一致")
        projected: list[ToolApprovalActionDTO] = []
        for action, review in zip(actions, reviews, strict=True):
            if review["action_name"] != action["name"]:
                raise RuntimeError("工具审批请求与 review config 身份不一致")
            for call in calls:
                call_id = call["id"]
                if not isinstance(call_id, str) or not call_id:
                    raise RuntimeError("工具审批检查点缺少稳定 tool_call_id")
                if call_id in matched:
                    continue
                name, args = call["name"], call["args"]
                if name == "invoke_extension_tool" and name != action["name"]:
                    name, args = args.get("tool_name"), args.get("arguments", {})
                if name == action["name"] and args == action["args"]:
                    matched.add(call_id)
                    projected.append(ToolApprovalActionDTO(
                        tool_call_id=call_id, name=action["name"], args=action["args"],
                        description=action.get("description"), allowed_decisions=review["allowed_decisions"],
                    ))
                    break
            else:
                raise RuntimeError("工具审批请求无法精确关联原 tool_call_id")
        approvals.append(ToolApprovalDTO(
            approval_id=interrupt.id, checkpoint_id=checkpoint_id,
            job_id=job_id, session_id=session_id, thread_id=thread_id, actions=projected,
        ))
    return approvals


@dataclass(slots=True)
class _ApprovalWait:
    requests: list[ToolApprovalDTO]
    inbox: AgentControlInbox
    response: asyncio.Future[Command]


class ToolApprovalService:
    """只保存活动等待句柄；请求归原 checkpoint，决定归原控制 inbox。"""

    def __init__(self) -> None:
        self._waiting: dict[str, _ApprovalWait] = {}

    def list(self, job_id: str) -> list[ToolApprovalDTO]:
        waiting = self._waiting.get(job_id)
        return [request.model_copy(deep=True) for request in waiting.requests] if waiting else []

    def list_for_thread(self, session_id: str, thread_id: str) -> list[ToolApprovalDTO]:
        return [request.model_copy(deep=True) for waiting in self._waiting.values()
                for request in waiting.requests
                if request.session_id == session_id and request.thread_id == thread_id]

    async def wait(
        self, snapshot: StateSnapshot, *, job_id: str, session_id: str,
        thread_id: str, inbox: AgentControlInbox,
    ) -> Command:
        requests = checkpoint_approvals(snapshot, job_id=job_id, session_id=session_id, thread_id=thread_id)
        if not requests or job_id in self._waiting:
            raise RuntimeError("工具审批等待必须对应唯一非空检查点")
        waiting = _ApprovalWait(requests, inbox, asyncio.get_running_loop().create_future())
        self._waiting[job_id] = waiting
        activity = current_activity_runtime()
        activity_id = f"{inbox.turn_stream_id}:approval:{':'.join(entry.approval_id for entry in requests)}"
        try:
            if activity is not None:
                await activity.started(
                    activity_id=activity_id, kind="approval.wait", summary="等待用户确认工具操作",
                    cancellable=True, side_effect_policy="none",
                    detail={"approval_id": requests[0].approval_id, "required_action": "approve_or_reject"},
                )
                await activity.updated(activity_id=activity_id, kind="approval.wait", status="waiting")
            result = await waiting.response
            if activity is not None:
                await activity.completed(activity_id=activity_id, kind="approval.wait", summary="用户已提交工具审批决定")
            return result
        finally:
            del self._waiting[job_id]

    def submit(self, job_id: str, request: ToolApprovalSubmitRequest) -> ToolApprovalCommandDTO:
        waiting = self._waiting.get(job_id)
        if waiting is None:
            raise ValueError("当前 Job 没有可恢复的活动工具审批")
        self._resume(waiting, request)
        payload = {"job_id": job_id, **request.model_dump(mode="json")}
        existing = waiting.inbox.get(request.command_id)
        if existing is not None and (existing.kind != "approval.result" or existing.payload != payload):
            raise ValueError("审批 command_id 已绑定不同决定")
        if any(command.kind == "approval.result" and command.payload.get("checkpoint_id") == request.checkpoint_id
               and command.command_id != request.command_id and command.status != "rejected"
               for command in waiting.inbox.snapshot()):
            raise ValueError("原检查点已接受另一份审批决定")
        command = waiting.inbox.accept(
            command_id=request.command_id, kind="approval.result",
            idempotency_key=f"approval:{request.command_id}", payload=payload,
        )
        return ToolApprovalCommandDTO(
            command_id=command.command_id, checkpoint_id=request.checkpoint_id,
            status=cast(LiteralCommandStatus, command.status),
        )

    async def process(self, command: AgentControlCommand) -> dict[str, object]:
        job_id = command.payload.get("job_id")
        waiting = self._waiting.get(job_id) if isinstance(job_id, str) else None
        if waiting is None or waiting.response.done() or waiting.inbox.get(command.command_id) != command:
            return {"status": "rejected", "reason": "approval_not_active"}
        request = ToolApprovalSubmitRequest.model_validate({key: value for key, value in command.payload.items() if key != "job_id"})
        resume = self._resume(waiting, request)
        try:
            waiting.inbox.mark(command.command_id, "consumed")
        except Exception as error:
            # inbox 落盘失败必须交还原执行，不能留下无限等待或先执行工具。
            waiting.response.set_exception(error)
            raise
        waiting.response.set_result(resume)
        return {"status": "consumed"}

    @staticmethod
    def _resume(waiting: _ApprovalWait, request: ToolApprovalSubmitRequest) -> Command:
        if request.checkpoint_id != waiting.requests[0].checkpoint_id:
            raise ValueError("审批检查点已变化")
        responses = {response.approval_id: response for response in request.responses}
        if len(responses) != len(request.responses) or set(responses) != {entry.approval_id for entry in waiting.requests}:
            raise ValueError("必须为原检查点的每个审批提交唯一决定")
        resume: dict[str, object] = {}
        for entry in waiting.requests:
            decisions = {decision.tool_call_id: decision for decision in responses[entry.approval_id].decisions}
            if len(decisions) != len(responses[entry.approval_id].decisions) or set(decisions) != {action.tool_call_id for action in entry.actions}:
                raise ValueError("审批决定必须与原 tool_call_id 逐一对应")
            ordered = []
            for action in entry.actions:
                decision = decisions[action.tool_call_id]
                if decision.type not in action.allowed_decisions:
                    raise ValueError(f"工具 {action.name} 不允许 {decision.type} 决定")
                ordered.append(decision.model_dump(mode="json", exclude={"tool_call_id"}, exclude_none=True))
            resume[entry.approval_id] = {"decisions": ordered}
        return Command(resume=resume)
