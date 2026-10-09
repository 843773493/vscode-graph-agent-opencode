"""后台工作通过既有 Session catalog/gate 访问唯一 SessionControlStore。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from app.core.path_utils import get_session_path_resolver
from app.core.session_control_primitives import CONTROL_DATABASE_NAME
from app.core.session_control_store import SessionControlStore
from app.core.session_lifecycle_gate import (
    NavigationTopologyGate,
    SessionDeletionPendingError,
    SessionLifecycleGate,
)


class BackgroundWorkStore:
    def __init__(self, *, sessions_dir: Path) -> None:
        self.sessions_dir = sessions_dir
        self._resolver = get_session_path_resolver(sessions_dir)
        self._topology = NavigationTopologyGate(sessions_dir)
        self._lifecycle = SessionLifecycleGate(sessions_dir)

    def session_ids(self) -> list[str]:
        _revision, nodes = self._resolver.catalog_store.read_nodes_snapshot()
        return [node.node_id for node in nodes if node.kind == "session" and node.state == "active"]

    def resolve_thread(self, session_id: str, thread_id: str) -> None:
        self._resolver.resolve_thread_node(session_id, thread_id)

    @contextmanager
    def control(self, session_id: str, *, read_only: bool = False) -> Iterator[SessionControlStore]:
        store = SessionControlStore(
            self._resolver.resolve_session_node(session_id) / CONTROL_DATABASE_NAME,
            read_only=read_only,
        )
        try:
            yield store
        finally:
            store.close()

    @asynccontextmanager
    async def admission(self, session_id: str) -> AsyncIterator[None]:
        async with self._topology.shared(), self._lifecycle.exclusive(session_id):
            if self._resolver.get_node(session_id).state != "active":
                raise SessionDeletionPendingError(f"Session 删除中，拒绝后台工作准入: {session_id}")
            with self.control(session_id) as control:
                if control.get_fence()[0] != "active":
                    raise SessionDeletionPendingError(f"Session fence 删除中: {session_id}")
            yield
