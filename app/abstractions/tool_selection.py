from __future__ import annotations

from typing import Protocol


class ToolSelectionReader(Protocol):
    def execution_overrides(self, agent_id: str) -> dict[str, bool]: ...

    def model_visibility_overrides(self, agent_id: str) -> dict[str, bool]: ...
