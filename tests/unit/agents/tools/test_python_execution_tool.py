from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.agents.tools import python_execution


@pytest.fixture
def interpreter_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> dict[str, Path]:
    venv_python_path = (
        Path("Scripts") / "python.exe"
        if sys.platform == "win32"
        else Path("bin") / "python"
    )
    workspace_root = tmp_path / "workspace"
    project_root = tmp_path / "project"
    uv_env = tmp_path / "uv-env"
    paths = {
        "explicit": tmp_path / "custom-python" / venv_python_path,
        "workspace": workspace_root / ".venv" / venv_python_path,
        "project": project_root / ".venv" / venv_python_path,
        "uv": uv_env / venv_python_path,
    }
    for path in paths.values():
        path.parent.mkdir(parents=True)
        path.touch()

    monkeypatch.delenv("BOXTEAM_PYTHON_EXECUTABLE", raising=False)
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", str(uv_env))
    monkeypatch.setenv("BOXTEAM_PROJECT_ROOT", str(project_root))
    monkeypatch.setattr(python_execution, "get_workspace_root", lambda: workspace_root)
    monkeypatch.setattr(python_execution, "get_project_root", lambda: project_root)
    return paths


@pytest.mark.parametrize("source", ["explicit", "uv", "workspace", "project", "backend"])
def test_python_executable_selection(
    monkeypatch: pytest.MonkeyPatch,
    interpreter_paths: dict[str, Path],
    source: str,
) -> None:
    if source == "explicit":
        monkeypatch.setenv("BOXTEAM_PYTHON_EXECUTABLE", str(interpreter_paths["explicit"]))
    elif source == "workspace":
        monkeypatch.delenv("UV_PROJECT_ENVIRONMENT")
    elif source == "project":
        monkeypatch.delenv("UV_PROJECT_ENVIRONMENT")
        interpreter_paths["workspace"].unlink()
    elif source == "backend":
        monkeypatch.delenv("UV_PROJECT_ENVIRONMENT")
        interpreter_paths["workspace"].unlink()
        interpreter_paths["project"].unlink()

    expected = Path(sys.executable) if source == "backend" else interpreter_paths[source]
    assert python_execution.get_python_executable() == expected


def test_python_executable_rejects_invalid_explicit_override(
    monkeypatch: pytest.MonkeyPatch,
    interpreter_paths: dict[str, Path],
) -> None:
    missing_python = interpreter_paths["explicit"]
    missing_python.unlink()
    monkeypatch.setenv("BOXTEAM_PYTHON_EXECUTABLE", str(missing_python))

    with pytest.raises(FileNotFoundError, match="BOXTEAM_PYTHON_EXECUTABLE"):
        python_execution.get_python_executable()
