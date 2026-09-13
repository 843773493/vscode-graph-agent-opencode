"""模型/工具流的 terminal、retry 与 interruption 状态 owner。"""

from __future__ import annotations

from app.domain.itemized.enums import CanonicalItemStatus


class StreamTerminalStateMixin:
    async def finish_model(
        self,
        *,
        completion_reason: str = "upstream_completed",
        partial: bool = False,
    ) -> None:
        async with self._stream_state_lock:
            await self._finish_model(
                completion_reason=completion_reason,
                partial=partial,
            )

    async def _finish_model(
        self,
        *,
        completion_reason: str = "upstream_completed",
        partial: bool = False,
    ) -> None:
        model_call_id = self.current_model_call_id
        if model_call_id is not None:
            self._completed_model_call_ids.add(model_call_id)
        for block_id in tuple(self._active_block_order):
            if block_id not in self._active_blocks:
                continue
            block_model_call_id = self._block_model_call_ids.get(block_id)
            if (
                model_call_id is not None
                and block_model_call_id is not None
                and block_model_call_id != model_call_id
            ):
                continue
            await self._close_block(
                block_id,
                completion_reason=completion_reason,
                partial=partial,
                model_call_id=block_model_call_id or model_call_id,
            )
        accumulator = (
            self._canonical_block_accumulators.pop(model_call_id, None)
            if model_call_id is not None
            else self._canonical_block_accumulator
        )
        if accumulator is not None and self._canonical_item_sink is not None:
            status = (
                CanonicalItemStatus.PARTIAL.value
                if partial
                else CanonicalItemStatus.FAILED.value
                if completion_reason == "provider_failed"
                else CanonicalItemStatus.COMPLETED.value
            )
            items = accumulator.finalize(first_item_sequence=1, status=status)
            if items:
                await self._canonical_item_sink(items)
        if model_call_id is not None:
            # 这是 canonical storage 的不可变 terminal 边界。之后即使 provider
            # callback 迟到，也只能进入消息流/trace，不能再创建同一 item_id。
            self._sealed_canonical_model_call_ids.add(model_call_id)
        # on_chat_model_end 已经封存当前 accumulator；下一次 model start
        # 可能仍先调用一次 finish_model 来完成旧调用的控制事件。清空引用
        # 使该次生命周期收尾幂等，避免同一 block identity 被重新生成并与
        # immutable item catalog 发生 payload/created_at 冲突。
        if accumulator is self._canonical_block_accumulator:
            self._canonical_block_accumulator = None
        retained_block_order: list[str] = []
        for block_id in self._active_block_order:
            block_model_call_id = self._block_model_call_ids.get(block_id)
            if (
                model_call_id is not None
                and block_model_call_id is not None
                and block_model_call_id != model_call_id
            ):
                retained_block_order.append(block_id)
                continue
            self._active_blocks.discard(block_id)
            self._closing_blocks.discard(block_id)
            self._block_metadata.pop(block_id, None)
            self._block_local_seq.pop(block_id, None)
            self._block_model_call_ids.pop(block_id, None)
        self._active_block_order = retained_block_order

    async def complete_model(self, *, outcome: str, reason: str | None = None) -> None:
        async with self._stream_state_lock:
            await self._complete_model(outcome=outcome, reason=reason)

    async def _complete_model(self, *, outcome: str, reason: str | None = None) -> None:
        if self.current_model_call_id is None:
            return
        # provider delta hook 与 LangChain on_chat_model_end 可能短暂乱序；
        # model.completed 是最后的 model-call 收敛边界，必须在写入它之前
        # 再次关闭迟到 delta 创建的 block，才能允许 stream.completed 原子
        # 校验通过，同时保留这些 delta 的 UI/trace 事实。
        await self._finish_model()
        payload: dict[str, object] = {
            "model_call_id": self.current_model_call_id,
            "attempt": self.current_attempt,
            "outcome": outcome,
        }
        if reason is not None:
            payload["reason"] = reason
        await self.writer.commit(
            "model.completed",
            payload,
            model_call_id=self.current_model_call_id,
        )
        if self._model_call_outcome_sink is not None:
            normalized_outcome = (
                "completed"
                if outcome in {"accepted", "completed", "success"}
                else "cancelled"
                if outcome in {"cancelled", "user_interrupt"}
                else "interrupted"
                if outcome in {"interrupted", "timeout"}
                else "failed"
            )
            await self._model_call_outcome_sink(
                self.current_model_call_id,
                normalized_outcome,
            )
        self._model_completed = True

    async def retrying(self, reason: str) -> None:
        if self.current_model_call_id is None:
            return
        await self.writer.commit(
            "model.retrying",
            {
                "model_call_id": self.current_model_call_id,
                "attempt": self.current_attempt,
                "reason": reason,
            },
            model_call_id=self.current_model_call_id,
        )

    async def fail_model(
        self,
        *,
        code: str,
        message: str,
        outcome: str = "upstream_error",
        retryable: bool = True,
    ) -> None:
        async with self._stream_state_lock:
            await self._fail_model(
                code=code,
                message=message,
                outcome=outcome,
                retryable=retryable,
            )

    async def _fail_model(
        self,
        *,
        code: str,
        message: str,
        outcome: str = "upstream_error",
        retryable: bool = True,
    ) -> None:
        if self.current_model_call_id is None or self._model_completed:
            return
        await self._finish_model(
            completion_reason=(
                "user_interrupt" if outcome == "user_interrupt" else "provider_failed"
            ),
            partial=outcome == "user_interrupt",
        )
        await self.writer.commit(
            "model.failed",
            {
                "model_call_id": self.current_model_call_id,
                "attempt": self.current_attempt,
                "outcome": outcome,
                "error_code": code,
                "message": message,
                "retryable": retryable,
            },
            model_call_id=self.current_model_call_id,
        )
        if self._model_call_outcome_sink is not None:
            await self._model_call_outcome_sink(
                self.current_model_call_id,
                "cancelled" if outcome == "user_interrupt" else "failed",
            )
        self._model_completed = True

    async def finalize_interruption_facts(self) -> None:
        async with self._stream_state_lock:
            await self._finalize_interruption_facts()

    async def _finalize_interruption_facts(self) -> None:
        """在线性化的 interrupt.requested 后闭合所有已知运行时事实。"""
        if self._interruption_facts_finalized:
            return
        self._interruption_facts_finalized = True
        await self._finish_model(
            completion_reason="user_interrupt",
            partial=True,
        )
        for tool_call_id in tuple(self._tool_call_ids_by_index.values()):
            if tool_call_id in self._completed_tool_call_ids:
                continue
            tool_invocation_id = self._tool_invocation_id_for(tool_call_id)
            arguments_complete = self._tool_call_arguments_complete.get(
                tool_call_id,
                False,
            )
            await self.writer.commit(
                "tool_call.completed",
                {
                    "tool_call_id": tool_call_id,
                    "tool_invocation_id": tool_invocation_id,
                    "tool_name": self._tool_call_names_by_id.get(tool_call_id, ""),
                    "status": "cancelled" if arguments_complete else "incomplete",
                    "completion_reason": "user_interrupt",
                    "arguments_complete": arguments_complete,
                },
                model_call_id=self.current_model_call_id,
                tool_call_id=tool_call_id,
                tool_invocation_id=tool_invocation_id,
            )
            self._completed_tool_call_ids.add(tool_call_id)
        for tool_execution_id, (tool_call_id, tool_name) in tuple(
            self._active_tool_executions.items()
        ):
            await self.complete_tool(
                tool_execution_id=tool_execution_id,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                status="completed",
                outcome="unknown",
                result="",
                error="用户中断时工具结果无法确认",
            )
        await self._fail_model(
            code="user_interrupt",
            message="用户请求中断当前模型调用",
            outcome="user_interrupt",
            retryable=False,
        )
