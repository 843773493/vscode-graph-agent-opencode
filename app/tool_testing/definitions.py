from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from langchain_core.tools import BaseTool

from app.agents.model_tool_schema import export_model_tool_json_schema


@dataclass(frozen=True, slots=True)
class PreparedToolTest:
    prompt: str
    tool: BaseTool
    injected_arguments: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolTestEvaluation:
    passed: bool
    detail: str


class ToolTestCase(Protocol):
    case_id: str
    tool_name: str
    title: str

    def prepare(
        self,
        *,
        workspace_root: Path,
        attempt_root: Path,
        asset_root: Path,
    ) -> PreparedToolTest: ...

    def evaluate(
        self,
        *,
        attempt_root: Path,
        tool_result: object,
    ) -> ToolTestEvaluation: ...


get_model_tool_parameters = export_model_tool_json_schema
