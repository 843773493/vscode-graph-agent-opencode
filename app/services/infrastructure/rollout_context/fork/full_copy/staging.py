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

    def root(
        self,
        session_id: str,
        checkpoint_ns: str = "",
        *,
        thread_id: str | None = None,
    ) -> Path:
        """staging owner 的 rollout 定位：只接受显式 source/target session。

        私有 staging 根是按 target session 建立的单个 rollout 目录（fork 的
        目标就是该 Session 的 main thread）；显式 non-main thread 在此没有
        独立 staging 根，必须 fail closed 而不是把 thread 当 session 静默解析。
        """
        if thread_id is not None:
            raise ValueError(
                "fork staging 只承载 target session 的 main thread rollout，"
                f"不接受显式 thread_id: {thread_id!r}"
            )
        if session_id == self._target:
            return self._stage_root
        if session_id == self._source:
            return self._owner.root(session_id, checkpoint_ns)
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
