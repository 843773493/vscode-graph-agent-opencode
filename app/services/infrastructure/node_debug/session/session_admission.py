"""Node 调试 mutation 的 Session 生命周期准入。

调试 mutation（方案增删改、激活、导入/复制、启动/停止/重启、断点与求值等）
必须在 Session 生命周期准入下执行：目标 Session 必须存在且未删除，显式 thread
必须是该 Session 在权威目录索引中的物理子节点。这里只做准入判定，不承担任何
调试状态或进程编排。
"""

from __future__ import annotations

from app.core.exceptions import NotFoundError
from app.services.infrastructure.node_debug.session.thread_owner import (
    NodeDebugThreadOwner,
    SessionLifecycleReader,
    SessionNodePathResolver,
    resolve_node_debug_owner,
)


class NodeDebugSessionAdmission:
    """校验调试 mutation 的 SessionThread owner 准入。"""

    def __init__(
        self,
        *,
        session_service: SessionLifecycleReader,
        path_resolver: SessionNodePathResolver,
    ) -> None:
        self._session_service = session_service
        self._path_resolver = path_resolver

    async def admit(
        self,
        session_id: str,
        thread_id: str,
    ) -> NodeDebugThreadOwner:
        """返回受检的精确 owner；Session 缺失/已删除或 thread 不合法时直接失败。"""
        try:
            await self._session_service.get(session_id)
        except NotFoundError as error:
            raise FileNotFoundError(
                f"调试目标会话不存在或已删除: session_id={session_id}"
            ) from error
        try:
            return resolve_node_debug_owner(
                self._path_resolver,
                session_id=session_id,
                thread_id=thread_id,
            )
        except KeyError as error:
            raise FileNotFoundError(
                "调试目标 thread 不存在: "
                f"session_id={session_id}, thread_id={thread_id}"
            ) from error


__all__ = ["NodeDebugSessionAdmission"]
