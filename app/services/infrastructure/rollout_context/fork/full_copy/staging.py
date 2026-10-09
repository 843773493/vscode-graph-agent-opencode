"""完整 fork 的私有 storage owner；只重定向明确绑定的目标路径。"""

from __future__ import annotations

from pathlib import Path

from app.services.infrastructure.rollout_context.fork.store import RolloutForkStore
from app.services.infrastructure.rollout_context.runtime.detail_fork import (
    ForkDetailCapability,
)
from app.services.infrastructure.rollout_context.storage.service import RolloutStorage
from app.services.infrastructure.rollout_context.store import RolloutContextStore


class FullCopyStagingStorage(RolloutStorage):
    def __init__(
        self,
        owner: RolloutContextStore,
        *,
        source: str,
        target: str,
        root: Path,
        capability: ForkDetailCapability,
    ) -> None:
        super().__init__(
            owner.storage.sessions_dir, serde=owner.storage._serde, message_codec=owner.storage._message_codec,
            fork_recovery=owner.storage._fork_recovery, initialize_activation=owner.storage._initialize_activation
        )
        self._owner = owner.storage
        self._source = source
        self._target = target
        self._stage_root = root
        self._capability = capability

    def root(
        self,
        session_id: str,
        checkpoint_ns: str = "",
        *,
        thread_id: str | None = None,
    ) -> Path:
        """staging owner 只接受 source 与 target 的显式 main Thread。

        私有 staging 根是按 target session 建立的单个 rollout 目录（fork 的
        目标就是该 Session 的 main thread）；显式 non-main thread 在此没有
        独立 staging 根，必须 fail closed。
        """
        resolved_thread_id = self._resolve_thread_identity(session_id, thread_id)
        if session_id == self._target:
            expected_thread_id = self._resolve_thread_identity(self._target, None)
            if resolved_thread_id != expected_thread_id:
                raise ValueError(
                    "fork staging 只承载 target session 的 main thread rollout，"
                    f"不接受其它 thread_id: {resolved_thread_id!r}"
                )
            return self._stage_root
        if session_id == self._source:
            expected_thread_id = self._resolve_thread_identity(self._source, None)
            if resolved_thread_id != expected_thread_id:
                raise ValueError(
                    "fork staging 只读取 source session 的 main thread rollout，"
                    f"不接受其它 thread_id: {resolved_thread_id!r}"
                )
            return self._owner.root(
                session_id,
                checkpoint_ns,
                thread_id=resolved_thread_id,
            )
        raise ValueError("fork staging 只能访问显式 source/target")

class FullCopyForkStore(RolloutForkStore):
    """私有 full-copy owner 把 detail capability 限定在 staging 存储。"""

    def _require_private_detail_staging(self) -> None:
        stage = self._storage
        if stage._stage_root == stage._owner.root(stage._target):
            raise RuntimeError("fork staging 不得绑定已注册 target rollout")

    def _remap_full_copy_v2_entities(self, connection, **kwargs) -> None:
        super()._remap_full_copy_v2_entities(
            connection, **kwargs, detail_capability=self._storage._capability,
        )
