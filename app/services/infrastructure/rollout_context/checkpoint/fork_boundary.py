"""fork/history-prefix 边界的 typed owner：只委托唯一 tool protocol validator。

context_fork 与 history_prefix_fork 在 target staging 物化、任何 durable
target mutation 或 pending epoch transition 之前调用本模块验证 source view
前缀闭合。本模块只读，不产生任何持久化副作用，也不维护第二套配对判断；
配对事实来源仍是 message projection 写入的 tool_calls 索引。
"""

from __future__ import annotations

from app.services.infrastructure.rollout_context.checkpoint.tool_protocol_boundary import (
    validate_tool_protocol_closure,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_optional_text,
    strict_text,
)


class RolloutForkBoundaryOwnerMixin:
    """fork source view 前缀闭合验证 owner（由 RolloutStorage 组合）。"""

    def validate_fork_source_closure(
        self,
        thread_id: str,
        *,
        checkpoint_ns: str,
        source_checkpoint_id: str | None,
        cutoff_message_sequence: int | None,
    ) -> None:
        """验证 fork 边界不拆散 source view 的 tool-call 配对。

        cutoff_message_sequence 为 None 时验证完整 source view；否则与
        rewind_to_turn 同一条切点规则：取 view 内序号不大于该值的最大
        前缀。冲突时抛出 ToolProtocolBoundaryConflict，code 为
        tool-protocol-boundary-conflict；本方法全程 read-only。
        """
        thread_id = strict_text(thread_id, field="fork_boundary.thread_id")
        strict_text(
            checkpoint_ns,
            field="fork_boundary.checkpoint_ns",
            allow_empty=True,
        )
        source_checkpoint_id = strict_optional_text(
            source_checkpoint_id,
            field="fork_boundary.source_checkpoint_id",
        )
        if cutoff_message_sequence is not None and type(
            cutoff_message_sequence
        ) is not int:
            raise TypeError(
                "fork_boundary.cutoff_message_sequence 必须是 int 或 None: "
                + repr(cutoff_message_sequence)
            )
        with self._connect(thread_id, checkpoint_ns, read_only=True) as connection:
            self._require_v2_runtime(connection)
            source = self._checkpoint_row(
                connection,
                checkpoint_ns,
                source_checkpoint_id,
            )
            if source is None:
                if source_checkpoint_id is not None:
                    raise RuntimeError(
                        "fork source checkpoint 行不存在: session="
                        + thread_id
                        + " checkpoint_id="
                        + source_checkpoint_id
                    )
                # 空 runtime 没有任何可见消息，边界平凡闭合。
                return
            view_id = strict_text(source[6], field="checkpoints.view_id")
            source_sequences = self._view_message_sequences(
                thread_id,
                checkpoint_ns,
                view_id,
                set(),
                connection=connection,
            )
            cutoff = len(source_sequences)
            if cutoff_message_sequence is not None:
                matching = [
                    index
                    for index, sequence in enumerate(source_sequences)
                    if sequence <= cutoff_message_sequence
                ]
                cutoff = matching[-1] + 1 if matching else 0
            validate_tool_protocol_closure(connection, source_sequences, cutoff)


__all__ = ["RolloutForkBoundaryOwnerMixin"]
