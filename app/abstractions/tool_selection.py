from __future__ import annotations

from typing import Protocol


class ToolSelectionReader(Protocol):
    def disabled_tools(self, agent_id: str) -> set[str]: ...

