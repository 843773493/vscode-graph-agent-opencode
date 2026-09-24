"""Context source delta 的唯一 revision、diff 与队列实现。"""

from __future__ import annotations

import difflib
import hashlib
from typing import Literal

from app.services.infrastructure.rollout_context.runtime.context_sources.models import (
        ContextSourceDelta,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.state import (
        SourceState,
)


class SourceDeltaMixin:
        def _apply_content(
            self,
            state: SourceState,
            content: str,
            *,
            kind: Literal["activation", "delta", "rebuild"],
        ) -> None:
            revision = _revision(content)
            if state.latest_revision == revision and state.latest_content == content:
                return
            state.latest_revision = revision
            state.latest_content = content
            state.pending_kind = kind
            self._queue_delta(state)

        def _record_observation(self, state: SourceState, revision: str) -> None:
            """按观察顺序记录 delta provenance；同一 revision 只记一次。"""
            if revision not in state.observed_revisions:
                state.observed_revisions.append(revision)

        def _queue_delta(self, state: SourceState) -> None:
            if state.latest_revision is None or state.latest_content is None:
                raise RuntimeError("Context source 缺少 latest revision/content")
            if state.pending_kind == "rebuild":
                self._pending[state.source_id] = ContextSourceDelta(
                    source_id=state.source_id,
                    source_name=state.name,
                    source_kind=state.source_kind,
                    revision=state.latest_revision,
                    previous_revision=state.latest_visible_committed_revision,
                    content=state.latest_content,
                    content_hash=_revision(state.latest_content),
                    kind="rebuild",
                )
                return
            previous_content = state.latest_visible_committed_content
            if previous_content is None:
                content = state.latest_content
                kind: Literal["activation", "delta", "rebuild"] = state.pending_kind or "activation"
            else:
                diff = "".join(
                    difflib.unified_diff(
                        previous_content.splitlines(keepends=True),
                        state.latest_content.splitlines(keepends=True),
                        fromfile=f"{state.name}@{state.latest_visible_committed_revision}",
                        tofile=f"{state.name}@{state.latest_revision}",
                    )
                )
                if not diff:
                    state.latest_revision = state.latest_visible_committed_revision
                    state.latest_content = state.latest_visible_committed_content
                    state.pending_kind = None
                    state.observed_revisions.clear()
                    self._pending.pop(state.source_id, None)
                    return
                content = diff
                kind = state.pending_kind or "delta"
            self._pending[state.source_id] = ContextSourceDelta(
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
