"""资源 owner 测试共用的会话/线程定位与准入替身。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from app.schemas.internal_v2.session import SessionDTO
from app.services.business.session_context_resource import (
    session_resource_identity,
    session_resource_uri,
)

WORKSPACE_ID = "00000000-0000-4000-8000-000000000001"


SESSION_A = "ses_019b94653e71714b8d406f83ab3d2b3b"


SESSION_B = "ses_019b94653e71714b8d406f83ab3d2b31"


THREAD_A = "thr_019b94653e71714b8d406f83ab3d2b3b"


THREAD_B = "thr_019b94653e71714b8d406f83ab3d2b31"


CHILD_THREAD_A = "thr_019b94653e71714b8d406f83ab3d2b32"


class ResourceOwnerSessionsFake:
    """提供定位、主线程和写准入；不创建真实 Session catalog。"""

    workspace_id = WORKSPACE_ID

    def __init__(self, root: Path) -> None:
        self.root = root
        # 每个Session的线程列表，首项是主线程；与磁盘导航树无关。
        self.refs = {SESSION_A: [THREAD_A, CHILD_THREAD_A], SESSION_B: [THREAD_B]}
        self.closed: set[str] = set()
        self.admitted: list[str] = []
        self.unloading_generation = 1

    def accepts_release(self, session_id: str, thread_id: str, generation: int) -> bool:
        return thread_id in self.refs[session_id] and generation == self.unloading_generation

    @asynccontextmanager
    async def session_write_admission(self, session_id: str) -> AsyncIterator[None]:
        if session_id in self.closed:
            raise ValueError("Session 准入已关闭")
        await self.get(session_id)
        self.admitted.append(session_id)
        yield

    async def get(self, session_id: str) -> SessionDTO:
        return SessionDTO(session_id=session_id, workspace_id=WORKSPACE_ID,
                          resource_identity=session_resource_identity(WORKSPACE_ID, session_id),
                          uri=session_resource_uri(WORKSPACE_ID, session_id), scope="workspace",
                          title="测试会话", current_agent_id="default",
                          created_at=datetime(2026, 10, 5, tzinfo=UTC),
                          updated_at=datetime(2026, 10, 5, tzinfo=UTC),
                          thread_id=self.refs[session_id][0])

    def main_thread_id(self, session_id: str) -> str:
        return self.refs[session_id][0]

    def resolve_thread_node(self, session_id: str, thread_id: str) -> Path:
        if thread_id not in self.refs[session_id]:
            raise KeyError("目标 Thread 不在本 Session catalog 中")
        return self.root / session_id / thread_id
