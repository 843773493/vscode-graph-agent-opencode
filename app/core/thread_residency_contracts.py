"""runtime owner 与后台工作共用的纯 blocker、generation 合同。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ResidencyBlocker:
    """仅携带 owner 核实的占用类别与脱敏话术。"""

    kind: str
    reason: str


class ResidencyBlockerSource(Protocol):
    def residency_blockers(self, session_id: str, thread_id: str) -> Sequence[ResidencyBlocker]: ...


class ThreadGenerationSnapshot(Protocol):
    @property
    def generation(self) -> int: ...


class BackgroundResidencyPort(Protocol):
    def add_blocker_source(self, source: ResidencyBlockerSource) -> None: ...

    def assert_blocker_admission(self, session_id: str, thread_id: str) -> None: ...

    def snapshot(self, session_id: str, thread_id: str) -> ThreadGenerationSnapshot: ...

    def is_current_generation(self, session_id: str, thread_id: str, generation: int) -> bool: ...
