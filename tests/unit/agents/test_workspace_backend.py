from __future__ import annotations

from app.agents.workspace_backend import (
    BOXTEAM_ARTIFACTS_ROOT,
    build_workspace_backend,
)


def test_workspace_backend_routes_artifacts_into_boxteam(tmp_path):
    backend = build_workspace_backend(tmp_path)

    assert backend.artifacts_root == BOXTEAM_ARTIFACTS_ROOT

    result = backend.write("/.boxteam/conversation_history/session.md", "history")

    assert result.error is None
    assert (
        tmp_path / ".boxteam" / "conversation_history" / "session.md"
    ).read_text(encoding="utf-8") == "history"


def test_workspace_backend_reads_relative_and_virtual_absolute_paths(tmp_path):
    (tmp_path / "README.md").write_text("# Workspace\n", encoding="utf-8")
    backend = build_workspace_backend(tmp_path)

    relative_result = backend.read("README.md")
    absolute_result = backend.read("/README.md")

    assert relative_result.error is None
    assert relative_result.file_data is not None
    assert relative_result.file_data["content"] == "# Workspace\n"
    assert absolute_result.error is None
    assert absolute_result.file_data is not None
    assert absolute_result.file_data["content"] == "# Workspace\n"
