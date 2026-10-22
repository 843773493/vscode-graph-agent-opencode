from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest
from langchain_core.messages import ToolMessage

from app.tool_testing.cases.edit_file_case import (
    EditFileScenarioCase,
    create_edit_file_cases,
)
from app.tool_testing.definitions import PreparedToolTest
from app.tool_testing.registry import ToolTestRegistry


@pytest.fixture(params=create_edit_file_cases(), ids=lambda item: item.case_id)
def case(request: pytest.FixtureRequest) -> EditFileScenarioCase:
    return request.param


@pytest.fixture
def fixture_workspace_root() -> Path:
    return (
        Path.cwd()
        / "tests"
        / "fixtures"
        / "workspaces"
        / "model_tool_test_workspace"
    )


@pytest.fixture
def prepared_case(
    tmp_path: Path,
    case: EditFileScenarioCase,
    fixture_workspace_root: Path,
) -> tuple[PreparedToolTest, Path]:
    attempt_root = tmp_path / ".boxteam" / "tool_tests" / "attempt"
    prepared = case.prepare(
        workspace_root=tmp_path,
        attempt_root=attempt_root,
        asset_root=fixture_workspace_root,
    )
    return prepared, attempt_root


def test_prepare_copies_seed_and_selects_real_edit_file_tool(
    prepared_case: tuple[PreparedToolTest, Path],
    case: EditFileScenarioCase,
) -> None:
    prepared, attempt_root = prepared_case

    assert prepared.tool.name == "edit_file"
    assert set(prepared.tool.tool_call_schema.model_json_schema()["properties"]) == {
        "file_path",
        "expected_version",
        "old_string",
        "new_string",
        "replace_all",
    }
    assert case.file_name in prepared.prompt
    assert "/.boxteam/tool_tests" not in prepared.prompt
    assert json.dumps(case.old_string, ensure_ascii=False) in prepared.prompt
    assert (attempt_root / case.file_name).is_file()


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
async def test_real_edit_file_tool_and_evaluate(
    prepared_case: tuple[PreparedToolTest, Path],
    case: EditFileScenarioCase,
    asynchronous: bool,
) -> None:
    prepared, attempt_root = prepared_case

    arguments = {
            "file_path": case.file_name,
            "expected_version": "sha256:"
            + sha256(
                (attempt_root / case.file_name).read_bytes()
            ).hexdigest(),
            "old_string": case.old_string,
            "new_string": case.new_string,
            "replace_all": case.replace_all,
            **prepared.injected_arguments,
    }
    result = await prepared.tool.ainvoke(arguments) if asynchronous else prepared.tool.invoke(arguments)

    assert isinstance(result, ToolMessage)
    assert result.status == "success"
    evaluation = case.evaluate(attempt_root=attempt_root, tool_result=result)
    assert evaluation.passed is True
    assert evaluation.detail == f"{case.title}结果正确"


def test_evaluate_rejects_incorrect_file_result(
    prepared_case: tuple[PreparedToolTest, Path],
    case: EditFileScenarioCase,
) -> None:
    _, attempt_root = prepared_case
    (attempt_root / case.file_name).write_text(
        "错误内容\n", encoding="utf-8"
    )

    evaluation = case.evaluate(attempt_root=attempt_root, tool_result=None)

    assert evaluation.passed is False
    assert "文件内容不符合预期" in evaluation.detail


def test_registry_includes_edit_file_case_by_default() -> None:
    registry = ToolTestRegistry()

    cases = registry.cases_for("edit_file")
    assert len(cases) == 10
    assert len({item.case_id for item in cases}) == 10
    assert "edit_file" in registry.supported_tools()
    assert "apply_patch" in registry.supported_tools()
