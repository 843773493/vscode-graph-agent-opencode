"""装配侧 phase 观察端口；不拥有业务状态，也不改变事务结果。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol


class PhaseObserver(Protocol):
    async def reached(
        self, operation_id: str, phase: str, *, details: Mapping[str, object] | None = None,
    ) -> None: ...

    def reached_sync(
        self, operation_id: str, phase: str, *, details: Mapping[str, object] | None = None,
    ) -> None: ...


class ImmediatePhaseObserver:
    async def reached(
        self, operation_id: str, phase: str, *, details: Mapping[str, object] | None = None,
    ) -> None:
        return

    def reached_sync(
        self, operation_id: str, phase: str, *, details: Mapping[str, object] | None = None,
    ) -> None:
        return


_observer: PhaseObserver = ImmediatePhaseObserver()


def bind_phase_observer(observer: PhaseObserver) -> None:
    """在进程启动装配中绑定一次；运行中的应用不能切换观察端口。"""
    global _observer
    if not isinstance(_observer, ImmediatePhaseObserver):
        raise RuntimeError("PhaseObserver 已绑定，禁止运行期替换")  # noqa: TRY004
    _observer = observer


async def observe_phase(
    operation_id: str, phase: str, *, details: Mapping[str, object] | None = None,
) -> None:
    """details 只观察调用处的事实；不持有对象或进入业务存储。"""
    await _observer.reached(operation_id, phase, details=details)


def observe_phase_sync(
    operation_id: str, phase: str, *, details: Mapping[str, object] | None = None,
) -> None:
    _observer.reached_sync(operation_id, phase, details=details)
