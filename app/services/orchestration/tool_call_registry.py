"""实时 tool-call registry 与 tool result transition owner。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from langchain_core.messages import BaseMessage, ToolMessage

from app.domain.itemized.enums import CanonicalItemStatus
from app.domain.itemized.records import CanonicalItemRecord


class StreamToolRegistryMixin:
    async def start_tool(
        self,
        *,
        tool_execution_id: str,
        tool_call_id: str,
        tool_name: str,
    ) -> None:
        tool_call_id = self._resolve_tool_call_id(tool_call_id)
        tool_invocation_id = self._tool_invocation_id_for(tool_call_id)
        if tool_call_id not in self._completed_tool_call_ids:
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
                model_call_id=self.current_model_call_id,
                tool_call_id=tool_call_id,
                tool_invocation_id=tool_invocation_id,
            )
            self._completed_tool_call_ids.add(tool_call_id)
        self._started_tool_execution_ids.add(tool_execution_id)
        self._active_tool_executions[tool_execution_id] = (tool_call_id, tool_name)
        if tool_call_id not in self._tool_call_model_call_ids:
            self._tool_call_model_call_ids[tool_call_id] = self.current_model_call_id
        await self.writer.commit(
            "tool.started",
            {
                "tool_execution_id": tool_execution_id,
                "tool_call_id": tool_call_id,
                "tool_invocation_id": tool_invocation_id,
                "tool_attempt_id": tool_execution_id,
                "tool_name": tool_name,
            },
            model_call_id=self.current_model_call_id,
            tool_execution_id=tool_execution_id,
            tool_call_id=tool_call_id,
            tool_invocation_id=tool_invocation_id,
            tool_attempt_id=tool_execution_id,
        )

    def claim_tool_call_id(
        self,
        tool_name: str,
        tool_args: Mapping[str, object] | None = None,
        *,
        target_tool_name: str | None = None,
    ) -> str | None:
        """将 AgentLoop 工具执行严格关联到模型工具调用。

        固定信封工具的模型参数形如 ``{tool_name, arguments}``。外层 Agent
        事件携带的是目标工具名和目标参数，不能因为事件先到而退回到最新
        pending call；那会把同一轮后续工具的结果绑定到当前工具。
        """
        candidates: list[str] = []
        reconciled_candidates: list[str] = []
        for tool_call_id in reversed(self._tool_call_order):
            if tool_call_id in self._claimed_tool_call_ids and not (
                tool_call_id in self._reconciled_tool_call_ids
                and tool_call_id
                not in self._reconciled_late_claimed_tool_call_ids
            ):
                continue
            if self._tool_call_names_by_id.get(tool_call_id) != tool_name:
                continue
            if (
                tool_call_id in self._reconciled_tool_call_ids
                and tool_call_id not in self._reconciled_late_claimed_tool_call_ids
            ):
                reconciled_candidates.append(tool_call_id)
            else:
                candidates.append(tool_call_id)
        if reconciled_candidates:
            # 迟到的 on_tool_start 属于已在请求边界 reconcile 的调用（其
            # ToolMessage 先于事件流到达）。事件流内部 on_tool_start 之间保持
            # 顺序，因此这里优先认领最早的可重领 reconciled 调用；否则同名
            # 同参的更新 pending 调用会被错绑，导致同一 tool_call_id 在
            # tool 侧与 provider 侧提交两个不同正文。
            candidates = list(reversed(reconciled_candidates)) + candidates
        if tool_args is not None:
            normalized_tool_args = dict(tool_args)
            for tool_call_id in candidates:
                provider_arguments = self._tool_call_arguments_by_id.get(
                    tool_call_id,
                    {},
                )
                if provider_arguments == normalized_tool_args:
                    self._claimed_tool_call_ids.add(tool_call_id)
                    if tool_call_id in self._reconciled_tool_call_ids:
                        self._reconciled_late_claimed_tool_call_ids.add(tool_call_id)
                    return tool_call_id
                nested_tool_name = provider_arguments.get("tool_name")
                nested_arguments = provider_arguments.get("arguments")
                if (
                    nested_tool_name
                    == (target_tool_name if target_tool_name is not None else tool_name)
                    and isinstance(nested_arguments, Mapping)
                    and dict(nested_arguments) == normalized_tool_args
                ):
                    self._claimed_tool_call_ids.add(tool_call_id)
                    if tool_call_id in self._reconciled_tool_call_ids:
                        self._reconciled_late_claimed_tool_call_ids.add(tool_call_id)
                    return tool_call_id
            incomplete_candidates = [
                tool_call_id
                for tool_call_id in candidates
                if not self._tool_call_arguments_complete.get(tool_call_id, False)
            ]
            if len(incomplete_candidates) == 1:
                tool_call_id = incomplete_candidates[0]
                self._claimed_tool_call_ids.add(tool_call_id)
                if tool_call_id in self._reconciled_tool_call_ids:
                    self._reconciled_late_claimed_tool_call_ids.add(tool_call_id)
                return tool_call_id
        if tool_args is None and candidates:
            tool_call_id = candidates[0]
            self._claimed_tool_call_ids.add(tool_call_id)
            if tool_call_id in self._reconciled_tool_call_ids:
                self._reconciled_late_claimed_tool_call_ids.add(tool_call_id)
            return tool_call_id
        return None

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
            tool_invocation_id = self._tool_invocation_id_for(tool_call_id)
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
                model_call_id=self.current_model_call_id,
                tool_call_id=tool_call_id,
                tool_invocation_id=tool_invocation_id,
            )
            self._completed_tool_call_ids.add(tool_call_id)

    async def complete_tool(
        self,
        *,
        tool_execution_id: str,
        tool_call_id: str,
        tool_name: str,
        status: str,
        result: str,
        error: str | None = None,
        outcome: str | None = None,
        persist_canonical: bool = True,
    ) -> None:
        tool_call_id = self._resolve_tool_call_id(tool_call_id)
        async with self._tool_completion_lock:
            # on_tool_end 与下一次 model call 中的 ToolMessage 可能并发到达。
            # completion 的幂等键是实际 tool attempt（当前由
            # tool_execution_id 承载）；同一逻辑 call 绑定不同 attempt 必须报
            # 冲突，不能把后到的结果静默覆盖到已有 canonical item。
            if tool_execution_id in self._completed_tool_execution_ids:
                return
            if tool_call_id in self._completed_tool_result_call_ids:
                previous_execution_id = self._completed_tool_result_execution_by_call_id.get(
                    tool_call_id
                )
                if previous_execution_id == tool_execution_id:
                    return
                if tool_call_id in self._reconciled_tool_call_ids:
                    # 请求边界已经收到同一调用的 ToolMessage，但 LangGraph
                    # 的 on_tool_start/on_tool_end 可能在外层事件流中迟到。
                    # 迟到的 execution 只完成生命周期收口，不能把已确认的
                    # 结果再当成第二次工具执行。这里仍要写一条 execution
                    # 的终态事件；否则内存 registry 虽已移除它，持久化的
                    # message-stream snapshot 仍会把它保留为 running。
                    normalized_status = (
                        "completed" if status == "succeeded" else status
                    )
                    normalized_outcome = outcome or (
                        "success"
                        if normalized_status == "completed"
                        else "provider_error"
                    )
                    if normalized_outcome == "unknown":
                        normalized_outcome = "outcome_unknown"
                    tool_invocation_id = self._tool_invocation_id_for(tool_call_id)
                    model_call_id = self._tool_call_model_call_ids.get(tool_call_id)
                    if model_call_id is None:
                        model_call_id = self.current_model_call_id
                        self._tool_call_model_call_ids[tool_call_id] = model_call_id
                    payload: dict[str, object] = {
                        "tool_execution_id": tool_execution_id,
                        "tool_call_id": tool_call_id,
                        "tool_invocation_id": tool_invocation_id,
                        "tool_attempt_id": tool_execution_id,
                        "tool_name": tool_name,
                        "status": normalized_status,
                        "outcome": normalized_outcome,
                        "completion_reason": "reconciled_tool_message",
                        "result": result,
                    }
                    if error is not None:
                        payload["error"] = error
                    await self.writer.commit(
                        "tool.completed",
                        payload,
                        model_call_id=model_call_id,
                        tool_execution_id=tool_execution_id,
                        tool_call_id=tool_call_id,
                        tool_invocation_id=tool_invocation_id,
                        tool_attempt_id=tool_execution_id,
                    )
                    self._active_tool_executions.pop(tool_execution_id, None)
                    self._completed_tool_execution_ids.add(tool_execution_id)
                    return
                raise RuntimeError(
                    "同一 tool_call_id 绑定了多个已完成 tool attempt: "
                    f"tool_call_id={tool_call_id} "
                    f"previous={previous_execution_id} current={tool_execution_id}"
                )
            normalized_status = "completed" if status == "succeeded" else status
            normalized_outcome = outcome or (
                "success" if normalized_status == "completed" else "provider_error"
            )
            if normalized_outcome == "unknown":
                normalized_outcome = "outcome_unknown"
            payload: dict[str, object] = {
                "tool_execution_id": tool_execution_id,
                "tool_call_id": tool_call_id,
                "tool_invocation_id": self._tool_invocation_id_for(tool_call_id),
                "tool_attempt_id": tool_execution_id,
                "tool_name": tool_name,
                "status": normalized_status,
                "outcome": normalized_outcome,
                "completion_reason": (
                    "tool_completed"
                    if normalized_outcome == "success"
                    else "provider_failed"
                ),
                "result": result,
            }
            if error is not None:
                payload["error"] = error
            model_call_id = self._tool_call_model_call_ids.get(tool_call_id)
            if model_call_id is None:
                model_call_id = self.current_model_call_id
                self._tool_call_model_call_ids[tool_call_id] = model_call_id
            await self.writer.commit(
                "tool.completed",
                payload,
                model_call_id=model_call_id,
                tool_execution_id=tool_execution_id,
                tool_call_id=tool_call_id,
                tool_invocation_id=str(payload["tool_invocation_id"]),
                tool_attempt_id=tool_execution_id,
            )
            if (
                persist_canonical
                and self._canonical_item_sink is not None
                and self._canonical_turn_id is not None
            ):
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
                # 工具执行失败与结果记录是否完整是两种事实；这里已经收到完整
                # 终态通知，执行结果只由 tool_outcome 表达。
                # TODO：由 Saver 的工具终态端口创建 item；此处最终只传递事件与引用。
                result_item = CanonicalItemRecord.create(
                    item_sequence=1,
                    item_id=(
                        f"item-{tool_execution_id}-result-"
                        f"{tool_call_id}"
                    ),
                    semantic_kind="tool_result",
                    payload_kind="tool_result",
                    status=CanonicalItemStatus.COMPLETED.value,
                    producer_ref={
                        "producer_kind": "tool",
                        "producer_id": tool_execution_id,
                        "invocation_id": model_call_id,
                    },
                    payload={
                        "tool_call_id": tool_call_id,
                        "tool_invocation_id": payload["tool_invocation_id"],
                        "tool_attempt_id": tool_execution_id,
                        "result_id": tool_execution_id,
                        "name": tool_name,
                        "content": result,
                        "tool_outcome": result_outcome,
                    },
                    metadata={
                        "execution_id": tool_execution_id,
                        "model_call_id": model_call_id,
                        "tool_execution_id": tool_execution_id,
                        "tool_call_id": tool_call_id,
                        "tool_invocation_id": payload["tool_invocation_id"],
                        "tool_attempt_id": tool_execution_id,
                        "execution_confirmed": result_outcome != "unknown",
                    },
                    turn_id=self._canonical_turn_id,
                    turn_scope="turn_member",
                    message_group_id=f"message-{tool_execution_id}",
                    wire_role="tool",
                )
                await self._canonical_item_sink((result_item,))
            self._active_tool_executions.pop(tool_execution_id, None)
            self._completed_tool_execution_ids.add(tool_execution_id)
            self._completed_tool_result_call_ids.add(tool_call_id)
            self._completed_tool_result_execution_by_call_id[tool_call_id] = (
                tool_execution_id
            )

    async def complete_tool_from_message(self, message: BaseMessage) -> None:
        """在模型请求边界闭合已进入请求的 ToolMessage。

        某些 LangGraph 事件流会在 ``on_tool_end`` 外层事件之前启动下一次
        model call。请求中的 ToolMessage 已经是 provider 要消费的事实，此处
        使用已登记的 tool execution 先提交同一完成事实；稍后到达的
        ``on_tool_end`` 通过 execution identity 幂等返回。
        """
        if not isinstance(message, ToolMessage):
            return
        tool_call_id = message.tool_call_id
        matches = [
            (execution_id, tool_name)
            for execution_id, (active_call_id, tool_name) in self._active_tool_executions.items()
            if active_call_id == tool_call_id
            or self._provider_tool_call_ids_by_id.get(active_call_id)
            == tool_call_id
        ]
        if not matches:
            pending_matches = [
                pending_call_id
                for pending_call_id in self._tool_call_order
                if pending_call_id not in self._claimed_tool_call_ids
                and pending_call_id not in self._completed_tool_call_ids
                and (
                    pending_call_id == tool_call_id
                    or self._provider_tool_call_ids_by_id.get(pending_call_id)
                    == tool_call_id
                )
            ]
            if len(pending_matches) > 1:
                raise RuntimeError(
                    "请求边界的 ToolMessage 无法唯一关联 pending tool call: "
                    f"tool_call_id={tool_call_id} candidates={pending_matches}"
                )
            if pending_matches:
                pending_call_id = pending_matches[0]
                self._claimed_tool_call_ids.add(pending_call_id)
                tool_name = self._tool_call_names_by_id.get(pending_call_id)
                if not isinstance(tool_name, str) or not tool_name:
                    raise RuntimeError(
                        "pending tool call 缺少工具名称，无法从 ToolMessage reconciliation: "
                        f"tool_call_id={pending_call_id}"
                    )
                execution_id = (
                    message.id
                    if isinstance(message.id, str) and message.id
                    else f"reconciled:{pending_call_id}"
                )
                self._reconciled_tool_call_ids.add(pending_call_id)
                matches = [(execution_id, tool_name)]
        for execution_id, tool_name in matches:
            content = message.content
            if isinstance(content, str):
                result = content
            elif isinstance(content, Sequence):
                result = "".join(
                    str(
                        block.get("text", block.get("content", ""))
                        if isinstance(block, Mapping)
                        else block
                    )
                    for block in content
                )
            else:
                result = str(content)
            status = "succeeded" if message.status == "success" else "failed"
            await self.complete_tool(
                tool_execution_id=execution_id,
                tool_call_id=tool_call_id,
                tool_name=message.name or tool_name,
                status=status,
                result=result,
                error=result if status == "failed" else None,
                outcome="success" if status == "succeeded" else "failure",
                # ToolMessage 已经是请求边界的 canonical 输入；下一次
                # _prepare 会将它写入唯一的 checkpoint carrier。这里仅
                # 收口 runtime/trace 生命周期，不能再写一个 stream result。
                persist_canonical=False,
            )
