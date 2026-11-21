"""Context source delta 的唯一 revision、diff 与队列实现。"""

from __future__ import annotations

import hashlib
from typing import Literal

from app.core.text_diff import iter_unified_diff
from app.services.infrastructure.rollout_context.runtime.context_sources.models import (
        ContextSourceDelta,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.state import (
        SourceState,
)


class SourceDeltaMixin:
        def _queue_source(self, state: SourceState) -> None:
            if state.latest_revision is None or state.latest_content is None:
                raise RuntimeError("Context source 缺少 latest revision/content")
            # 从未向模型发布过的空来源没有需要激活或撤销的正文。
            if state.latest_visible_committed_revision is None and not state.latest_content:
                state.pending_kind = None
                state.observed_revisions.clear()
                self._pending.pop(state.source_id, None)
                return
            if (
                state.pending_kind not in {"activation", "rebuild"}
                and state.latest_revision == state.latest_visible_committed_revision
                and state.latest_content == state.latest_visible_committed_content
            ):
                state.latest_content = state.latest_visible_committed_content
                state.pending_kind = None
                state.observed_revisions.clear()
                self._pending.pop(state.source_id, None)
                return
            # 观察只合并来源标记；中间版本不会发给模型，不提前计算其 diff。
            self._pending[state.source_id] = None

        @staticmethod
        def _build_source_delta(state: SourceState) -> ContextSourceDelta:
            if state.latest_revision is None or state.latest_content is None:
                raise RuntimeError("Context source 缺少 latest revision/content")
            content = state.latest_content
            previous_content = state.latest_visible_committed_content
            kind: Literal["activation", "delta", "rebuild"] = state.pending_kind or (
                "activation" if previous_content is None else "delta"
            )
            if kind not in {"activation", "rebuild"} and previous_content is not None:
                diff = "".join(
                    # diff 不为缺少行尾的内容补分隔；用标准标记避免 -旧+新粘连。
                    line if line.endswith(("\n", "\r")) else line + "\n\\ No newline at end of file\n"
                    for line in iter_unified_diff(
                        previous_content,
                        state.latest_content,
                        fromfile=f"{state.name}@{state.latest_visible_committed_revision}",
                        tofile=f"{state.name}@{state.latest_revision}",
                    )
                )
                # semantic revision 与正文 hash 独立；同正文的新 owner revision
                # 仍需在下一次 seal 精确应用，不能回退到旧 revision。
                content = diff or state.latest_content
            return ContextSourceDelta(
                source_id=state.source_id,
                source_name=state.name,
                source_kind=state.source_kind,
                revision=state.latest_revision,
                previous_revision=(
                    state.latest_visible_committed_revision
                ),
                content=content,
                content_hash=_revision(content),
                observation_provenance=tuple(state.observed_revisions),
                kind=kind,
                selection_sequence=state.selection_sequence,
                control_revision=state.persisted_state_revision,
            )
def _adopt_restored_baseline(
    state: SourceState,
    content: str,
    revision: str,
) -> bool:
    """重启恢复后的首帧：同一 revision 只重建 diff 基准，不重复注入。

    只作用于刚从持久化控制状态恢复、还没有内存正文的 registration；
    untracked 状态在 ``observe`` 之前就被拦截，因此不会走到这里。
    """
    if (
        state.latest_visible_committed_revision is None
        or state.latest_visible_committed_revision != revision
        or state.latest_revision != revision
        or state.latest_visible_committed_content is not None
        or state.latest_content is not None
        or state.pending_kind is not None
    ):
        return False
    state.latest_visible_committed_content = content
    state.latest_content = content
    return True


def _revision(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


__all__ = ["SourceDeltaMixin", "_adopt_restored_baseline", "_revision"]
