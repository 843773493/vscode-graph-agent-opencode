"""实时 tool-call registry 与 tool result transition owner。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass

from langchain_core.messages import ToolMessage

from app.abstractions.turn_terminal_status import (
    ToolResultCompletion,
    ToolResultContent,
    tool_result_content_text,
)
from app.agents.tool_result_text import tool_output_status
from app.core.identifier import create_prefixed_id
from app.domain.itemized.identity.item_projection import ItemProjectionIdentity
from app.domain.itemized.identity.tool_call_identity import provider_tool_call_id
from app.schemas.internal_v2.mcp import McpInvocationReceipt
from app.services.orchestration.event_stream.contracts import CanonicalItemObserverError


@dataclass(frozen=True, slots=True)
class PendingToolCompletion:
    event_id: str
    model_call_id: str | None
    tool_execution_id: str
    tool_call_id: str
    tool_invocation_id: str
    tool_name: str
    payload: Mapping[str, object]


class StreamToolRegistryMixin:
    _tool_call_names_by_id: dict[str, str]

    async def complete_tool_approval(self, message: ToolMessage, *, model_call_id: str) -> None:
        """用户直接拒绝／提供结果也是原调用的一次明确结算，不等待不存在的 SDK callback。"""
        call_id = self.claim_tool_call_id(model_call_id, message.tool_call_id)
        attempt_id = f"{call_id}:approval"
        tool_name = self._tool_call_names_by_id[call_id]
        await self.start_tool(tool_execution_id=attempt_id, tool_call_id=call_id, tool_name=tool_name)
        successful = tool_output_status(message) == "success"
        await self.complete_tool(
            tool_execution_id=attempt_id, tool_call_id=call_id, tool_name=tool_name,
            status="succeeded" if successful else "failed", result=message.content,
            artifact=message.artifact, outcome="success" if successful else "provider_error",
            completion_reason="user_approval", error=None if successful else tool_result_content_text(message.content),
        )

    def _tool_call_binding(self, callback_run_id: str) -> asyncio.Future[str]:
        binding = self._tool_call_bindings_by_callback_run_id.get(callback_run_id)
        if binding is None:
            binding = asyncio.get_running_loop().create_future()
            self._tool_call_bindings_by_callback_run_id[callback_run_id] = binding
        return binding

    async def tool_call_for_callback(self, callback_run_id: str) -> str:
        """事件先到也只等待真实 callback 的身份交接，不按名称或参数猜调用。"""
        # callback 和事件各消费一次；交接保留至本次 step 结束，失败回报不能
        # 在事件已取走身份后重新创建一个无人消费的 Future。
        return await asyncio.shield(self._tool_call_binding(callback_run_id))

    def reject_tool_start(self, callback_run_id: str, error: BaseException) -> None:
        binding = self._tool_call_binding(callback_run_id)
        if not binding.done():
            if isinstance(error, asyncio.CancelledError):
                binding.cancel()
            else:
                binding.set_exception(error)

    async def wait_tool_start(
        self, model_call_id: str, tool_call_id: str, callback_run_id: str,
    ) -> None:
        """真实 callback 绑定原调用，工具体只在该 attempt 持久开始后执行。"""
        call_id = self.claim_tool_call_id(model_call_id, tool_call_id)
        binding = self._tool_call_binding(callback_run_id)
        if binding.done():
            raise RuntimeError(f"工具开始回调重复绑定: {callback_run_id}")
        binding.set_result(call_id)
        expected_attempt = self.resumed_tool_execution_id(call_id, callback_run_id)
        completed_attempt = self._completed_tool_result_execution_by_call_id.get(call_id)
        if completed_attempt == expected_attempt:
            return
        actual_attempt, _tool_name = await self._require_tool_attempt(call_id)
        if actual_attempt != expected_attempt:
            raise RuntimeError(
                f"工具开始回调与原 attempt 不一致: {call_id} "
                f"expected={expected_attempt} actual={actual_attempt}"
            )

    async def start_tool(
        self,
        *,
        tool_execution_id: str,
        tool_call_id: str,
        tool_name: str,
    ) -> None:
        if tool_execution_id in self._completed_tool_execution_ids:
            return
        previous = self._active_tool_executions.get(tool_execution_id)
        if previous is not None:
            if previous != (tool_call_id, tool_name):
                raise RuntimeError("恢复 tool attempt 与原 call/name 不一致")
            return
        tool_call_id = self._resolve_tool_call_id(tool_call_id)
        if tool_call_id not in self._tool_call_model_call_ids:
            self._tool_call_model_call_ids[tool_call_id] = self.current_model_call_id
        model_call_id = self._tool_call_model_call_ids[tool_call_id]
        tool_invocation_id = self._tool_invocation_id_for(tool_call_id)
        try:
            if tool_call_id not in self._completed_tool_call_ids:
                if self.writer is not None:
                    await self.writer.commit(
                        "tool_call.completed",
                        {
                            "tool_call_id": tool_call_id,
                            "tool_invocation_id": tool_invocation_id,
                            "tool_name": tool_name,
                            "status": "completed",
                            "completion_reason": "tool_started",
                            "arguments_complete": self._tool_call_arguments_complete.get(
                                tool_call_id,
                                False,
                            ),
                        },
                        model_call_id=model_call_id,
                        tool_call_id=tool_call_id,
                        tool_invocation_id=tool_invocation_id,
                    )
                self._completed_tool_call_ids.add(tool_call_id)
            if self.writer is not None:
                await self.writer.commit(
                    "tool.started",
                    {
                        "tool_execution_id": tool_execution_id,
                        "tool_call_id": tool_call_id,
                        "tool_invocation_id": tool_invocation_id,
                        "tool_attempt_id": tool_execution_id,
                        "tool_name": tool_name,
                    },
                    model_call_id=model_call_id,
                    tool_execution_id=tool_execution_id,
                    tool_call_id=tool_call_id,
                    tool_invocation_id=tool_invocation_id,
                    tool_attempt_id=tool_execution_id,
                )
        except BaseException as error:
            pending = self._pending_tool_starts.get(tool_call_id)
            if pending is not None and not pending.done():
                if isinstance(error, asyncio.CancelledError):
                    pending.cancel()
                else:
                    pending.set_exception(error)
            raise
        self._active_tool_executions[tool_execution_id] = (tool_call_id, tool_name)
        pending = self._pending_tool_starts.pop(tool_call_id, None)
        if pending is not None:
            pending.set_result((tool_execution_id, tool_name))

    async def fail_tool_invocation(
        self, model_call_id: str, tool_call_id: str, *, error: str, artifact: object = None,
    ) -> None:
        """只闭合已经开始的原 attempt，再允许工具异常进入 scheduler。"""
        provider_id = provider_tool_call_id(
            ItemProjectionIdentity(model_call_id=model_call_id), tool_call_id,
        )
        call_id = self._tool_call_ids_by_provider_key.get((model_call_id, provider_id))
        if call_id is None or call_id in self._completed_tool_result_call_ids:
            return
        attempts = [
            (execution_id, tool_name)
            for execution_id, (active_call_id, tool_name) in self._active_tool_executions.items()
            if active_call_id == call_id
        ]
        if len(attempts) > 1:
            raise RuntimeError(f"工具调用绑定了多个 active attempt: {call_id}")
        # callback 拒绝发生在工具体之前；没有 active attempt 就保留原异常，
        # 不能新建一个永远不会由 on_tool_start 兑现的等待。
        if not attempts:
            return
        execution_id, tool_name = attempts[0]
        await self.complete_tool(
            tool_execution_id=execution_id, tool_call_id=call_id, tool_name=tool_name,
            status="failed", result=error, error=error, artifact=artifact,
            outcome="outcome_unknown", completion_reason="execution_error",
        )

    async def _require_tool_attempt(self, call_id: str) -> tuple[str, str]:
        """只认实际 on_tool_start；request 消息 ID 不能充当 attempt。"""
        attempts = [
            (execution_id, tool_name)
            for execution_id, (active_call_id, tool_name) in self._active_tool_executions.items()
            if active_call_id == call_id
        ]
        if len(attempts) > 1:
            raise RuntimeError(f"工具调用绑定了多个 active attempt: {call_id}")
        if attempts:
            execution_id, tool_name = attempts[0]
        else:
            # on_tool_start 经事件流到达；只等待本调用的真实 attempt，不伪造 ID。
            pending = self._pending_tool_starts.get(call_id)
            if pending is None:
                pending = asyncio.get_running_loop().create_future()
                self._pending_tool_starts[call_id] = pending
            execution_id, tool_name = await asyncio.shield(pending)
        return execution_id, tool_name

    def claim_tool_call_id(self, model_call_id: str, tool_call_id: str) -> str:
        """只用 ToolNode 冻结的 emitting ModelCall/provider call 认领。"""
        provider_id = provider_tool_call_id(
            ItemProjectionIdentity(model_call_id=model_call_id), tool_call_id,
        )
        call_id = self._tool_call_ids_by_provider_key.get((model_call_id, provider_id))
        if call_id is None:
            raise RuntimeError(f"工具开始回调缺少原 emitting ModelCall/ToolCall: {model_call_id}/{tool_call_id}")
        if call_id in self._claimed_tool_call_ids:
            raise RuntimeError(f"工具调用已经认领: {call_id}")
        self._claimed_tool_call_ids.add(call_id)
        return call_id

    def pending_tool_calls(self) -> tuple[tuple[str, str, bool], ...]:
        """返回已经收到但尚未进入 Agent 工具执行器的调用。"""
        pending: list[tuple[str, str, bool]] = []
        for tool_call_id in self._tool_call_order:
            if (
                tool_call_id in self._claimed_tool_call_ids
                or tool_call_id in self._completed_tool_call_ids
            ):
                continue
            pending.append(
                (
                    tool_call_id,
                    self._tool_call_names_by_id.get(tool_call_id, ""),
                    self._tool_call_arguments_complete.get(tool_call_id, False),
                )
            )
        return tuple(pending)

    async def fail_pending_tool_calls(
        self,
        *,
        completion_reason: str,
        error: str,
    ) -> None:
        """在分派丢失时闭合工具调用，避免 snapshot 永远停在 accumulating。"""
        for tool_call_id, tool_name, arguments_complete in self.pending_tool_calls():
            if tool_call_id not in self._tool_call_model_call_ids:
                raise RuntimeError(
                    "pending tool call 缺少 emitting model-call 归属: "
                    f"tool_call_id={tool_call_id}"
                )
            model_call_id = self._tool_call_model_call_ids[tool_call_id]
            tool_invocation_id = self._tool_invocation_id_for(tool_call_id)
            if self.writer is not None:
                await self.writer.commit(
                    "tool_call.completed",
                    {
                        "tool_call_id": tool_call_id,
                        "tool_invocation_id": tool_invocation_id,
                        "tool_name": tool_name,
                        "status": "incomplete",
                        "completion_reason": completion_reason,
                        "arguments_complete": arguments_complete,
                        "error": error,
                    },
                    model_call_id=model_call_id,
                    tool_call_id=tool_call_id,
                    tool_invocation_id=tool_invocation_id,
                )
            self._completed_tool_call_ids.add(tool_call_id)

    async def fail_active_tool_executions(
        self,
        *,
        completion_reason: str,
        error: str,
    ) -> None:
        attempts = dict(self._active_tool_executions)
        for tool_execution_id, pending in self._pending_tool_completions.items():
            attempts.setdefault(
                tool_execution_id,
                (pending.tool_call_id, pending.tool_name),
            )
        for tool_execution_id, (tool_call_id, tool_name) in tuple(attempts.items()):
            await self.complete_tool(
                tool_execution_id=tool_execution_id,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                status="failed",
                result=error,
                error=error,
                outcome="outcome_unknown",
                completion_reason=completion_reason,
            )

    async def complete_tool(
        self,
        *,
        tool_execution_id: str,
        tool_call_id: str,
        tool_name: str,
        status: str,
        result: ToolResultContent,
        artifact: object = None,
        error: str | None = None,
        outcome: str | None = None,
        completion_reason: str | None = None,
    ) -> None:
        tool_call_id = self._resolve_tool_call_id(tool_call_id)
        async with self._tool_completion_lock:
            # on_tool_end 与下一次 model call 中的 ToolMessage 可能并发到达。
            # completion 的幂等键是实际 tool attempt（当前由
            # tool_execution_id 承载）；同一逻辑 call 绑定不同 attempt 必须报
            # 冲突，不能把后到的结果静默覆盖到已有 canonical item。
            if tool_execution_id in self._completed_tool_execution_ids:
                return
            pending = self._pending_tool_completions.get(tool_execution_id)
            if pending is not None:
                if (
                    pending.tool_call_id != tool_call_id
                    or pending.tool_name != tool_name
                ):
                    raise RuntimeError(
                        "待提交的 tool completion 与重入 attempt 身份不一致: "
                        f"tool_execution_id={tool_execution_id}"
                    )
                await self._commit_pending_tool_completion(pending)
                return
            if tool_call_id in self._completed_tool_result_call_ids:
                previous_execution_id = self._completed_tool_result_execution_by_call_id.get(
                    tool_call_id
                )
                if previous_execution_id == tool_execution_id:
                    return
                raise RuntimeError(
                    "同一 tool_call_id 绑定了多个已完成 tool attempt: "
                    f"tool_call_id={tool_call_id} "
                    f"previous={previous_execution_id} current={tool_execution_id}"
                )
            normalized_status = "completed" if status == "succeeded" else status
            unknown_receipt = McpInvocationReceipt.from_artifact(artifact)
            if unknown_receipt is not None and normalized_status != "failed":
                raise ValueError("MCP 结果未知回执不能登记为成功")
            normalized_outcome = "outcome_unknown" if unknown_receipt is not None else outcome or (
                "success" if normalized_status == "completed" else "provider_error"
            )
            if normalized_outcome == "unknown":
                normalized_outcome = "outcome_unknown"
            if tool_call_id in self._tool_call_model_call_ids:
                model_call_id = self._tool_call_model_call_ids[tool_call_id]
            else:
                model_call_id = self.current_model_call_id
                self._tool_call_model_call_ids[tool_call_id] = model_call_id
            tool_invocation_id = self._tool_invocation_id_for(tool_call_id)
            payload: dict[str, object] = {
                "tool_execution_id": tool_execution_id,
                "tool_call_id": tool_call_id,
                "tool_invocation_id": tool_invocation_id,
                "tool_attempt_id": tool_execution_id,
                "tool_name": tool_name,
                "status": normalized_status,
                "outcome": normalized_outcome,
                "completion_reason": (
                    completion_reason
                    or (
                        "tool_completed"
                        if normalized_outcome == "success"
                        else "provider_failed"
                    )
                ),
                "result": tool_result_content_text(result),
            }
            if error is not None:
                payload["error"] = error
            observer_error: Exception | None = None
            if self._canonical_item_sink is not None:
                result_outcome = (
                    "unknown"
                    if normalized_outcome == "outcome_unknown"
                    else "success"
                    if normalized_outcome == "success"
                    else "cancelled"
                    if normalized_outcome in {"cancelled", "user_interrupt"}
                    else "failure"
                    if normalized_status == "failed" or error is not None
                    else "unknown"
                )
                if self._tool_result_sink is None:
                    raise RuntimeError("工具终态缺少 Saver-owned tool_result_sink")
                completion = ToolResultCompletion(
                    model_call_id=model_call_id, tool_call_id=tool_call_id,
                    tool_invocation_id=tool_invocation_id, tool_attempt_id=tool_execution_id,
                    tool_name=tool_name,
                    content=result, artifact=artifact, tool_outcome=result_outcome,
                )
                try:
                    item = await self._tool_result_sink(completion)
                except CanonicalItemObserverError as observer_failure:
                    if result_outcome == "unknown":
                        # 没有收到 owner 的真实结果，不可用未知正文伪造已完成镜像。
                        raise
                    if observer_failure.committed_item_ids != (completion.item_id,):
                        raise
                    # 已提交结果只更新既有运行镜像，失败清理不得再生成同 ID 的结果。
                    observer_error = observer_failure.observer_error
                else:
                    if result_outcome == "unknown":
                        # canonical 是唯一正文来源；收尾不得把先提交的成功/失败改成未知。
                        persisted = item.payload
                        persisted_outcome = persisted["tool_outcome"]
                        payload["result"] = tool_result_content_text(persisted["content"])
                        payload["outcome"] = (
                            "outcome_unknown" if persisted_outcome == "unknown"
                            else "success" if persisted_outcome == "success"
                            else "cancelled" if persisted_outcome == "cancelled"
                            else "provider_error"
                        )
                        if persisted_outcome != "unknown":
                            payload["status"] = "failed" if persisted_outcome == "failure" else "completed"
                            payload.pop("error", None)
                            payload["completion_reason"] = (
                                "user_interrupt" if persisted_outcome == "cancelled" else "tool_completed"
                            )
            # 完成镜像必须晚于 canonical 提交；重试时保留原事件身份和正文，
            # 避免把同一 canonical 结果按清理错误再次写入。
            pending = self._pending_tool_completion(
                model_call_id=model_call_id,
                tool_execution_id=tool_execution_id,
                tool_call_id=tool_call_id,
                tool_invocation_id=str(payload["tool_invocation_id"]),
                tool_name=tool_name,
                payload=payload,
            )
            await self._commit_pending_tool_completion(pending)
            if observer_error is not None:
                raise observer_error

    def _pending_tool_completion(
        self,
        *,
        model_call_id: str | None,
        tool_execution_id: str,
        tool_call_id: str,
        tool_invocation_id: str,
        tool_name: str,
        payload: Mapping[str, object],
    ) -> PendingToolCompletion:
        pending = PendingToolCompletion(
            event_id=create_prefixed_id("evt"),
            model_call_id=model_call_id,
            tool_execution_id=tool_execution_id,
            tool_call_id=tool_call_id,
            tool_invocation_id=tool_invocation_id,
            tool_name=tool_name,
            payload=dict(payload),
        )
        self._pending_tool_completions[tool_execution_id] = pending
        return pending

    async def _commit_pending_tool_completion(
        self,
        pending: PendingToolCompletion,
    ) -> None:
        if self.writer is not None:
            await self.writer.commit(
                "tool.completed",
                pending.payload,
                model_call_id=pending.model_call_id,
                tool_execution_id=pending.tool_execution_id,
                tool_call_id=pending.tool_call_id,
                tool_invocation_id=pending.tool_invocation_id,
                tool_attempt_id=pending.tool_execution_id,
                event_id=pending.event_id,
            )
        self._pending_tool_completions.pop(pending.tool_execution_id, None)
        self._active_tool_executions.pop(pending.tool_execution_id, None)
        self._completed_tool_execution_ids.add(pending.tool_execution_id)
        self._completed_tool_result_call_ids.add(pending.tool_call_id)
        self._completed_tool_result_execution_by_call_id[pending.tool_call_id] = (
            pending.tool_execution_id
        )

    async def complete_tool_from_message(
        self, message: ToolMessage, *, model_call_id: str,
    ) -> None:
        """请求 preparation 前把真实结果提交到原 attempt 的唯一终态端口。"""
        # 请求携带历史 ToolMessage；只认它实际 emitting model-call 的调用，
        # 不能用同名 provider raw ID 把旧结果写到当前 model-call 的 execution。
        raw_call_id = provider_tool_call_id(
            ItemProjectionIdentity(model_call_id=model_call_id), message.tool_call_id,
        )
        tool_call_id = self._tool_call_ids_by_provider_key.get((model_call_id, raw_call_id))
        if tool_call_id is None:
            return
        if tool_call_id in self._completed_tool_result_call_ids:
            return
        execution_id, tool_name = await self._require_tool_attempt(tool_call_id)
        result = tool_result_content_text(message.content)
        status = "succeeded" if tool_output_status(message) == "success" else "failed"
        await self.complete_tool(
            tool_execution_id=execution_id, tool_call_id=tool_call_id, tool_name=tool_name,
            status=status, result=message.content, artifact=message.artifact,
            error=result if status == "failed" else None,
        )
