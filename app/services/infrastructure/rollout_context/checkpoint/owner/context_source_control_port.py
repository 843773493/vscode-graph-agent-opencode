"""向 CSM 暴露 Saver 控制状态端口；持久化归唯一 checkpoint durable owner。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from app.abstractions.internal_message import RuntimeContextMode
from app.domain.itemized.enums import SemanticKind, TurnScope
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.mutation_intents import ApplySourceLifecycleDecision
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceControlMutation,
    ContextSourceControlReceipt,
    ContextSourceControlState,
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.models import (
    ContextSourceDescriptor,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutNotMaterializedError,
)

if TYPE_CHECKING:
    from _thread import RLock

    from app.services.infrastructure.rollout_context.store import RolloutContextStore


class ContextSourceControlOwnerMixin:
    """暴露给 CSM 的唯一 owner 端口；实现委托给 RolloutContextStore。"""

    if TYPE_CHECKING:
        _storage: RolloutContextStore
        _lock: RLock

    def runtime_context_publication(self, session_id: str, thread_id: str, item_id: str, *,
                                    context_mode: RuntimeContextMode = "event") -> tuple[str, str] | None:
        """精确核对仅发布事件的已提交正文；无接受事实与数据损坏分别处理。"""
        storage = self._storage.storage
        try:
            with storage.read_connection(session_id, thread_id) as connection:
                row = connection.execute(
                    "SELECT execution_id FROM item_catalog WHERE item_id=?", (item_id,),
                ).fetchone()
                if row is None:
                    return None
                item = storage._read_canonical_item_from_catalog(
                    connection, session_id=session_id, thread_id=thread_id, checkpoint_ns="", item_id=item_id,
                )
                if (row != (None,) or item.semantic_kind != SemanticKind.RUNTIME_NOTICE
                        or item.turn_scope != TurnScope.AMBIENT or item.turn_id is not None
                        or item.producer_ref["producer_kind"] != "runtime"):
                    raise RuntimeError("runtime context 恢复引用不属于仅发布事件")
                annotations = item.extensions["boxteam.checkpoint.annotations"]
                if not isinstance(annotations, Mapping):
                    raise TypeError("runtime context annotations 必须是已知扩展对象")
                metadata = annotations["value"]
                revision = sha256_jcs({"content": item.payload, "metadata": metadata})
                if item.source_manifest is None or item.source_manifest.source_revision is None:
                    raise RuntimeError("runtime context缺少来源版本")
                stored_revision = item.source_manifest.source_revision
                if context_mode == "event" and stored_revision != revision:
                    raise RuntimeError("runtime context 正文与来源 revision 不一致")
                return sha256_jcs({
                    "session_id": session_id, "thread_id": thread_id,
                    "source_id": item.producer_ref["producer_id"], "prompt": item.payload,
                    "metadata": metadata, "delivery_mode": "context_only",
                    **({"context_mode": "state"} if context_mode == "state" else {}),
                }).rsplit(":", 1)[1], stored_revision
        except RolloutNotMaterializedError:
            return None

    def install_runtime_context_sources(self, session_id: str, thread_id: str, manager: ContextSourceManager) -> None:
        """安全prepare边界只重水合canonical状态版本，不读取任务当前定义或外部来源。"""
        states = tuple(state for state in self.load_context_source_control_states(ContextSourceOwnerKey(session_id, thread_id))
                       if state.source_kind == "runtime_state" and state.latest_revision is not None and state.tracking_status == "tracked")
        if not states:
            return
        storage = self._storage.storage
        with storage.read_connection(session_id, thread_id) as connection:
            def read(source_id: str, revision: str) -> str:
                row = connection.execute(
                    "SELECT item_id FROM item_catalog WHERE source_revision=? "
                    "AND json_extract(producer_ref_json,'$.producer_id')=? AND item_id GLOB 'item-rstate_*' "
                    "ORDER BY item_sequence LIMIT 1", (revision, source_id),
                ).fetchone()
                if row is None:
                    raise RuntimeError(f"runtime-state-unavailable: source_id={source_id}, revision={revision}")
                item = storage._read_canonical_item_from_catalog(
                    connection, session_id=session_id, thread_id=thread_id, checkpoint_ns="", item_id=row[0],
                )
                if (not isinstance(item.payload, str) or item.turn_scope != TurnScope.AMBIENT
                        or item.semantic_kind != SemanticKind.RUNTIME_NOTICE or item.turn_id is not None
                        or item.source_manifest is None or item.source_manifest.source_revision != revision):
                    raise RuntimeError("runtime-state-conflict: canonical状态正文与版本不一致")
                return item.payload

            # 先完整核验所有版本；缺少任一正文时不发布局部内存快照。
            frozen = []
            for state in states:
                assert state.latest_revision is not None
                content = read(state.source_id, state.latest_revision)
                baseline = None if state.latest_visible_committed_revision is None else read(state.source_id, state.latest_visible_committed_revision)
                frozen.append((state, content, baseline))
        for state, content, baseline in frozen:
            manager.register(ContextSourceDescriptor(
                source_id=state.source_id, source_kind=state.source_kind, name=state.name,
                description="持久发布的运行态来源", internal_locator=state.source_id,
            ), tracking_status="tracked")
            if baseline is not None:
                manager.restore_committed_baseline(state.source_id, lambda _revision, body=baseline: body)
            manager.observe(state.source_id, content, revision=state.latest_revision).require_tracked()

    def receive_team_source_observation(self, session_id: str, thread_id: str, *, ledger_revision: int,
                                       event_hash: str, payload_json: str) -> None:
        with self._lock:
            self._storage.checkpoints.receive_team_source_observation(session_id, thread_id, ledger_revision=ledger_revision,
                                                          event_hash=event_hash, payload_json=payload_json)

    def require_team_source_observation(self, session_id: str, thread_id: str, *, ledger_revision: int,
                                       event_hash: str, payload_json: str) -> None:
        with self._lock:
            self._storage.checkpoints.require_team_source_observation(session_id, thread_id, ledger_revision=ledger_revision,
                                                          event_hash=event_hash, payload_json=payload_json)

    def context_source_control_owner_available(
        self,
        owner: ContextSourceOwnerKey,
    ) -> bool:
        """该 SessionThread 是否有 durable ContextStore owner。

        装配层用它在合成 session（例如工具清单检查）上把 CSM 保持为纯内存
        对象，而不是为不存在的 ContextStore 伪造持久化成功。
        """
        if not isinstance(owner, ContextSourceOwnerKey):
            raise TypeError(
                "context_source_control_owner_available 需要 ContextSourceOwnerKey"
            )
        return self._storage.checkpoints.context_source_control_owner_available(
            owner.session_id,
            thread_id=owner.thread_id,
        )

    def load_context_source_control_states(
        self,
        owner: ContextSourceOwnerKey,
    ) -> tuple[ContextSourceControlState, ...]:
        if not isinstance(owner, ContextSourceOwnerKey):
            raise TypeError(
                "load_context_source_control_states 需要 ContextSourceOwnerKey"
            )
        return self._storage.checkpoints.read_context_source_control_states(
            owner.session_id,
            thread_id=owner.thread_id,
        )

    def apply_context_source_control_mutation(
        self,
        mutation: ContextSourceControlMutation,
    ) -> ContextSourceControlReceipt:
        if not isinstance(mutation, ContextSourceControlMutation):
            raise TypeError(
                "apply_context_source_control_mutation 需要 ContextSourceControlMutation"
            )
        return self._storage.checkpoints.apply_context_source_control_mutation(mutation)

    def apply_source_lifecycle_decision(
        self,
        decision: ApplySourceLifecycleDecision,
        item: CanonicalItemRecord | None,
    ) -> tuple[int, ...]:
        """通过唯一 Saver/ContextStore owner 提交 source intent。"""
        if not isinstance(decision, ApplySourceLifecycleDecision):
            raise TypeError(
                "apply_source_lifecycle_decision 需要 ApplySourceLifecycleDecision"
            )
        return self._storage.checkpoints.apply_source_lifecycle_decision(decision, item)
