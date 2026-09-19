"""实时 provider block/delta 的 canonicalization owner。"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from langchain_core.messages import AIMessageChunk

from app.services.infrastructure.rollout_context.runtime.stream_accumulator import (
    CanonicalBlockAccumulator,
)
from app.services.mapping.agent_content_mapper import extract_reasoning_summary


class StreamBlockAssemblyMixin:
    async def start_model(self, model_call_id: str, model: str) -> None:
        async with self._stream_state_lock:
            await self._start_model(model_call_id, model)

    async def _start_model(self, model_call_id: str, model: str) -> None:
        previous_model_call_id = self.current_model_call_id
        if self.current_model_call_id is not None and not self._model_completed:
            await self._finish_model()
            await self._complete_model(
                outcome="accepted",
                reason="模型完成工具循环并进入下一次调用",
            )
        next_attempt = self.current_attempt + 1
        # model.started 必须以持久化登记成功为前提。否则失败收口会拿一个从未
        # 登记的 model_call_id 更新 outcome，覆盖原始异常并把消息流留在 open。
        registered_execution_id: str | None = None
        if self._model_call_registrar is not None:
            registered_execution_id = await self._model_call_registrar(
                model_call_id,
                next_attempt,
                model,
            )
            if not isinstance(registered_execution_id, str) or not registered_execution_id:
                raise RuntimeError(
                    "model-call registrar 必须返回非空 execution_id: "
                    f"model_call_id={model_call_id}"
                )
            self._model_execution_ids[model_call_id] = registered_execution_id
        self.current_attempt = next_attempt
        self.current_model_call_id = model_call_id
        self._model_completed = False
        self._interruption_facts_finalized = False
        if previous_model_call_id is not None:
            for block_id, block_model_call_id in tuple(
                self._normalized_block_model_call_ids.items()
            ):
                if block_model_call_id == previous_model_call_id:
                    self._normalized_text_by_block.pop(block_id, None)
                    self._normalized_carrier_by_block.pop(block_id, None)
                    self._normalized_block_model_call_ids.pop(block_id, None)
        for block_id in self._active_block_order:
            if self._block_model_call_ids.get(block_id) is None:
                self._block_model_call_ids[block_id] = model_call_id
        # finish_model 只清理上一 model call 自己的 block。provider delta
        # 可能已经先于本次 on_chat_model_start 到达，不能在这里清空工具映射。
        # 映射按 ModelCall 隔离，既能承载乱序前置段，也不会跨调用复用 index。
        if (
            self._canonical_item_sink is not None
            and self._canonical_turn_id is not None
        ):
            accumulator = self._canonical_block_accumulators.get(model_call_id)
            if accumulator is None:
                accumulator = CanonicalBlockAccumulator(
                    turn_id=self._canonical_turn_id,
                    execution_id=registered_execution_id or model_call_id,
                    producer_id=model_call_id,
                    model_call_id=model_call_id,
                )
                self._canonical_block_accumulators[model_call_id] = accumulator
            elif registered_execution_id is not None:
                accumulator.bind_execution_id(registered_execution_id)
            self._canonical_block_accumulator = accumulator
            for block in self._pending_canonical_blocks:
                accumulator.accept(block)
            self._pending_canonical_blocks.clear()
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

    async def accept_message_chunk(
        self,
        chunk: AIMessageChunk,
        *,
        model_call_id: str | None = None,
    ) -> None:
        async with self._stream_state_lock:
            await self._accept_message_chunk(chunk, model_call_id=model_call_id)

    async def _accept_message_chunk(
        self,
        chunk: AIMessageChunk,
        *,
        model_call_id: str | None = None,
    ) -> None:
        effective_model_call_id = self._resolve_model_call_id(model_call_id)
        content = getattr(chunk, "content", None)
        if isinstance(content, list):
            for block in content:
                if isinstance(block, Mapping):
                    await self._accept_content_block(
                        block,
                        model_call_id=effective_model_call_id,
                    )
        elif isinstance(content, str) and content:
            await self._accept_content_block(
                {
                    "id": self._fallback_block_id("text", 0),
                    "index": 0,
                    "type": "text",
                    "text": content,
                },
                model_call_id=effective_model_call_id,
            )
        raw_tool_chunks = getattr(chunk, "tool_call_chunks", None) or []
        for raw_tool_chunk in raw_tool_chunks:
            if isinstance(raw_tool_chunk, Mapping):
                await self._accept_tool_call_chunk(
                    raw_tool_chunk,
                    model_call_id=effective_model_call_id,
                )

    def _resolve_model_call_id(self, model_call_id: str | None) -> str | None:
        """解析 provider delta 的模型调用归属。

        ``on_chat_model_start`` 事件提供当前模型调用的权威边界。部分 provider
        hook 在 Agent 工具循环中会复用上一层 run_id；当该 run_id 已完成且不再
        有对应 accumulator 时，迟到 delta 必须归入当前调用。尚未 start 的新
        调用如果已经有自己的 accumulator，则继续保留显式身份，覆盖合法乱序。
        """
        if model_call_id is None:
            return self.current_model_call_id
        current_model_call_id = self.current_model_call_id
        if current_model_call_id is None or model_call_id == current_model_call_id:
            return model_call_id
        if model_call_id in self._canonical_block_accumulators:
            return model_call_id
        if model_call_id in self._completed_model_call_ids:
            return current_model_call_id
        return model_call_id

    def visible_text_for_model_call(self, model_call_id: str | None = None) -> str:
        """返回消息流已经提交的当前模型调用可见正文。

        Agent 事件流的 ``model.end`` 可能先于 ``model.stream`` 被消费。调用
        方必须先看 canonical runtime，再决定是否使用 end 事件兜底，否则同一
        份正文会被第二次提交。
        """
        effective_model_call_id = model_call_id or self.current_model_call_id
        return "".join(
            self._normalized_text_by_block[block_id]
            for block_id in self._active_block_order
            if (
                self._normalized_block_model_call_ids.get(block_id)
                == effective_model_call_id
                and block_id in self._normalized_text_by_block
            )
        )

    def model_call_has_carrier(
        self,
        model_call_id: str | None,
        carrier_types: set[str],
    ) -> bool:
        """判断 canonical runtime 是否已提交指定模型调用的 carrier。"""
        effective_model_call_id = model_call_id or self.current_model_call_id
        return any(
            self._block_model_call_ids.get(block_id) == effective_model_call_id
            and self._block_metadata.get(block_id, (0, ""))[1] in carrier_types
            for block_id in self._block_metadata
        )

    async def _accept_content_block(
        self,
        block: Mapping[str, Any],
        *,
        model_call_id: str | None = None,
    ) -> None:
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
        provider_block_id = (
            raw_block_id
            if isinstance(raw_block_id, str) and raw_block_id
            else self._fallback_block_id(carrier_type, block_index)
        )
        block_id = self._scoped_block_id(
            provider_block_id,
            model_call_id=model_call_id or self.current_model_call_id,
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
                        model_call_id=model_call_id,
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
                self._block_model_call_ids[block_id] = (
                    model_call_id or self.current_model_call_id
                )
                payload = {
                    "block_id": block_id,
                    "block_index": block_index,
                    "carrier_type": carrier_type,
                    "projection": "streaming",
                }
                await self.writer.commit(
                    "block.started",
                    payload,
                    model_call_id=model_call_id or self.current_model_call_id,
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
                        self._accept_canonical_block(
                            payload,
                            model_call_id=model_call_id,
                        )
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
                model_call_id=model_call_id or self.current_model_call_id,
                block_id=block_id,
            )
            await self._observe("block.delta", payload)
            self._accept_canonical_block(payload, model_call_id=model_call_id)
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
                model_call_id=model_call_id or self.current_model_call_id,
                block_id=block_id,
            )
            await self._observe("block.delta", payload)
            self._accept_canonical_block(payload, model_call_id=model_call_id)
            if carrier_type in {"text", "output_text", "refusal"}:
                self._normalized_text_by_block[block_id] = (
                    self._normalized_text_by_block.get(block_id, "") + text
                )
                self._normalized_carrier_by_block[block_id] = carrier_type
                self._normalized_block_model_call_ids[block_id] = (
                    model_call_id or self.current_model_call_id
                )

    async def _accept_tool_call_chunk(
        self,
        chunk: Mapping[str, Any],
        *,
        model_call_id: str | None = None,
    ) -> None:
        raw_tool_index = chunk.get("index")
        if (
            raw_tool_index is not None
            and (
                not isinstance(raw_tool_index, int)
                or isinstance(raw_tool_index, bool)
                or raw_tool_index < 0
            )
        ):
            raise RuntimeError("tool call chunk.index 必须是非负整数")
        raw_tool_call_id = chunk.get("id")
        effective_model_call_id = model_call_id or self.current_model_call_id
        provider_tool_call_id = (
            raw_tool_call_id
            if isinstance(raw_tool_call_id, str) and raw_tool_call_id
            else None
        )
        provider_key = (
            (effective_model_call_id, provider_tool_call_id)
            if provider_tool_call_id is not None
            else None
        )
        existing_tool_call_id = (
            self._tool_call_ids_by_provider_key.get(provider_key)
            if provider_key is not None
            else None
        )
        tool_call_id: str | None = None

        if existing_tool_call_id is not None:
            # TODO: provider 统一输出稳定的 call index 后，可移除重复 index 的兼容分支。
            # provider ID 是比 chunk.index 更强的身份依据。某些兼容 OpenAI
            # 的 provider 会在并行调用中重复发送 index，但后续片段仍携带
            # 正确的 provider ID；此时必须回到已经登记的 canonical call。
            tool_call_id = existing_tool_call_id
            tool_index = self._tool_call_indexes_by_id[tool_call_id]
        else:
            requested_index = (
                raw_tool_index if isinstance(raw_tool_index, int) else 0
            )
            tool_index = requested_index
            existing_by_index = self._tool_call_ids_by_index.get(
                (effective_model_call_id, tool_index)
            )
            if existing_by_index is not None:
                existing_provider_tool_call_id = (
                    self._provider_tool_call_ids_by_id.get(existing_by_index)
                )
                if (
                    provider_tool_call_id is not None
                    and existing_provider_tool_call_id != provider_tool_call_id
                ):
                    # TODO: provider 修正并行 tool call 的 index 后，删除此局部
                    # index 分配，仅保留 provider ID 到 canonical identity 的映射。
                    # 这是同一模型调用中的第二个并行 tool call，不是身份
                    # 冲突。用新 index 建立局部 wire 身份，保留 provider
                    # 原始 ID，后续执行关联仍可按 provider ID 找回它。
                    tool_index = self._next_tool_call_index(effective_model_call_id)
                elif provider_tool_call_id is None:
                    tool_call_id = existing_by_index
                    tool_index = self._tool_call_indexes_by_id[tool_call_id]
                else:
                    tool_index = self._next_tool_call_index(effective_model_call_id)
            if tool_call_id is None:
                provider_tool_call_id = provider_tool_call_id or self._fallback_block_id(
                    "tool_call",
                    tool_index,
                )
                tool_call_id = self._scoped_tool_call_id(
                    provider_tool_call_id,
                    model_call_id=effective_model_call_id,
                )
        if tool_call_id is None:
            raise AssertionError("tool call chunk 未解析出 canonical tool_call_id")
        tool_call_key = (effective_model_call_id, tool_index)
        self._tool_call_ids_by_index[tool_call_key] = tool_call_id
        self._tool_call_indexes_by_id[tool_call_id] = tool_index
        if provider_tool_call_id is not None:
            self._provider_tool_call_ids_by_id[tool_call_id] = provider_tool_call_id
            self._tool_call_ids_by_provider_key[
                (effective_model_call_id, provider_tool_call_id)
            ] = tool_call_id
        if tool_call_id not in self._tool_call_model_call_ids:
            self._tool_call_model_call_ids[tool_call_id] = (
                model_call_id or self.current_model_call_id
            )
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
                model_call_id=model_call_id,
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
            model_call_id=model_call_id or self.current_model_call_id,
            tool_call_id=tool_call_id,
            tool_invocation_id=self._tool_invocation_id_for(
                tool_call_id,
                model_call_id=effective_model_call_id,
            ),
        )
        self._accept_canonical_block(
            {
                "block_id": tool_call_id,
                "block_index": tool_index,
                "carrier_type": "tool_call",
                "args": arguments,
                "name": self._tool_call_names_by_id.get(tool_call_id, ""),
                "tool_call_id": tool_call_id,
                "tool_invocation_id": self._tool_invocation_id_for(
                    tool_call_id,
                    model_call_id=effective_model_call_id,
                ),
            },
            model_call_id=model_call_id,
        )

    def _next_tool_call_index(self, model_call_id: str | None) -> int:
        index = 0
        while (model_call_id, index) in self._tool_call_ids_by_index:
            index += 1
        return index

    def _fallback_block_id(self, carrier_type: str, block_index: int) -> str:
        model_call_id = self.current_model_call_id or "unknown-model-call"
        return f"{model_call_id}:{carrier_type}:{block_index}"

    def _scoped_block_id(
        self,
        provider_block_id: str,
        *,
        model_call_id: str | None,
    ) -> str:
        """把 provider 的单次调用 block ID 提升为 Turn 内稳定身份。

        provider 可能在工具循环的下一次模型调用中复用相同的 content-block
        ID。消息流 reducer、canonical item 和前端快照都需要看到不同的实体，
        因此不能直接把 provider ID 作为 Turn 全局 block ID。
        """
        scoped_model_call_id = model_call_id or "unbound-model-call"
        return f"{scoped_model_call_id}:block:{provider_block_id}"

    def _scoped_tool_call_id(
        self,
        provider_tool_call_id: str,
        *,
        model_call_id: str | None,
    ) -> str:
        """把 provider 的单次调用 tool-call ID 提升为 Turn 内稳定身份。"""
        scoped_model_call_id = model_call_id or "unbound-model-call"
        return f"{scoped_model_call_id}:tool-call:{provider_tool_call_id}"

    def _accept_canonical_block(
        self,
        payload: Mapping[str, object],
        *,
        model_call_id: str | None = None,
    ) -> None:
        if self._canonical_item_sink is None or self._canonical_turn_id is None:
            return
        effective_model_call_id = model_call_id or self.current_model_call_id
        if effective_model_call_id is None:
            self._pending_canonical_blocks.append(dict(payload))
            return
        if effective_model_call_id in self._sealed_canonical_model_call_ids:
            # canonical item 已在该 model call 的 terminal 边界封存。部分 provider
            # 的 callback 会在 on_chat_model_end 之后迟到；不能重新创建同一 item_id
            # 并以新的 payload/status 重写 immutable item。普通消息流和 trace 仍会
            # 接收这段迟到事实，这里只阻止它再次进入 canonical storage。
            logging.getLogger(__name__).warning(
                "[itemized-context] ignore late canonical block after model call sealed: "
                "model_call_id=%s block_id=%s",
                effective_model_call_id,
                payload.get("block_id"),
            )
            return
        accumulator = self._canonical_block_accumulators.get(effective_model_call_id)
        if accumulator is None:
            accumulator = CanonicalBlockAccumulator(
                turn_id=self._canonical_turn_id,
                execution_id=self._model_execution_ids.get(
                    effective_model_call_id,
                    effective_model_call_id,
                ),
                producer_id=effective_model_call_id,
                model_call_id=effective_model_call_id,
            )
            self._canonical_block_accumulators[effective_model_call_id] = accumulator
        accumulator.accept(payload)

    def _tool_invocation_id_for(
        self,
        tool_call_id: str,
        *,
        model_call_id: str | None = None,
    ) -> str:
        existing = self._tool_invocation_ids_by_call_id.get(tool_call_id)
        if existing is not None:
            return existing
        owner_model_call_id = (
            self._tool_call_model_call_ids.get(tool_call_id)
            or model_call_id
            or self.current_model_call_id
            or "unknown-model-call"
        )
        provider_tool_call_id = self._provider_tool_call_ids_by_id.get(
            tool_call_id,
            tool_call_id,
        )
        invocation_id = (
            f"tool-invocation:{owner_model_call_id}:{provider_tool_call_id}"
        )
        self._tool_invocation_ids_by_call_id[tool_call_id] = invocation_id
        return invocation_id

    def _resolve_tool_call_id(self, tool_call_id: str) -> str:
        """把外部仍携带的 provider ID 解析为 Turn 内工具调用身份。"""
        if tool_call_id in self._tool_call_model_call_ids:
            return tool_call_id
        current_key = (self.current_model_call_id, tool_call_id)
        current_tool_call_id = self._tool_call_ids_by_provider_key.get(current_key)
        if current_tool_call_id is not None:
            return current_tool_call_id
        for (model_call_id, provider_id), scoped_tool_call_id in reversed(
            tuple(self._tool_call_ids_by_provider_key.items())
        ):
            if provider_id == tool_call_id:
                return scoped_tool_call_id
        return tool_call_id

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
        model_call_id: str | None = None,
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
                model_call_id=model_call_id or self.current_model_call_id,
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
