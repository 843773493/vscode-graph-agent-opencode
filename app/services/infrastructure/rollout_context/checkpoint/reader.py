"""rollout context 的统一只读入口。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass

from langchain_core.messages import BaseMessage

from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.storage.primitives import (
    ContextTurnItemWindow,
    HistorySnapshotBounds,
    InternalExecutionHistoryPage,
    RolloutCheckpointIndex,
    RolloutNotMaterializedError,
    RolloutReadSnapshot,
    RolloutTurnAnchor,
)
from app.services.infrastructure.rollout_context.store import RolloutContextStore


@dataclass(frozen=True, slots=True)
class ContextChain:
    """同一 snapshot 中 active view 或冻结 checkpoint 的逻辑 boundary 链。"""

    message_sequence: int
    ranges: tuple[tuple[str, int, int], ...]


class RolloutContextReader:
    """统一执行 projection、detail 和 full 三种 context 读取。

    `RolloutContextStore` 只在本类内部提供 SQLite/JSONL 低层 primitive。调用方
    必须先取得一个 snapshot，再通过本类解析逻辑引用链，避免不同读取路径
    各自实现 boundary、branch 或 offset 定位。
    """

    def __init__(self, storage: RolloutContextStore) -> None:
        self._storage = storage

    @asynccontextmanager
    async def read_lease(
        self,
        session_id: str,
        thread_id: str,
        checkpoint_ns: str = "",
        *,
        allow_unmaterialized: bool = False,
    ) -> AsyncIterator[None]:
        """异步等待统一准入，内部同步 snapshot 在同一 Task 内重入。"""
        try:
            lock = self._storage.storage.read_lock(session_id, thread_id, checkpoint_ns)
        except RolloutNotMaterializedError:
            if not allow_unmaterialized:
                raise
            yield
            return
        async with lock:
            yield

    def open_snapshot(
        self,
        session_id: str,
        thread_id: str,
        checkpoint_ns: str = "",
        *,
        validate_integrity: bool = False,
    ) -> RolloutReadSnapshot:
        return self._storage.storage.open_read_snapshot(
            session_id,
            thread_id,
            checkpoint_ns,
            validate_integrity=validate_integrity,
        )

    def latest_checkpoint(
        self,
        snapshot: RolloutReadSnapshot,
        checkpoint_id: str | None = None,
    ) -> RolloutCheckpointIndex | None:
        return self._storage.checkpoints.latest_checkpoint(
            snapshot.session_id,
            snapshot.thread_id,
            snapshot.checkpoint_ns,
            checkpoint_id,
            snapshot=snapshot,
        )

    def active_view_id(self, snapshot: RolloutReadSnapshot) -> str | None:
        return self._storage.checkpoints.active_view_id(snapshot)

    def resolve_chain(
        self,
        snapshot: RolloutReadSnapshot,
        message_sequence: int | None = None,
    ) -> ContextChain:
        """无序号时读取 active view，指定序号时读取冻结 checkpoint view。"""
        ranges = self._storage.checkpoints.resolve_context_chain_ranges(
            snapshot,
            message_sequence,
        )
        return ContextChain(
            message_sequence=(
                snapshot.manifest.committed_sequence
                if message_sequence is None
                else message_sequence
            ),
            ranges=ranges,
        )

    def read_full_messages(
        self,
        snapshot: RolloutReadSnapshot,
        message_sequence: int,
        *,
        chain: ContextChain | None = None,
    ) -> list[BaseMessage]:
        """显式 full 模式，返回 LangGraph 可执行的完整消息列表。"""
        resolved_chain = chain or self.resolve_chain(snapshot, message_sequence)
        return self._storage.checkpoints.materialize_messages(
            snapshot.session_id,
            snapshot.thread_id,
            snapshot.checkpoint_ns,
            message_sequence,
            snapshot=snapshot,
            context_ranges=resolved_chain.ranges,
        )

    def read_canonical_items(
        self,
        snapshot: RolloutReadSnapshot,
        *,
        item_ids: Iterable[str] | None = None,
    ) -> list[CanonicalItemRecord]:
        """按已提交 catalog 读取 canonical item，不扫描 JSONL 头部。"""
        return self._storage.storage.read_items(
            snapshot.session_id,
            snapshot.thread_id,
            checkpoint_ns=snapshot.checkpoint_ns,
            item_ids=item_ids,
            snapshot=snapshot,
        )

    def read_context_items(
        self,
        snapshot: RolloutReadSnapshot,
        *,
        chain: ContextChain,
    ) -> list[CanonicalItemRecord]:
        """按 context view 的 logical item ordinal 读取已提交 item。"""
        return self._storage.checkpoints.read_items_for_view(snapshot, self.context_view_id(chain))

    def resolve_turn_anchor(
        self,
        snapshot: RolloutReadSnapshot,
        turn_id: str,
        *,
        anchor_mode: str = "inclusive",
        require_completed: bool = False,
    ) -> RolloutTurnAnchor:
        return self._storage.checkpoints.resolve_turn_anchor(
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
        return self._storage.checkpoints.resolve_latest_completed_turn_anchor(
            snapshot,
            anchor_mode=anchor_mode,
        )

    def read_turn_anchor_messages(
        self,
        snapshot: RolloutReadSnapshot,
        anchor: RolloutTurnAnchor,
    ) -> list[BaseMessage]:
        connection = self._storage.storage._snapshot_connection(snapshot)
        sequences = self._storage.storage._view_message_sequences_from_connection(connection, anchor.view_id)
        values = self._storage.checkpoints._read_messages(
            snapshot.session_id, snapshot.thread_id, snapshot.checkpoint_ns,
            [sequence for sequence in sequences if sequence <= anchor.cutoff_message_sequence],
            connection=connection,
        )
        return [self._storage.storage._codec().from_dict(value) for value in values]

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
        return self._storage.checkpoints.read_indexed_records(
            snapshot.session_id,
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

    def read_existing_turn_ids(
        self, snapshot: RolloutReadSnapshot, turn_ids: Iterable[str]
    ) -> set[str]:
        return self._storage.storage.read_existing_turn_ids(snapshot, turn_ids)

    def read_turn_projections(
        self,
        snapshot: RolloutReadSnapshot,
        chain: ContextChain,
        turn_ids: Iterable[str],
    ) -> dict[str, dict[str, object]]:
        return self._storage.storage.read_turn_projections(
            snapshot, self.context_view_id(chain), turn_ids
        )

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
        return self._storage.checkpoints.read_indexed_records_batch(
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
        return self._storage.checkpoints.indexed_turn_spans_for_ranges(snapshot, chain.ranges)

    def context_view_id(self, chain: ContextChain) -> str:
        if not chain.ranges:
            raise RuntimeError("context view 链为空")
        return chain.ranges[-1][0]

    def context_turn_count(
        self,
        snapshot: RolloutReadSnapshot,
        chain: ContextChain,
    ) -> int:
        return self._storage.checkpoints.context_turn_count(
            snapshot,
            self.context_view_id(chain),
        )

    def read_history_snapshot_bounds(
        self,
        snapshot: RolloutReadSnapshot,
        chain: ContextChain,
    ) -> HistorySnapshotBounds:
        """读取本 snapshot/view 的冻结 history revision 与分页上界。"""
        return self._storage.storage.read_history_snapshot_bounds(
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
        upper_ordinal: int | None,
        limit: int,
    ) -> tuple[list[tuple[str, int, int, int, int]], bool]:
        return self._storage.storage.read_context_turn_page(
            snapshot,
            self.context_view_id(chain),
            direction=direction,
            anchor_ordinal=anchor_ordinal,
            upper_ordinal=upper_ordinal,
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
    ) -> list[tuple[str, int, int, int, int]]:
        return self._storage.storage.read_context_turn_window(
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
    ) -> list[tuple[str, int, int, int, int]]:
        return self._storage.storage.read_context_turn_ids(
            snapshot,
            self.context_view_id(chain),
            turn_ids,
        )

    def resolve_context_turn_item_window(
        self,
        snapshot: RolloutReadSnapshot,
        chain: ContextChain,
        *,
        first_logical_turn_ordinal: int,
        last_logical_turn_ordinal: int,
    ) -> ContextTurnItemWindow:
        """在本 snapshot 中解析 Turn window 对应的 membership ordinal bounds。"""
        return self._storage.storage.resolve_context_turn_item_window(
            snapshot,
            self.context_view_id(chain),
            first_logical_turn_ordinal=first_logical_turn_ordinal,
            last_logical_turn_ordinal=last_logical_turn_ordinal,
        )

    def read_internal_execution_history_page(
        self,
        snapshot: RolloutReadSnapshot,
        chain: ContextChain,
        *,
        direction: str,
        anchor_logical_item_ordinal: int | None,
        lower_logical_item_ordinal: int | None,
        upper_logical_item_ordinal: int | None,
        snapshot_upper_logical_item_ordinal: int | None,
        limit: int,
        execution_id: str | None = None,
    ) -> InternalExecutionHistoryPage:
        """同一 view/snapshot 在 SQL 中择一，再读取有界元数据与正文页。"""
        return self._storage.storage.read_internal_execution_history_page(
            snapshot,
            self.context_view_id(chain),
            direction=direction,
            anchor_logical_item_ordinal=anchor_logical_item_ordinal,
            lower_logical_item_ordinal=lower_logical_item_ordinal,
            upper_logical_item_ordinal=upper_logical_item_ordinal,
            snapshot_upper_logical_item_ordinal=snapshot_upper_logical_item_ordinal,
            limit=limit,
            execution_id=execution_id,
        )

    def decode_message(
        self,
        value: object,
        *,
        summary_only: bool = False,
    ) -> BaseMessage:
        return self._storage.checkpoints.decode_indexed_message(
            value,
            summary_only=summary_only,
        )
