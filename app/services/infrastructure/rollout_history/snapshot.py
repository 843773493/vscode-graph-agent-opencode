"""在同一 rollout 读快照中解析历史视图。"""

from __future__ import annotations

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
class IndexedHistory:
    rollout_id: str
    projection_epoch: int
    view_id: str | None
    snapshot: RolloutReadSnapshot
    chain: ContextChain


class IndexedHistorySnapshots:
    def __init__(self, context_reader: RolloutContextReader) -> None:
        self._context_reader = context_reader

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
            # active view 已由单次索引查询解析；直接消费当前事务，避免重复定位及跨请求缓存失效管理。
            chain = self._context_reader.resolve_chain(snapshot)
            return IndexedHistory(
                rollout_id=snapshot.manifest.rollout_id,
                projection_epoch=snapshot.manifest.projection_epoch,
                view_id=self._context_reader.context_view_id(chain),
                snapshot=snapshot,
                chain=chain,
            )
        except Exception:
            snapshot.close()
            raise
