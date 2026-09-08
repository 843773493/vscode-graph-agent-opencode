"""完整 fork 的私有 storage owner；只重定向明确绑定的目标路径。"""

from __future__ import annotations

from pathlib import Path

from app.services.infrastructure.rollout_context.runtime.detail_fork import (
    ForkDetailCapability,
)
from app.services.infrastructure.rollout_context.storage.service import RolloutStorage


class FullCopyStagingStorage(RolloutStorage):
    def __init__(
        self,
        owner: RolloutStorage,
        *,
        source: str,
        target: str,
        root: Path,
        capability: ForkDetailCapability,
        target_session_key: bytes,
    ) -> None:
        super().__init__(
            owner.sessions_dir, serde=owner._serde, message_codec=owner._message_codec
        )
        self._owner = owner
        self._source = source
        self._target = target
        self._stage_root = root
        self._capability = capability
        self._target_session_key = target_session_key

    def root(self, thread_id: str, checkpoint_ns: str = "") -> Path:
        if thread_id == self._target:
            return self._stage_root
        if thread_id == self._source:
            return self._owner.root(thread_id, checkpoint_ns)
        raise ValueError("fork staging 只能访问显式 source/target")

    def _require_private_detail_staging(self) -> None:
        if self._stage_root == self._owner.root(self._target):
            raise RuntimeError("fork staging 不得绑定已注册 target rollout")

    def _remap_full_copy_v2_entities(self, connection, **kwargs) -> None:
        super()._remap_full_copy_v2_entities(
            connection,
            **kwargs,
            detail_capability=self._capability,
            target_session_key=self._target_session_key,
        )
