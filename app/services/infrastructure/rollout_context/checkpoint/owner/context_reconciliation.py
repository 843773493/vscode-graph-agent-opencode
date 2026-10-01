"""Saver-owned context reconciliation ledger owner。"""

from __future__ import annotations

from app.services.infrastructure.rollout_context.runtime.reconciliation import (
    ContextReconciliation,
)


class ContextReconciliationMixin:
    """只管理已提交 history/source revision 的运行时 ledger。"""

    def reconcile_context(
        self,
        session_id: str,
        *,
        operation: str,
        checkpoint_ns: str = "",
        history_view_revision: int | None = None,
        delta_ref: str | None = None,
        source_overlay_epoch: int | None = None,
        source_revision: str | None = None,
        materialize_overlay: bool = False,
    ) -> ContextReconciliation:
        """记录一次已提交 context 变化，并保持 history/source 两条轴独立。

        调用方必须先完成相应的 Saver-owned storage 操作，再调用本方法；本
        方法不直接写 JSONL/SQLite，也不接受业务层自行扫描出的 refs。若没有
        显式传入 revision，则只把当前已提交 manifest 作为同步点，不伪造一次
        history mutation。
        """
        if not session_id:
            raise ValueError("reconcile_context 缺少 session_id")
        if not isinstance(checkpoint_ns, str):
            raise TypeError("reconcile_context checkpoint namespace 必须是字符串")
        with self._context_reader.open_snapshot(session_id, checkpoint_ns) as snapshot:
            manifest = snapshot.manifest
            committed_overlays = self._storage.list_source_overlays(
                session_id,
                checkpoint_ns=checkpoint_ns,
                snapshot=snapshot,
            )
        active = [
            overlay
            for overlay in committed_overlays
            if overlay["status"] == "active"
        ]
        base_ref = next(
            (
                overlay["base_ref"]
                for overlay in active
                if overlay["base_ref"] is not None
            ),
            None,
        )
        delta_refs = tuple(
            overlay["delta_ref"]
            for overlay in active
            if overlay["delta_ref"] is not None
        )
        source_revisions = tuple(overlay["source_revision"] for overlay in active)
        key = (session_id, checkpoint_ns)
        with self._lock:
            current = self._context_reconciliations.get(key)
            if current is None:
                current = ContextReconciliation(
                    history_view_revision=(
                        max(0, manifest.history_view_revision - 1)
                        if history_view_revision is not None
                        and history_view_revision > manifest.history_view_revision - 1
                        else manifest.history_view_revision
                    ),
                    source_overlay_epoch=(
                        max(
                            0,
                            (source_overlay_epoch - 1)
                            if source_overlay_epoch is not None
                            and source_overlay_epoch > manifest.source_overlay_epoch - 1
                            else manifest.source_overlay_epoch,
                        )
                    ),
                    base_ref=base_ref,
                    delta_refs=delta_refs,
                    materialized=False,
                    source_revision_set=source_revisions,
                )
            else:
                # 另一个进程可能已经提交了 view/source 更新；不让旧的内存
                # view 把新 manifest 降级成历史状态。两条轴可以独立前进，
                # 只校验本次操作显式触及的轴。
                if (
                    history_view_revision is not None
                    and current.history_view_revision > manifest.history_view_revision
                ) or (
                    source_overlay_epoch is not None
                    and current.source_overlay_epoch > manifest.source_overlay_epoch
                ):
                    raise RuntimeError(
                        "context reconciliation 内存 revision 高于已提交 manifest"
                    )
        next_state = current.reconcile(
            operation=operation,
            history_view_revision=history_view_revision,
            delta_ref=delta_ref,
            source_overlay_epoch=source_overlay_epoch,
            source_revision=source_revision,
            materialize_overlay=materialize_overlay,
        )
        # 用数据库事实补足 overlay selection；但保留 reconcile 对这次操作
        # 产生的 from/to revision 和 outcome，便于 UI/扩展查看原因。
        if history_view_revision is None and delta_ref is None:
            next_state = ContextReconciliation(
                history_view_revision=manifest.history_view_revision,
                source_overlay_epoch=manifest.source_overlay_epoch,
                base_ref=next_state.base_ref or base_ref,
                delta_refs=next_state.delta_refs or delta_refs,
                materialized=next_state.materialized,
                from_view_revision=next_state.from_view_revision,
                to_view_revision=next_state.to_view_revision,
                previous_overlay_epoch=next_state.previous_overlay_epoch,
                next_overlay_epoch=next_state.next_overlay_epoch,
                source_revision_set=next_state.source_revision_set or source_revisions,
                reason=next_state.reason,
                outcome=next_state.outcome,
            )
        with self._lock:
            self._context_reconciliations[key] = next_state
        return next_state

    def get_context_reconciliation(
        self,
        session_id: str,
        *,
        checkpoint_ns: str = "",
    ) -> ContextReconciliation:
        """返回当前进程的实时 context reconciliation metadata。"""
        key = (session_id, checkpoint_ns)
        with self._lock:
            cached = self._context_reconciliations.get(key)
        if cached is not None:
            return cached
        return self.reconcile_context(
            session_id,
            operation="checkpoint_restore",
            checkpoint_ns=checkpoint_ns,
        )


__all__ = ["ContextReconciliationMixin"]
