"""history view 与 source overlay 两条生命周期轴的边界。"""

from __future__ import annotations


def source_epoch_for_operation(
    *, operation: str, current_epoch: int, materialize_overlay: bool
) -> int:
    if current_epoch < 0:
        raise ValueError("source_overlay_epoch 不能为负数")
    if (
        operation in {"rewind", "history_replay", "checkpoint_restore"}
        and materialize_overlay
    ):
        raise ValueError("history-only operation 不得隐式物化 source overlay")
    if materialize_overlay:
        return current_epoch + 1
    return current_epoch


__all__ = ["source_epoch_for_operation"]
