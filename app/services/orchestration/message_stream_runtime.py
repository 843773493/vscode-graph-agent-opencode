from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from langchain_core.messages import AIMessageChunk

from app.core.job_event_bus import EventType
from app.services.infrastructure.message_stream_store import MessageStreamWriter
from app.services.mapping.agent_content_mapper import extract_reasoning_summary
from app.services.orchestration.activity_runtime import (
    ActivityHandlerRegistry,
    ActivityRuntime,
)

NormalizedBlockObserver = Callable[[str, Mapping[str, object]], Awaitable[None]]


class MessageStreamTraceObserver:
    """将已提交的规范化 block 投影为旧 Trace 的诊断事件。

    该投影只服务事件/诊断视图，聊天主时间线不再消费这些事件。
    """

    _TEXT_CARRIERS = frozenset({"text", "output_text", "refusal"})
    _REASONING_CARRIERS = frozenset(
        {"reasoning", "reasoning_content", "thinking"}
    )

    def __init__(self, publish: Callable[[str, dict[str, Any]], Awaitable[None]]) -> None:
        self._publish = publish
        self._parts: dict[str, dict[str, object]] = {}

    async def observe(
        self,
        event_type: str,
        payload: Mapping[str, object],
    ) -> None:
        if event_type == "block.started":
            carrier_type = str(payload.get("carrier_type") or "text")
            kind = self._trace_kind(carrier_type)
            if kind is None:
                return
            block_id = self._required_block_id(payload)
            self._parts[block_id] = {
                "kind": kind,
                "text": "",
                "carrier_type": carrier_type,
                "content_block_index": payload.get("block_index", 0),
            }
            await self._publish(
                EventType.TEXT_START,
                self._trace_payload(block_id, self._parts[block_id]),
            )
            return

        block_id = self._required_block_id(payload)
        part = self._parts.get(block_id)
        if part is None:
            return
        if event_type == "block.delta":
            if payload.get("operation") != "append":
                return
            text = payload.get("text")
            if not isinstance(text, str) or not text:
                return
            part["text"] = str(part.get("text") or "") + text
            await self._publish(
                EventType.TEXT_DELTA,
                {
                    **self._trace_payload(block_id, part),
                    "text": text,
                },
            )
            return
        if event_type == "block.completed":
            await self._publish(
                EventType.TEXT_END,
                {
                    **self._trace_payload(block_id, part),
                    "text": str(part.get("text") or ""),
                },
            )
            del self._parts[block_id]

    @classmethod
    def _trace_kind(cls, carrier_type: str) -> str | None:
        if carrier_type in cls._TEXT_CARRIERS:
            return "markdown"
        if carrier_type in cls._REASONING_CARRIERS:
            return "reasoning"
        return None

    @staticmethod
    def _required_block_id(payload: Mapping[str, object]) -> str:
        block_id = payload.get("block_id")
        if not isinstance(block_id, str) or not block_id:
            raise RuntimeError("规范化 block 事件缺少 block_id")
        return block_id

    @staticmethod
    def _trace_payload(
        block_id: str,
        part: Mapping[str, object],
    ) -> dict[str, object]:
        return {
            "part_id": block_id,
            "kind": part["kind"],
            "carrier_type": part["carrier_type"],
            "content_block_index": part["content_block_index"],
        }


