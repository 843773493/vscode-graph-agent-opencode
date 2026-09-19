"""跨进程生命周期 gate、读 guard 与通用 operation lease 类型合同（OpenSpec 2.3-E/2.3-F）。

固定锁序（不得反向获取或同时持有两个 Session gate）::

    NavigationTopologyGate → 至多一个 SessionLifecycleGate / SessionReadGuard
    → 至多一个 SQLite 写事务

全部 gate 均为跨进程短时 fcntl.flock OS 锁：

- topology 锁文件：<workspace .boxteam>/navigation/topology.lock；
- session gate 锁文件：
  <workspace .boxteam>/navigation/session-lifecycle-gates/{session_id}.lock；
- 锁文件只创建、从不删除或重建（锁 inode 不重建）；进程退出时由 OS
  自动释放；同进程经不同 fd 重复获取同一把锁同样互斥（不可重入）。

通用 operation lease（SessionOperationLease）的字段集与状态闭集在
本模块冻结（design.md「通用lease」）；持久化实现（schema、非终态索引、
跨库「先 durable commit 再 terminal」顺序）位于 SessionControlStore。
lease 没有墙钟自动到期；恢复 owner 验证旧 holder 失效并 CAS 新 token
后才可继续或 settle。
"""

from __future__ import annotations

import asyncio
import fcntl
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from app.core.session_catalog_store import validate_session_id


class SessionDeletionPendingError(RuntimeError):
    """catalog/fence 已进入删除流，新业务准入或可见性发布必须取消（2.3-D）。

    统一错误合同：catalog 节点 deleting 或 local fence deleting 时，新
    thread 创建/history/detail/mutation 准入一律返回本错误（fail closed，
    不得回退 active、不得扫盘恢复）。调用方按稳定 identity 收敛已准入
    operation 后终止；本错误不重试、不换 key 重放。
    """


__all__ = [
    "SESSION_OPERATION_LEASE_KINDS",
    "SESSION_OPERATION_LEASE_STATES",
    "SESSION_OPERATION_LEASE_TERMINAL_STATES",
    "NavigationTopologyGate",
    "SessionDeletionPendingError",
    "SessionLifecycleGate",
    "SessionOperationLease",
    "SessionReadGuard",
]

# workspace 导航目录名（<workspace .boxteam>/navigation/）。
_NAVIGATION_DIRECTORY_NAME = "navigation"
# per-session gate 锁文件目录名（2.3-E 冻结路径）。
_SESSION_GATE_DIRECTORY_NAME = "session-lifecycle-gates"
# workspace 级 topology 锁文件名。
_TOPOLOGY_LOCK_FILE_NAME = "topology.lock"

# 通用 operation lease 状态闭集（design.md §544）。
SESSION_OPERATION_LEASE_STATES = (
    "active",
    "settling",
    "completed",
    "cancelled",
    "failed",
)
# 终态闭集：进入终态后旧 fencing token 的 callback 一律失败。
SESSION_OPERATION_LEASE_TERMINAL_STATES = ("completed", "cancelled", "failed")

# operation kind 闭集（design.md §544 原文冻结，不得按名称猜测扩展）。
SESSION_OPERATION_LEASE_KINDS = (
    "thread_creation",
    "board_migration",
    "collaboration_fanout",
    "runtime_owner",
    "execution",
    "context_control",
    "communication_source",
    "communication_target",
    "federated_call",
    "remote_observation",
    "attachment",
    "fork_retention",
    "session_catalog_mutation",
)


@dataclass(frozen=True, slots=True)
class SessionOperationLease:
    """session_operation_leases 表行的不可变投影（2.3-E 通用 lease）。

    captured_lifecycle_generation 是准入时捕获的 fence generation；
    holder_generation / fencing_token 是每行单调递增的领取/CAS 令牌
    （恢复接管时 +1，旧 token callback 一律失败）；state 闭集为
    active|settling|completed|cancelled|failed；lease 没有墙钟自动到期，
    recovery_ref 是可选的稳定恢复引用（不含物理路径或凭据）。
    """

    lease_id: str
    operation_kind: str
    operation_identity: str
    preimage_hash: str
    captured_lifecycle_generation: int
    holder_generation: int
    fencing_token: int
    state: str
    revision: int
    recovery_ref: str | None
    created_at: str
    updated_at: str


