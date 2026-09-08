"""跨 session fork 的 target-local identity 辅助。"""

from __future__ import annotations

import hashlib


class RolloutIdentityMixin:
    """生成 rollout 的稳定本地标识。"""

    def _rollout_id(self, thread_id: str) -> str:
        return (
            "rollout-"
            + hashlib.sha256(str(self.root(thread_id)).encode()).hexdigest()[:24]
        )


def target_local_acceptance_identity(
    *,
    target_session_id: str,
    fork_id: str,
    source_accepted_ingress_id: str,
    source_acceptance_key: str,
) -> tuple[str, str]:
    """生成不复用 source key 的 target ingress/key，并保留可审计映射输入。"""
    seed = (
        f"{target_session_id}|{fork_id}|{source_accepted_ingress_id}|"
        f"{source_acceptance_key}"
    )
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]
    return (
        f"fork-ingress:{target_session_id}:{digest}",
        f"fork-acceptance:{target_session_id}:{digest}",
    )


__all__ = ["RolloutIdentityMixin", "target_local_acceptance_identity"]
