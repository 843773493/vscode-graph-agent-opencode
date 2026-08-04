from __future__ import annotations

from pathlib import Path

import pytest

from app.tool_testing.cases.apply_patch_case import (
    ApplyPatchScenarioCase,
    create_apply_patch_cases,
)


@pytest.fixture(params=create_apply_patch_cases(), ids=lambda case: case.case_id)
def case(request: pytest.FixtureRequest) -> ApplyPatchScenarioCase:
    return request.param


def test_prepare_creates_each_distinct_apply_patch_scenario(
    tmp_path: Path,
    case: ApplyPatchScenarioCase,
) -> None:
    attempt_root = tmp_path / ".boxteam" / "tool_tests" / case.case_id

    prepared = case.prepare(
        workspace_root=tmp_path,
        attempt_root=attempt_root,
        asset_root=tmp_path / "unused-assets",
    )

    assert prepared.tool.name == "apply_patch"
    assert "/.boxteam" not in prepared.prompt
    assert ".boxteam/tool_tests" not in prepared.prompt


def test_evaluate_accepts_expected_state(
    tmp_path: Path,
    case: ApplyPatchScenarioCase,
) -> None:
    attempt_root = tmp_path / case.case_id
    attempt_root.mkdir(parents=True)
    for relative_path, expected_content in case.expected_files.items():
        if expected_content is None:
            continue
        target = attempt_root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(expected_content, encoding="utf-8")

    evaluation = case.evaluate(attempt_root=attempt_root, tool_result=None)

    assert evaluation.passed is True


def test_evaluate_ignores_only_one_optional_final_newline(tmp_path: Path) -> None:
    case = create_apply_patch_cases()[1]
    attempt_root = tmp_path / case.case_id
    target = attempt_root / "src" / "generated.py"
    target.parent.mkdir(parents=True)
    target.write_text("def answer():\n    return 42", encoding="utf-8")

    evaluation = case.evaluate(attempt_root=attempt_root, tool_result=None)

    assert evaluation.passed is True