def _navigation_root(sessions_root: Path) -> Path:
    """由会话物理树根解析 workspace 导航目录。

    sessions_root 是 .boxteam/sessions/ 目录；其父目录即 workspace
    .boxteam/ 根（与会话目录权威索引、迁移 staging 的既有推导口径
    一致）。调用方必须显式传入受控运行时状态中的会话树根。
    """
    if not isinstance(sessions_root, Path):
        raise TypeError(f"sessions_root 必须是 Path: {sessions_root!r}")
    return sessions_root.expanduser().resolve().parent / _NAVIGATION_DIRECTORY_NAME


class _CrossProcessFileLock:
    """基于 fcntl.flock 的跨进程文件锁。

    每次获取独立 open 一个 fd：同进程两次获取经两个不同 fd 同样按
    OS 语义互斥（不可重入，无双 gate）；进程退出时 OS 关闭全部 fd 并
    自动释放锁。锁文件只创建（O_CREAT），从不 unlink 或重建。
    """

    def __init__(self, path: Path, *, exclusive: bool) -> None:
        self._path = path
        self._exclusive = exclusive
        self._fd: int | None = None

    @property
    def path(self) -> Path:
        """锁文件绝对路径（诊断/测试用）。"""
        return self._path

    async def __aenter__(self) -> Self:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        mode = fcntl.LOCK_EX if self._exclusive else fcntl.LOCK_SH
        try:
            await asyncio.to_thread(fcntl.flock, fd, mode)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        fd = self._fd
        self._fd = None
        if fd is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


class SessionReadGuard(_CrossProcessFileLock):
    """Session 共享读 guard（2.3-E；6.14 跨 Session copy 的 source guard）。

    LOCK_SH 覆盖 cold read / source snapshot 冻结的全部 handle 阶段；
    删除流对同一锁文件取 exclusive，因此必须等待全部 read guard 释放。
    guard 持有期间不得再取 topology exclusive 或另一个 Session gate
    （固定锁序）。
    """


class NavigationTopologyGate:
    """workspace 级跨进程导航拓扑 gate。

    exclusive() 临界区只包住 catalog 的单个 SQLite 事务与必要的预检；
    shared() 用于短时准入（fresh 校验 + 建立 lease/等价 record）。
    临界区必须短：不得在临界区内等待模型、网络或长时间 worker。
    锁序固定为 topology → Session gate → SQLite 写事务。
    """

    def __init__(self, sessions_root: Path) -> None:
        self._lock_path = _navigation_root(sessions_root) / _TOPOLOGY_LOCK_FILE_NAME

    @property
    def lock_path(self) -> Path:
        """topology 锁文件绝对路径（诊断/测试用）。"""
        return self._lock_path

    def exclusive(self) -> _CrossProcessFileLock:
        """以 exclusive 语义进入导航拓扑临界区（导航 create/move/delete）。"""
        return _CrossProcessFileLock(self._lock_path, exclusive=True)

    def shared(self) -> _CrossProcessFileLock:
        """以 shared 语义进入导航拓扑临界区（短时准入 fresh 校验）。"""
        return _CrossProcessFileLock(self._lock_path, exclusive=False)


class SessionLifecycleGate:
    """per-session 跨进程生命周期 gate。

    锁文件为 navigation/session-lifecycle-gates/{session_id}.lock。
    exclusive() 是短时准入/删除临界区；shared() 返回 SessionReadGuard，
    覆盖 cold read 的全部 handle。锁文件按 session_id 一会话一把，
    目录内不使用任何非 canonical ID 命名。
    """

    def __init__(self, sessions_root: Path) -> None:
        self._gates_root = (
            _navigation_root(sessions_root) / _SESSION_GATE_DIRECTORY_NAME
        )

    @property
    def gates_root(self) -> Path:
        """session gate 锁文件目录（诊断/测试用）。"""
        return self._gates_root

    def _lock_path(self, session_id: str) -> Path:
        validate_session_id(session_id)
        return self._gates_root / f"{session_id}.lock"

    def exclusive(self, session_id: str) -> _CrossProcessFileLock:
        """以 exclusive 语义进入指定 session 的生命周期临界区。"""
        return _CrossProcessFileLock(self._lock_path(session_id), exclusive=True)

    def shared(self, session_id: str) -> SessionReadGuard:
        """以 shared 语义取得指定 session 的读 guard（删除等待其释放）。"""
        return SessionReadGuard(self._lock_path(session_id), exclusive=False)
