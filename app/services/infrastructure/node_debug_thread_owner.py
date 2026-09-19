"""Node 调试 owner 的精确归属解析。

本模块把 Session 级产品入口（裸 ``session_id``）与显式 thread 统一成精确的
``(session_id, thread_id)`` owner，并通过权威会话目录索引解析 thread 节点的
绝对路径。服务层不再保留任何隐式的 ``thread_id="main"`` 默认值：只有本模块
负责“裸 session_id 等价 main thread”这一条产品语义。

约束：
- 不拼接会话/线程物理路径；``main`` 节点就是会话自身节点，child thread 必须
  是目录索引中真实存在且归属于目标会话的物理子节点。
- 不扫描磁盘、不按 display name 或 session 猜测目标节点。
- 同一实体只能有一个 owner key：child thread 节点即子会话自身节点，因此
  ``(parent_session, child_session)`` 必须折叠为 ``(child_session, main)``，
  杜绝两个 key 共写同一 ``debug/node/`` 目录。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from app.schemas.internal_v2.session import SessionDTO

#: Session 级产品入口（裸 session_id）对应的 main thread 标识。
MAIN_THREAD_ID = "main"

#: 精确到 SessionThread 的调试 owner key。
NodeDebugOwner = tuple[str, str]


class SessionNodePathResolver(Protocol):
    """解析会话与线程物理节点所需的权威目录索引能力。"""

    def resolve_session_node(self, session_id: str) -> Path: ...

    def resolve_thread_node(self, session_id: str, thread_id: str) -> Path: ...


class SessionLifecycleReader(Protocol):
    """Session 生命周期准入所需的最小读取能力（由 SessionService 实现）。"""

    async def get(self, session_id: str) -> SessionDTO: ...


@dataclass(frozen=True, slots=True)
class NodeDebugThreadOwner:
    """精确到 SessionThread 的调试 owner 及其受检 thread 节点目录。"""

    session_id: str
    thread_id: str
    thread_node: Path

    @property
    def key(self) -> NodeDebugOwner:
        return (self.session_id, self.thread_id)


def normalize_thread_id(session_id: str, thread_id: str | None) -> str:
    """把 Session 级入口与显式 thread 归一成精确 thread_id。

    Session 级产品 API 的裸 ``session_id``（``thread_id`` 为 ``None``）等价于
    main thread；显式传入 ``main`` 或会话自身 ID 同样归一为 main thread。
    """
    if not session_id:
        raise ValueError("Node 调试 owner 必须包含非空 session_id")
    if thread_id is None:
        return MAIN_THREAD_ID
    normalized = thread_id.strip()
    if not normalized:
        raise ValueError("Node 调试 owner 的 thread_id 不能为空")
    if normalized == MAIN_THREAD_ID or normalized == session_id:
        return MAIN_THREAD_ID
    return normalized


def resolve_debug_thread_node(
    path_resolver: SessionNodePathResolver,
    *,
    session_id: str,
    thread_id: str,
) -> Path:
    """按权威目录索引解析 thread 节点绝对路径。

    ``main`` 使用会话自身节点；child thread 走受检的 ``resolve_thread_node``，
    由 resolver 校验它确实是目标会话的物理子节点。
    """
    canonical_thread_id = normalize_thread_id(session_id, thread_id)
    if canonical_thread_id == MAIN_THREAD_ID:
        return path_resolver.resolve_session_node(session_id)
    return path_resolver.resolve_thread_node(session_id, canonical_thread_id)


def resolve_node_debug_owner(
    path_resolver: SessionNodePathResolver,
    *,
    session_id: str,
    thread_id: str | None = None,
) -> NodeDebugThreadOwner:
    """归一 owner key 并解析其受检 thread 节点。

    单一归属决策：child thread 节点就是子会话自身节点，因此
    ``(parent_session, child_session)`` 与 ``(child_session, main)`` 是同一实体的
    两个地址，必须折叠成同一个 owner key ``(child_session, main)``。否则两个内存
    key 会共享同一物理目录 ``<child_node>/debug/node/``，manifest 互相覆盖。
    折叠以权威目录索引为准：只有当 ``thread_id`` 本身可按 session 寻址、且解析出的
    节点正是受检 thread 节点时才折叠；``threads/<id>`` 形态的 thread 节点保持
    ``(session_id, thread_id)`` 原样。
    """
    canonical_thread_id = normalize_thread_id(session_id, thread_id)
    if canonical_thread_id == MAIN_THREAD_ID:
        return NodeDebugThreadOwner(
            session_id=session_id,
            thread_id=MAIN_THREAD_ID,
            thread_node=path_resolver.resolve_session_node(session_id),
        )
    thread_node = resolve_debug_thread_node(
        path_resolver,
        session_id=session_id,
        thread_id=canonical_thread_id,
    )
    try:
        thread_session_node = path_resolver.resolve_session_node(canonical_thread_id)
    except KeyError:
        # 该 thread 不是可按 session 寻址的节点：保留受检的 (session_id, thread_id)。
        thread_session_node = None
    if thread_session_node != thread_node:
        # 生产 resolver 下 child thread 节点就是子会话自身节点，两者必然相等；不等时
        # 说明这是独立 thread 节点（如 ``threads/<id>`` 形态的实现），保持原 owner 形态。
        return NodeDebugThreadOwner(
            session_id=session_id,
            thread_id=canonical_thread_id,
            thread_node=thread_node,
        )
    return NodeDebugThreadOwner(
        session_id=canonical_thread_id,
        thread_id=MAIN_THREAD_ID,
        thread_node=thread_node,
    )


def normalize_node_debug_owner(
    session_id: str,
    thread_id: str | None = None,
) -> NodeDebugOwner:
    """只做 owner key 归一，不触碰目录索引（无持久化场景使用）。"""
    return (session_id, normalize_thread_id(session_id, thread_id))


__all__ = [
    "MAIN_THREAD_ID",
    "NodeDebugOwner",
    "NodeDebugThreadOwner",
    "SessionLifecycleReader",
    "SessionNodePathResolver",
    "normalize_node_debug_owner",
    "normalize_thread_id",
    "resolve_debug_thread_node",
    "resolve_node_debug_owner",
]
