from __future__ import annotations

from typing import Protocol


class ToolSelectionReader(Protocol):
    def disabled_tools(self, agent_id: str) -> set[str]: ...

    def model_hidden_tools(
        self,
        agent_id: str,
        *,
        default_hidden_tool_names: set[str],
    ) -> set[str]: ...
