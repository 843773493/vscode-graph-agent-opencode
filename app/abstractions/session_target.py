from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.abstractions.session_context import WorkspaceSessionContextAccessError


@dataclass(frozen=True, slots=True)
class SessionTarget:
    """经过解析的会话目标；workspace_id 为空表示当前工作区。"""

    session_id: str
    workspace_id: str | None = None


class SessionTargetResolutionError(WorkspaceSessionContextAccessError):
    """会话目标无法唯一解析时返回给工具调用方的错误。"""


class SessionTargetResolverProtocol(Protocol):
    async def resolve_session(
        self,
        session_id: str,
        *,
        workspace_id: str | None = None,
    ) -> SessionTarget: ...


__all__ = [
    "SessionTarget",
    "SessionTargetResolutionError",
    "SessionTargetResolverProtocol",
]
