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
        for block_id in tuple(self._active_block_order):
            if block_id not in self._active_blocks:
                continue
            await self._close_block(
                block_id,
                completion_reason=completion_reason,
                partial=partial,
            )
        accumulator = self._canonical_block_accumulator
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
        # on_chat_model_end 已经封存当前 accumulator；下一次 model start
        # 可能仍先调用一次 finish_model 来完成旧调用的控制事件。清空引用
        # 使该次生命周期收尾幂等，避免同一 block identity 被重新生成并与
        # immutable item catalog 发生 payload/created_at 冲突。
        self._canonical_block_accumulator = None
        self._active_blocks.clear()
        self._closing_blocks.clear()
        self._active_block_order.clear()
        self._block_metadata.clear()
        self._block_local_seq.clear()

    async def complete_model(self, *, outcome: str, reason: str | None = None) -> None:
        if self.current_model_call_id is None:
            return
        # provider delta hook 与 LangChain on_chat_model_end 可能短暂乱序；
        # model.completed 是最后的 model-call 收敛边界，必须在写入它之前
        # 再次关闭迟到 delta 创建的 block，才能允许 stream.completed 原子
        # 校验通过，同时保留这些 delta 的 UI/trace 事实。
        await self.finish_model()
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
        if self.current_model_call_id is None or self._model_completed:
            return
        await self.finish_model(
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
        """在线性化的 interrupt.requested 后闭合所有已知运行时事实。"""
        if self._interruption_facts_finalized:
            return
        self._interruption_facts_finalized = True
        await self.finish_model(
            completion_reason="user_interrupt",
            partial=True,
        )
        for tool_call_id in tuple(self._tool_call_ids_by_index.values()):
            if tool_call_id in self._completed_tool_call_ids:
                continue
            arguments_complete = self._tool_call_arguments_complete.get(
                tool_call_id,
                False,
            )
            await self.writer.commit(
                "tool_call.completed",
                {
                    "tool_call_id": tool_call_id,
                    "tool_name": self._tool_call_names_by_id.get(tool_call_id, ""),
                    "status": "cancelled" if arguments_complete else "incomplete",
                    "completion_reason": "user_interrupt",
                    "arguments_complete": arguments_complete,
                },
                model_call_id=self.current_model_call_id,
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
        await self.fail_model(
            code="user_interrupt",
            message="用户请求中断当前模型调用",
            outcome="user_interrupt",
            retryable=False,
        )
