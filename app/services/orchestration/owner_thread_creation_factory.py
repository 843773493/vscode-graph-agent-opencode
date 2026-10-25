"""Owner session 级 ThreadCreationService 工厂（OpenSpec 8.5-B，R25）。

``SessionSubagentService``` 经本工厂取得绑定 owner Session 的
``ThreadCreationService```：workspace catalog store 来自新 catalog
resolver 单例（``SessionCatalogPathResolver.store```），control store 按
owner Session 目录在显式操作作用域内建立并关闭，gate 为工厂级共享实例（同进程内 delegate
与其它 thread 创建操作互斥）。

红线：

- child thread 创建必须绑定 catalog resolver；取得 owner Session 的
  ThreadCreationService 时直接校验，不做降级、不扫盘、不猜测目录。
- control store 只按 owner Session 的 ``session-control.sqlite``` 建立
  （8.5-A 契约），不触碰 workspace 级其他状态。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from app.agents.graph_binding import compute_capability_profile_hash
from app.core.path_utils import get_session_path_resolver
from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.core.session_control_primitives import CONTROL_DATABASE_NAME
from app.core.session_control_store import SessionControlStore
from app.core.session_control_store.collaboration_fanout import (
    CollaborationFanoutRecord,
)
from app.core.session_lifecycle_gate import NavigationTopologyGate
from app.core.thread_creation import ThreadCreationService

__all__ = ["OwnerThreadCreationFactory"]


class OwnerThreadCreationFactory:
    """按 owner Session 为一次操作持有 Thread 创建依赖。"""

    def __init__(
        self,
        *,
        sessions_root: Path,
        workspace_id: str,
        path_resolver: SessionCatalogPathResolver | None = None,
    ) -> None:
        if not isinstance(sessions_root, Path):
            raise TypeError(f"sessions_root 必须是 Path: {sessions_root!r}")
        if not isinstance(workspace_id, str) or not workspace_id:
            raise ValueError(f"workspace_id 不能为空: {workspace_id!r}")
        if path_resolver is not None and not isinstance(
            path_resolver, SessionCatalogPathResolver
        ):
            raise TypeError(
                f"path_resolver 必须是 SessionCatalogPathResolver: "
                f"{path_resolver!r}"
            )
        self._sessions_root = sessions_root.expanduser().resolve()
        self._workspace_id = workspace_id
        self._resolver = (
            path_resolver
            if path_resolver is not None
            else get_session_path_resolver(self._sessions_root)
        )
        self._gate = NavigationTopologyGate(self._sessions_root)
        self._controls: set[SessionControlStore] = set()
        self._collaboration_publication: Callable[[CollaborationFanoutRecord], None] | None = None
        self._collaboration_lock = None

    def bind_collaboration_publication(self, callback: Callable[[CollaborationFanoutRecord], None], publication_lock) -> None:
        self._collaboration_publication = callback
        self._collaboration_lock = publication_lock
        for control in self._controls:
            control.bind_collaboration_publication(callback, publication_lock)

    @contextmanager
    def for_owner_session(self, session_id: str) -> Iterator[ThreadCreationService]:
        """连接覆盖本次查询或完整创建，成功、异常和取消都显式关闭。"""
        resolver = self._require_catalog_resolver()
        session_dir = self._session_dir_for(session_id)
        control_store = SessionControlStore(session_dir / CONTROL_DATABASE_NAME)
        with closing(control_store):
            self._controls.add(control_store)
            try:
                if self._collaboration_publication is not None:
                    control_store.bind_collaboration_publication(self._collaboration_publication, self._collaboration_lock)
                yield ThreadCreationService(
                    store=resolver.catalog_store,
                    control_store=control_store,
                    sessions_root=self._sessions_root,
                    workspace_id=self._workspace_id,
                    compute_capability_profile_hash=compute_capability_profile_hash,
                    gate=self._gate,
                )
            finally:
                self._controls.remove(control_store)

    def validate_owner_thread(self, session_id: str, thread_id: str) -> None:
        self._require_catalog_resolver().resolve_thread_node(session_id, thread_id)

    def _require_catalog_resolver(self) -> SessionCatalogPathResolver:
        """child thread 唯一入口的 resolver 模式校验（fail closed）。"""
        if not isinstance(self._resolver, SessionCatalogPathResolver):
            # 运行时模式错误，不是参数类型错误，不适用 TypeError。
            raise RuntimeError(  # noqa: TRY004
                "child thread 创建要求 catalog resolver: "
                f"sessions_root={self._sessions_root}"
            )
        return self._resolver

    def _session_dir_for(self, session_id: str) -> Path:
        node = self._require_catalog_resolver().catalog_store.get_node(session_id)
        locator = node.storage_relative_locator
        if locator is None:
            raise RuntimeError(
                "owner session 节点缺 storage_relative_locator（catalog 被"
                f"外部改动，fail closed）: session_id={session_id!r}"
            )
        return self._sessions_root / locator[len("sessions/"):]
