"""无 durable owner 装配下的压缩 preflight 显式失败端口。"""

from __future__ import annotations

from collections.abc import Sequence


class NoDurableOwnerCompactionPreflight:
    """合成装配（无 RolloutCheckpointSaver durable owner）的显式失败端口。

    按合成/test assembly 合同：没有 durable owner 就不允许静默跳过
    compaction preflight；真实触发压缩时立即报错，不伪造安全结论。
    """

    def safe_compaction_prefix_cutoffs(
        self,
        session_id: str,
        *,
        checkpoint_ns: str,
        state_messages: Sequence[object],
        cutoff_indexes: Sequence[int],
    ) -> frozenset[int]:
        raise RuntimeError(
            "compaction preflight 需要 RolloutCheckpointSaver durable owner；"
            f"当前装配 session={session_id} 没有 Saver 端口"
        )
