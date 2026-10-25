"""Thread runtime 驻留 owner 使用的内存状态与租约类型。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Hashable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Generic, Literal, Protocol, TypeAlias, TypeVar

from app.core.lifecycle import LifetimeScope
from app.core.thread_residency_contracts import ResidencyBlocker

RuntimeValue_co = TypeVar("RuntimeValue_co", covariant=True)
LeaseValue = TypeVar("LeaseValue")

# residency 是固定闭集；scope-owning runtime 的构建/释放在快照中投影为 loading/unloading。
ThreadResidencyStateName: TypeAlias = Literal[
    "cold", "loading", "resident", "unloading"
]


class ThreadRuntimeBuilder(Protocol[RuntimeValue_co]):
    """在 owner 已持有的 scope 中构建精确 thread runtime。"""

    def __call__(
        self, session_id: str, thread_id: str, lifetime_scope: LifetimeScope
    ) -> RuntimeValue_co | Awaitable[RuntimeValue_co]: ...


@dataclass(slots=True)
class _ThreadRuntimeSlot:
    session_id: str
    thread_id: str
    generation: int
    generation_key: Hashable
    value: object
    lifetime_scope: LifetimeScope
    active_leases: int = 0
    closing: bool = False
    drained: asyncio.Event = field(default_factory=asyncio.Event)


class _ThreadRuntimeLeaseOwner(Protocol):
    def _release_runtime_lease(self, runtime: _ThreadRuntimeSlot) -> None: ...


class ThreadRuntimeLease(Generic[LeaseValue]):
    """runtime 的显式使用权；释放后不能再从 lease 取得 runtime。"""

    __slots__ = ("_released", "_slot", "_tracker", "_value")

    def __init__(
        self,
        tracker: _ThreadRuntimeLeaseOwner,
        slot: _ThreadRuntimeSlot,
        value: LeaseValue,
    ) -> None:
        self._tracker = tracker
        self._slot = slot
        self._value = value
        self._released = False

    @property
    def session_id(self) -> str:
        return self._slot.session_id

    @property
    def thread_id(self) -> str:
        return self._slot.thread_id

    @property
    def generation(self) -> int:
        return self._slot.generation

    @property
    def runtime(self) -> LeaseValue:
        if self._released:
            raise RuntimeError("已释放的 ThreadRuntimeLease 不能继续使用")
        return self._value

    def release(self) -> None:
        """释放本次 runtime 使用权；重复释放幂等。"""
        if self._released:
            return
        self._released = True
        self._tracker._release_runtime_lease(self._slot)

    async def __aenter__(self) -> ThreadRuntimeLease[LeaseValue]:
        if self._released:
            raise RuntimeError("已释放的 ThreadRuntimeLease 不能重新进入")
        return self

    async def __aexit__(self, *_: object) -> None:
        self.release()


@dataclass(frozen=True, slots=True)
class ThreadResidencySnapshot:
    """只读 residency 快照（OpenSpec 2.8 字段清单）。

    ``residency``：``cold``（没有 resident runtime）、``loading``（builder 在飞）、
    ``resident``（runtime scope 可用）或 ``unloading``（scope 正在关闭或关闭失败待重试）。
    ``cold_eligible``：当前满足卸载条件且尚未卸载（无 blocker、idle 越过阈值、
    已登记 runtime generation）。
    ``execution_state``：execution 概要——有 blocker 时为 blocker 类别联合
    （如 ``node_debug_process``）；存在 runtime lease/admission 时为 ``active``，否则
    ``idle``。
    ``last_activity_at``/``idle_deadline``：墙钟展示值；阻断期间 ``idle_deadline``
    为 ``None``（idle 不累计，无 deadline 可言）。
    """

    session_id: str
    thread_id: str
    residency: ThreadResidencyStateName
    cold_eligible: bool
    execution_state: str
    last_activity_at: datetime | None
    idle_deadline: datetime | None
    idle_seconds: float | None
    blockers: tuple[ResidencyBlocker, ...]
    generation: int


@dataclass(slots=True)
class _ThreadResidencyState:
    """tracker 内部的 per-thread 记账状态（不对外发布）。"""

    session_id: str
    thread_id: str
    #: 每次真实建图成功后取得进程内唯一代际令牌；尚未建图时为 0。
    generation: int = 0
    #: ``None`` = 正被 blocker 阻断（idle 不累计）；否则为最近一次"无 blocker"起算锚点。
    unblocked_since: float | None = None
    #: 当前 generation 已触发过 unload 的标记（防止同一代重复卸载）。
    unloaded_generation: int | None = None
    #: push 登记的 blocker（key → 脱敏视图）；pull 源在评估时另行查询。
    blockers: dict[str, ResidencyBlocker] = field(default_factory=dict)
    #: 墙钟展示用：最近一次 idle 锚点重置时刻。
    last_activity_at: datetime | None = None
    #: 唯一真实 runtime 资源槽，由 generation scope 持有。
    runtime: _ThreadRuntimeSlot | None = None
    #: 同一精确 thread 的并发首次 admission 共享一个构建任务。
    runtime_build: asyncio.Task[_ThreadRuntimeSlot] | None = None
    #: 编译开始前已由 owner 持有；构建和清理双失败时仍保留供重试。
    runtime_build_scope: LifetimeScope | None = None
    #: 正在完成的 scope close；新 admission 等它收敛后再决定是否重建。
    runtime_unload: asyncio.Task[None] | None = None
    #: 已开始 admission、尚未拿到 lease 的等待者也阻止 idle unload。
    pending_runtime_leases: int = 0


__all__ = [
    "ThreadResidencySnapshot",
    "ThreadRuntimeBuilder",
    "ThreadRuntimeLease",
]
