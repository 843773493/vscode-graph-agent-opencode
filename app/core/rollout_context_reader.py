"""rollout context 的统一只读入口。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from langchain_core.messages import BaseMessage

from app.core.rollout_storage import (
    RolloutCheckpointIndex,
    RolloutReadSnapshot,
    RolloutStorage,
    RolloutTurnAnchor,
)


@dataclass(frozen=True, slots=True)
class ContextChain:
    """一个 checkpoint 对应的逻辑 context boundary 链。"""

    message_sequence: int
    ranges: tuple[tuple[str, int, int], ...]


class RolloutContextReader:
    """统一执行 projection、detail 和 full 三种 context 读取。

    `RolloutStorage` 只在本类内部提供 SQLite/JSONL 低层 primitive。调用方
    必须先取得一个 snapshot，再通过本类解析逻辑引用链，避免不同读取路径
    各自实现 boundary、branch 或 offset 定位。
    """

    def __init__(self, storage: RolloutStorage) -> None:
        self._storage = storage

    def open_snapshot(
        self,
        thread_id: str,
        checkpoint_ns: str = "",
        *,
        recover: bool = False,
        validate_integrity: bool = False,
    ) -> RolloutReadSnapshot:
        if recover:
            self._storage.initialize(thread_id, checkpoint_ns)
        return self._storage.open_read_snapshot(
            thread_id,
            checkpoint_ns,
            validate_integrity=validate_integrity,
        )

    def repair_active_context_view(
        self,
        thread_id: str,
        checkpoint_ns: str = "",
    ) -> bool:
        """在建立历史只读快照前修复旧版本生成的 active Turn 索引。"""
        return self._storage.repair_active_context_view(thread_id, checkpoint_ns)

    def latest_checkpoint(
        self,
        snapshot: RolloutReadSnapshot,
        checkpoint_id: str | None = None,
    ) -> RolloutCheckpointIndex | None:
        return self._storage.latest_checkpoint(
            snapshot.thread_id,
            snapshot.checkpoint_ns,
            checkpoint_id,
            snapshot=snapshot,
        )

    def resolve_chain(
        self,
        snapshot: RolloutReadSnapshot,
        message_sequence: int,
    ) -> ContextChain:
        ranges = self._storage.resolve_context_chain_ranges(
            snapshot,
            message_sequence,
        )
        return ContextChain(message_sequence=message_sequence, ranges=ranges)

    def read_full_messages(
        self,
        snapshot: RolloutReadSnapshot,
        message_sequence: int,
        *,
        chain: ContextChain | None = None,
    ) -> list[BaseMessage]:
        """显式 full 模式，返回 LangGraph 可执行的完整消息列表。"""
        resolved_chain = chain or self.resolve_chain(snapshot, message_sequence)
        return self._storage.materialize_messages(
            snapshot.thread_id,
            snapshot.checkpoint_ns,
            message_sequence,
            snapshot=snapshot,
            context_ranges=resolved_chain.ranges,
        )

    def resolve_turn_anchor(
        self,
        snapshot: RolloutReadSnapshot,
        turn_id: str,
        *,
        anchor_mode: str = "inclusive",
        require_completed: bool = False,
    ) -> RolloutTurnAnchor:
        return self._storage.resolve_turn_anchor(
            snapshot,
            turn_id,
            anchor_mode=anchor_mode,
            require_completed=require_completed,
        )

    def resolve_latest_completed_turn_anchor(
        self,
        snapshot: RolloutReadSnapshot,
        *,
        anchor_mode: str = "inclusive",
    ) -> RolloutTurnAnchor | None:
        return self._storage.resolve_latest_completed_turn_anchor(
            snapshot,
            anchor_mode=anchor_mode,
        )

    def read_turn_anchor_messages(
        self,
        snapshot: RolloutReadSnapshot,
        anchor: RolloutTurnAnchor,
    ) -> list[BaseMessage]:
        return self._storage.materialize_turn_anchor(snapshot, anchor)

    def read_projection_records(
        self,
        snapshot: RolloutReadSnapshot,
        *,
        chain: ContextChain,
        after_sequence: int = 0,
        through_sequence: int | None = None,
        branch_id: str | None = None,
        turn_id: str | None = None,
        kinds: Iterable[str] | None = None,
    ) -> list[dict[str, object]]:
        """projection/detail 模式的有限 offset 记录读取。"""
        return self._storage.read_indexed_records(
            snapshot.thread_id,
            snapshot.checkpoint_ns,
            after_sequence=after_sequence,
            through_sequence=through_sequence,
            sequence_ranges=chain.ranges,
            branch_id=branch_id,
            turn_id=turn_id,
            kinds=kinds,
            snapshot=snapshot,
        )

    def read_turn_projections(
        self,
        snapshot: RolloutReadSnapshot,
        turn_ids: Iterable[str],
    ) -> dict[str, dict[str, object]]:
        return self._storage.read_turn_projections(snapshot, turn_ids)

    def read_projection_records_batch(
        self,
        snapshot: RolloutReadSnapshot,
        *,
        turn_ids: Iterable[str],
        chain: ContextChain,
        kinds: Iterable[str] = ("message_append",),
        message_roles: Iterable[str] | None = None,
        tool_kinds: Iterable[str] | None = None,
        tool_call_ids: Iterable[str] | None = None,
        required_sequences: Mapping[str, Iterable[int]] | None = None,
    ) -> dict[str, list[dict[str, object]]]:
        return self._storage.read_indexed_records_batch(
            snapshot,
            turn_ids=turn_ids,
            sequence_ranges=chain.ranges,
            kinds=kinds,
            message_roles=message_roles,
            tool_kinds=tool_kinds,
            tool_call_ids=tool_call_ids,
            required_sequences=required_sequences,
        )

    def read_turn_spans(
        self,
        snapshot: RolloutReadSnapshot,
        chain: ContextChain,
    ) -> list[tuple[str, int, int]]:
        return self._storage.indexed_turn_spans_for_ranges(snapshot, chain.ranges)

    def context_view_id(self, chain: ContextChain) -> str:
        if not chain.ranges:
            raise RuntimeError("context view 链为空")
        return chain.ranges[-1][0]

    def context_turn_count(
        self,
        snapshot: RolloutReadSnapshot,
        chain: ContextChain,
    ) -> int:
        return self._storage.context_turn_count(
            snapshot,
            self.context_view_id(chain),
        )

    def read_context_turn_page(
        self,
        snapshot: RolloutReadSnapshot,
        chain: ContextChain,
        *,
        direction: str,
        anchor_ordinal: int | None,
        limit: int,
    ) -> tuple[list[tuple[str, int, int, int]], bool]:
        return self._storage.read_context_turn_page(
            snapshot,
            self.context_view_id(chain),
            direction=direction,
            anchor_ordinal=anchor_ordinal,
            limit=limit,
        )

    def read_context_turn_window(
        self,
        snapshot: RolloutReadSnapshot,
        chain: ContextChain,
        *,
        anchor_ordinal: int,
        before: int,
        after: int,
    ) -> list[tuple[str, int, int, int]]:
        return self._storage.read_context_turn_window(
            snapshot,
            self.context_view_id(chain),
            anchor_ordinal=anchor_ordinal,
            before=before,
            after=after,
        )

    def read_context_turn_ids(
        self,
        snapshot: RolloutReadSnapshot,
        chain: ContextChain,
        turn_ids: Iterable[str],
    ) -> list[tuple[str, int, int, int]]:
        return self._storage.read_context_turn_ids(
            snapshot,
            self.context_view_id(chain),
            turn_ids,
        )

    def decode_message(
        self,
        value: object,
        *,
        summary_only: bool = False,
    ) -> BaseMessage:
        return self._storage.decode_indexed_message(
            value,
            summary_only=summary_only,
        )
