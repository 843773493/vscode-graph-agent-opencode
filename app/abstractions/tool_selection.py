"""工具能力控制面的单次原子读取合同。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from app.domain.itemized.hashing import sha256_jcs


@dataclass(frozen=True, slots=True, init=False)
class ToolSelectionSnapshot:
    """同一次读取的执行/模型可见开关；版本由精确选择事实确定。"""

    agent_id: str
    _execution: tuple[tuple[str, bool], ...]
    _visibility: tuple[tuple[str, bool], ...]

    def __init__(
        self, agent_id: str,
        execution_overrides: Mapping[str, bool],
        model_visibility_overrides: Mapping[str, bool],
    ) -> None:
        if not isinstance(agent_id, str) or not agent_id:
            raise ValueError("ToolSelectionSnapshot.agent_id 必须是非空字符串")
        for settings in (execution_overrides, model_visibility_overrides):
            if not isinstance(settings, Mapping) or not all(
                isinstance(name, str) and name and isinstance(enabled, bool)
                for name, enabled in settings.items()
            ):
                raise TypeError("工具选择快照必须是工具名到 boolean 的 mapping")
        object.__setattr__(self, "agent_id", agent_id)
        object.__setattr__(self, "_execution", tuple(sorted(execution_overrides.items())))
        object.__setattr__(self, "_visibility", tuple(sorted(model_visibility_overrides.items())))

    @property
    def execution_overrides(self) -> dict[str, bool]:
        return dict(self._execution)

    @property
    def model_visibility_overrides(self) -> dict[str, bool]:
        return dict(self._visibility)

    @property
    def revision(self) -> str:
        return sha256_jcs({
            "agent_id": self.agent_id,
            "execution_overrides": self.execution_overrides,
            "model_visibility_overrides": self.model_visibility_overrides,
        })


class ToolSelectionReader(Protocol):
    def snapshot(self, agent_id: str) -> ToolSelectionSnapshot: ...
