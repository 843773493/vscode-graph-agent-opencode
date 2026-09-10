"""实时 provider block/delta 的 canonicalization owner。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from langchain_core.messages import AIMessageChunk

from app.services.infrastructure.rollout_context.runtime.stream_accumulator import (
    CanonicalBlockAccumulator,
)
from app.services.mapping.agent_content_mapper import extract_reasoning_summary


class StreamBlockAssemblyMixin:
    async def start_model(self, model_call_id: str, model: str) -> None:
        if self.current_model_call_id is not None and not self._model_completed:
            await self.finish_model()
            await self.complete_model(
                outcome="accepted",
                reason="模型完成工具循环并进入下一次调用",
            )
        next_attempt = self.current_attempt + 1
        # model.started 必须以持久化登记成功为前提。否则失败收口会拿一个从未
        # 登记的 model_call_id 更新 outcome，覆盖原始异常并把消息流留在 open。
        if self._model_call_registrar is not None:
            await self._model_call_registrar(
                model_call_id,
                next_attempt,
                model,
            )
        self.current_attempt = next_attempt
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
        # provider delta 会先于 astream_events 的 on_tool_start 到达；下一次
        # ModelCall 可能已经开始，而上一调用的工具事件仍在队列中。工具 identity
        # 因此属于整个 Turn，只有 index->id 是单次 ModelCall 的临时映射。
        if (
            self._canonical_item_sink is not None
            and self._canonical_turn_id is not None
        ):
            self._canonical_block_accumulator = CanonicalBlockAccumulator(
                turn_id=self._canonical_turn_id,
                execution_id=model_call_id,
                producer_id=model_call_id,
            )
        else:
            self._canonical_block_accumulator = None
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
        raw_block_index = block.get("index")
        if raw_block_index is None:
            block_index = len(self._active_block_order)
        elif (
            not isinstance(raw_block_index, int)
            or isinstance(raw_block_index, bool)
            or raw_block_index < 0
        ):
            raise RuntimeError("content block.index 必须是非负整数")
        else:
            block_index = raw_block_index
        raw_carrier_type = block.get("type", "text")
        if not isinstance(raw_carrier_type, str) or not raw_carrier_type:
            raise RuntimeError("content block.type 必须是非空字符串")
        carrier_type = raw_carrier_type
        raw_block_id = block.get("id")
        if raw_block_id is not None and (
            not isinstance(raw_block_id, str) or not raw_block_id
        ):
            raise RuntimeError("content block.id 必须是非空字符串或缺省")
        block_id = (
            raw_block_id
            if isinstance(raw_block_id, str) and raw_block_id
            else self._fallback_block_id(carrier_type, block_index)
        )
        if block_id not in self._active_blocks:
            for previous_block_id in tuple(self._active_block_order):
                if (
                    previous_block_id != block_id
                    and previous_block_id in self._active_blocks
                ):
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
                        self._accept_canonical_block(payload)
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
            self._accept_canonical_block(payload)
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
            self._accept_canonical_block(payload)
            if carrier_type in {"text", "output_text", "refusal"}:
                self._normalized_text_by_block[block_id] = (
                    self._normalized_text_by_block.get(block_id, "") + text
                )
                self._normalized_carrier_by_block[block_id] = carrier_type

    async def _accept_tool_call_chunk(self, chunk: Mapping[str, Any]) -> None:
        raw_tool_index = chunk.get("index", 0)
        if raw_tool_index is None:
            raw_tool_index = 0
        if (
            not isinstance(raw_tool_index, int)
            or isinstance(raw_tool_index, bool)
            or raw_tool_index < 0
        ):
            raise RuntimeError("tool call chunk.index 必须是非负整数")
        tool_index = raw_tool_index
        raw_tool_call_id = chunk.get("id")
        tool_call_id = (
            raw_tool_call_id
            if isinstance(raw_tool_call_id, str) and raw_tool_call_id
            else self._tool_call_ids_by_index.get(tool_index)
            or self._fallback_block_id("tool_call", tool_index)
        )
        self._tool_call_ids_by_index[tool_index] = tool_call_id
        if tool_call_id not in self._tool_call_order:
            self._tool_call_order.append(tool_call_id)
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
        self._accept_canonical_block(
            {
                "block_id": tool_call_id,
                "block_index": tool_index,
                "carrier_type": "tool_call",
                "args": arguments,
                "name": self._tool_call_names_by_id.get(tool_call_id, ""),
                "tool_call_id": tool_call_id,
            }
        )

    def _fallback_block_id(self, carrier_type: str, block_index: int) -> str:
        model_call_id = self.current_model_call_id or "unknown-model-call"
        return f"{model_call_id}:{carrier_type}:{block_index}"

    def _accept_canonical_block(self, payload: Mapping[str, object]) -> None:
        accumulator = self._canonical_block_accumulator
        if accumulator is not None:
            accumulator.accept(payload)

    def _next_local_seq(self, block_id: str) -> int:
        next_seq = self._block_local_seq.get(block_id, 0)
        self._block_local_seq[block_id] = next_seq + 1
        return next_seq

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
