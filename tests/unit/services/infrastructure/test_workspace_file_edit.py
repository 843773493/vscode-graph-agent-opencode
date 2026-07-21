from __future__ import annotations

import codecs
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.workspace_service import (
    WorkspaceFileConflictError,
    WorkspaceService,
)


@pytest.fixture
def workspace_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> WorkspaceService:
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    return WorkspaceService(config_service=Mock(spec=ConfigService))


@pytest.mark.asyncio
async def test_update_file_content_saves_atomically_and_preserves_mode(
    workspace_service: WorkspaceService,
    tmp_path: Path,
) -> None:
    target = tmp_path / "notes.txt"
    target.write_text("before\n", encoding="utf-8")
    target.chmod(0o640)
    opened = await workspace_service.get_file_content(path="notes.txt")

    saved = await workspace_service.update_file_content(
        path="notes.txt",
        content="after\nsecond line\n",
        expected_revision=opened.revision,
    )

    assert target.read_text(encoding="utf-8") == "after\nsecond line\n"
    assert saved.content == "after\nsecond line\n"
    assert saved.revision != opened.revision
    assert saved.size == len("after\nsecond line\n".encode())
    assert target.stat().st_mode & 0o777 == 0o640
    assert list(tmp_path.glob(".notes.txt.*.tmp")) == []


@pytest.mark.asyncio
async def test_update_file_content_rejects_stale_revision(
    workspace_service: WorkspaceService,
    tmp_path: Path,
) -> None:
    target = tmp_path / "notes.txt"
    target.write_text("opened\n", encoding="utf-8")
    opened = await workspace_service.get_file_content(path="notes.txt")
    target.write_text("changed elsewhere\n", encoding="utf-8")

    with pytest.raises(WorkspaceFileConflictError, match="编辑期间发生变化"):
        await workspace_service.update_file_content(
            path="notes.txt",
            content="editor draft\n",
            expected_revision=opened.revision,
        )

    assert target.read_text(encoding="utf-8") == "changed elsewhere\n"


@pytest.mark.asyncio
async def test_update_file_content_preserves_utf8_bom(
    workspace_service: WorkspaceService,
    tmp_path: Path,
) -> None:
    target = tmp_path / "bom.txt"
    target.write_bytes(codecs.BOM_UTF8 + "before\n".encode())
    opened = await workspace_service.get_file_content(path="bom.txt")

    await workspace_service.update_file_content(
        path="bom.txt",
        content="after\n",
        expected_revision=opened.revision,
    )

    assert target.read_bytes() == codecs.BOM_UTF8 + "after\n".encode()


@pytest.mark.asyncio
async def test_update_file_content_rejects_symlink(
    workspace_service: WorkspaceService,
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.txt"
    target.write_text("target\n", encoding="utf-8")
    link = tmp_path / "link.txt"
    os.symlink(target.name, link)
    opened = await workspace_service.get_file_content(path="link.txt")

    with pytest.raises(ValueError, match="符号链接"):
        await workspace_service.update_file_content(
            path="link.txt",
            content="changed\n",
            expected_revision=opened.revision,
        )

    assert target.read_text(encoding="utf-8") == "target\n"
