from __future__ import annotations

from pathlib import Path

import pytest

from tests.harness.python.run_context import TestRunContext
from tests.support.paths import output_root_for_test


@pytest.mark.parametrize("layer", ["unit", "integration", "e2e"])
@pytest.mark.parametrize("relative_file", [False, True])
def test_explicit_project_root_resolves_file_independently_of_cwd(
    tmp_path: Path, layer: str, relative_file: bool,
) -> None:
    project_root = tmp_path / "独立项目 with spaces"
    relative_path = Path("tests") / layer / "中文目录" / "test_case.py"
    test_file = relative_path if relative_file else project_root / relative_path

    output_root = output_root_for_test(
        test_file, test_layer=layer, project_root=project_root,
    )

    assert output_root == project_root / "out/tests" / layer / "中文目录/test_case"
    assert output_root == TestRunContext.from_test_file(
        test_file, project_root=project_root,
    ).output_root
    assert not output_root.exists()


@pytest.mark.parametrize("relative_root", [False, True])
@pytest.mark.parametrize("relative_file", [False, True])
def test_custom_tests_root_uses_the_same_explicit_project_root(
    tmp_path: Path, relative_root: bool, relative_file: bool,
) -> None:
    project_root = tmp_path / "project"
    relative_tests_root = Path("checks/验证目录")
    relative_test_file = relative_tests_root / "nested/../test_custom.py"
    tests_root = relative_tests_root if relative_root else project_root / relative_tests_root
    test_file = relative_test_file if relative_file else project_root / relative_test_file

    output_root = output_root_for_test(
        test_file, test_layer="integration", project_root=project_root,
        tests_root=tests_root,
    )

    assert output_root == project_root / "out/tests/integration/test_custom"
    assert not output_root.exists()


def test_project_root_defaults_to_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    assert output_root_for_test(
        Path("tests/unit/test_default.py"), test_layer="unit",
    ) == tmp_path / "out/tests/unit/test_default"


@pytest.mark.parametrize("outside_path", ["tests/unit/../../outside.py", "../outside.py"])
def test_relative_file_cannot_escape_selected_tests_root(
    tmp_path: Path, outside_path: str,
) -> None:
    with pytest.raises(ValueError):
        output_root_for_test(
            Path(outside_path), test_layer="unit", project_root=tmp_path,
        )


def test_absolute_custom_tests_root_can_be_outside_project(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    tests_root = tmp_path / "external-tests"

    assert output_root_for_test(
        tests_root / "nested/test_custom.py", test_layer="integration",
        project_root=project_root, tests_root=tests_root,
    ) == project_root / "out/tests/integration/nested/test_custom"
