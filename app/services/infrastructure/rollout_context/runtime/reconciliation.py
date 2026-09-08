"""分离 history view 与 source overlay 的实时上下文重整。"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.infrastructure.rollout_context.fork.rewind_compaction import (
    source_epoch_for_operation,
)


@dataclass(frozen=True, slots=True)
class ContextReconciliation:
    """把 history view 变化与 source overlay 变化分成两个独立维度。"""

    history_view_revision: int
    source_overlay_epoch: int
    base_ref: str | None = None
    delta_refs: tuple[str, ...] = ()
    materialized: bool = False
    from_view_revision: int | None = None
    to_view_revision: int | None = None
    previous_overlay_epoch: int | None = None
    next_overlay_epoch: int | None = None
    source_revision_set: tuple[str, ...] = ()
    reason: str = "initial"
    outcome: str = "overlay_reused"

    def __post_init__(self) -> None:
        if self.history_view_revision < 0 or self.source_overlay_epoch < 0:
            raise ValueError("reconciliation revision 不能为负数")
        if self.from_view_revision is not None and self.from_view_revision < 0:
            raise ValueError("from_view_revision 不能为负数")
        if self.to_view_revision is not None and self.to_view_revision < 0:
            raise ValueError("to_view_revision 不能为负数")
        if self.previous_overlay_epoch is not None and self.previous_overlay_epoch < 0:
            raise ValueError("previous_overlay_epoch 不能为负数")
        if self.next_overlay_epoch is not None and self.next_overlay_epoch < 0:
            raise ValueError("next_overlay_epoch 不能为负数")
        if self.outcome not in {
            "history_view_changed",
            "overlay_reused",
            "delta_appended",
            "overlay_materialized",
            "overlay_invalid",
        }:
            raise ValueError(f"未知 reconciliation outcome: {self.outcome}")
        if not self.reason:
            raise ValueError("reconciliation reason 不能为空")
        if any(not isinstance(value, str) or not value for value in self.source_revision_set):
            raise ValueError("source_revision_set 必须是非空字符串数组")

    def _next(
        self,
        *,
        history_view_revision: int,
        source_overlay_epoch: int,
        base_ref: str | None,
        delta_refs: tuple[str, ...],
        materialized: bool,
        reason: str,
        outcome: str,
        source_revision_set: tuple[str, ...] | None = None,
    ) -> ContextReconciliation:
        return ContextReconciliation(
            history_view_revision=history_view_revision,
            source_overlay_epoch=source_overlay_epoch,
            base_ref=base_ref,
            delta_refs=delta_refs,
            materialized=materialized,
            from_view_revision=self.history_view_revision,
            to_view_revision=history_view_revision,
            previous_overlay_epoch=self.source_overlay_epoch,
            next_overlay_epoch=source_overlay_epoch,
            source_revision_set=(
                self.source_revision_set
                if source_revision_set is None
                else tuple(dict.fromkeys(source_revision_set))
            ),
            reason=reason,
            outcome=outcome,
        )

    def history_change(
        self,
        *,
        revision: int,
        reason: str = "history_view_changed",
    ) -> ContextReconciliation:
        if revision < self.history_view_revision:
            raise ValueError("history_view_revision 不能回退")
        return self._next(
            history_view_revision=revision,
            source_overlay_epoch=self.source_overlay_epoch,
            base_ref=self.base_ref,
            delta_refs=self.delta_refs,
            materialized=self.materialized,
            reason=reason,
            outcome=(
                "history_view_changed"
                if revision != self.history_view_revision
                else "overlay_reused"
            ),
        )

    def source_change(
        self,
        *,
        delta_ref: str,
        source_overlay_epoch: int,
        materialize: bool = False,
        reason: str = "source_edit",
        source_revision: str | None = None,
    ) -> ContextReconciliation:
        if source_overlay_epoch < self.source_overlay_epoch:
            raise ValueError("source_overlay_epoch 不能回退")
        if not delta_ref:
            raise ValueError("source overlay delta_ref 不能为空")
        revisions = (
            (*self.source_revision_set, source_revision)
            if source_revision
            else self.source_revision_set
        )
        if materialize:
            return self._next(
                history_view_revision=self.history_view_revision,
                source_overlay_epoch=source_overlay_epoch,
                base_ref=delta_ref,
                delta_refs=(),
                materialized=True,
                reason=reason,
                outcome="overlay_materialized",
                source_revision_set=tuple(revisions),
            )
        return self._next(
            history_view_revision=self.history_view_revision,
            source_overlay_epoch=source_overlay_epoch,
            base_ref=self.base_ref,
            delta_refs=tuple(dict.fromkeys((*self.delta_refs, delta_ref))),
            materialized=False,
            reason=reason,
            outcome="delta_appended",
            source_revision_set=tuple(revisions),
        )

    def reconcile(
        self,
        *,
        operation: str,
        history_view_revision: int | None = None,
        delta_ref: str | None = None,
        source_overlay_epoch: int | None = None,
        source_revision: str | None = None,
        materialize_overlay: bool = False,
    ) -> ContextReconciliation:
        """统一处理会改变运行时上下文的操作。

        history-only 操作只产生新的 view revision；只有明确给出 source
        delta/base 变化并要求 materialize 时才推进 overlay epoch。这样 rewind
        的尾部变化不会误触发 source base 物化。
        """
        if operation not in {
            "rewind",
            "history_replay",
            "fork",
            "checkpoint_restore",
            "compaction",
            "source_edit",
            "tool_change",
            "environment_change",
            "policy_change",
        }:
            raise ValueError(f"未知 context reconciliation operation: {operation}")
        current = self
        if history_view_revision is not None:
            current = current.history_change(
                revision=history_view_revision,
                reason=operation,
            )
        if delta_ref is None:
            source_epoch_for_operation(
                operation=operation,
                current_epoch=current.source_overlay_epoch,
                materialize_overlay=materialize_overlay,
            )
            return current
        target_epoch = source_epoch_for_operation(
            operation=operation,
            current_epoch=current.source_overlay_epoch,
            materialize_overlay=materialize_overlay,
        )
        if (
            materialize_overlay
            and source_overlay_epoch is not None
            and source_overlay_epoch != target_epoch
        ):
            raise ValueError(
                "overlay materialization 必须把 source_overlay_epoch 推进一个 epoch"
            )
        if not materialize_overlay and source_overlay_epoch is not None:
            target_epoch = source_overlay_epoch
        return current.source_change(
            delta_ref=delta_ref,
            source_overlay_epoch=target_epoch,
            materialize=materialize_overlay,
            reason=operation,
            source_revision=source_revision,
        )

    def selection(self) -> tuple[str, ...]:
        if self.materialized:
            return (self.base_ref,) if self.base_ref else ()
        return tuple(item for item in (self.base_ref, *self.delta_refs) if item)


__all__ = ["ContextReconciliation"]
