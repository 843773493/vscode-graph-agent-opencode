from __future__ import annotations

from pathlib import Path


def e2e_output_root_for_test(
    test_file_path: Path,
    *,
    project_root: Path | None = None,
    tests_root: Path | None = None,
) -> Path:
    """返回严格镜像 E2E 测试文件路径的正式输出目录。"""

    resolved_project_root = (
        project_root.resolve() if project_root is not None else Path.cwd().resolve()
    )
    resolved_tests_root = (
        tests_root.resolve()
        if tests_root is not None
        else resolved_project_root / "tests" / "e2e"
    )
    relative_test_path = test_file_path.resolve().relative_to(
        resolved_tests_root
    ).with_suffix("")
    return resolved_project_root / "out" / "tests" / "e2e" / relative_test_path
