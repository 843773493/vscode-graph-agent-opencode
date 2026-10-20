from __future__ import annotations

import asyncio
import codecs
import io
import os
import shutil
import threading
import zipfile
from pathlib import Path
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest

from app.core.exceptions import ForbiddenError
from app.core.resource_uri import parse_resource_uri, resource_display_uri
from app.gateway.control.gateway_state import GatewayStateStore
from app.schemas.internal_v2.workspace_file_operations import (
    WorkspaceFileMutationIntentDTO,
)
from app.services.infrastructure import workspace_service as workspace_service_module
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.workspace_files import text_publication
from app.services.infrastructure.workspace_service import (
    WorkspaceFileConflictError,
    WorkspaceService,
)
from tests.support.workspaces import prepare_default_test_workspace


def _workspace_config_service_mock() -> Mock:
    config_service = Mock(spec=ConfigService)
    config_service.get_workspace_file_default_limit.return_value = 500
    config_service.get_workspace_preview_max_bytes.return_value = 1024 * 1024
    config_service.get_workspace_preview_binary_sample_bytes.return_value = 8192
    return config_service


@pytest.fixture
def native_bindings(tmp_path):
    """文件内容测试复用真实 Gateway 根持久化；HTTP 控制链另有集成覆盖。"""
    gateway_id = "gateway_01900000000070008000000000000001"
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    class Bindings:
        def bind(self, root):
            directory_id = state.bind_native_directory(gateway_id=gateway_id, native_root=str(root))
            return resource_display_uri(scope="gateway", scope_id=gateway_id, kind="files", tail_segments=(directory_id,))
        def resolve(self, parsed):
            return Path(state.resolve_native_directory(gateway_id=parsed.scope_id, directory_id=parsed.tail_segments[0]))
    yield Bindings()
    state.close()


@pytest.fixture
def workspace_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> WorkspaceService:
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    return WorkspaceService(config_service=_workspace_config_service_mock(), workspace_root=tmp_path)


