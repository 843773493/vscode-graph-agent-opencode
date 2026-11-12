from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def chromium_temporary_environment(temporary: Path) -> Iterator[dict[str, str]]:
    """提供短临时路径，规避原生路径限长且保留正式 runtime 归属。"""
    temporary.mkdir(parents=True, exist_ok=True)
    descriptor: int | None = None
    alias_root: tempfile.TemporaryDirectory | None = None
    try:
        if Path("/proc/self/fd").is_dir():
            descriptor = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY)
            short_path = f"/proc/{os.getpid()}/fd/{descriptor}"
        elif os.name == "posix" and Path("/tmp").is_dir():
            alias_root = tempfile.TemporaryDirectory(prefix="bt-", dir="/tmp")
            alias = Path(alias_root.name) / "t"
            alias.symlink_to(temporary.resolve(), target_is_directory=True)
            short_path = str(alias)
        elif os.name == "nt":
            alias_root = tempfile.TemporaryDirectory(prefix="bt-")
            alias = Path(alias_root.name) / "t"
            # Junction 不要求符号链接权限；只创建别名，正文仍归正式 runtime。
            subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(alias), str(temporary.resolve())],
                check=True, capture_output=True, text=True, timeout=10,
            )
            short_path = str(alias)
        else:
            short_path = str(temporary)
        if Path(short_path).resolve() != temporary.resolve():
            raise RuntimeError(f"Chromium 临时目录别名不匹配: {short_path}")
        yield {name: short_path for name in ("TMPDIR", "TMP", "TEMP")}
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if alias_root is not None:
            alias_root.cleanup()


def output_root_for_test(
    test_file_path: Path,
    *,
    test_layer: str,
    project_root: Path | None = None,
    tests_root: Path | None = None,
) -> Path:
    """返回严格镜像测试文件路径的正式输出目录。"""

    resolved_project_root = (
        project_root.resolve() if project_root is not None else Path.cwd().resolve()
    )
    resolved_tests_root = (
        (resolved_project_root / tests_root).resolve()
        if tests_root is not None
        else resolved_project_root / "tests" / test_layer
    )
    relative_test_path = (
        (resolved_project_root / test_file_path)
        .resolve()
        .relative_to(resolved_tests_root)
        .with_suffix("")
    )
    return resolved_project_root / "out" / "tests" / test_layer / relative_test_path
