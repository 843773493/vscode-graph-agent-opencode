from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.support.processes import workspace as processes


@dataclass(frozen=True)
class PlatformPythonPaths:
    project_root: Path
    platform: str
    expected_python: Path
    foreign_python: Path


@pytest.fixture(params=["linux", "darwin", "win32"])
def python_paths(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> PlatformPythonPaths:
    # 只模拟平台选择，不修改宿主平台，也不执行这些空的候选文件。
    platform = str(request.param)
    monkeypatch.setattr(processes, "sys", SimpleNamespace(platform=platform), raising=False)
    posix_python = tmp_path / ".venv/bin/python"
    windows_python = tmp_path / ".venv/Scripts/python.exe"
    expected, foreign = (windows_python, posix_python) if platform == "win32" else (posix_python, windows_python)
    expected.parent.mkdir(parents=True)
    foreign.parent.mkdir(parents=True)
    return PlatformPythonPaths(tmp_path, platform, expected, foreign)


def test_both_layouts_present_selects_current_platform(python_paths: PlatformPythonPaths) -> None:
    python_paths.expected_python.touch()
    python_paths.foreign_python.touch()
    assert processes.resolve_workspace_python_executable(python_paths.project_root) == python_paths.expected_python


def test_current_platform_file_is_accepted(python_paths: PlatformPythonPaths) -> None:
    python_paths.expected_python.touch()
    assert processes.resolve_workspace_python_executable(python_paths.project_root) == python_paths.expected_python


def test_only_foreign_layout_is_not_used_as_fallback(python_paths: PlatformPythonPaths) -> None:
    python_paths.foreign_python.touch()
    with pytest.raises(FileNotFoundError) as failure:
        processes.resolve_workspace_python_executable(python_paths.project_root)
    assert str(python_paths.expected_python) in str(failure.value)
    assert python_paths.platform in str(failure.value)


def test_directory_cannot_be_selected_as_an_interpreter(python_paths: PlatformPythonPaths) -> None:
    python_paths.expected_python.mkdir()
    python_paths.foreign_python.touch()
    with pytest.raises(FileNotFoundError):
        processes.resolve_workspace_python_executable(python_paths.project_root)
