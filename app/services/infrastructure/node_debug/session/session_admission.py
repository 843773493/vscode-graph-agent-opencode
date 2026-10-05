"""Node 调试 Session 操作的生命周期准入。

调试 mutation（方案增删改、激活、导入/复制、启动/停止/重启、断点与求值等）
必须在 Session 生命周期准入下执行：目标 Session 必须存在且未删除，显式 thread
必须是该 Session 在权威目录索引中的物理子节点。这里只做准入判定，不承担任何
调试状态或进程编排。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.core.exceptions import NotFoundError
from app.services.infrastructure.node_debug.session.thread_owner import (
    NodeDebugThreadOwner,
    SessionLifecycleReader,
    SessionNodePathResolver,
    resolve_node_debug_owner,
)


class NodeDebugSessionAdmission:
    """持有单个 Session 生命周期 gate 并校验调试 owner。"""

    def __init__(
        self,
        *,
        session_service: SessionLifecycleReader,
        path_resolver: SessionNodePathResolver,
    ) -> None:
        self._session_service = session_service
        self._path_resolver = path_resolver

    @asynccontextmanager
    async def admit(
        self,
        session_id: str,
        thread_id: str | None,
    ) -> AsyncIterator[NodeDebugThreadOwner]:
        """在整个 owner 操作期间只持有目标 Session 的写准入 gate。"""
        owner_yielded = False
        try:
            async with self._session_service.session_write_admission(session_id):
                await self._session_service.get(session_id)
                try:
                    resolved_thread_id = (
                        self._path_resolver.main_thread_id(session_id)
                        if thread_id is None
                        else thread_id
                    )
                    owner = resolve_node_debug_owner(
                        self._path_resolver,
                        session_id=session_id,
                        thread_id=resolved_thread_id,
                    )
                except KeyError as error:
                    raise FileNotFoundError(
                        "调试目标 thread 不存在: "
                        f"session_id={session_id}, thread_id={thread_id}"
                    ) from error
                owner_yielded = True
                yield owner
        except NotFoundError as error:
            if owner_yielded:
                raise
            raise FileNotFoundError(
                f"调试目标会话不存在或已删除: session_id={session_id}"
            ) from error


__all__ = ["NodeDebugSessionAdmission"]
