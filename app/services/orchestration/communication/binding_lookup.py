"""communication selector 的生产 binding lookup。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol

from app.core.session_catalog_store import validate_session_id
from app.core.session_control_store import SessionControlStore
from app.services.business.communication.wait import CommunicationWaitBinding


class SessionPathResolverPort(Protocol):
    """只依赖 session-control 冷读所需的路径解析能力。"""

    def resolve_session_node(self, session_id: str) -> Path: ...


class SessionControlStoreWaitBindingLookup:
    """按显式 target session 冷读 communication inbox，不加载目标 runtime。"""

    def __init__(self, *, path_resolver: SessionPathResolverPort) -> None:
        self._path_resolver = path_resolver

    async def resolve(
        self,
        *,
        target_session_id: str,
        communication_id: str,
    ) -> CommunicationWaitBinding | None:
        validate_session_id(target_session_id)
        return await asyncio.to_thread(
            self._resolve_sync,
            target_session_id=target_session_id,
            communication_id=communication_id,
        )

    def _resolve_sync(
        self,
        *,
        target_session_id: str,
        communication_id: str,
    ) -> CommunicationWaitBinding | None:
        """同步冷读；磁盘 IO 由 asyncio.to_thread 隔离。"""
        session_node = self._path_resolver.resolve_session_node(target_session_id)
        control_path = session_node / "session-control.sqlite"
        if not control_path.is_file():
            return None
        store = SessionControlStore(control_path)
        try:
            inbox = store.get_communication_inbox(communication_id)
        except KeyError:
            return None
        finally:
            store.close()
        if inbox.session_id != target_session_id:
            raise RuntimeError(
                "communication inbox 所属 session 与请求不一致（fail closed）: "
                f"expected={target_session_id!r}, actual={inbox.session_id!r}"
            )
        return CommunicationWaitBinding(
            target_session_id=inbox.session_id,
            target_main_thread_id=inbox.target_thread_id,
            job_id=inbox.job_id,
            turn_id=inbox.turn_id,
        )
