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
    @property
    def case_id(self) -> str: ...

    @property
    def tool_name(self) -> str: ...

    @property
    def title(self) -> str: ...

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


def tool_test_failure_detail(error: BaseException, *, stage: str) -> str:
    """失败记录只保存阶段和异常类别，原生位置随原异常进入诊断日志。"""
    detail = f"工具测试失败: stage={stage}, type={type(error).__name__}"
    if isinstance(error, OSError):
        for name in ("errno", "winerror"):
            code = getattr(error, name, None)
            if isinstance(code, int):
                detail += f", {name}={code}"
    return detail
