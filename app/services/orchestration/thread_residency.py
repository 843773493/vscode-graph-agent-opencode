"""ThreadResidency：thread 级 30 分钟 idle 卸载判定与 debug 进程 idle blocker 登记。

对应 OpenSpec ``add-context-injection-lifecycle`` 任务 2.8 与
``add-itemized-rollout-context`` 任务 8.8/8.8-A 的 residency 基础设施切片：

- 可注入单调时钟（默认 ``time.monotonic``）是 idle 计时的唯一依据；墙钟只进快照
  展示字段，绝不参与 idle 判定。产品阈值 30 分钟不缩短，测试用 fake clock 验证
  29:59/30:00 边界。
- debug owner 核实的 ``launch_pending|starting|running|paused|stopping`` 进程 claim
  与 ``reconcile_required`` 映射为精确 ``(session_id, thread_id)`` 的 idle blocker：
  blocker 活跃期间不累计 idle；核实终态且 lease 结清（blocker 解除）后从解除时刻
  重新起算。
- backend 重启恢复：评估时通过 :class:`ResidencyBlockerSource` 向 debug owner 拉取
  磁盘上的活跃 durable claim，全新 tracker 也不会把有活跃占用的 thread 虚报为
  cold-eligible。
- generation-only unload 回调：thread 无 blocker 且跨过阈值时调用既有 owner callback；
  进程内资源的唯一释放合同仍是 ``app/core/lifecycle.py`` 的 ``LifetimeScope``。
- scope-owning runtime 入口：按精确 ``(session_id, thread_id)`` single-flight 建立
  runtime，以显式 lease 阻止 idle unload，并由 tracker 在 idle 时关闭其唯一 scope。

红线（8.8-A，落地为注释与代码边界）：

1. residency owner 不据 scope close、PID/端口重用或内存缺项推断进程停止；进程事实
   只能由 debug owner 以 durable claim + OS 起始身份核实后，经 blocker 上报进入本模块。
2. idle unload 不新增 item、epoch 或上下文到期状态：本轮无 CSM 接线，unload 回调只
   释放进程内 runtime 资源，不触碰任何上下文账目（后续 CSM 接入轮必须保持该边界）。
3. 迟到 callback 不得写旧 generation：generation-only callback 仍须在写回前核验
   精确 thread/generation；scope-owning runtime 在关闭开始或失败后立即 fence 旧代。
   本切片只提供 owner API，不接入生产 GraphBinding/admission。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Generic, Literal, Protocol, TypeAlias, TypeVar, cast

from app.core.lifecycle import LifetimeScope

logger = logging.getLogger(__name__)

#: 产品阈值：连续 30 分钟无活动且无 blocker 才允许 idle unload（1800 秒）。
#: 测试必须用 fake clock 推进到 29:59（仍 resident）与 30:00（cold-eligible）验证
#: 边界，绝不缩短该常量。
THREAD_IDLE_UNLOAD_SECONDS: float = 30 * 60

#: idle 计时的唯一时钟形态：单调秒。默认 ``time.monotonic``，测试注入可推进 fake。
ResidencyClock: TypeAlias = Callable[[], float]

#: 墙钟只用于快照展示（``last_activity_at``/``idle_deadline``），不参与 idle 判定。
WallClock: TypeAlias = Callable[[], datetime]

#: residency 是固定闭集；scope-owning runtime 的构建/释放在快照中投影为 loading/unloading。
ThreadResidencyStateName: TypeAlias = Literal[
    "cold", "loading", "resident", "unloading"
]


@dataclass(frozen=True, slots=True)
class ResidencyBlocker:
    """idle blocker 的脱敏视图。

    只携带 blocker 类别与固定话术 reason；绝不携带 PID、端口、路径或
    process_instance_id 正文（脱敏由上报方负责，本模块原样透传）。
    """

    kind: str
    reason: str


@dataclass(frozen=True, slots=True)
class ThreadResidencyGeneration:
    """一次 ``register_generation`` 登记的 resident runtime 代令牌。"""

    session_id: str
    thread_id: str
    generation: int


@dataclass(frozen=True, slots=True)
class ThreadUnloadRequest:
    """unload 回调的入参：携带决策时刻的 generation 令牌与 idle 事实。

    回调持有方据此释放该 generation 的 ``LifetimeScope``；回调内若要把任何状态
    写回 thread，必须先用该令牌向真实 owner 重新核验当前代（迟到 callback 不得
    写旧 generation）。
    """

    session_id: str
    thread_id: str
    generation: int
    idle_seconds: float


#: owner 注册的卸载回调：同步或异步；抛错原样向上传播，绝不被吞。
UnloadCallback: TypeAlias = Callable[[ThreadUnloadRequest], object]

RuntimeValue_co = TypeVar("RuntimeValue_co", covariant=True)
LeaseValue = TypeVar("LeaseValue")
TaskResult = TypeVar("TaskResult")


@dataclass(frozen=True, slots=True)
class ThreadRuntime(Generic[RuntimeValue_co]):
    """一个精确 thread 独占的进程内 runtime 及其唯一释放 scope。"""

    value: RuntimeValue_co
    lifetime_scope: LifetimeScope


class ThreadRuntimeBuilder(Protocol[RuntimeValue_co]):
    """按调用方显式给出的 session/thread pair 构建 thread runtime。"""

    def __call__(
        self, session_id: str, thread_id: str
    ) -> ThreadRuntime[RuntimeValue_co] | Awaitable[ThreadRuntime[RuntimeValue_co]]: ...


@dataclass(slots=True)
class _ThreadRuntimeSlot:
    session_id: str
    thread_id: str
    generation: int
    value: object
    lifetime_scope: LifetimeScope
    active_leases: int = 0
    closing: bool = False


class ThreadRuntimeLease(Generic[LeaseValue]):
    """runtime 的显式使用权；释放后不能再从 lease 取得 runtime。"""

    __slots__ = ("_released", "_slot", "_tracker", "_value")

    def __init__(
        self,
        tracker: ThreadResidencyTracker,
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


class ResidencyBlockerSource(Protocol):
    """blocker 拉取源协议：评估时向 owner（如 debug 服务）查询活跃 blocker。

    用于 backend 重启恢复（pull）：tracker 无需 owner 推送也能看到磁盘上的活跃
    durable claim。实现方必须只上报"owner 已核实的占用"，不做进程状态猜测。
    """

    def residency_blockers(
        self, session_id: str, thread_id: str
    ) -> Sequence[ResidencyBlocker]: ...


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
    #: 0 = tracker 尚未见到任何 resident runtime 登记；owner 每次 register_generation 递增。
    generation: int = 0
    #: ``None`` = 正被 blocker 阻断（idle 不累计）；否则为最近一次"无 blocker"起算锚点。
    unblocked_since: float | None = None
    #: 当前 generation 已触发过 unload 的标记（防止同一代重复卸载）。
    unloaded_generation: int | None = None
    #: 在飞卸载标记：该 generation 的 unload 回调正在进行中（await 窗口内）。
    #: 并发 sweep 据此去重，确保同一代回调至多触发一次。
    unloading_generation: int | None = None
    #: push 登记的 blocker（key → 脱敏视图）；pull 源在评估时另行查询。
    blockers: dict[str, ResidencyBlocker] = field(default_factory=dict)
    #: 墙钟展示用：最近一次 idle 锚点重置时刻。
    last_activity_at: datetime | None = None
    #: 新 runtime owner 的资源槽；generation-only 调用方仍走既有 unload callback。
    runtime: _ThreadRuntimeSlot | None = None
    #: 明确禁止手动 generation API 与 scope-owning runtime API 混用。
    runtime_managed: bool = False
    #: 同一精确 thread 的并发首次 admission 共享一个构建任务。
    runtime_build: asyncio.Task[_ThreadRuntimeSlot] | None = None
    #: 正在完成的 scope close；新 admission 等它收敛后再决定是否重建。
    runtime_unload: asyncio.Task[None] | None = None
    #: 已开始 admission、尚未拿到 lease 的等待者也阻止 idle unload。
    pending_runtime_leases: int = 0


class ThreadResidencyTracker:
    """30 分钟 idle 卸载判定器：blocker 登记、idle 记账与 unload 回调缝。

    单事件循环假设：全部同步方法内部无 ``await``，同步记账不需要加锁；``sweep``
    在回调 ``await`` 期间不修改遍历集合（先快照再遍历），并以 per-thread 的在飞
    标记（``unloading_generation``）保证同一代的 unload 回调至多触发一次。
    """

    def __init__(
        self,
        *,
        clock: ResidencyClock = time.monotonic,
        wall_clock: WallClock | None = None,
        idle_timeout_seconds: float = THREAD_IDLE_UNLOAD_SECONDS,
        unload_callback: UnloadCallback | None = None,
    ) -> None:
        if idle_timeout_seconds <= 0:
            raise ValueError(
                f"idle 阈值必须是正数: {idle_timeout_seconds!r}"
            )
        self._clock = clock
        self._wall_clock = wall_clock or (lambda: datetime.now(UTC))
        self._idle_timeout_seconds = idle_timeout_seconds
        self._unload_callback = unload_callback
        self._blocker_sources: list[ResidencyBlockerSource] = []
        self._states: dict[tuple[str, str], _ThreadResidencyState] = {}

    # ---- 装配期（app/container.py） ----

    def add_blocker_source(self, source: ResidencyBlockerSource) -> None:
        """登记一个 blocker 拉取源（重启恢复兜底）；缺方法时显式失败。"""
        if not callable(getattr(source, "residency_blockers", None)):
            raise TypeError(
                "ResidencyBlockerSource 必须实现 residency_blockers(session_id, thread_id): "
                f"{type(source)!r}"
            )
        self._blocker_sources.append(source)

    def set_unload_callback(self, callback: UnloadCallback) -> None:
        """装配期注册 unload 回调（runtime owner 释放可重建资源的唯一入口）。

        与构造参数等价；供 container 先建 tracker、后建 runtime owner 的装配
        顺序使用。重复注册以后一次为准，不做双轨兼容。
        """
        if not callable(callback):
            raise TypeError(f"UnloadCallback 必须可调用: {type(callback)!r}")
        self._unload_callback = callback

    # ---- owner / 活动入口 ----

    def record_activity(self, session_id: str, thread_id: str) -> None:
        """记录 thread 活动：未被阻断时把 idle 起算锚点重置为当前时刻。

        阻断期间 idle 本就不累计，锚点保持 ``None``，由 blocker 解除时刻统一重启
        计时（OpenSpec 2.8：解除后重新起算）。
        """
        state = self._ensure_state(session_id, thread_id)
        if state.unblocked_since is not None:
            self._mark_unblocked(state)

    def register_generation(
        self, session_id: str, thread_id: str
    ) -> ThreadResidencyGeneration:
        """登记新一代 resident runtime（generation 递增并清除上一代的卸载标记）。

        新一代意味着旧的已卸载 runtime 已被 owner 重建；上一代的 unload 结果不再
        约束新一代。
        """
        state = self._ensure_state(session_id, thread_id)
        if state.runtime_managed:
            raise RuntimeError(
                "ThreadRuntime 已由 acquire_runtime 管理，不能手动登记 generation: "
                f"session_id={session_id}, thread_id={thread_id}"
            )
        state.generation += 1
        state.unloaded_generation = None
        return ThreadResidencyGeneration(
            session_id=state.session_id,
            thread_id=state.thread_id,
            generation=state.generation,
        )

    def is_current_generation(
        self, session_id: str, thread_id: str, generation: int
    ) -> bool:
        """generation fence：迟到 callback 行动前必须核验当前代。

        只有 generation 仍是该 thread 当前登记代且尚未卸载时返回 True；未知
        thread、非法代、已被新一代取代的过期代或已卸载代一律 False（fail
        closed：迟到 callback 不得凭过期 generation 释放或写回任何状态）。
        """
        state = self._states.get((session_id, thread_id))
        if state is None or generation <= 0:
            return False
        runtime = state.runtime
        if runtime is not None and runtime.generation == generation:
            return not runtime.closing and runtime.lifetime_scope.state == "open"
        return (
            state.generation == generation
            and state.unloaded_generation != generation
        )

    async def acquire_runtime(
        self,
        session_id: str,
        thread_id: str,
        builder: ThreadRuntimeBuilder[RuntimeValue_co],
    ) -> ThreadRuntimeLease[RuntimeValue_co]:
        """取得精确 thread 的 runtime lease，首次或 cold admission 时 single-flight 建图。

        session/thread 必须由 admission 调用方显式传入。构建失败原样传播且不
        登记 generation，下一次调用可以重试；等待中的 admission 也计作 pending
        lease，所以 idle sweep 不会抢先关闭刚构建的 scope。
        """
        if not session_id or not thread_id:
            raise ValueError("ThreadRuntime owner 必须显式提供 session_id 与 thread_id")
        if not callable(builder):
            raise TypeError(f"ThreadRuntimeBuilder 必须可调用: {type(builder)!r}")
        state = self._ensure_state(session_id, thread_id)
        if state.generation > 0 and not state.runtime_managed:
            raise RuntimeError(
                "已手动登记的 generation 不能切换为 acquire_runtime 管理: "
                f"session_id={session_id}, thread_id={thread_id}"
            )
        state.runtime_managed = True

        while True:
            unload = state.runtime_unload
            if unload is not None:
                await asyncio.shield(unload)
                continue

            runtime = state.runtime
            if runtime is not None:
                if runtime.closing or runtime.lifetime_scope.state != "open":
                    unload = self._start_runtime_unload(state, runtime)
                    await asyncio.shield(unload)
                    continue
                runtime.active_leases += 1
                state.unblocked_since = None
                return ThreadRuntimeLease(
                    self,
                    runtime,
                    cast(RuntimeValue_co, runtime.value),
                )

            state.pending_runtime_leases += 1
            build = state.runtime_build
            if build is None:
                build = asyncio.create_task(
                    self._build_runtime(
                        state,
                        cast(ThreadRuntimeBuilder[object], builder),
                    ),
                    name=(
                        f"thread-runtime-builder:{state.session_id}:{state.thread_id}"
                    ),
                )
                state.runtime_build = build
                build.add_done_callback(self._consume_task_exception)
            try:
                runtime = await asyncio.shield(build)
            except BaseException:
                state.pending_runtime_leases -= 1
                raise

            state.pending_runtime_leases -= 1
            if state.runtime is not runtime or runtime.closing:
                # 显式关闭或代际 fence 可能已替换 owner slot；按精确 owner 重试，不能返回旧 runtime。
                continue
            runtime.active_leases += 1
            state.unblocked_since = None
            return ThreadRuntimeLease(
                self,
                runtime,
                cast(RuntimeValue_co, runtime.value),
            )

    async def _build_runtime(
        self,
        state: _ThreadResidencyState,
        builder: ThreadRuntimeBuilder[object],
    ) -> _ThreadRuntimeSlot:
        task = asyncio.current_task()
        try:
            result = builder(state.session_id, state.thread_id)
            if inspect.isawaitable(result):
                result = await cast(Awaitable[ThreadRuntime[object]], result)
            if not isinstance(result, ThreadRuntime):
                raise TypeError(
                    f"ThreadRuntimeBuilder 必须返回 ThreadRuntime: {type(result)!r}"
                )
            if not isinstance(result.lifetime_scope, LifetimeScope):
                raise TypeError(
                    "ThreadRuntime.lifetime_scope 必须是 LifetimeScope: "
                    f"{type(result.lifetime_scope)!r}"
                )
            if result.lifetime_scope.state != "open":
                raise RuntimeError(
                    "ThreadRuntimeBuilder 返回的 scope 不可用: "
                    f"state={result.lifetime_scope.state}"
                )

            generation = state.generation + 1
            runtime = _ThreadRuntimeSlot(
                session_id=state.session_id,
                thread_id=state.thread_id,
                generation=generation,
                value=result.value,
                lifetime_scope=result.lifetime_scope,
            )
            state.runtime = runtime
            state.generation = generation
            state.unloaded_generation = None
            state.unblocked_since = None
            return runtime
        finally:
            if state.runtime_build is task:
                state.runtime_build = None

    def _release_runtime_lease(self, runtime: _ThreadRuntimeSlot) -> None:
        state = self._states.get((runtime.session_id, runtime.thread_id))
        if state is None or state.runtime is not runtime:
            raise RuntimeError(
                "ThreadRuntimeLease 不属于当前 owner generation: "
                f"session_id={runtime.session_id}, thread_id={runtime.thread_id}, "
                f"generation={runtime.generation}"
            )
        if runtime.active_leases <= 0:
            raise RuntimeError(
                "ThreadRuntimeLease 计数异常: "
                f"session_id={runtime.session_id}, thread_id={runtime.thread_id}, "
                f"generation={runtime.generation}"
            )
        runtime.active_leases -= 1
        if runtime.active_leases == 0 and state.pending_runtime_leases == 0:
            # 从 lease 释放时刻重启 idle；pull blocker 查询失败时保留错误且不计 idle。
            state.unblocked_since = None
            self._observe(state)

    def _start_runtime_unload(
        self,
        state: _ThreadResidencyState,
        runtime: _ThreadRuntimeSlot,
    ) -> asyncio.Task[None]:
        if state.runtime_unload is not None:
            return state.runtime_unload
        runtime.closing = True
        task = asyncio.create_task(
            self._close_runtime(state, runtime),
            name=(
                f"thread-runtime-close:{runtime.session_id}:{runtime.thread_id}:"
                f"{runtime.generation}"
            ),
        )
        state.runtime_unload = task
        task.add_done_callback(self._consume_task_exception)
        return task

    async def _close_runtime(
        self,
        state: _ThreadResidencyState,
        runtime: _ThreadRuntimeSlot,
    ) -> None:
        task = asyncio.current_task()
        try:
            await runtime.lifetime_scope.close()
            if state.runtime is runtime and state.generation == runtime.generation:
                state.runtime = None
                state.unloaded_generation = runtime.generation
        except BaseException:
            runtime.closing = False
            raise
        finally:
            if state.runtime_unload is task:
                state.runtime_unload = None

    @staticmethod
    def _consume_task_exception(task: asyncio.Task[TaskResult]) -> None:
        """后台 owner task 结束时记录异常；等待者仍会收到原异常。"""
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "Thread runtime 后台任务失败: task=%s",
                task.get_name(),
                exc_info=(type(error), error, error.__traceback__),
            )

    # ---- blocker push（debug owner 核实的相位变化） ----

    def register_blocker(
        self,
        session_id: str,
        thread_id: str,
        *,
        blocker_key: str,
        kind: str,
        reason: str,
    ) -> None:
        """登记/更新一个 idle blocker：阻断期间 idle 不累计。"""
        if not blocker_key:
            raise ValueError("blocker_key 不能为空")
        if not kind:
            raise ValueError("blocker kind 不能为空")
        if not reason:
            raise ValueError("blocker reason 不能为空（脱敏话术也必须说明阻断原因）")
        state = self._ensure_state(session_id, thread_id)
        state.blockers[blocker_key] = ResidencyBlocker(kind=kind, reason=reason)
        state.unblocked_since = None

    def release_blocker(self, session_id: str, thread_id: str, *, blocker_key: str) -> None:
        """解除一个 blocker；解除最后一个后从当前时刻重新起算 idle。

        解除不存在的 key 是幂等无操作（pull 源可能已先于 push 看到解除）。
        """
        state = self._states.get((session_id, thread_id))
        if state is None:
            return
        state.blockers.pop(blocker_key, None)
        if (
            not state.blockers
            and state.unblocked_since is None
            and not self._has_runtime_users(state)
        ):
            # 解除时重新查询 pull 源，避免 blocker 未被观察到时把阻断时长计入 idle。
            self._observe(state)

    # ---- 查询与评估 ----

    def snapshot(self, session_id: str, thread_id: str) -> ThreadResidencySnapshot:
        """只读快照：汇总 push/pull blocker 与 idle 记账，绝不触发 unload 回调。"""
        state = self._ensure_state(session_id, thread_id)
        blockers = self._observe(state)
        return self._build_snapshot(state, blockers)

    async def sweep(self) -> tuple[ThreadResidencySnapshot, ...]:
        """评估全部已知 thread；对 cold-eligible 的触发 owner 的 unload 回调。

        回调抛错原样向上传播（fail-loud），对应 thread 不标记卸载，下次 sweep 可重试；
        只有回调成功返回后才标记 ``unloaded_generation``。标记严格按**回调前快照**
        的 generation 做 CAS：回调 await 期间若 owner 登记了新一代（generation
        递增），说明新一代已是当前代，绝不把新一代错标为已卸载；同一代已由另一个
        并发 sweep 在飞卸载时，本调用方不重复触发回调。
        """
        fired: list[ThreadResidencySnapshot] = []
        for key in list(self._states):
            state = self._states[key]
            snapshot = await self._sweep_thread(state)
            if snapshot is not None:
                fired.append(snapshot)
        return tuple(fired)

    async def _sweep_thread(
        self, state: _ThreadResidencyState
    ) -> ThreadResidencySnapshot | None:
        """评估单个 thread 并在 cold-eligible 时触发一次去重的 unload。"""
        blockers = self._observe(state)
        if not self._is_cold_eligible(state):
            return None
        idle_seconds = self._idle_seconds(state)
        if idle_seconds is None:  # 理论不可达（cold-eligible 必然未被阻断）
            raise RuntimeError(
                "cold-eligible thread 缺少 idle 时长，记账状态异常: "
                f"session_id={state.session_id}, thread_id={state.thread_id}"
            )
        runtime = state.runtime
        if runtime is not None:
            if state.runtime_unload is not None:
                # 同代 scope 已由另一个 sweep 关闭；不重复触发。
                return None
            unload = self._start_runtime_unload(state, runtime)
            await asyncio.shield(unload)
            return self._build_snapshot(state, blockers)

        callback = self._unload_callback
        if callback is None:
            # 没有 owner 回调就没有可释放的 LifetimeScope：保持 resident，
            # 不虚报卸载。
            return None
        # 回调前快照要卸载的代；提交时一律按该快照代 CAS，绝不在回调后重读当前代。
        target_generation = state.generation
        if not self._begin_unload_flight(state, target_generation):
            # 同一代已由并发 sweep 在飞卸载：不重复触发回调。
            return None
        request = ThreadUnloadRequest(
            session_id=state.session_id,
            thread_id=state.thread_id,
            generation=target_generation,
            idle_seconds=idle_seconds,
        )
        try:
            result = callback(request)
            if inspect.isawaitable(result):
                await result
        finally:
            # 无论回调成功或抛错都清位；抛错原样向上传播，不标记卸载。
            if state.unloading_generation == target_generation:
                state.unloading_generation = None
        # 跨代守卫：仅当该 thread 仍是发起本次 sweep 的同一代时才提交卸载标记。
        # 若 await 窗口内 owner 已登记新一代（甚至已被另一 sweep 完整卸载），迟到的
        # 旧代回调 MUST NOT 覆盖/清掉新一代的 unloaded_generation（绝不写旧代）。
        if state.generation == target_generation:
            state.unloaded_generation = target_generation
        return self._build_snapshot(state, blockers)

    @staticmethod
    def _begin_unload_flight(
        state: _ThreadResidencyState, target_generation: int
    ) -> bool:
        """原子登记 target_generation 的在飞卸载；同一代已在飞时返回 False。

        同步段无 ``await``，单事件循环下同一代至多一个调用方登记成功。
        """
        if state.unloading_generation == target_generation:
            return False
        state.unloading_generation = target_generation
        return True

    # ---- 内部记账 ----

    def _ensure_state(self, session_id: str, thread_id: str) -> _ThreadResidencyState:
        key = (session_id, thread_id)
        state = self._states.get(key)
        if state is None:
            state = _ThreadResidencyState(
                session_id=session_id,
                thread_id=thread_id,
            )
            # 首次观察即起算 idle：tracker 见到该 thread 之前不可能判定它 idle 超时。
            self._mark_unblocked(state)
            self._states[key] = state
        return state

    def _mark_unblocked(self, state: _ThreadResidencyState) -> None:
        state.unblocked_since = self._clock()
        state.last_activity_at = self._wall_clock()

    def _observe(self, state: _ThreadResidencyState) -> tuple[ResidencyBlocker, ...]:
        """汇总 push/pull blocker 并维护 idle 锚点；供 snapshot 与 sweep 共用。"""
        collected: dict[str, ResidencyBlocker] = dict(state.blockers)
        for source in self._blocker_sources:
            for blocker in source.residency_blockers(state.session_id, state.thread_id):
                # pull 源只贡献脱敏视图；去重按 (kind, reason)，同话术只保留一份。
                collected[f"pull:{blocker.kind}:{blocker.reason}"] = blocker
        if collected:
            state.unblocked_since = None
        elif state.unblocked_since is None:
            if self._has_runtime_users(state):
                return tuple(collected.values())
            # 从阻断到解除的第一次观察：从解除（观察）时刻重新起算。push 路径已在
            # release_blocker 精确起算过，这里覆盖 pull 恢复路径。
            self._mark_unblocked(state)
        return tuple(collected.values())

    @staticmethod
    def _has_runtime_users(state: _ThreadResidencyState) -> bool:
        runtime = state.runtime
        return state.pending_runtime_leases > 0 or (
            runtime is not None and runtime.active_leases > 0
        )

    def _idle_seconds(self, state: _ThreadResidencyState) -> float | None:
        if state.unblocked_since is None:
            return None
        return self._clock() - state.unblocked_since

    def _is_cold_eligible(self, state: _ThreadResidencyState) -> bool:
        if state.generation <= 0:
            # 没有 owner 登记过的 runtime generation 就没有可卸载的 LifetimeScope。
            return False
        if state.runtime_build is not None or state.pending_runtime_leases:
            return False
        if state.runtime_unload is not None:
            return False
        if state.runtime is not None and state.runtime.active_leases:
            return False
        if state.unloaded_generation == state.generation:
            return False
        idle_seconds = self._idle_seconds(state)
        return idle_seconds is not None and idle_seconds >= self._idle_timeout_seconds

    def _build_snapshot(
        self,
        state: _ThreadResidencyState,
        blockers: tuple[ResidencyBlocker, ...],
    ) -> ThreadResidencySnapshot:
        idle_seconds = self._idle_seconds(state)
        cold = state.unloaded_generation == state.generation and state.generation > 0
        if blockers:
            deadline: datetime | None = None
        elif idle_seconds is not None:
            remaining = max(self._idle_timeout_seconds - idle_seconds, 0.0)
            deadline = self._wall_clock() + timedelta(seconds=remaining)
        else:
            deadline = None
        execution_state = "+".join(sorted({blocker.kind for blocker in blockers}))
        if not execution_state:
            execution_state = "active" if self._has_runtime_users(state) else "idle"
        if state.runtime_managed:
            runtime = state.runtime
            if state.runtime_build is not None:
                residency: ThreadResidencyStateName = "loading"
            elif state.runtime_unload is not None or (
                runtime is not None
                and (runtime.closing or runtime.lifetime_scope.state != "open")
            ):
                residency = "unloading"
            elif runtime is None:
                residency = "cold"
            else:
                residency = "resident"
        else:
            residency = "cold" if state.generation == 0 or cold else "resident"
        return ThreadResidencySnapshot(
            session_id=state.session_id,
            thread_id=state.thread_id,
            residency=residency,
            cold_eligible=self._is_cold_eligible(state),
            execution_state=execution_state,
            last_activity_at=state.last_activity_at,
            idle_deadline=deadline,
            idle_seconds=idle_seconds,
            blockers=blockers,
            generation=state.generation,
        )


__all__ = [
    "THREAD_IDLE_UNLOAD_SECONDS",
    "ResidencyBlocker",
    "ResidencyBlockerSource",
    "ResidencyClock",
    "ThreadResidencyGeneration",
    "ThreadResidencySnapshot",
    "ThreadResidencyTracker",
    "ThreadRuntime",
    "ThreadRuntimeBuilder",
    "ThreadRuntimeLease",
    "ThreadUnloadRequest",
    "UnloadCallback",
    "WallClock",
]
