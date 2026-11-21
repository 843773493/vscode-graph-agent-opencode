"""工具发现输入的选择器与预算约束。"""

import pytest
from pydantic import ValidationError

from app.agents.tools.tool_discovery import SearchToolsInput


@pytest.mark.parametrize("arguments", [
    {}, {"query": ""}, {"query": "   "}, {"tool_ids": []}, {"tool_ids": [""]},
    {"query": "CI", "tool_ids": ["ci_logs"]}, {"query": "CI", "limit": True},
    {"query": "CI", "limit": 0}, {"query": "CI", "max_response_bytes": 2047},
    {"query": "CI", "unknown": True},
])
def test_invalid_selectors_and_budgets_are_rejected(arguments):
    with pytest.raises(ValidationError):
        SearchToolsInput.model_validate(arguments)