class MessageStreamRuntime:
    """把模型 chunk 和 AgentLoop 生命周期提交到同一个消息流 writer。"""

    def __init__(
        self,
        writer: MessageStreamWriter,
        *,
        normalized_block_observer: NormalizedBlockObserver | None = None,
        activity_registry: ActivityHandlerRegistry | None = None,
    ) -> None:
        self.writer = writer
        self.activities = ActivityRuntime(
            writer,
            activity_registry or ActivityHandlerRegistry(),
        )
        self._normalized_block_observer = normalized_block_observer
        self.current_model_call_id: str | None = None
        self.current_attempt = 0
        self._active_blocks: set[str] = set()
        self._closing_blocks: set[str] = set()
        self._active_block_order: list[str] = []
        self._block_metadata: dict[str, tuple[int, str]] = {}
        self._block_local_seq: dict[str, int] = {}
        self._normalized_text_by_block: dict[str, str] = {}
        self._normalized_carrier_by_block: dict[str, str] = {}
        self._tool_call_ids_by_index: dict[int, str] = {}
        self._tool_call_names_by_id: dict[str, str] = {}
        self._tool_call_arguments: dict[str, str] = {}
        self._tool_call_arguments_by_id: dict[str, dict[str, object]] = {}
        self._tool_call_arguments_complete: dict[str, bool] = {}
        self._claimed_tool_call_ids: set[str] = set()
        self._started_tool_execution_ids: set[str] = set()
        self._active_tool_executions: dict[str, tuple[str, str]] = {}
        self._model_completed = False
        self._interruption_facts_finalized = False

    async def start_model(self, model_call_id: str, model: str) -> None:
        if self.current_model_call_id is not None and not self._model_completed:
            await self.finish_model()
            await self.complete_model(
                outcome="accepted",
                reason="模型完成工具循环并进入下一次调用",
            )
        self.current_attempt += 1
        self.current_model_call_id = model_call_id
        self._model_completed = False
        self._interruption_facts_finalized = False
        self._active_blocks.clear()
        self._closing_blocks.clear()
        self._active_block_order.clear()
        self._block_metadata.clear()
        self._block_local_seq.clear()
        self._normalized_text_by_block.clear()
        self._normalized_carrier_by_block.clear()
        self._tool_call_ids_by_index.clear()
        self._tool_call_names_by_id.clear()
        self._tool_call_arguments.clear()
        self._tool_call_arguments_by_id.clear()
        self._tool_call_arguments_complete.clear()
        self._claimed_tool_call_ids.clear()
        self._started_tool_execution_ids.clear()
        self._active_tool_executions.clear()
        await self.writer.commit(
            "model.started",
            {
                "model_call_id": model_call_id,
                "attempt": self.current_attempt,
                "model": model,
            },
            model_call_id=model_call_id,
        )

    async def accept_message_chunk(self, chunk: AIMessageChunk) -> None:
        content = getattr(chunk, "content", None)
        if isinstance(content, list):
            for block in content:
                if isinstance(block, Mapping):
                    await self._accept_content_block(block)
        elif isinstance(content, str) and content:
            await self._accept_content_block(
                {
                    "id": self._fallback_block_id("text", 0),
                    "index": 0,
                    "type": "text",
                    "text": content,
                }
            )
        raw_tool_chunks = getattr(chunk, "tool_call_chunks", None) or []
        for raw_tool_chunk in raw_tool_chunks:
            if isinstance(raw_tool_chunk, Mapping):
                await self._accept_tool_call_chunk(raw_tool_chunk)

    async def _accept_content_block(self, block: Mapping[str, Any]) -> None:
        block_index = block.get("index")
        if not isinstance(block_index, int):
            block_index = len(self._active_block_order)
        carrier_type = str(block.get("type") or "text")
        raw_block_id = block.get("id")
        block_id = (
            raw_block_id
            if isinstance(raw_block_id, str) and raw_block_id
            else self._fallback_block_id(carrier_type, block_index)
        ) 
        if block_id not in self._active_blocks:
            for previous_block_id in tuple(self._active_block_order):
                if previous_block_id != block_id and previous_block_id in self._active_blocks:
                    await self._close_block(
                        previous_block_id,
                        completion_reason="carrier_switched",
                        partial=False,
                    )
            existing_metadata = self._block_metadata.get(block_id)
            if existing_metadata is not None:
                # 同一 provider id 在前一个 block 已闭合后再次出现，必须创建
                # 新的公共 block 身份，避免把两个 carrier 区段重新拼成一个块。
                block_id = f"{block_id}:segment:{len(self._block_metadata)}"
                existing_metadata = None
            if existing_metadata is not None and existing_metadata != (
                block_index,
                carrier_type,
            ):
                raise RuntimeError(
                    "同一模型调用中的 block 身份发生冲突: "
                    f"block_id={block_id} existing={existing_metadata!r} "
                    f"incoming={(block_index, carrier_type)!r}"
                )
            self._active_blocks.add(block_id)
            if existing_metadata is None:
                self._active_block_order.append(block_id)
                self._block_metadata[block_id] = (block_index, carrier_type)
                self._block_local_seq[block_id] = 0
                payload = {
                    "block_id": block_id,
                    "block_index": block_index,
                    "carrier_type": carrier_type,
                    "projection": "streaming",
                }
                await self.writer.commit(
                    "block.started",
                    payload,
                    model_call_id=self.current_model_call_id,
                    block_id=block_id,
                )
                await self._observe("block.started", payload)

        if carrier_type == "reasoning_items":
            items = block.get("reasoning_items")
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, Mapping):
                        payload = {
                            "block_id": block_id,
                            "block_index": block_index,
                            "local_seq": self._next_local_seq(block_id),
                            "carrier_type": carrier_type,
                            "operation": "item_upsert",
                            "item": dict(item),
                        }
                        await self.writer.commit(
                            "block.delta",
                            payload,
                            model_call_id=self.current_model_call_id,
                            block_id=block_id,
                        )
                        await self._observe("block.delta", payload)
            return

        if carrier_type == "redacted_thinking":
            payload = {
                "block_id": block_id,
                "block_index": block_index,
                "carrier_type": carrier_type,
                "operation": "redacted",
                "redacted": True,
            }
            await self.writer.commit(
                "block.delta",
                payload,
                model_call_id=self.current_model_call_id,
                block_id=block_id,
            )
            await self._observe("block.delta", payload)
            return

        text = block.get("text")
        if not isinstance(text, str) and carrier_type == "reasoning":
            nested_content = block.get("content")
            if isinstance(nested_content, list):
                text = extract_reasoning_summary(nested_content)
        if not isinstance(text, str):
            for key in ("reasoning_content", "reasoning", "thinking"):
                value = block.get(key)
                if isinstance(value, str):
                    text = value
                    break
        if isinstance(text, str) and text:
            payload = {
                "block_id": block_id,
                "block_index": block_index,
                "local_seq": self._next_local_seq(block_id),
                "carrier_type": carrier_type,
                "operation": "append",
                "text": text,
            }
            await self.writer.commit(
                "block.delta",
                payload,
                model_call_id=self.current_model_call_id,
                block_id=block_id,
            )
            await self._observe("block.delta", payload)
            if carrier_type in {"text", "output_text", "refusal"}:
                self._normalized_text_by_block[block_id] = (
                    self._normalized_text_by_block.get(block_id, "") + text
                )
                self._normalized_carrier_by_block[block_id] = carrier_type

    async def _accept_tool_call_chunk(self, chunk: Mapping[str, Any]) -> None:
        tool_index = int(chunk.get("index") or 0)
        raw_tool_call_id = chunk.get("id")
        tool_call_id = (
            raw_tool_call_id
            if isinstance(raw_tool_call_id, str) and raw_tool_call_id
            else self._tool_call_ids_by_index.get(tool_index)
            or self._fallback_block_id("tool_call", tool_index)
        )
        self._tool_call_ids_by_index[tool_index] = tool_call_id
        raw_tool_name = chunk.get("name")
        if isinstance(raw_tool_name, str) and raw_tool_name:
            self._tool_call_names_by_id[tool_call_id] = raw_tool_name
        raw_args = chunk.get("args")
        if isinstance(raw_args, str):
            self._tool_call_arguments[tool_call_id] = (
                self._tool_call_arguments.get(tool_call_id, "") + raw_args
            )
        accumulated_args = self._tool_call_arguments.get(tool_call_id, "")
        arguments: dict[str, object]
        if accumulated_args:
            try:
                parsed = json.loads(accumulated_args)
            except json.JSONDecodeError:
                parsed = {"raw": accumulated_args}
            arguments = (
                parsed if isinstance(parsed, dict) else {"raw": accumulated_args}
            )
        elif isinstance(raw_args, Mapping):
            arguments = dict(raw_args)
        else:
            arguments = {}
        self._tool_call_arguments_by_id[tool_call_id] = arguments
        self._tool_call_arguments_complete[tool_call_id] = bool(
            isinstance(arguments, dict) and arguments and "raw" not in arguments
        )
        for block_id in tuple(self._active_blocks):
            await self._close_block(
                block_id,
                completion_reason="carrier_switched",
                partial=False,
            )
        await self.writer.commit(
            "tool_call.delta",
            {
                "tool_call_id": tool_call_id,
                "tool_name": self._tool_call_names_by_id.get(tool_call_id, ""),
                "arguments": arguments,
                "status": "accumulating",
                "arguments_complete": self._tool_call_arguments_complete[tool_call_id],
            },
            model_call_id=self.current_model_call_id,
        )

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
        self._active_blocks.clear()
        self._closing_blocks.clear()
        self._active_block_order.clear()
        self._block_metadata.clear()
        self._block_local_seq.clear()

    async def complete_model(self, *, outcome: str, reason: str | None = None) -> None:
        if self.current_model_call_id is None:
            return
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

    async def start_tool(
        self,
        *,
        tool_execution_id: str,
        tool_call_id: str,
        tool_name: str,
    ) -> None:
        self._started_tool_execution_ids.add(tool_execution_id)
        self._active_tool_executions[tool_execution_id] = (tool_call_id, tool_name)
        await self.writer.commit(
            "tool.started",
            {
                "tool_execution_id": tool_execution_id,
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
            },
            model_call_id=self.current_model_call_id,
            tool_execution_id=tool_execution_id,
        )

    def claim_tool_call_id(
        self,
        tool_name: str,
        tool_args: Mapping[str, object] | None = None,
    ) -> str | None:
        """将 AgentLoop 的工具执行关联到最近尚未消费的模型工具调用。"""
        candidates: list[str] = []
        for tool_index in reversed(tuple(self._tool_call_ids_by_index)):
            tool_call_id = self._tool_call_ids_by_index[tool_index]
            if tool_call_id in self._claimed_tool_call_ids:
                continue
            if self._tool_call_names_by_id.get(tool_call_id) != tool_name:
                continue
            candidates.append(tool_call_id)
        if tool_args is not None:
            normalized_tool_args = dict(tool_args)
            for tool_call_id in candidates:
                provider_arguments = self._tool_call_arguments_by_id.get(
                    tool_call_id,
                    {},
                )
                if provider_arguments == normalized_tool_args:
                    self._claimed_tool_call_ids.add(tool_call_id)
                    return tool_call_id
                nested_tool_name = provider_arguments.get("tool_name")
                nested_arguments = provider_arguments.get("arguments")
                if (
                    nested_tool_name == tool_name
                    and isinstance(nested_arguments, Mapping)
                    and dict(nested_arguments) == normalized_tool_args
                ):
                    self._claimed_tool_call_ids.add(tool_call_id)
                    return tool_call_id
        if candidates:
            tool_call_id = candidates[0]
            self._claimed_tool_call_ids.add(tool_call_id)
            return tool_call_id
        return None

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
    ) -> None:
        normalized_status = "completed" if status == "succeeded" else status
        normalized_outcome = outcome or (
            "success" if normalized_status == "completed" else "provider_error"
        )
        payload: dict[str, object] = {
            "tool_execution_id": tool_execution_id,
            "tool_call_id": tool_call_id,
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
        await self.writer.commit(
            "tool.completed",
            payload,
            model_call_id=self.current_model_call_id,
            tool_execution_id=tool_execution_id,
        )
        self._active_tool_executions.pop(tool_execution_id, None)

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
            if tool_call_id in self._started_tool_execution_ids:
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
        for tool_execution_id, (tool_call_id, tool_name) in tuple(
            self._active_tool_executions.items()
        ):
            await self.complete_tool(
                tool_execution_id=tool_execution_id,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                status="completed",
                outcome="outcome_unknown",
                result="",
                error="用户中断时工具结果无法确认",
            )
        await self.fail_model(
            code="user_interrupt",
            message="用户请求中断当前模型调用",
            outcome="user_interrupt",
            retryable=False,
        )

    def _fallback_block_id(self, carrier_type: str, block_index: int) -> str:
        model_call_id = self.current_model_call_id or "unknown-model-call"
        return f"{model_call_id}:{carrier_type}:{block_index}"

    def _next_local_seq(self, block_id: str) -> int:
        next_seq = self._block_local_seq.get(block_id, 0)
        self._block_local_seq[block_id] = next_seq + 1
        return next_seq

    async def _observe(
        self,
        event_type: str,
        payload: Mapping[str, object],
    ) -> None:
        if self._normalized_block_observer is not None:
            await self._normalized_block_observer(event_type, payload)

    async def _close_block(
        self,
        block_id: str,
        *,
        completion_reason: str,
        partial: bool,
    ) -> None:
        if block_id not in self._active_blocks or block_id in self._closing_blocks:
            return
        self._closing_blocks.add(block_id)
        block_index, carrier_type = self._block_metadata[block_id]
        payload = {
            "block_id": block_id,
            "block_index": block_index,
            "carrier_type": carrier_type,
            "status": "completed",
            "completion_reason": completion_reason,
            "partial": partial,
        }
        try:
            await self.writer.commit(
                "block.completed",
                payload,
                model_call_id=self.current_model_call_id,
                block_id=block_id,
            )
            await self._observe("block.completed", payload)
        finally:
            self._active_blocks.discard(block_id)
            self._closing_blocks.discard(block_id)

    def normalized_final_text(self) -> str:
        """返回当前 ModelCall 规范化文本，用于与最终聚合结果做诊断对账。"""
        return "".join(
            self._normalized_text_by_block[block_id]
            for block_id in self._normalized_text_by_block
            if self._normalized_carrier_by_block.get(block_id)
            in {"text", "output_text", "refusal"}
        )
