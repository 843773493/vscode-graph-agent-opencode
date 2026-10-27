"""授权等待只观察原操作；完成通知和独立执行沿既有持久 owner 发布。"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Literal

from pydantic import JsonValue

from app.abstractions.job_event_bus import JobEventBusProtocol
from app.abstractions.mcp_connection_wait import McpWaitCheckpoint, McpWaitPayload
from app.abstractions.tool_selection import ToolSelectionReader
from app.agents.policy.tool_capability import ToolMetadata
from app.core.background_task_contracts import (
    BackgroundMessageDTO,
    BackgroundMessageKind,
    BackgroundTaskDescriptor,
    BackgroundTaskHandle,
    BackgroundTaskStep,
)
from app.core.phase_observer import observe_phase
from app.core.trace_middleware import (
    reset_current_gateway_id,
    reset_current_request_id,
    set_current_gateway_id,
    set_current_request_id,
)
from app.prompting import PromptSection, internal_message_factory
from app.services.business.session_service import SessionService
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.mcp.connection_service import McpConnectionService
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.orchestration.runtime_source_dispatcher import RuntimeSourceDispatcher

_INVALID_ORIGIN_OUTCOMES = {"failed", "cancelled", "interrupted", "execution_lost"}


class McpConnectionWaitHandler:
    kind = "mcp_connection_wait"
    version = 1
    effect: Literal["atomic_mailbox"] = "atomic_mailbox"

    def __init__(
        self, *, connections: McpConnectionService, saver: RolloutCheckpointSaver,
        events: JobEventBusProtocol, config: ConfigService,
        selection: ToolSelectionReader, sessions: SessionService,
    ) -> None:
        self._connections = connections
        self._saver = saver
        self._events = events
        self._config = config
        self._selection = selection
        self._sessions = sessions
        self._dispatcher: RuntimeSourceDispatcher | None = None

    def bind_dispatcher(self, dispatcher: RuntimeSourceDispatcher) -> None:
        if self._dispatcher is not None:
            raise RuntimeError("MCP 等待投递入口不能重复装配")
        self._dispatcher = dispatcher

    def initial_checkpoint(self, descriptor: BackgroundTaskDescriptor) -> dict[str, JsonValue]:
        McpWaitPayload.model_validate(descriptor.payload)
        return McpWaitCheckpoint().model_dump(mode="json")

    def validate_checkpoint(self, checkpoint: dict[str, JsonValue]) -> None:
        McpWaitCheckpoint.model_validate(checkpoint)

    async def reconcile(self, handle: BackgroundTaskHandle) -> BackgroundTaskStep | None:
        raise ValueError("MCP 等待没有外部授权副作用；投递由原幂等执行入口恢复")

    async def _origin_result_committed(self, handle: BackgroundTaskHandle, payload: McpWaitPayload) -> bool:
        subscription = await self._events.subscribe(
            payload.origin_job_id, subscriber_kind="mcp-connection-wait",
            metadata={"session_id": handle.session_id, "thread_id": handle.thread_id},
            event_types=frozenset({"tool_call_end", "job_completed", "job_failed", "job_cancelled"}),
        )
        try:
            while True:
                result_id = self._saver.committed_tool_result_id(
                    handle.session_id, handle.thread_id, payload.model_call_id, payload.tool_call_id,
                )
                if result_id is not None:
                    result = self._saver.get_canonical_item(handle.session_id, thread_id=handle.thread_id, item_id=result_id)
                    if result is None:
                        raise RuntimeError("MCP 等待的已提交 ToolResult 正文丢失")
                    body = result.payload
                    if not isinstance(body, Mapping) or body.get("tool_outcome") != "success":
                        return False
                    content = body.get("content")
                    if not isinstance(content, str):
                        return False
                    receipt = json.loads(content)
                    return isinstance(receipt, dict) and isinstance(receipt.get("wait"), dict) and receipt["wait"].get("task_id") == handle.task_id
                outcome = self._saver.runtime_execution_outcome(
                    handle.session_id, handle.thread_id, payload.origin_execution_id,
                )
                if outcome in _INVALID_ORIGIN_OUTCOMES | {"completed", "completed_empty"} or time.time() >= payload.deadline:
                    return False
                try:
                    async with asyncio.timeout(payload.deadline - time.time()):
                        await subscription.get()
                except TimeoutError:
                    return False
        finally:
            await self._events.unsubscribe(payload.origin_job_id, subscription, reason="mcp-wait-origin-checked")

    def _finish(self, checkpoint: McpWaitCheckpoint, delivery: str) -> BackgroundTaskStep:
        settled = checkpoint.model_copy(update={"phase": "settled", "delivery": delivery})
        return BackgroundTaskStep(
            checkpoint=settled.model_dump(mode="json"), completed=True,
            result={"delivery": delivery, "operation": checkpoint.operation.model_dump(mode="json") if checkpoint.operation else None},
        )

    async def step(self, handle: BackgroundTaskHandle) -> BackgroundTaskStep:
        payload = McpWaitPayload.model_validate(handle.descriptor.payload)
        checkpoint = McpWaitCheckpoint.model_validate(handle.checkpoint)
        if checkpoint.phase == "observe":
            if not await self._origin_result_committed(handle, payload):
                return self._finish(checkpoint, "origin_result_uncommitted")
            operation = await self._connections.wait_terminal(payload.operation_id, deadline=payload.deadline)
            if operation.connection_id != payload.connection_id:
                raise RuntimeError("MCP 等待操作的连接归属改变")
            checkpoint = McpWaitCheckpoint(phase="deliver", operation=operation, wait_expired=not operation.terminal)
            message = BackgroundMessageDTO(
                message_id=f"{handle.task_id}:completion", session_id=handle.session_id,
                thread_id=handle.thread_id, agent_id=handle.agent_id, source_id=handle.task_id,
                kind=BackgroundMessageKind.normal,
                content="MCP 连接等待已到期" if checkpoint.wait_expired else f"MCP 连接操作已结束：{operation.status}",
                payload={"operation": operation.model_dump(mode="json"), "wait_expired": checkpoint.wait_expired},
                timestamp=datetime.fromtimestamp(min(operation.updated_at, payload.deadline), UTC),
            )
            return BackgroundTaskStep(checkpoint=checkpoint.model_dump(mode="json"), messages=(message,))
        if checkpoint.phase != "deliver":
            raise RuntimeError("已结算的 MCP 等待不应再次执行")
        # 上一步已经由原控制库同时提交完成消息和待投递 checkpoint。
        await observe_phase(handle.task_id, "mcp.wait.mailbox_committed")
        dispatcher = self._dispatcher
        if dispatcher is None:
            raise RuntimeError("MCP 等待尚未装配内部执行投递入口")
        existing = dispatcher.submitted(handle.session_id, handle.thread_id, idempotency_key=handle.task_id)
        if existing is not None:
            await dispatcher.dispatch(existing)
        else:
            reason = await self._blocked_reason(handle, payload, checkpoint)
            if reason is not None:
                return self._finish(checkpoint, reason)
            gateway_token = set_current_gateway_id(payload.gateway_id)
            request_token = set_current_request_id(payload.request_id)
            try:
                await dispatcher.submit(
                    handle.session_id, handle.thread_id,
                    internal_message_factory.build(
                        kind="mcp_connection_completed",
                        control=("MCP 连接操作已核实成功。检查本执行冻结的新目录和当前权限，再按原意图继续。"
                                 "完成通知不证明任何业务工具已执行；结果未知的写入必须先核查，不能自动重放。"),
                        sections=(
                            PromptSection("control_context", {
                                "operation_id": payload.operation_id, "connection_id": payload.connection_id,
                                "origin_model_call_id": payload.model_call_id, "origin_tool_call_id": payload.tool_call_id,
                            }),
                            PromptSection("untrusted_instructions", payload.continuation),
                        ),
                        metadata={"source": self.kind},
                    ),
                    source_id=handle.task_id, idempotency_key=handle.task_id, delivery_policy="after_turn",
                )
            finally:
                reset_current_request_id(request_token)
                reset_current_gateway_id(gateway_token)
        await observe_phase(handle.task_id, "mcp.wait.dispatcher_accepted")
        return self._finish(checkpoint, "submitted")

    async def _blocked_reason(
        self, handle: BackgroundTaskHandle, payload: McpWaitPayload, checkpoint: McpWaitCheckpoint,
    ) -> str | None:
        if checkpoint.wait_expired or time.time() >= payload.deadline:
            return "deadline_exceeded"
        operation = await self._connections.status(payload.operation_id)
        if operation.status != "succeeded" or operation.action != "connect":
            return f"operation_{operation.status}"
        if not any(row["status"] == "ready" for row in self._connections.list_connections(operation.server_id)):
            return "connection_not_ready"
        if self._saver.runtime_execution_outcome(
            handle.session_id, handle.thread_id, payload.origin_execution_id,
        ) in _INVALID_ORIGIN_OUTCOMES:
            return "origin_execution_invalid"
        session = await self._sessions.get(handle.session_id)
        if session.current_agent_id != handle.agent_id:
            return "agent_changed"
        enabled = self._config.resolve_agent_tool_policy(handle.agent_id).enabled_names
        policy = self._config.get_tool_policy_resolver(handle.agent_id).resolve(
            ToolMetadata("connections.manage", "custom", "extension", "extension:connections"),
            execution_override=self._selection.snapshot(handle.agent_id).execution_overrides.get("connections.manage"),
        )
        if "connections.manage" not in enabled or not policy.execution_enabled:
            return "permission_revoked"
        return None
