from __future__ import annotations

import codecs
import io
import os
import shutil
import zipfile
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.services.infrastructure import workspace_service as workspace_service_module
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
    assert saved.size == len(b"after\nsecond line\n")
    if os.name != "nt":
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
    target.write_bytes(codecs.BOM_UTF8 + b"before\n")
    opened = await workspace_service.get_file_content(path="bom.txt")

    await workspace_service.update_file_content(
        path="bom.txt",
        content="after\n",
        expected_revision=opened.revision,
    )

    assert target.read_bytes() == codecs.BOM_UTF8 + b"after\n"


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


def test_resolve_raw_file_returns_safe_path_and_media_type(
    workspace_service: WorkspaceService,
    tmp_path: Path,
) -> None:
    target = tmp_path / "asset" / "diagram.svg"
    target.parent.mkdir()
    target.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8")

    resolved_path, media_type = workspace_service.resolve_raw_file(
        path="asset/diagram.svg"
    )

    assert resolved_path == target
    assert media_type == "image/svg+xml"


def test_resolve_raw_file_rejects_path_outside_workspace(
    workspace_service: WorkspaceService,
) -> None:
    with pytest.raises(ValueError, match="上级目录"):
        workspace_service.resolve_raw_file(path="../secret.png")


@pytest.mark.asyncio
async def test_filesystem_scope_lists_and_reads_absolute_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = (
        Path.cwd()
        / "out/tests/unit/services/infrastructure/test_workspace_file_edit"
    )
    filesystem_scope_root = output_root / "workspace" / "filesystem_scope"
    if filesystem_scope_root.exists():
        shutil.rmtree(filesystem_scope_root)
    workspace_root = filesystem_scope_root / "project"
    workspace_root.mkdir(parents=True)
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace_root))
    workspace_service = WorkspaceService(config_service=Mock(spec=ConfigService))
    external_root = filesystem_scope_root / "external"
    external_root.mkdir()
    external_file = external_root / "torch-cache.txt"
    external_file.write_bytes(b"cached model\n")

    listing = await workspace_service.list_files(
        path=str(external_root),
        scope="filesystem",
    )
    content = await workspace_service.get_file_content(
        path=str(external_file),
        scope="filesystem",
    )

    assert listing.path == str(external_root.resolve())
    assert listing.items[0].path == str(external_file.resolve())
    assert content.path == str(external_file.resolve())
    assert content.content == "cached model\n"


@pytest.mark.asyncio
async def test_filesystem_scope_rejects_relative_path(
    workspace_service: WorkspaceService,
) -> None:
    with pytest.raises(ValueError, match="必须是绝对路径"):
        await workspace_service.list_files(
            path="relative/path",
            scope="filesystem",
        )


@pytest.mark.asyncio
async def test_directory_listing_cursor_reads_every_page(
    file_manager_service: tuple[WorkspaceService, Path, Path],
) -> None:
    workspace_service, project_root, _ = file_manager_service
    for name in ["c.txt", "a.txt", "d.txt", "b.txt", "e.txt"]:
        (project_root / name).write_text(name, encoding="utf-8")

    first = await workspace_service.list_files(limit=2)
    second = await workspace_service.list_files(limit=2, cursor=first.next_cursor)
    third = await workspace_service.list_files(limit=2, cursor=second.next_cursor)

    assert [item.name for item in first.items] == ["a.txt", "b.txt"]
    assert [item.name for item in second.items] == ["c.txt", "d.txt"]
    assert [item.name for item in third.items] == ["e.txt"]
    assert first.next_cursor is not None
    assert second.next_cursor is not None
    assert third.next_cursor is None


@pytest.mark.asyncio
async def test_directory_listing_rejects_invalid_cursor(
    file_manager_service: tuple[WorkspaceService, Path, Path],
) -> None:
    workspace_service, _, _ = file_manager_service
    with pytest.raises(ValueError, match="游标"):
        await workspace_service.list_files(cursor="not-a-cursor")


