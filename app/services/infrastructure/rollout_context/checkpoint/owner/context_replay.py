"""Saver 到 storage replay port 的专责适配。"""

from __future__ import annotations


class ContextReplayMixin:
    """把显式 replay 暴露为 Saver port，不拥有 Turn 持久化事实。"""

    def replay_as_new_turn(
        self,
        session_id: str,
        *,
        source_turn_id: str,
        checkpoint_ns: str = "",
        branch_id: str | None = None,
        acceptance_idempotency_key: str | None = None,
    ) -> dict[str, object]:
        """显式创建新 Turn 的 replay；不复用 source Turn/root。"""
        return self._storage.replay_as_new_turn(
            session_id,
            source_turn_id=source_turn_id,
            checkpoint_ns=checkpoint_ns,
            branch_id=branch_id,
            acceptance_idempotency_key=acceptance_idempotency_key,
        )


__all__ = ["ContextReplayMixin"]
