"""持有并校验一个 rollout 读快照和有界索引缓存。"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass

from app.services.infrastructure.rollout_context.checkpoint.reader import (
    ContextChain,
    RolloutContextReader,
)
from app.services.infrastructure.rollout_context.storage.service import (
    RolloutReadSnapshot,
)


@dataclass(frozen=True, slots=True)
class IndexedTurnSpan:
    turn_id: str
    first_sequence: int
    last_sequence: int
    ordinal: int


@dataclass(frozen=True, slots=True)
class IndexedHistoryCacheEntry:
    rollout_id: str
    projection_epoch: int
    committed_sequence: int
    active_branch_id: str
    checkpoint_id: str
    message_sequence: int
    view_id: str
    turn_count: int
    context_ranges: tuple[tuple[str, int, int], ...]


@dataclass(frozen=True, slots=True)
class IndexedHistory:
    rollout_id: str
    projection_epoch: int
    view_id: str | None
    turn_count: int
    snapshot: RolloutReadSnapshot
    chain: ContextChain


_INDEXED_HISTORY_CACHE_LIMIT = 64


class IndexedHistorySnapshots:
    def __init__(self, context_reader: RolloutContextReader) -> None:
        self._context_reader = context_reader
        self._indexed_history_cache: OrderedDict[str, IndexedHistoryCacheEntry] = (
            OrderedDict()
        )
        self._indexed_history_cache_lock = threading.RLock()

    def read(
        self,
        session_id: str,
    ) -> IndexedHistory:
        # 历史读取必须保持纯只读：open_snapshot() 持有 rollout 的共享文件锁，
        # 而 repair_active_context_view() 会申请同一个文件的独占写锁。若调用方
        # 已经持有读快照（例如历史分页或异常回收路径），先 repair 会在 Linux
        # 的 flock 上自锁。索引修复属于显式维护/写入边界，不能塞进读取入口；
        # 这里只读取一个固定快照，异常也由下面的 finally 路径关闭它。
        snapshot = self._context_reader.open_snapshot(session_id)
        try:
            return self._read_indexed_history_snapshot(session_id, snapshot)
        except Exception:
            snapshot.close()
            raise

    def _read_indexed_history_snapshot(
        self,
        session_id: str,
        snapshot: RolloutReadSnapshot,
    ) -> IndexedHistory:
        """只通过统一 reader 解析逻辑链和 SQLite Turn 范围。"""
        manifest = snapshot.manifest
        checkpoint = self._context_reader.latest_checkpoint(snapshot)
        if checkpoint is None:
            # acceptance/terminal convergence 可能在首个 LangGraph checkpoint
            # 之前完成。此时 active context view 仍是已提交的 v2 索引；不能把
            # “没有 checkpoint”误判为“没有历史”，否则 item-bearing terminal
            # convergence 的 output 只在 storage 中可见，Web history 却丢失。
            chain = self._context_reader.resolve_chain(
                snapshot,
                snapshot.manifest.committed_sequence,
            )
            view_id = self._context_reader.context_view_id(chain)
            turn_count = self._context_reader.context_turn_count(snapshot, chain)
            return IndexedHistory(
                rollout_id=manifest.rollout_id,
                projection_epoch=manifest.projection_epoch,
                view_id=view_id,
                turn_count=turn_count,
                snapshot=snapshot,
                chain=chain,
            )
        cache_entry = self._cached_indexed_history(
            session_id,
            rollout_id=manifest.rollout_id,
            projection_epoch=manifest.projection_epoch,
            committed_sequence=manifest.committed_sequence,
            active_branch_id=manifest.active_branch_id,
            checkpoint_id=checkpoint.checkpoint_id,
            message_sequence=checkpoint.message_sequence,
        )
        if cache_entry is not None:
            return IndexedHistory(
                rollout_id=manifest.rollout_id,
                projection_epoch=manifest.projection_epoch,
                view_id=cache_entry.view_id,
                turn_count=cache_entry.turn_count,
                snapshot=snapshot,
                chain=ContextChain(
                    message_sequence=checkpoint.message_sequence,
                    ranges=cache_entry.context_ranges,
                ),
            )
        chain = self._context_reader.resolve_chain(
            snapshot,
            checkpoint.message_sequence,
        )
        view_id = self._context_reader.context_view_id(chain)
        turn_count = self._context_reader.context_turn_count(snapshot, chain)
        self._cache_indexed_history(
            session_id,
            IndexedHistoryCacheEntry(
                rollout_id=manifest.rollout_id,
                projection_epoch=manifest.projection_epoch,
                committed_sequence=manifest.committed_sequence,
                active_branch_id=manifest.active_branch_id,
                checkpoint_id=checkpoint.checkpoint_id,
                message_sequence=checkpoint.message_sequence,
                view_id=view_id,
                turn_count=turn_count,
                context_ranges=chain.ranges,
            ),
        )
        return IndexedHistory(
            rollout_id=manifest.rollout_id,
            projection_epoch=manifest.projection_epoch,
            view_id=view_id,
            turn_count=turn_count,
            snapshot=snapshot,
            chain=chain,
        )

    def _cached_indexed_history(
        self,
        session_id: str,
        *,
        rollout_id: str,
        projection_epoch: int,
        committed_sequence: int,
        active_branch_id: str,
        checkpoint_id: str,
        message_sequence: int,
    ) -> IndexedHistoryCacheEntry | None:
        with self._indexed_history_cache_lock:
            entry = self._indexed_history_cache.get(session_id)
            if entry is None:
                return None
            if (
                entry.rollout_id != rollout_id
                or entry.projection_epoch != projection_epoch
                or entry.committed_sequence != committed_sequence
                or entry.active_branch_id != active_branch_id
                or entry.checkpoint_id != checkpoint_id
                or entry.message_sequence != message_sequence
            ):
                self._indexed_history_cache.pop(session_id, None)
                return None
            self._indexed_history_cache.move_to_end(session_id)
            return entry

    def _cache_indexed_history(
        self,
        session_id: str,
        entry: IndexedHistoryCacheEntry,
    ) -> None:
        with self._indexed_history_cache_lock:
            self._indexed_history_cache[session_id] = entry
            self._indexed_history_cache.move_to_end(session_id)
            while len(self._indexed_history_cache) > _INDEXED_HISTORY_CACHE_LIMIT:
                self._indexed_history_cache.popitem(last=False)
