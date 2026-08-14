from pathlib import Path

import pytest

from tests.harness.python.run_context import TestRunContext


def test_run_context_mirrors_test_path(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    test_file = project_root / "tests" / "integration" / "sample" / "test_case.py"
    context = TestRunContext.from_test_file(
        test_file,
        project_root=project_root,
    )

    assert context.output_root == (
        project_root / "out" / "tests" / "integration" / "sample" / "test_case"
    )
    assert context.workspace_root == context.output_root / "workspace"
    assert context.artifacts_dir == context.output_root / "artifacts"


def test_run_context_isolates_boxteam_home_by_test_node(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOXTEAM_TEST_RUN_ID", "run-context-test")
    context = TestRunContext.from_test_file(
        tmp_path / "tests" / "unit" / "test_context.py",
        project_root=tmp_path,
    )

    first = context.boxteam_home_for_node("tests/unit/test_context.py::test_first")
    second = context.boxteam_home_for_node("tests/unit/test_context.py::test_second")

    assert first != second
    assert first.parent.parent.parent == context.output_root / "runtime"
    assert first.parent.parent.name.startswith("run-")
    assert first.parent.name.startswith("node-")
    assert first.name == "boxteam-home"


def test_run_context_rejects_empty_test_node(tmp_path: Path) -> None:
    context = TestRunContext.from_test_file(
        tmp_path / "tests" / "unit" / "test_context.py",
        project_root=tmp_path,
    )

    with pytest.raises(ValueError, match="pytest 节点 ID 不能为空"):
        context.runtime_root_for_node("  ")


def test_run_context_rejects_file_outside_tests(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="测试文件必须位于"):
        TestRunContext.from_test_file(
            tmp_path / "scripts" / "not-a-test.py",
            project_root=tmp_path,
        )


def test_run_context_cleans_up_in_reverse_order(tmp_path: Path) -> None:
    context = TestRunContext.from_test_file(
        tmp_path / "tests" / "unit" / "test_cleanup.py",
        project_root=tmp_path,
    )
    order: list[str] = []
    context.add_cleanup("first", lambda: order.append("first"))
    context.add_cleanup("second", lambda: order.append("second"))

    context.close()

    assert order == ["second", "first"]
