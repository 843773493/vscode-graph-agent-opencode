import gc
import json
import weakref
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
    assert context.run_root.parent == context.output_root / "runs"
    assert context.run_root.name.startswith("run-")
    assert context.workspace_root == context.output_root / "workspace"
    assert context.artifacts_dir.parent.parent.parent == context.run_root
    assert context.artifacts_dir.parent.name.startswith("node-")
    assert context.boxteam_home == context.output_root / "boxteam-home"


def test_run_context_separates_artifacts_between_test_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    test_file = project_root / "tests" / "integration" / "sample" / "test_case.py"
    monkeypatch.setenv("BOXTEAM_TEST_RUN_ID", "first-run")
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    first = TestRunContext.from_test_file(test_file, project_root=project_root)
    same_run = TestRunContext.from_test_file(test_file, project_root=project_root)

    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw1")
    parallel_worker = TestRunContext.from_test_file(
        test_file, project_root=project_root
    )
    monkeypatch.setenv("BOXTEAM_TEST_RUN_ID", "second-run")
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    second = TestRunContext.from_test_file(test_file, project_root=project_root)

    assert first.run_root == same_run.run_root
    assert first.artifacts_dir == same_run.artifacts_dir
    assert first.run_root != parallel_worker.run_root
    assert first.run_root != second.run_root
    assert first.artifacts_dir != second.artifacts_dir
    assert first.boxteam_home == second.boxteam_home
    node_id = "tests/sample.py::case"
    assert first.boxteam_home_for_node(node_id) != second.boxteam_home_for_node(node_id)
    assert first.workspace_root == second.workspace_root
    first.write_result("passed", {"run": "first"})
    second.write_result("passed", {"run": "second"})

    first_result = json.loads((first.artifacts_dir / "result.json").read_text())
    second_result = json.loads((second.artifacts_dir / "result.json").read_text())
    assert first_result["details"] == {"run": "first"}
    assert second_result["details"] == {"run": "second"}


def test_run_context_separates_artifacts_between_pytest_nodes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    test_file = project_root / "tests" / "integration" / "sample" / "test_case.py"
    monkeypatch.setenv("BOXTEAM_TEST_RUN_ID", "same-run")
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/sample.py::first (call)")
    context = TestRunContext.from_test_file(test_file, project_root=project_root)
    first_artifacts = context.artifacts_dir
    context.write_result("passed", {"case": "first"})
    assert (
        first_artifacts.parent.name
        == context.runtime_root_for_node("tests/sample.py::first").name
    )

    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/sample.py::second (call)")
    second_artifacts = context.artifacts_dir
    context.write_result("failed", {"case": "second"})

    assert first_artifacts != second_artifacts
    assert json.loads((first_artifacts / "result.json").read_text())["details"] == {
        "case": "first"
    }
    assert json.loads((second_artifacts / "result.json").read_text())["details"] == {
        "case": "second"
    }


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


@pytest.fixture
def cleanup_context(tmp_path: Path) -> TestRunContext:
    return TestRunContext.from_test_file(
        tmp_path / "tests/unit/test_cleanup.py",
        project_root=tmp_path,
    )


@pytest.mark.parametrize("interruption", [KeyboardInterrupt("停止"), SystemExit(3)])
def test_cleanup_retains_original_errors_and_labels(
    cleanup_context: TestRunContext,
    interruption: BaseException,
) -> None:
    first_error = ValueError("文件关闭失败")
    order = []

    def first() -> None:
        order.append("first")
        raise first_error

    def second() -> None:
        order.append("second")
        raise interruption

    cleanup_context.add_cleanup("first", first)
    cleanup_context.add_cleanup("second", second)
    with pytest.raises(BaseExceptionGroup) as failure:
        cleanup_context.close()

    assert order == ["second", "first"]
    assert failure.value.exceptions == (interruption, first_error)
    assert "second" in " ".join(interruption.__notes__)
    assert "first" in " ".join(first_error.__notes__)


def test_closed_context_releases_objects_captured_only_by_cleanup(
    cleanup_context: TestRunContext,
) -> None:
    class Payload:
        pass

    payload = Payload()
    observed = weakref.ref(payload)
    cleanup_context.add_cleanup("payload", lambda held=payload: None)
    del payload

    cleanup_context.close()
    gc.collect()

    assert observed() is None


def test_cleanup_failure_is_not_retried_and_closed_context_rejects_new_resources(
    cleanup_context: TestRunContext,
) -> None:
    attempts = []

    def close() -> None:
        attempts.append("attempt")
        raise ValueError("关闭失败")

    cleanup_context.add_cleanup("resource", close)
    with pytest.raises(ExceptionGroup):
        cleanup_context.close()
    cleanup_context.close()

    assert attempts == ["attempt"]
    with pytest.raises(RuntimeError, match="已经关闭"):
        cleanup_context.add_cleanup("late", lambda: None)


def test_context_exit_preserves_body_failure_alongside_original_cleanup_error(
    cleanup_context: TestRunContext,
) -> None:
    body_error = LookupError("测试主体失败")
    cleanup_error = OSError("关闭失败")

    def close() -> None:
        raise cleanup_error

    cleanup_context.add_cleanup("resource", close)
    with pytest.raises(ExceptionGroup) as failure, cleanup_context:
        raise body_error

    assert failure.value.exceptions == (cleanup_error,)
    assert failure.value.__context__ is body_error


@pytest.mark.parametrize(
    "relative_file",
    [
        "tests/unit/test_relative.py",
        "./tests/unit/../unit/nested/test_relative.py",
    ],
)
def test_relative_test_path_uses_explicit_project_root_instead_of_cwd(
    tmp_path: Path,
    relative_file: str,
) -> None:
    project_root = tmp_path / "project with 中文"
    context = TestRunContext.from_test_file(
        Path(relative_file), project_root=project_root
    )

    expected_file = (project_root / relative_file).resolve()
    expected_output = (
        project_root
        / "out/tests"
        / expected_file.relative_to(project_root / "tests").with_suffix("")
    )
    assert context.project_root == project_root
    assert context.test_file == expected_file
    assert context.output_root == expected_output
    assert context.workspace_root == expected_output / "workspace"
    assert context.run_root.parent == expected_output / "runs"
    assert context.artifacts_dir.parent.parent.parent == context.run_root
    assert context.artifacts_dir.parent.name.startswith("node-")
