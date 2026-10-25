"""ThreadRuntime 的唯一 admission、lease、generation fence 与 idle scope owner。

按精确 SessionThread 懒构建、跨 Turn 复用；换代和卸载只在 lease 排空后
关闭 LifetimeScope。debug blocker 仍由外部资源 owner 核实，不从 scope 推断。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable, Hashable
from contextvars import Context
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Generic, Literal, Protocol, TypeAlias, TypeVar, cast
from uuid import uuid4

from app.core.lifecycle import LifetimeScope
from app.core.phase_observer import observe_phase_sync
from app.core.shielded_cleanup import run_cleanup_shielded
from app.core.thread_residency_contracts import ResidencyBlocker, ResidencyBlockerSource
from app.domain.itemized.execution_admission import ExecutionAdmission
from app.domain.itemized.mutation_intents import MutationIntentOwner

logger = logging.getLogger(__name__)


class ThreadUnloadBlocked(RuntimeError):
    """卸载线性化前出现新 blocker；runtime owner 保留。"""

#: 产品阈值：连续 30 分钟无活动且无 blocker 才允许 idle unload（1800 秒）。
#: 测试必须用 fake clock 推进到 29:59（仍 resident）与 30:00（cold-eligible）验证
#: 边界，绝不缩短该常量。
THREAD_IDLE_UNLOAD_SECONDS: float = 30 * 60

#: 单个 idle unload 的扫描等待预算；到期不取消 owner 持有的真实清理任务。
THREAD_IDLE_UNLOAD_WAIT_SECONDS: float = 30

#: 单次 runtime 准入或 Session runtime 排空共用的总等待预算；超时保留原任务。
THREAD_RUNTIME_WAIT_SECONDS: float = 30

#: idle 计时的唯一时钟形态：单调秒。默认 ``time.monotonic``，测试注入可推进 fake。
ResidencyClock: TypeAlias = Callable[[], float]

#: 墙钟只用于快照展示（``last_activity_at``/``idle_deadline``），不参与 idle 判定。
WallClock: TypeAlias = Callable[[], datetime]

#: residency 是固定闭集；scope-owning runtime 的构建/释放在快照中投影为 loading/unloading。
ThreadResidencyStateName: TypeAlias = Literal[
    "cold", "loading", "resident", "unloading"
]


RuntimeValue_co = TypeVar("RuntimeValue_co", covariant=True)
LeaseValue = TypeVar("LeaseValue")
TaskResult = TypeVar("TaskResult")


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


class ThreadResidencyTracker:
    """精确 thread 的 runtime、lease 与 30 分钟 idle scope owner。

    单事件循环假设：全部同步方法内部无 ``await``，同步记账不需要加锁；``sweep``
    先快照再遍历，并以 per-thread 的在飞 task 合并构建和关闭。
    """

    def __init__(
        self,
        *,
        clock: ResidencyClock = time.monotonic,
        wall_clock: WallClock | None = None,
        idle_timeout_seconds: float = THREAD_IDLE_UNLOAD_SECONDS,
    ) -> None:
        if idle_timeout_seconds <= 0:
            raise ValueError(f"idle 阈值必须是正数: {idle_timeout_seconds!r}")
        self._clock = clock
        self._wall_clock = wall_clock or (lambda: datetime.now(UTC))
        self._idle_timeout_seconds = idle_timeout_seconds
        self._closing = False
        self._blocker_sources: list[ResidencyBlockerSource] = []
        self._unload_listeners: list[Callable[[str, str, int], Awaitable[None]]] = []
        self._acquire_listeners: list[Callable[[str, str, str], None]] = []
        self._states: dict[tuple[str, str], _ThreadResidencyState] = {}
        self._next_generation = 0
        self._instance_id = uuid4().hex

    # ---- 装配期（app/container.py） ----

    def add_blocker_source(self, source: ResidencyBlockerSource) -> None:
        """登记一个 blocker 拉取源（重启恢复兜底）；缺方法时显式失败。"""
        if not callable(getattr(source, "residency_blockers", None)):
            raise TypeError(
                "ResidencyBlockerSource 必须实现 residency_blockers(session_id, thread_id): "
                f"{type(source)!r}"
            )
        self._blocker_sources.append(source)

    # ---- owner / 活动入口 ----

    def runtime_binding_token(self, generation: int) -> str:
        """持久关联使用进程实例与真实 generation，重启后不会碰撞。"""
        if generation <= 0:
            raise ValueError("runtime 关联必须使用已经物化的正 generation")
        return f"runtime:{self._instance_id}:{generation}"

    def is_current_runtime_binding(self, session_id: str, thread_id: str, token: str) -> bool:
        state = self._states.get((session_id, thread_id))
        return bool(
            state is not None and state.runtime is not None
            and self.is_current_generation(session_id, thread_id, state.runtime.generation)
            and token == self.runtime_binding_token(state.runtime.generation)
        )

    def add_acquire_listener(self, listener: Callable[[str, str, str], None]) -> None:
        """lease 发布前同步绑定领域资源；失败不增加使用计数，也不跨 await 留窗口。"""
        self._acquire_listeners.append(listener)

    def add_unload_listener(
        self, listener: Callable[[str, str, int], Awaitable[None]],
    ) -> None:
        """外部领域 owner 的真实清理回执先于进程内 scope 释放。"""
        self._unload_listeners.append(listener)

    def record_activity(self, session_id: str, thread_id: str) -> None:
        """记录 thread 活动：未被阻断时把 idle 起算锚点重置为当前时刻。

        阻断期间 idle 本就不累计，锚点保持 ``None``，由 blocker 解除时刻统一重启
        计时（OpenSpec 2.8：解除后重新起算）。
        """
        state = self._states.get((session_id, thread_id))
        if state is None:
            return
        if state.unblocked_since is not None:
            self._mark_unblocked(state)

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
        return (
            runtime is not None
            and runtime.generation == generation
            and not runtime.closing
            and runtime.lifetime_scope.state == "open"
        )

    async def acquire(
        self,
        pair: MutationIntentOwner,
        admission: ExecutionAdmission,
        generation_key: Hashable,
        builder: ThreadRuntimeBuilder[RuntimeValue_co],
    ) -> ThreadRuntimeLease[RuntimeValue_co]:
        """从真实 committed admission 取得 lease，slot 不保留任何 execution 身份。"""
        if not isinstance(pair, MutationIntentOwner) or not isinstance(
            admission, ExecutionAdmission
        ):
            raise TypeError(
                "ThreadRuntime acquire 要求 typed pair 与真实 ExecutionAdmission"
            )
        if (pair.session_id, pair.thread_id) != (
            admission.session_id,
            admission.thread_id,
        ):
            raise ValueError("ThreadRuntime pair 与 execution admission owner 冲突")
        hash(generation_key)
        if not callable(builder):
            raise TypeError("ThreadRuntimeBuilder 必须可调用")
        if self._closing:
            raise RuntimeError("ThreadResidencyTracker 正在关闭，拒绝新 admission")
        state = self._ensure_state(pair.session_id, pair.thread_id)
        deadline = asyncio.get_running_loop().time() + THREAD_RUNTIME_WAIT_SECONDS
        state.pending_runtime_leases += 1
        try:
            while True:
                if self._closing:
                    raise RuntimeError(
                        "ThreadResidencyTracker 正在关闭，拒绝新 admission"
                    )
                if state.runtime_unload is not None:
                    await self._wait_runtime_task(state.runtime_unload, state, deadline, "admission")
                    continue
                runtime = state.runtime
                if runtime is not None:
                    if (
                        runtime.generation_key != generation_key
                        or runtime.closing
                        or runtime.lifetime_scope.state != "open"
                    ):
                        await self._wait_runtime_task(
                            self._start_runtime_unload(state, runtime), state, deadline, "admission",
                        )
                        continue
                    token = self.runtime_binding_token(runtime.generation)
                    for listener in self._acquire_listeners:
                        listener(runtime.session_id, runtime.thread_id, token)
                    runtime.active_leases += 1
                    runtime.drained.clear()
                    state.unblocked_since = None
                    return ThreadRuntimeLease(
                        self, runtime, cast(RuntimeValue_co, runtime.value)
                    )
                build = state.runtime_build
                if build is None:
                    if state.runtime_build_scope is not None:
                        # 旧编译失败的资源未关闭时，不能物化下一代。
                        await self._wait_runtime_task(
                            self._start_runtime_unload(state, None), state, deadline, "admission",
                        )
                        continue
                    scope = LifetimeScope(
                        f"thread-runtime:{state.session_id}:{state.thread_id}:"
                        f"{self._next_generation + 1}"
                    )
                    state.runtime_build_scope = scope
                    # 长期 builder/reactor 不继承首次 invocation 的 admission、collector 或 scope。
                    build = asyncio.create_task(
                        self._build_runtime(
                            state,
                            generation_key,
                            cast(ThreadRuntimeBuilder[object], builder),
                            scope,
                        ),
                        name=f"thread-runtime-builder:{state.session_id}:{state.thread_id}",
                        context=Context(),
                    )
                    state.runtime_build = build
                    build.add_done_callback(self._consume_task_exception)
                await self._wait_runtime_task(build, state, deadline, "admission")
        finally:
            state.pending_runtime_leases -= 1
            if not self._has_runtime_users(state):
                self._observe(state)
            self._drop_cold_state(state)

    async def _build_runtime(
        self,
        state: _ThreadResidencyState,
        generation_key: Hashable,
        builder: ThreadRuntimeBuilder[object],
        scope: LifetimeScope,
    ) -> _ThreadRuntimeSlot:
        task = asyncio.current_task()
        try:
            result = builder(state.session_id, state.thread_id, scope)
            if inspect.isawaitable(result):
                result = await cast(Awaitable[object], result)
            if scope.state != "open":
                raise RuntimeError(
                    "ThreadRuntimeBuilder 返回的 scope 不可用: "
                    f"state={scope.state}"
                )

            self._next_generation += 1
            generation = self._next_generation
            runtime = _ThreadRuntimeSlot(
                session_id=state.session_id,
                thread_id=state.thread_id,
                generation=generation,
                generation_key=generation_key,
                value=result,
                lifetime_scope=scope,
            )
            runtime.drained.set()
            state.runtime = runtime
            state.runtime_build_scope = None
            state.generation = generation
            state.unloaded_generation = None
            state.unblocked_since = None
            if not state.pending_runtime_leases:
                self._observe(state)
            return runtime
        except BaseException as build_error:
            try:
                await run_cleanup_shielded(scope.close)
            except BaseException as close_error:  # noqa: BLE001 - 保留取消与清理双失败。
                # scope 仍由 state 持有；两个原始错误都必须对调用方可见。
                raise BaseExceptionGroup(
                    "ThreadRuntime 构建与资源释放均失败", [build_error, close_error]
                ) from None
            state.runtime_build_scope = None
            raise
        finally:
            if state.runtime_build is task:
                state.runtime_build = None
            self._drop_cold_state(state)

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
        if runtime.active_leases == 0:
            runtime.drained.set()
        if runtime.active_leases == 0 and state.pending_runtime_leases == 0:
            # 从 lease 释放时刻重启 idle；pull blocker 查询失败时保留错误且不计 idle。
            state.unblocked_since = None
            self._observe(state)

    def _start_runtime_unload(
        self,
        state: _ThreadResidencyState,
        runtime: _ThreadRuntimeSlot | None,
        *,
        require_unblocked: bool = True,
    ) -> asyncio.Task[None]:
        if state.runtime_unload is not None:
            return state.runtime_unload
        scope = runtime.lifetime_scope if runtime is not None else state.runtime_build_scope
        if scope is None:
            raise RuntimeError("ThreadRuntime 关闭缺少 owner scope")
        task = asyncio.create_task(
            self._close_runtime(state, runtime, scope, require_unblocked=require_unblocked),
            name=(
                f"thread-runtime-close:{state.session_id}:{state.thread_id}:"
                f"{state.generation}"
            ),
            context=Context(),
        )
        state.runtime_unload = task
        task.add_done_callback(self._consume_task_exception)
        return task

    async def _close_runtime(
        self,
        state: _ThreadResidencyState,
        runtime: _ThreadRuntimeSlot | None,
        scope: LifetimeScope,
        *,
        require_unblocked: bool,
    ) -> None:
        task = asyncio.current_task()
        try:
            if runtime is not None:
                await runtime.drained.wait()
                if require_unblocked and self._observe(state):
                    raise ThreadUnloadBlocked("Thread runtime 新增活跃 blocker，卸载已阻断")
                # 从核实 blocker 到关闭准入之间没有 await；新 claim 必须先消费这道门。
                runtime.closing = True
                for listener in self._unload_listeners:
                    await listener(runtime.session_id, runtime.thread_id, runtime.generation)
            await scope.close()
            if state.runtime_build_scope is scope:
                state.runtime_build_scope = None
            if runtime is not None and state.runtime is runtime:
                state.runtime = None
                state.unloaded_generation = runtime.generation
                observe_phase_sync(runtime.thread_id, "thread.runtime.closed", details={
                    "session_id": runtime.session_id, "thread_id": runtime.thread_id,
                    "generation": runtime.generation, "runtime_object_id": id(runtime.value),
                    "scope_object_id": id(scope), "scope_state": scope.state,
                })
        except BaseException:
            if runtime is not None:
                runtime.closing = False
            raise
        finally:
            if state.runtime_unload is task:
                state.runtime_unload = None
            self._drop_cold_state(state)

    def is_unloading_generation(
        self, session_id: str, thread_id: str, generation: int,
    ) -> bool:
        """领域 owner 的释放回执只接受 tracker 当前正在卸载的精确代。"""
        state = self._states.get((session_id, thread_id))
        return bool(
            state is not None and state.runtime_unload is not None
            and state.runtime is not None and state.runtime.closing
            and state.runtime.generation == generation
        )

    def _drop_cold_state(self, state: _ThreadResidencyState) -> None:
        """清理成功只留下全局单调令牌；失败 owner 与活跃 blocker 必须仍可重试。"""
        if (
            state.runtime is None and state.runtime_build is None
            and state.runtime_build_scope is None and state.runtime_unload is None
            and not state.pending_runtime_leases and not state.blockers
            and self._states.get((state.session_id, state.thread_id)) is state
        ):
            del self._states[(state.session_id, state.thread_id)]

    async def _wait_runtime_task(
        self, task: asyncio.Task[TaskResult], state: _ThreadResidencyState,
        deadline: float | None, operation: str,
    ) -> TaskResult:
        if deadline is None:
            return await asyncio.shield(task)
        if not task.done():
            remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            done, _ = await asyncio.wait({task}, timeout=remaining)
            if not done:
                raise TimeoutError(
                    f"Thread runtime {operation} 等待超过 {THREAD_RUNTIME_WAIT_SECONDS:g} 秒；"
                    "原任务仍由 owner 持有，未报告排空或释放: "
                    f"session_id={state.session_id} thread_id={state.thread_id} "
                    f"generation={state.generation} task={task.get_name()}"
                )
        return task.result()

    async def close(self, *, session_id: str | None = None) -> None:
        """关闭指定 Session 或进程内全部 generation，等待真实 build/lease/scope。

        Session 删除只关闭该 Session；全进程关闭才停止 tracker 的新准入。
        删除调用者须先排空领域资源，且在成功返回前保留原控制库与 locator。
        """
        if session_id is None:
            self._closing = True
        # Backend保留失败步骤及依赖存储，超时不取消原build/unload。
        # TODO(5.4)：其它owner的排空与同步阻塞仍须纳入整个shutdown总期限。
        deadline = asyncio.get_running_loop().time() + THREAD_RUNTIME_WAIT_SECONDS
        waiters = [
            asyncio.create_task(
                self._close_thread_state(state, deadline, session_id=session_id),
                name=f"thread-runtime-drain-wait:{state.session_id}:{state.thread_id}",
            )
            for state in tuple(self._states.values())
            if session_id is None or state.session_id == session_id
        ]
        try:
            results = await asyncio.gather(*waiters)
        except BaseException:
            # 只排空本次等待者；原 build/unload 继续由各 state 持有，不能取消后重发清理。
            for waiter in waiters:
                waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)
            raise
        errors = [error for result in results for error in result]
        if errors:
            raise ExceptionGroup("ThreadRuntime 关闭失败", errors)

    async def _close_thread_state(
        self, state: _ThreadResidencyState, deadline: float | None, *, session_id: str | None,
    ) -> list[Exception]:
        """每个 Thread 独立等待原构建/卸载，错误仍按输入 owner 顺序汇总。"""
        errors: list[Exception] = []
        operation = "shutdown-drain" if session_id is None else "session-drain"
        build = state.runtime_build
        if build is not None:
            try:
                await self._wait_runtime_task(build, state, deadline, operation)
            except Exception as error:  # noqa: BLE001
                errors.append(error)
                if not build.done():
                    # builder 仍持有 scope，不能在其继续注册资源时并发关闭。
                    return errors
        try:
            runtime = state.runtime
            if runtime is not None or state.runtime_build_scope is not None:
                await self._wait_runtime_task(
                    self._start_runtime_unload(
                        state, runtime, require_unblocked=session_id is not None,
                    ), state, deadline, operation,
                )
            self._drop_cold_state(state)
            if session_id is not None and self._states.get((state.session_id, state.thread_id)) is state:
                raise RuntimeError(
                    "Session ThreadRuntime 排空仍有未终结状态: "
                    f"session_id={state.session_id}, thread_id={state.thread_id}, "
                    f"pending_runtime_leases={state.pending_runtime_leases}, "
                    f"blockers={state.blockers}"
                )
        except Exception as error:  # noqa: BLE001 - 排空其它 owner 后汇总显式抛出。
            errors.append(error)
        return errors

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
        self.assert_blocker_admission(session_id, thread_id)
        state = self._ensure_state(session_id, thread_id)
        state.blockers[blocker_key] = ResidencyBlocker(kind=kind, reason=reason)
        state.unblocked_since = None

    def assert_blocker_admission(self, session_id: str, thread_id: str) -> None:
        """领域 owner 必须在持久登记或外部副作用前调用；同步准入不跨 await。"""
        state = self._states.get((session_id, thread_id))
        if state is not None and state.runtime is not None and (
            state.runtime.closing or state.runtime.lifetime_scope.state != "open"
        ):
            raise ThreadUnloadBlocked("Thread runtime 正在卸载，拒绝新增外部 blocker")

    def release_blocker(
        self, session_id: str, thread_id: str, *, blocker_key: str
    ) -> None:
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
        self._drop_cold_state(state)

    # ---- 查询与评估 ----

    def snapshot(self, session_id: str, thread_id: str) -> ThreadResidencySnapshot:
        """只读快照：汇总 push/pull blocker 与 idle 记账，绝不关闭 scope。"""
        state = self._states.get((session_id, thread_id))
        if state is None:
            # 冷 Thread 的只读观察不制造永久记账项。
            state = _ThreadResidencyState(session_id=session_id, thread_id=thread_id)
        blockers = self._observe(state)
        return self._build_snapshot(state, blockers)

    async def sweep(self) -> tuple[ThreadResidencySnapshot, ...]:
        """逐 Thread 清理；失败保留 owner，整批尝试后显式报告，供下一轮重试。"""
        fired: list[ThreadResidencySnapshot] = []
        errors: list[Exception] = []
        for key in list(self._states):
            state = self._states.get(key)
            if state is None:
                continue
            try:
                snapshot = await self._sweep_thread(state)
            except Exception as error:  # noqa: BLE001 - 单项失败不饿死其它 owner，整批结束后抛出。
                error.add_note(
                    f"Thread 闲置清理失败: session_id={state.session_id} "
                    f"thread_id={state.thread_id} generation={state.generation}"
                )
                errors.append(error)
                continue
            if snapshot is not None:
                fired.append(snapshot)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise ExceptionGroup("Thread 闲置清理存在未收敛 owner", errors)
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
            try:
                done, _ = await asyncio.wait(
                    {unload}, timeout=THREAD_IDLE_UNLOAD_WAIT_SECONDS,
                )
                if not done:
                    # TODO(add-managed-attachment-directories 5.4): 由 owner 核实挂起终止；
                    # 等待超时不证明外部进程已停止，不能取消任务后重复发起释放。
                    raise TimeoutError(
                        f"Thread 闲置清理等待超过 {THREAD_IDLE_UNLOAD_WAIT_SECONDS:g} 秒；"
                        "原卸载任务仍由 owner 持有，资源尚未释放: "
                        f"session_id={state.session_id} thread_id={state.thread_id} "
                        f"generation={state.generation}"
                    )
                unload.result()
            except ThreadUnloadBlocked:
                return None
            return self._build_snapshot(state, blockers)

        return None

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
            if self._has_runtime_users(state) or (
                state.runtime is None and state.runtime_build_scope is None
            ):
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
        if state.runtime_build_scope is not None or state.pending_runtime_leases:
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
        runtime = state.runtime
        if state.runtime_build is not None:
            residency: ThreadResidencyStateName = "loading"
        elif state.runtime_build_scope is not None or state.runtime_unload is not None or (
            runtime is not None
            and (runtime.closing or runtime.lifetime_scope.state != "open")
        ):
            residency = "unloading"
        elif runtime is None:
            residency = "cold"
        else:
            residency = "resident"
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
    "ResidencyClock",
    "ThreadResidencySnapshot",
    "ThreadResidencyTracker",
    "ThreadRuntimeBuilder",
    "ThreadRuntimeLease",
    "WallClock",
]
