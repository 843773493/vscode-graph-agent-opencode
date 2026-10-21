"""最终文件访问错误不把原生文件名带出文本 owner。"""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

from app.core.resource_uri import resource_display_uri
from app.services.infrastructure.workspace_files import text_publication


@pytest.fixture
def source(tmp_path: Path) -> tuple[Path, str]:
    target = tmp_path / "notes.txt"
    target.write_bytes(b"original\r\n")
    uri = resource_display_uri(scope="workspace", scope_id="6fc6e424-ec7e-4e35-8dca-34b6d1264571",
                               kind="files", tail_segments=("root", target.name))
    return target, uri


@pytest.mark.parametrize("number", [errno.EACCES, errno.EIO, errno.ELOOP, errno.ENOENT])
@pytest.mark.parametrize("stage", ["stat_before", "open", "read", "stat_after"])
def test_read_failures_preserve_type_errno_cause_and_uri(source, monkeypatch, number, stage):
    target, uri = source
    original = OSError(number, f"原始系统消息含路径 {target}", str(target))
    lstat, open_file = Path.lstat, Path.open
    stats = 0

    def observed_stat(path, *args, **kwargs):
        nonlocal stats
        if path == target:
            stats += 1
            if (stage == "stat_before" and stats == 1) or (stage == "stat_after" and stats == 2):
                raise original
        return lstat(path, *args, **kwargs)

    class FailedRead:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, *_args):
            raise original

    def observed_open(path, *args, **kwargs):
        if path == target:
            if stage == "open":
                raise original
            if stage == "read":
                return FailedRead()
        return open_file(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "lstat", observed_stat)
        patch.setattr(Path, "open", observed_open)
        with pytest.raises(type(original)) as caught:
            text_publication.read_file_preimage(target, reference=uri)
    error = caught.value
    assert type(error) is type(original) and error.errno == number
    assert error.__cause__ is original
    assert error.filename is None and error.filename2 is None
    assert uri in str(error) and f"errno={number}" in str(error)
    assert str(target.parent) not in str(error)
    assert original.filename == str(target)
    assert target.read_bytes() == b"original\r\n"


@pytest.mark.parametrize("stage", ["mkdir", "temporary", "fsync", "replace", "delete"])
@pytest.mark.parametrize("number", [errno.EACCES, errno.EIO])
def test_publication_failures_leave_original_and_remove_temporary(source, monkeypatch, stage, number):
    target, uri = source
    before = text_publication.read_file_preimage(target, reference=uri)
    original = OSError(number, f"原生系统消息含路径 {target.parent}", str(target), None, str(target.with_suffix(".tmp")))

    def fail(*_args, **_kwargs):
        raise original

    def fail_path(operation):
        def invoke(path, *args, **kwargs):
            if path == (target if stage == "delete" else target.parent):
                raise original
            return operation(path, *args, **kwargs)
        return invoke

    with monkeypatch.context() as patch:
        if stage == "mkdir":
            patch.setattr(Path, "mkdir", fail_path(Path.mkdir))
        elif stage == "temporary":
            patch.setattr(text_publication.tempfile, "mkstemp", fail)
        elif stage == "fsync":
            patch.setattr(text_publication.os, "fsync", fail)
        elif stage == "replace":
            patch.setattr(text_publication.os, "replace", fail)
        else:
            patch.setattr(Path, "unlink", fail_path(Path.unlink))
        with pytest.raises(type(original)) as caught:
            if stage == "delete":
                text_publication.delete_file_content(target, before=before, reference=uri)
            else:
                text_publication.publish_file_content(target, b"replacement", before=before, reference=uri)
    error = caught.value
    assert type(error) is type(original) and error.errno == number
    assert error.__cause__ is original
    assert error.filename is None and error.filename2 is None
    assert uri in str(error) and f"errno={number}" in str(error)
    assert str(target.parent) not in str(error)
    assert original.filename == str(target) and original.filename2 == str(target.with_suffix(".tmp"))
    assert target.read_bytes() == before.content
    assert list(target.parent.glob(".notes.txt.*.tmp")) == []


def test_postpublication_read_failure_keeps_unknown_effect(source, monkeypatch):
    target, uri = source
    before = text_publication.read_file_preimage(target, reference=uri)
    replace = text_publication.os.replace
    lstat = Path.lstat
    published = False
    original = OSError(errno.EIO, "读取已发布文件失败", str(target))

    def mark_replace(*args):
        nonlocal published
        replace(*args)
        published = True

    def fail_after_publication(path, *args, **kwargs):
        if published and path == target:
            raise original
        return lstat(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(text_publication.os, "replace", mark_replace)
        patch.setattr(Path, "lstat", fail_after_publication)
        with pytest.raises(text_publication.FilePublicationUnverifiedError) as caught:
            text_publication.publish_file_content(target, b"published", before=before, reference=uri)
    error = caught.value
    assert error.observed_hash is None and error.reference == uri
    assert uri in str(error) and str(target.parent) not in str(error)
    assert isinstance(error.__cause__, OSError) and error.__cause__.__cause__ is original
    assert target.read_bytes() == b"published"
    assert list(target.parent.glob(".notes.txt.*.tmp")) == []
