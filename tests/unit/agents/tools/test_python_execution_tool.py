from __future__ import annotations

import sys
from pathlib import Path

from app.agents.tools import python_execution


def test_python_executable_prefers_uv_project_environment(
    monkeypatch,
    tmp_path: Path,
):
    uv_env = tmp_path / "uv-env"
    uv_python = uv_env / "bin" / "python"
    uv_python.parent.mkdir(parents=True)
    uv_python.write_text("", encoding="utf-8")

    workspace_root = tmp_path / "workspace"
    project_root = tmp_path / "project"
    workspace_root.mkdir()
    project_root.mkdir()

    monkeypatch.delenv("BOXTEAM_PYTHON_EXECUTABLE", raising=False)
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", str(uv_env))
    monkeypatch.setattr(python_execution, "get_workspace_root", lambda: workspace_root)
    monkeypatch.setattr(python_execution, "get_project_root", lambda: project_root)

    assert python_execution.get_python_executable() == uv_python


def test_python_executable_falls_back_to_current_interpreter(
    monkeypatch,
    tmp_path: Path,
):
    workspace_root = tmp_path / "workspace"
    project_root = tmp_path / "project"
    workspace_root.mkdir()
    project_root.mkdir()

    monkeypatch.delenv("BOXTEAM_PYTHON_EXECUTABLE", raising=False)
    monkeypatch.delenv("UV_PROJECT_ENVIRONMENT", raising=False)
    monkeypatch.setattr(python_execution, "get_workspace_root", lambda: workspace_root)
    monkeypatch.setattr(python_execution, "get_project_root", lambda: project_root)

    assert python_execution.get_python_executable() == Path(sys.executable)
