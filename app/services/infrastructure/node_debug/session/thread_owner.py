"""Node 调试 owner 的精确归属解析。

NodeDebug 只接受 catalog 约束的 canonical ``(session_id, thread_id)``。
Session ID、逻辑 ``main`` 和其它历史别名都不是 thread 身份。
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from app.core.session_catalog_store import validate_thread_id

if TYPE_CHECKING:
    from app.schemas.internal_v2.session import SessionDTO


NodeDebugOwner = tuple[str, str]


class SessionNodePathResolver(Protocol):
    """解析会话与线程物理节点所需的权威目录索引能力。"""

    def main_thread_id(self, session_id: str) -> str: ...

    def resolve_session_node(self, session_id: str) -> Path: ...

    def resolve_thread_node(self, session_id: str, thread_id: str) -> Path: ...


class SessionLifecycleReader(Protocol):
    """NodeDebug mutation 共享的 Session 生命周期准入。"""

    def session_write_admission(
        self, session_id: str
    ) -> AbstractAsyncContextManager[None]: ...

    async def get(self, session_id: str) -> SessionDTO: ...


@dataclass(frozen=True, slots=True)
class NodeDebugThreadOwner:
    """精确到 SessionThread 的调试 owner 及其受检 thread 节点目录。"""

    session_id: str
    thread_id: str
    thread_node: Path

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id:
            raise ValueError("Node 调试 owner 必须包含非空 session_id")
        validate_thread_id(self.thread_id)

    @property
    def key(self) -> NodeDebugOwner:
        return (self.session_id, self.thread_id)


def normalize_thread_id(session_id: str, thread_id: str) -> str:
    """校验显式 thread 身份，不解析或接受任何别名。"""
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("Node 调试 owner 必须包含非空 session_id")
    validate_thread_id(thread_id)
    return thread_id


def resolve_debug_thread_node(
    path_resolver: SessionNodePathResolver,
    *,
    session_id: str,
    thread_id: str,
) -> Path:
    """按 canonical SessionThread 身份解析 Node Debug 线程目录。"""
    return resolve_node_debug_owner(
        path_resolver,
        session_id=session_id,
        thread_id=thread_id,
    ).thread_node


def resolve_node_debug_owner(
    path_resolver: SessionNodePathResolver,
    *,
    session_id: str,
    thread_id: str,
) -> NodeDebugThreadOwner:
    """通过权威 catalog 解析 canonical owner，拒绝 Session 与逻辑别名。"""
    canonical_thread_id = normalize_thread_id(session_id, thread_id)
    thread_node = path_resolver.resolve_thread_node(session_id, canonical_thread_id)
    return NodeDebugThreadOwner(
        session_id=session_id,
        thread_id=canonical_thread_id,
        thread_node=thread_node,
    )


def normalize_node_debug_owner(
    session_id: str,
    thread_id: str,
) -> NodeDebugOwner:
    """仅校验精确 owner key，不触碰目录索引（无持久化场景使用）。"""
    return (session_id, normalize_thread_id(session_id, thread_id))


__all__ = [
    "NodeDebugOwner",
    "NodeDebugThreadOwner",
    "SessionLifecycleReader",
    "SessionNodePathResolver",
    "normalize_node_debug_owner",
    "normalize_thread_id",
    "resolve_debug_thread_node",
    "resolve_node_debug_owner",
]
