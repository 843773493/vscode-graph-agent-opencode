"""Owner session 级 ThreadCreationService 工厂（OpenSpec 8.5-B，R25）。

``SessionSubagentService``` 经本工厂取得绑定 owner Session 的
``ThreadCreationService```：workspace catalog store 来自新 catalog
resolver 单例（``SessionCatalogPathResolver.store```），control store 按
owner Session 目录惰性建立并缓存，gate 为工厂级共享实例（同进程内 delegate
与其它 thread 创建操作互斥）。

红线：

- legacy resolver（``SessionPathResolver```）不支持 child thread 创建：
  取得 owner Session 的 ThreadCreationService 时明确报错，不做降级、不扫
  盘、不建任何目录；工厂构造本身不报错，保证 legacy opt-in 模式下后端与
  其余服务可正常启动。
- control store 只按 owner Session 的 ``session-control.sqlite``` 建立
  （8.5-A 契约），不触碰 workspace 级其他状态。
"""

from __future__ import annotations

from pathlib import Path

from app.agents.graph_binding import compute_capability_profile_hash
from app.core.path_utils import get_session_path_resolver
from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.core.session_control_store import SessionControlStore
from app.core.session_lifecycle_gate import NavigationTopologyGate
from app.core.thread_creation import ThreadCreationService

__all__ = ["OwnerThreadCreationFactory"]

_CONTROL_DATABASE_NAME = "session-control.sqlite"


class OwnerThreadCreationFactory:
    """按 owner Session 构建/复用绑定该 Session 的 ThreadCreationService。"""

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
        self._services: dict[str, ThreadCreationService] = {}

    def for_owner_session(self, session_id: str) -> ThreadCreationService:
        """返回绑定 owner Session 的 ThreadCreationService（进程内缓存）。"""
        resolver = self._require_catalog_resolver()
        service = self._services.get(session_id)
        if service is None:
            session_dir = self._session_dir_for(session_id)
            control_store = SessionControlStore(
                session_dir / _CONTROL_DATABASE_NAME
            )
            service = ThreadCreationService(
                store=resolver.catalog_store,
                control_store=control_store,
                sessions_root=self._sessions_root,
                workspace_id=self._workspace_id,
                compute_capability_profile_hash=compute_capability_profile_hash,
                gate=self._gate,
            )
            self._services[session_id] = service
        return service

    def owner_main_thread_id(self, session_id: str) -> str:
        """返回 owner Session 的唯一 main thread id（preimage 冻结面）。"""
        node = self._require_catalog_resolver().catalog_store.get_node(session_id)
        if node.kind != "session" or node.main_thread_id is None:
            raise RuntimeError(
                "owner 节点不是 session 或缺 main_thread_id（fail closed）: "
                f"session_id={session_id!r}, kind={node.kind!r}"
            )
        return str(node.main_thread_id)

    def _require_catalog_resolver(self) -> SessionCatalogPathResolver:
        """child thread 唯一入口的 resolver 模式校验（fail closed）。"""
        if not isinstance(self._resolver, SessionCatalogPathResolver):
            # 运行时模式错误（legacy resolver 不支持 thread 创建），不是
            # 参数类型错误，不适用 TypeError。
            raise RuntimeError(  # noqa: TRY004 —— 运行时模式错误，非类型错误
                "child thread 创建要求新 catalog resolver（当前为 legacy "
                "resolver，不支持 durable child thread，明确报错不降级）: "
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
