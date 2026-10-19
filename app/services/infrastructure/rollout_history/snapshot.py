"""持有并校验一个 rollout 读快照和有界索引缓存。"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass

from app.services.infrastructure.rollout_context.checkpoint.reader import (
    ContextChain,
    RolloutContextReader,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutNotMaterializedError,
    RolloutReadSnapshot,
)


@dataclass(frozen=True, slots=True)
class IndexedTurnSpan:
    turn_id: str
    first_sequence: int
    last_sequence: int
    ordinal: int
    root_logical_item_ordinal: int


@dataclass(frozen=True, slots=True)
class IndexedHistoryCacheEntry:
    rollout_id: str
    projection_epoch: int
    committed_sequence: int
    active_branch_id: str
    history_view_revision: int
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
        self._indexed_history_cache: OrderedDict[tuple[str, str], IndexedHistoryCacheEntry] = (
            OrderedDict()
        )
        self._indexed_history_cache_lock = threading.RLock()

    def read(
        self,
        session_id: str,
        thread_id: str,
    ) -> IndexedHistory | None:
        try:
            snapshot = self._context_reader.open_snapshot(session_id, thread_id)
        except RolloutNotMaterializedError:
            return None
        try:
            if (snapshot.session_id, snapshot.thread_id) != (session_id, thread_id):
                raise ValueError("history snapshot 与请求 Session/thread owner 不一致")
            return self._read_indexed_history_snapshot(session_id, snapshot)
        except Exception:
            snapshot.close()
            raise

    def _read_indexed_history_snapshot(
        self,
        session_id: str,
        snapshot: RolloutReadSnapshot,
    ) -> IndexedHistory:
        """history 绑定同一 snapshot 的 active view，不用 checkpoint 上界截断。"""
        manifest = snapshot.manifest
        active_view_id = self._context_reader.active_view_id(snapshot)
        cache_entry = self._cached_indexed_history(
            session_id,
            snapshot.thread_id,
            rollout_id=manifest.rollout_id,
            projection_epoch=manifest.projection_epoch,
            committed_sequence=manifest.committed_sequence,
            active_branch_id=manifest.active_branch_id,
            history_view_revision=manifest.history_view_revision,
            view_id=active_view_id,
        )
        if cache_entry is not None:
            return IndexedHistory(
                rollout_id=manifest.rollout_id,
                projection_epoch=manifest.projection_epoch,
                view_id=cache_entry.view_id,
                turn_count=cache_entry.turn_count,
                snapshot=snapshot,
                chain=ContextChain(
                    message_sequence=manifest.committed_sequence,
                    ranges=cache_entry.context_ranges,
                ),
            )
        chain = self._context_reader.resolve_chain(snapshot)
        view_id = self._context_reader.context_view_id(chain)
        if view_id != active_view_id:
            raise RuntimeError("history chain 与同一 snapshot 的 active view 不一致")
        turn_count = self._context_reader.context_turn_count(snapshot, chain)
        self._cache_indexed_history(
            session_id,
            snapshot.thread_id,
            IndexedHistoryCacheEntry(
                rollout_id=manifest.rollout_id,
                projection_epoch=manifest.projection_epoch,
                committed_sequence=manifest.committed_sequence,
                active_branch_id=manifest.active_branch_id,
                history_view_revision=manifest.history_view_revision,
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
        thread_id: str,
        *,
        rollout_id: str,
        projection_epoch: int,
        committed_sequence: int,
        active_branch_id: str,
        history_view_revision: int,
        view_id: str | None,
    ) -> IndexedHistoryCacheEntry | None:
        with self._indexed_history_cache_lock:
            key = (session_id, thread_id)
            entry = self._indexed_history_cache.get(key)
            if entry is None:
                return None
            if (
                entry.rollout_id != rollout_id
                or entry.projection_epoch != projection_epoch
                or entry.committed_sequence != committed_sequence
                or entry.active_branch_id != active_branch_id
                or entry.history_view_revision != history_view_revision
                or entry.view_id != view_id
            ):
                self._indexed_history_cache.pop(key, None)
                return None
            self._indexed_history_cache.move_to_end(key)
            return entry

    def _cache_indexed_history(
        self,
        session_id: str,
        thread_id: str,
        entry: IndexedHistoryCacheEntry,
    ) -> None:
        with self._indexed_history_cache_lock:
            key = (session_id, thread_id)
            self._indexed_history_cache[key] = entry
            self._indexed_history_cache.move_to_end(key)
            while len(self._indexed_history_cache) > _INDEXED_HISTORY_CACHE_LIMIT:
                self._indexed_history_cache.popitem(last=False)
