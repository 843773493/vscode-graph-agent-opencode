"""Provider normalized block/delta 的实时 accumulator。

这里仅维护当前 execution 的内存 draft。只有调用方把 ``finalize`` 返回值交给
Saver/storage 后，canonical item 才进入持久化事实源；本模块不读写 JSONL/SQLite。
"""

from __future__ import annotations

from collections.abc import Mapping

from app.domain.itemized.enums import (
    CanonicalItemStatus,
    PayloadKind,
    SemanticKind,
    TurnScope,
)
from app.domain.itemized.records import CanonicalItemRecord


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} 必须是非空字符串")
    return value


def _non_negative_int(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{field_name} 必须是非负整数")
    return value


class CanonicalBlockAccumulator:
    """把 normalized provider block/delta 聚合成可终态化的 item draft。"""

    def _item_id_for_block(self, block_id: str) -> str:
        """按单次模型调用生成 block item 的稳定身份。"""
        return (
            f"item-{self.execution_id}-model-{self.model_call_id}-block-{block_id}"
        )

    def __init__(
        self,
        *,
        turn_id: str,
        execution_id: str,
        producer_id: str,
        model_call_id: str | None = None,
    ) -> None:
        self.turn_id = _required_text(turn_id, "turn_id")
        self.execution_id = _required_text(execution_id, "execution_id")
        self.producer_id = _required_text(producer_id, "producer_id")
        self.model_call_id = _required_text(
            model_call_id or producer_id,
            "model_call_id",
        )
        self._drafts: dict[str, dict[str, object]] = {}
        self._order: list[str] = []

    def bind_execution_id(self, execution_id: str) -> None:
        """在 provider delta 先到达时，将草稿绑定到已登记的 execution。"""
        self.execution_id = _required_text(execution_id, "execution_id")
        for block_id, draft in self._drafts.items():
            draft["item_id"] = self._item_id_for_block(block_id)

    def accept(self, block: Mapping[str, object]) -> None:
        if not isinstance(block, Mapping):
            raise TypeError("normalized provider block 必须是 object")
        raw_block_id = block.get("block_id", block.get("id"))
        block_id = _required_text(raw_block_id, "normalized provider block.block_id")
        raw_carrier = block.get("carrier_type")
        if raw_carrier is None:
            raw_carrier = block.get("type", "text")
        carrier = _required_text(raw_carrier, "normalized provider block.carrier_type")
        raw_block_index = block.get("block_index", len(self._order))
        block_index = _non_negative_int(
            raw_block_index,
            "normalized provider block.block_index",
        )
        draft = self._drafts.get(block_id)
        if draft is None:
            semantic, payload_kind = self._schema_for_carrier(carrier)
            draft = {
                "item_id": self._item_id_for_block(block_id),
                "semantic_kind": semantic,
                "payload_kind": payload_kind,
                "carrier_type": carrier,
                "block_index": block_index,
                "text": "",
                "args": "",
                "name": block.get("name"),
                "tool_call_id": block.get("tool_call_id", block_id),
                "tool_invocation_id": block.get("tool_invocation_id"),
                "tool_attempt_id": block.get("tool_attempt_id"),
                "reasoning_items": [],
                "redacted": False,
            }
            self._drafts[block_id] = draft
            self._order.append(block_id)
        elif draft["block_index"] != block_index or draft["carrier_type"] != carrier:
            raise ValueError(
                "normalized provider block identity 与既有 draft 不一致: "
                f"block_id={block_id}"
            )
        text = block.get("text", block.get("delta"))
        if isinstance(text, str):
            draft["text"] = draft["text"] + text
        args = block.get("args", block.get("arguments"))
        if isinstance(args, str):
            draft["args"] = draft["args"] + args
        elif isinstance(args, Mapping):
            draft["args"] = dict(args)
        if isinstance(block.get("name"), str):
            draft["name"] = block["name"]
        if isinstance(block.get("tool_call_id"), str):
            draft["tool_call_id"] = block["tool_call_id"]
        if block.get("redacted") is True:
            draft["redacted"] = True
        if block.get("operation") == "item_upsert" and isinstance(
            block.get("item"), Mapping
        ):
            items = draft.get("reasoning_items")
            if isinstance(items, list):
                items.append(dict(block["item"]))

    def finalize(
        self,
        *,
        first_item_sequence: int,
        status: str = CanonicalItemStatus.COMPLETED,
    ) -> tuple[CanonicalItemRecord, ...]:
        first_item_sequence = _non_negative_int(
            first_item_sequence,
            "first_item_sequence",
        )
        if first_item_sequence <= 0:
            raise ValueError("first_item_sequence 必须是正整数")
        status = _required_text(status, "status")
        result: list[CanonicalItemRecord] = []
        for index, block_id in enumerate(self._order):
            draft = self._drafts[block_id]
            semantic = _required_text(draft["semantic_kind"], "draft.semantic_kind")
            payload_kind = _required_text(draft["payload_kind"], "draft.payload_kind")
            if semantic == SemanticKind.TOOL_CALL:
                raw_args = draft["args"] if draft["args"] else {}
                name = _required_text(draft.get("name"), "draft.tool_call.name")
                tool_call_id = _required_text(
                    draft.get("tool_call_id"),
                    "draft.tool_call.tool_call_id",
                )
                tool_invocation_id = draft.get("tool_invocation_id")
                if not isinstance(tool_invocation_id, str) or not tool_invocation_id:
                    tool_invocation_id = (
                        f"tool-invocation:{self.model_call_id}:{tool_call_id}"
                    )
                payload: object = {
                    "tool_call_id": tool_call_id,
                    "tool_invocation_id": tool_invocation_id,
                    "name": name,
                    "args": raw_args,
                }
                tool_attempt_id = draft.get("tool_attempt_id")
                if isinstance(tool_attempt_id, str) and tool_attempt_id:
                    payload["tool_attempt_id"] = tool_attempt_id
            elif semantic == SemanticKind.TOOL_RESULT:
                payload_kind = PayloadKind.TOOL_RESULT
                name = _required_text(draft.get("name"), "draft.tool_result.name")
                tool_call_id = _required_text(
                    draft.get("tool_call_id"),
                    "draft.tool_result.tool_call_id",
                )
                tool_invocation_id = draft.get("tool_invocation_id")
                if not isinstance(tool_invocation_id, str) or not tool_invocation_id:
                    tool_invocation_id = (
                        f"tool-invocation:{self.model_call_id}:{tool_call_id}"
                    )
                tool_attempt_id = _required_text(
                    draft.get("tool_attempt_id"),
                    "draft.tool_result.tool_attempt_id",
                )
                payload = {
                    "tool_call_id": tool_call_id,
                    "tool_invocation_id": tool_invocation_id,
                    "tool_attempt_id": tool_attempt_id,
                    "result_id": _required_text(draft["item_id"], "draft.item_id"),
                    "name": name,
                    "content": draft["text"],
                    "tool_outcome": "success" if status == "completed" else "unknown",
                }
            elif payload_kind == PayloadKind.SUMMARY:
                payload = {
                    "summary_id": _required_text(draft["item_id"], "draft.item_id"),
                    "view_revision": str(draft["block_index"]),
                    "content": list(draft["reasoning_items"]) or draft["text"],
                }
            elif payload_kind == PayloadKind.OPAQUE:
                payload = {
                    "encoding": "redacted",
                    "value": "<redacted>",
                    "wire_type": _required_text(
                        draft["carrier_type"], "draft.carrier_type"
                    ),
                    "schema_version": "v1",
                    "protection": {"redacted": True},
                }
            else:
                payload = draft["text"]
            identity_metadata = {
                key: payload[key]
                for key in (
                    "tool_invocation_id",
                    "tool_attempt_id",
                )
                if isinstance(payload, Mapping)
                and isinstance(payload.get(key), str)
                and payload[key]
            }
            result.append(
                CanonicalItemRecord.create(
                    item_sequence=first_item_sequence + index,
                    item_id=_required_text(draft["item_id"], "draft.item_id"),
                    semantic_kind=semantic,
                    payload_kind=payload_kind,
                    status=status,
                    producer_ref={
                        "producer_kind": "provider",
                        "producer_id": self.producer_id,
                        "invocation_id": self.model_call_id,
                    },
                    payload=payload,
                    metadata={
                        "block_id": block_id,
                        "block_index": draft["block_index"],
                        "execution_id": self.execution_id,
                        "model_call_id": self.model_call_id,
                        **identity_metadata,
                    },
                    turn_id=self.turn_id,
                    turn_scope=TurnScope.TURN_MEMBER,
                    # 一个 execution 可能包含多次模型调用；消息组必须绑定单次
                    # model call，否则不同调用的流式 carrier 会互相合并，无法
                    # 与对应的 checkpoint carrier 做 shadow 去重。
                    message_group_id=f"message-{self.model_call_id}",
                    wire_role="assistant"
                    if semantic != SemanticKind.TOOL_RESULT
                    else "tool",
                )
            )
        self._drafts.clear()
        self._order.clear()
        return tuple(result)

    @staticmethod
    def _schema_for_carrier(carrier: str) -> tuple[str, str]:
        if carrier in {"tool_call", "tool_call_chunk", "function_call"}:
            return SemanticKind.TOOL_CALL, PayloadKind.TOOL_CALL
        if carrier in {"tool_result", "tool_output"}:
            return SemanticKind.TOOL_RESULT, PayloadKind.TOOL_RESULT
        if carrier in {"reasoning_items"}:
            return SemanticKind.REASONING, PayloadKind.SUMMARY
        if carrier in {"redacted_thinking", "redacted_reasoning"}:
            return SemanticKind.REASONING, PayloadKind.OPAQUE
        if carrier in {"reasoning", "thinking", "reasoning_content"}:
            return SemanticKind.REASONING, PayloadKind.TEXT
        return SemanticKind.ASSISTANT_OUTPUT, PayloadKind.TEXT


__all__ = ["CanonicalBlockAccumulator"]
