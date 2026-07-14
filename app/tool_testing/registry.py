from __future__ import annotations

from collections import defaultdict

from app.tool_testing.cases.apply_patch_case import create_apply_patch_cases
from app.tool_testing.cases.edit_file_case import create_edit_file_cases
from app.tool_testing.definitions import ToolTestCase


class ToolTestRegistry:
    def __init__(self, cases: list[ToolTestCase] | None = None) -> None:
        self._cases: dict[str, list[ToolTestCase]] = defaultdict(list)
        default_cases: list[ToolTestCase] = [
            *create_apply_patch_cases(),
            *create_edit_file_cases(),
        ]
        for case in cases or default_cases:
            self.register(case)

    def register(self, case: ToolTestCase) -> None:
        if any(item.case_id == case.case_id for item in self._cases[case.tool_name]):
            raise ValueError(
                f"工具测试用例重复: tool={case.tool_name}, case={case.case_id}"
            )
        self._cases[case.tool_name].append(case)

    def cases_for(self, tool_name: str) -> list[ToolTestCase]:
        return list(self._cases.get(tool_name, []))

    def supported_tools(self) -> set[str]:
        return {name for name, cases in self._cases.items() if cases}
