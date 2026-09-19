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
- unload 回调缝：thread 无 blocker 且跨过阈值时触发 owner 注册的卸载回调，回调持有
  该 generation 的 ``LifetimeScope`` 释放（唯一释放合同在 ``app/core/lifecycle.py``，
  本模块不建立第二 dispose manager）。

红线（8.8-A，落地为注释与代码边界）：

1. residency owner 不据 scope close、PID/端口重用或内存缺项推断进程停止；进程事实
   只能由 debug owner 以 durable claim + OS 起始身份核实后，经 blocker 上报进入本模块。
2. idle unload 不新增 item、epoch 或上下文到期状态：本轮无 CSM 接线，unload 回调只
   释放进程内 runtime 资源，不触碰任何上下文账目（后续 CSM 接入轮必须保持该边界）。
3. 迟到 callback 不得写旧 generation：本轮回调面只有 generation 令牌下发，没有
   写回接口；真实 ThreadRuntime owner（8.4/8.5）落地后，回调内必须重新取得精确
   thread 的当前 owner/generation 才可提交任何状态，本模块绝不允许回调凭过期
   generation 直接写入。
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol, TypeAlias

#: 产品阈值：连续 30 分钟无活动且无 blocker 才允许 idle unload（1800 秒）。
#: 测试必须用 fake clock 推进到 29:59（仍 resident）与 30:00（cold-eligible）验证
#: 边界，绝不缩短该常量。
THREAD_IDLE_UNLOAD_SECONDS: float = 30 * 60

#: idle 计时的唯一时钟形态：单调秒。默认 ``time.monotonic``，测试注入可推进 fake。
ResidencyClock: TypeAlias = Callable[[], float]

#: 墙钟只用于快照展示（``last_activity_at``/``idle_deadline``），不参与 idle 判定。
WallClock: TypeAlias = Callable[[], datetime]

#: residency 状态：``cold`` 表示当前 generation 的进程内 runtime 已被 idle unload。
ThreadResidencyStateName = str


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

    ``residency``：``resident``（当前代在册或未卸载）或 ``cold``（当前代已 idle unload）。
    ``cold_eligible``：当前满足卸载条件且尚未卸载（无 blocker、idle 越过阈值、
    已登记 runtime generation）。
    ``execution_state``：execution 概要——有 blocker 时为 blocker 类别联合
    （如 ``node_debug_process``），否则 ``idle``。
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
    #: push 登记的 blocker（key → 脱敏视图）；pull 源在评估时另行查询。
    blockers: dict[str, ResidencyBlocker] = field(default_factory=dict)
    #: 墙钟展示用：最近一次 idle 锚点重置时刻。
    last_activity_at: datetime | None = None


class ThreadResidencyTracker:
    """30 分钟 idle 卸载判定器：blocker 登记、idle 记账与 unload 回调缝。

    单事件循环假设：全部同步方法内部无 ``await``，不需要加锁；``sweep`` 在回调
    ``await`` 期间不修改遍历集合（先快照再遍历）。
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
        return (
            state.generation == generation
            and state.unloaded_generation != generation
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
        if not state.blockers and state.unblocked_since is None:
            # 从解除时刻重新起算（OpenSpec 2.8）；若 pull 源仍上报 blocker，下一次
            # 观察会把锚点重置回 None，方向安全（绝不提前进入 cold-eligible）。
            self._mark_unblocked(state)

    # ---- 查询与评估 ----

    def snapshot(self, session_id: str, thread_id: str) -> ThreadResidencySnapshot:
        """只读快照：汇总 push/pull blocker 与 idle 记账，绝不触发 unload 回调。"""
        state = self._ensure_state(session_id, thread_id)
        blockers = self._observe(state)
        return self._build_snapshot(state, blockers)

    async def sweep(self) -> tuple[ThreadResidencySnapshot, ...]:
        """评估全部已知 thread；对 cold-eligible 的触发 owner 的 unload 回调。

        回调抛错原样向上传播（fail-loud），对应 thread 不标记卸载，下次 sweep 可重试；
        只有回调成功返回后才标记 ``unloaded_generation``。
        """
        fired: list[ThreadResidencySnapshot] = []
        for key in list(self._states):
            state = self._states[key]
            blockers = self._observe(state)
            if not self._is_cold_eligible(state):
                continue
            idle_seconds = self._idle_seconds(state)
            if idle_seconds is None:  # 理论不可达（cold-eligible 必然未被阻断）
                raise RuntimeError(
                    "cold-eligible thread 缺少 idle 时长，记账状态异常: "
                    f"session_id={key[0]}, thread_id={key[1]}"
                )
            request = ThreadUnloadRequest(
                session_id=state.session_id,
                thread_id=state.thread_id,
                generation=state.generation,
                idle_seconds=idle_seconds,
            )
            callback = self._unload_callback
            if callback is None:
                # 没有 owner 回调就没有可释放的 LifetimeScope：保持 resident，
                # 不虚报卸载。
                continue
            result = callback(request)
            if inspect.isawaitable(result):
                await result
            state.unloaded_generation = state.generation
            fired.append(self._build_snapshot(state, blockers))
        return tuple(fired)

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
            # 从阻断到解除的第一次观察：从解除（观察）时刻重新起算。push 路径已在
            # release_blocker 精确起算过，这里覆盖 pull 恢复路径。
            self._mark_unblocked(state)
        return tuple(collected.values())

    def _idle_seconds(self, state: _ThreadResidencyState) -> float | None:
        if state.unblocked_since is None:
            return None
        return self._clock() - state.unblocked_since

    def _is_cold_eligible(self, state: _ThreadResidencyState) -> bool:
        if state.generation <= 0:
            # 没有 owner 登记过的 runtime generation 就没有可卸载的 LifetimeScope。
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
        return ThreadResidencySnapshot(
            session_id=state.session_id,
            thread_id=state.thread_id,
            residency="cold" if cold else "resident",
            cold_eligible=self._is_cold_eligible(state),
            execution_state=execution_state or "idle",
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
    "ThreadUnloadRequest",
    "UnloadCallback",
    "WallClock",
]
