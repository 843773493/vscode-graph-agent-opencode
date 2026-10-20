import gc
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


@pytest.fixture
def cleanup_context(tmp_path: Path) -> TestRunContext:
    return TestRunContext.from_test_file(
        tmp_path / "tests/unit/test_cleanup.py", project_root=tmp_path,
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