@pytest.fixture
def file_manager_service(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[WorkspaceService, Path, Path]:
    output_root = (
        Path.cwd()
        / "out/tests/unit/services/infrastructure/test_workspace_file_edit"
        / "workspace/file_manager"
    )
    if output_root.exists():
        shutil.rmtree(output_root)
    project_root = output_root / "project"
    source_root = output_root / "clipboard"
    project_root.mkdir(parents=True)
    source_root.mkdir()
    monkeypatch.setenv("WORKSPACE_ROOT", str(project_root))
    return (
        WorkspaceService(config_service=Mock(spec=ConfigService)),
        project_root,
        source_root,
    )


@pytest.mark.asyncio
async def test_file_manager_creates_file_and_directory_with_parent_snapshot(
    file_manager_service: tuple[WorkspaceService, Path, Path],
) -> None:
    service, project_root, _ = file_manager_service

    after_file = await service.create_file_entry(
        directory_path="",
        scope="workspace",
        name="notes.txt",
        kind="file",
    )
    after_directory = await service.create_file_entry(
        directory_path="",
        scope="workspace",
        name="models",
        kind="directory",
    )

    assert (project_root / "notes.txt").read_text(encoding="utf-8") == ""
    assert (project_root / "models").is_dir()
    assert [item.name for item in after_file.items] == ["notes.txt"]
    assert [item.name for item in after_directory.items] == ["models", "notes.txt"]


@pytest.mark.asyncio
async def test_file_manager_pastes_multiple_paths_and_rejects_overwrite(
    file_manager_service: tuple[WorkspaceService, Path, Path],
) -> None:
    service, project_root, source_root = file_manager_service
    source_file = source_root / "weights.txt"
    source_file.write_text("weights\n", encoding="utf-8")
    source_directory = source_root / "torch_home"
    source_directory.mkdir()
    (source_directory / "index.json").write_text("{}\n", encoding="utf-8")

    listing = await service.paste_file_entries(
        directory_path="",
        scope="workspace",
        source_paths=[str(source_file), str(source_directory)],
    )

    assert [item.name for item in listing.items] == ["torch_home", "weights.txt"]
    assert (project_root / "weights.txt").read_text(encoding="utf-8") == "weights\n"
    assert (project_root / "torch_home/index.json").read_text(encoding="utf-8") == "{}\n"
    with pytest.raises(FileExistsError, match="已存在"):
        await service.paste_file_entries(
            directory_path="",
            scope="workspace",
            source_paths=[str(source_file)],
        )


@pytest.mark.asyncio
async def test_file_manager_copies_workspace_entry_by_scoped_path(
    file_manager_service: tuple[WorkspaceService, Path, Path],
) -> None:
    service, project_root, _ = file_manager_service
    source = project_root / "source.txt"
    source.write_text("workspace copy\n", encoding="utf-8")
    target = project_root / "target"
    target.mkdir()

    listing = await service.copy_file_entry(
        directory_path="target",
        scope="workspace",
        source_path="source.txt",
        source_scope="workspace",
    )

    assert [item.name for item in listing.items] == ["source.txt"]
    assert (target / "source.txt").read_text(encoding="utf-8") == "workspace copy\n"


@pytest.mark.asyncio
async def test_file_manager_uploads_multiple_files_with_relative_paths(
    file_manager_service: tuple[WorkspaceService, Path, Path],
) -> None:
    service, project_root, _ = file_manager_service

    listing = await service.upload_file_entries(
        directory_path="",
        scope="workspace",
        entries=[
            ("notes.txt", io.BytesIO(b"notes\n")),
            ("assets/logo.bin", io.BytesIO(b"\x00\x01logo")),
        ],
    )

    assert [item.name for item in listing.items] == ["assets", "notes.txt"]
    assert (project_root / "notes.txt").read_bytes() == b"notes\n"
    assert (project_root / "assets/logo.bin").read_bytes() == b"\x00\x01logo"


def test_file_manager_prepares_directory_download_as_zip(
    file_manager_service: tuple[WorkspaceService, Path, Path],
) -> None:
    service, project_root, _ = file_manager_service
    directory = project_root / "bundle"
    directory.mkdir()
    (directory / "readme.txt").write_bytes(b"download\n")

    archive_path, filename, media_type, temporary = service.prepare_file_download(
        path="bundle",
        scope="workspace",
    )
    try:
        assert filename == "bundle.zip"
        assert media_type == "application/zip"
        assert temporary is True
        with zipfile.ZipFile(archive_path) as archive:
            assert archive.read("bundle/readme.txt") == b"download\n"
    finally:
        archive_path.unlink(missing_ok=True)


def test_file_manager_reveals_path_with_host_file_manager(
    file_manager_service: tuple[WorkspaceService, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, project_root, _ = file_manager_service
    target = project_root / "notes.txt"
    target.write_text("notes\n", encoding="utf-8")
    popen = Mock()
    monkeypatch.setattr(workspace_service_module.sys, "platform", "linux")
    monkeypatch.setattr(workspace_service_module.os, "name", "posix")
    monkeypatch.setattr(
        workspace_service_module.shutil,
        "which",
        lambda executable: "/usr/bin/xdg-open" if executable == "xdg-open" else None,
    )
    monkeypatch.setattr(workspace_service_module.subprocess, "Popen", popen)

    revealed = service.reveal_file_entry(path="notes.txt", scope="workspace")

    assert revealed == target.resolve()
    assert popen.call_args.args[0] == ["/usr/bin/xdg-open", str(project_root.resolve())]