@pytest.mark.asyncio
async def test_workspace_service_assigns_distinct_persistent_ids_per_root(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first = WorkspaceService(
        config_service=_workspace_config_service_mock(),
        workspace_root=first_root,
    )
    second = WorkspaceService(
        config_service=_workspace_config_service_mock(),
        workspace_root=second_root,
    )

    first_id = first.workspace_id
    second_id = second.workspace_id
    restarted_first = WorkspaceService(
        config_service=_workspace_config_service_mock(),
        workspace_root=first_root,
    )

    assert UUID(first_id).version == 4
    assert UUID(second_id).version == 4
    assert first_id != second_id
    assert restarted_first.workspace_id == first_id


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
async def test_content_saves_share_lock_across_service_instances(workspace_service, tmp_path, monkeypatch):
    target = tmp_path / "notes.txt"
    target.write_text("before\n")
    opened = await workspace_service.get_file_content(path="notes.txt")
    other = WorkspaceService(config_service=_workspace_config_service_mock(), workspace_root=tmp_path)
    entered, release, second_read = threading.Event(), threading.Event(), threading.Event()
    original_publish = workspace_service_module.publish_file_content
    original_read = workspace_service_module.read_file_preimage

    def blocked_publish(*args, **kwargs):
        entered.set()
        assert release.wait(3), "测试必须解除文件发布屏障"
        return original_publish(*args, **kwargs)

    def observed_read(*args, **kwargs):
        if entered.is_set():
            second_read.set()
        return original_read(*args, **kwargs)

    monkeypatch.setattr(workspace_service_module, "publish_file_content", blocked_publish)
    monkeypatch.setattr(workspace_service_module, "read_file_preimage", observed_read)
    first = asyncio.create_task(workspace_service.update_file_content(path="notes.txt", content="first\n", expected_revision=opened.revision))
    second = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        second = asyncio.create_task(other.update_file_content(path="notes.txt", content="second\n", expected_revision=opened.revision))
        assert not await asyncio.to_thread(second_read.wait, 0.1)
    finally:
        release.set()
        outcomes = await asyncio.gather(*[task for task in (first, second) if task is not None], return_exceptions=True)
    assert outcomes[0].content == "first\n"
    assert isinstance(outcomes[1], WorkspaceFileConflictError)
    assert target.read_text() == "first\n"


@pytest.mark.asyncio
async def test_external_change_before_replace_is_preserved(workspace_service, tmp_path, monkeypatch):
    target = tmp_path / "notes.txt"
    target.write_text("original\n")
    opened = await workspace_service.get_file_content(path="notes.txt")
    original_sync = text_publication.os.fsync

    def change_before_preimage_check(descriptor):
        original_sync(descriptor)
        target.write_text("external\n")

    monkeypatch.setattr(text_publication.os, "fsync", change_before_preimage_check)
    with pytest.raises(WorkspaceFileConflictError, match="保存期间发生变化"):
        await workspace_service.update_file_content(path="notes.txt", content="draft\n", expected_revision=opened.revision)
    assert target.read_text() == "external\n"
    assert not list(tmp_path.glob(".notes.txt.*.tmp"))


@pytest.mark.asyncio
async def test_external_change_after_publish_is_reported_without_rollback(workspace_service, tmp_path, monkeypatch):
    target = tmp_path / "notes.txt"
    target.write_text("original\n")
    opened = await workspace_service.get_file_content(path="notes.txt")
    original_replace = text_publication.os.replace

    def change_after_replace(source, destination):
        original_replace(source, destination)
        target.write_text("other\n")

    monkeypatch.setattr(text_publication.os, "replace", change_after_replace)
    with pytest.raises(text_publication.FilePublicationUnverifiedError, match="实际效果未知") as failure:
        await workspace_service.update_file_content(path="notes.txt", content="saved\n", expected_revision=opened.revision)
    assert failure.value.expected_hash == "sha256:" + workspace_service_module.sha256_hex(b"saved\n")
    assert failure.value.observed_hash == "sha256:" + workspace_service_module.sha256_hex(b"other\n")
    assert target.read_text() == "other\n"
    assert not list(tmp_path.glob(".notes.txt.*.tmp"))


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
    with pytest.raises(ForbiddenError):
        workspace_service.resolve_raw_file(path="../secret.png")


@pytest.mark.asyncio
async def test_filesystem_scope_lists_and_reads_absolute_path(
    monkeypatch: pytest.MonkeyPatch,
    native_bindings,
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
    workspace_service = WorkspaceService(
        config_service=_workspace_config_service_mock(), workspace_root=workspace_root, native_directories=native_bindings,
    )
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

    assert parse_resource_uri(listing.path).scope == "gateway"
    assert listing.items[0].path == content.path
    assert workspace_service.file_paths.native(content.path) == external_file.resolve()
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
    page_root = project_root / "pages"
    page_root.mkdir()
    for name in ["c.txt", "a.txt", "d.txt", "b.txt", "e.txt"]:
        (page_root / name).write_text(name, encoding="utf-8")

    first = await workspace_service.list_files(path="pages", limit=2)
    second = await workspace_service.list_files(path="pages", limit=2, cursor=first.next_cursor)
    third = await workspace_service.list_files(path="pages", limit=2, cursor=second.next_cursor)

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
async def file_manager_service(
    monkeypatch: pytest.MonkeyPatch,
    native_bindings,
):
    output_root = (
        Path.cwd()
        / "out/tests/unit/services/infrastructure/test_workspace_file_edit"
        / "workspace"
    )
    if output_root.exists():
        shutil.rmtree(output_root)
    project_root = prepare_default_test_workspace(workspace_root=output_root / "project",
                                                  template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace")
    source_root = output_root / "clipboard"
    source_root.mkdir()
    monkeypatch.setenv("WORKSPACE_ROOT", str(project_root))
    service = WorkspaceService(config_service=_workspace_config_service_mock(), workspace_root=project_root, native_directories=native_bindings)
    await service.start()
    yield service, project_root, source_root
    await service.stop()
    service.close()


async def _file_intent(service: WorkspaceService, path: str = "") -> WorkspaceFileMutationIntentDTO:
    listing = await service.list_files(path=path)
    return WorkspaceFileMutationIntentDTO(
        client_operation_id="op_" + uuid4().hex, client_sequence=1,
        expected_directory_revision=listing.directory_revision, directory_key=listing.directory_key,
    )


async def _terminal(service: WorkspaceService, receipt):
    for _ in range(300):
        current = service.file_operations.status([receipt.operation_id], actor="local").operations[0]
        if current.state in ("committed", "rejected", "cancelled", "dependency_failed"):
            return current
        if service.file_operations.worker_state == "failed":
            raise RuntimeError(service.file_operations.worker_error)
        await asyncio.sleep(0.01)
    raise AssertionError("文件操作未在有界时间收口")


@pytest.mark.asyncio
async def test_file_manager_creates_file_and_directory_with_parent_snapshot(
    file_manager_service: tuple[WorkspaceService, Path, Path],
) -> None:
    service, project_root, _ = file_manager_service
    original_names = {item.name for item in (await service.list_files()).items}
    assert ".boxteam" in original_names

    after_file = await service.create_file_entry(
        directory_path="",
        scope="workspace",
        name="notes.txt",
        kind="file",
        intent=await _file_intent(service), actor="local",
    )
    after_file = await _terminal(service, after_file)
    after_directory = await service.create_file_entry(
        directory_path="",
        scope="workspace",
        name="models",
        kind="directory",
        intent=await _file_intent(service), actor="local",
    )
    after_directory = await _terminal(service, after_directory)

    assert (project_root / "notes.txt").read_text(encoding="utf-8") == ""
    assert (project_root / "models").is_dir()
    assert after_file.state == after_directory.state == "committed"
    assert {item.name for item in after_file.snapshots[0].items} == original_names | {"notes.txt"}
    assert {item.name for item in after_directory.snapshots[0].items} == original_names | {"models", "notes.txt"}


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

    original_names = {item.name for item in (await service.list_files()).items}
    listing = await service.paste_file_entries(
        directory_path="",
        scope="workspace",
        source_paths=[str(source_file), str(source_directory)],
        intent=await _file_intent(service), actor="local",
    )
    listing = await _terminal(service, listing)

    assert listing.state == "committed"
    assert {item.name for item in listing.snapshots[0].items} == original_names | {"torch_home", "weights.txt"}
    assert (project_root / "weights.txt").read_text(encoding="utf-8") == "weights\n"
    assert (project_root / "torch_home/index.json").read_text(encoding="utf-8") == "{}\n"
    with pytest.raises(FileExistsError, match="已存在"):
        await service.paste_file_entries(
            directory_path="",
            scope="workspace",
            source_paths=[str(source_file)],
            intent=await _file_intent(service), actor="local",
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
        intent=await _file_intent(service, "target"), actor="local",
    )
    listing = await _terminal(service, listing)

    assert listing.state == "committed"
    assert [item.name for item in listing.snapshots[0].items] == ["source.txt"]
    assert (target / "source.txt").read_text(encoding="utf-8") == "workspace copy\n"


@pytest.mark.asyncio
async def test_file_manager_uploads_multiple_files_with_relative_paths(
    file_manager_service: tuple[WorkspaceService, Path, Path],
) -> None:
    service, project_root, _ = file_manager_service

    original_names = {item.name for item in (await service.list_files()).items}
    listing = await service.upload_file_entries(
        directory_path="",
        scope="workspace",
        entries=[
            ("notes.txt", io.BytesIO(b"notes\n")),
            ("uploaded_assets/logo.bin", io.BytesIO(b"\x00\x01logo")),
        ],
        intent=await _file_intent(service), actor="local",
    )
    listing = await _terminal(service, listing)

    assert listing.state == "committed"
    assert {item.name for item in listing.snapshots[0].items} == original_names | {"uploaded_assets", "notes.txt"}
    assert (project_root / "notes.txt").read_bytes() == b"notes\n"
    assert (project_root / "uploaded_assets/logo.bin").read_bytes() == b"\x00\x01logo"


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

    assert service.file_paths.native(revealed) == target.resolve()
    assert popen.call_args.args[0] == ["/usr/bin/xdg-open", str(project_root.resolve())]
