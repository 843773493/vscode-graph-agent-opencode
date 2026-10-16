from __future__ import annotations

import pytest

from app.agents.workspace_backend import (
    BOXTEAM_ARTIFACTS_ROOT,
    build_workspace_backend,
)
from app.services.infrastructure.attachment_blob_catalog.store import (
    StoredAttachmentContent,
)


@pytest.fixture
def attachment_backend(tmp_path):
    reads = []

    def read(session_id: str, file_id: str) -> StoredAttachmentContent:
        if session_id != "current":
            raise PermissionError("其它 Session 没有本次工具 capability")
        reads.append(file_id)
        return StoredAttachmentContent(b"first\nsecond\n", "text/plain")

    return build_workspace_backend(tmp_path, attachment_reader=read), reads


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


def test_workspace_backend_lists_session_artifact_route_root(tmp_path):
    backend = build_workspace_backend(tmp_path)

    result = backend.ls("/session-artifacts")

    assert result.error is None
    assert result.entries == []


def test_workspace_backend_attachment_read_uses_injected_capability(attachment_backend):
    backend, reads = attachment_backend
    result = backend.read("/session-artifacts/current/attachments/att_owned", offset=1, limit=1)
    assert result.error is None
    assert result.file_data == {"content": "second\n", "encoding": "utf-8"}
    assert reads == ["boxteam-session://current/attachments/att_owned"]
    assert "其它 Session" in backend.read("/session-artifacts/other/attachments/att_owned").error


def test_workspace_backend_attachment_writes_cannot_create_legacy_files(attachment_backend, tmp_path):
    backend, reads = attachment_backend
    path = "/session-artifacts/current/attachments/att_owned"
    assert backend.write(path, "spoofed").error is not None
    assert backend.edit(path, "first", "spoofed").error is not None
    assert backend.upload_files([(path, b"spoofed")])[0].error is not None
    assert reads == []
    assert not (tmp_path / ".boxteam" / "sessions" / "current").exists()


def test_workspace_backend_without_thread_does_not_grant_attachment_read(tmp_path):
    result = build_workspace_backend(tmp_path).read("/session-artifacts/current/attachments/att_owned")
    assert result.file_data is None
    assert "capability" in result.error
