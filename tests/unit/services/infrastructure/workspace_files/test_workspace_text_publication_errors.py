"""最终文件访问保留原生原因，文本 owner 继续报告原 URI 和发布效果。"""

from __future__ import annotations

import errno
import logging
from pathlib import Path

import pytest

from app.core import failures
from app.core.atomic_fs import native_io_path
from app.core.resource_uri import resource_display_uri
from app.services.infrastructure.workspace_files import filesystem, text_publication


@pytest.fixture
def source(tmp_path: Path) -> tuple[Path, str]:
    target = tmp_path / "notes.txt"
    target.write_bytes(b"original\r\n")
    uri = resource_display_uri(scope="workspace", scope_id="6fc6e424-ec7e-4e35-8dca-34b6d1264571",
                               kind="files", tail_segments=("root", target.name))
    return target, uri


@pytest.fixture(params=[False, True], ids=["logging", "broken-logging"])
def diagnostic_sink(source, request: pytest.FixtureRequest):
    if not request.param:
        yield None
        return
    blocked = source[0].parent / "blocked-diagnostic-log"
    blocked.write_bytes(b"keep obstruction")
    class FailingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            assert record.exc_info is not None and record.exc_info[1] is not None
            with (blocked / "service.log").open("a", encoding="utf-8"):
                pass

    handler = FailingHandler(level=logging.ERROR)
    loggers = (logging.getLogger(filesystem.__name__), failures.logger)
    for logger in loggers:
        logger.addHandler(handler)
    try:
        yield blocked
    finally:
        for logger in loggers:
            logger.removeHandler(handler)
        handler.close()
        assert blocked.read_bytes() == b"keep obstruction"


@pytest.mark.parametrize("number", [errno.EACCES, errno.EIO, errno.ELOOP, errno.ENOENT])
@pytest.mark.parametrize("stage", ["stat_before", "open", "read", "stat_after"])
def test_read_failures_preserve_type_errno_cause_and_uri(source, monkeypatch, number, stage, diagnostic_sink):
    target, uri = source
    original = OSError(number, f"原始系统消息含路径 {target}", str(target))
    lstat, open_file = Path.lstat, Path.open
    stats = 0

    def observed_stat(path, *args, **kwargs):
        nonlocal stats
        if path == native_io_path(target):
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
        if path == native_io_path(target):
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
    assert error is original and error.errno == number and error.__cause__ is None
    assert error.filename == str(target) and error.strerror == f"原始系统消息含路径 {target}"
    detail = filesystem.file_access_error_detail(error)
    assert uri in detail and f"errno={number}" in detail and "stage=read" in detail
    assert str(error) in detail
    assert target.read_bytes() == b"original\r\n"
    if diagnostic_sink is not None:
        assert "diagnostic_sink_error=NotADirectoryError" in detail
        assert any(str(diagnostic_sink) in note for note in original.__notes__)


@pytest.mark.parametrize("stage", ["mkdir", "temporary", "fsync", "replace", "delete"])
@pytest.mark.parametrize("number", [errno.EACCES, errno.EIO])
def test_publication_failures_leave_original_and_remove_temporary(source, monkeypatch, stage, number, diagnostic_sink):
    target, uri = source
    before = text_publication.read_file_preimage(target, reference=uri)
    original = OSError(number, f"原生系统消息含路径 {target.parent}", str(target), None, str(target.with_suffix(".tmp")))

    def fail(*_args, **_kwargs):
        raise original

    def fail_path(operation):
        def invoke(path, *args, **kwargs):
            if path == native_io_path(target if stage == "delete" else target.parent):
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
    assert error is original and error.errno == number and error.__cause__ is None
    assert error.filename == str(target) and error.filename2 == str(target.with_suffix(".tmp"))
    assert error.strerror == f"原生系统消息含路径 {target.parent}"
    detail = filesystem.file_access_error_detail(error)
    assert uri in detail and f"errno={number}" in detail
    assert f"stage={'delete' if stage == 'delete' else 'publish'}" in detail and str(error) in detail
    assert target.read_bytes() == before.content
    assert list(target.parent.glob(".notes.txt.*.tmp")) == []
    if diagnostic_sink is not None:
        assert "diagnostic_sink_error=NotADirectoryError" in detail
        assert any(str(diagnostic_sink) in note for note in original.__notes__)


def test_postpublication_read_failure_keeps_unknown_effect(source, monkeypatch, diagnostic_sink):
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
        if published and path == native_io_path(target):
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
    assert error.__cause__ is original
    detail = filesystem.file_access_error_detail(error)
    assert str(error) in detail and str(original) in detail and "stage=read" in detail
    assert target.read_bytes() == b"published"
    assert list(target.parent.glob(".notes.txt.*.tmp")) == []
    if diagnostic_sink is not None:
        assert any(str(diagnostic_sink) in note for note in original.__notes__)


@pytest.mark.parametrize("filename_field", ["first", "second", "none"])
@pytest.mark.parametrize("number", [errno.EACCES, None])
def test_raw_file_error_keeps_public_facts_when_diagnostics_fail(source, diagnostic_sink, filename_field, number):
    target, _ = source
    original = PermissionError(number, f"private-strerror: {target}")
    if filename_field != "none":
        setattr(original, "filename" if filename_field == "first" else "filename2", str(target))
    detail = filesystem.file_access_error_detail(original)
    assert "PermissionError" in detail and f"errno={number}" in detail
    if filename_field == "first" and number is not None:
        assert original.strerror is not None
        assert repr(str(target)) in detail and original.strerror in detail
    else:
        assert str(target.parent) not in detail and "private-strerror" not in detail
    if diagnostic_sink is not None:
        assert "diagnostic_sink_error=NotADirectoryError" in detail
        assert any(str(diagnostic_sink) in note for note in original.__notes__)


def test_directory_rejection_preserves_existing_uri_detail(source):
    target, uri = source
    with pytest.raises(IsADirectoryError) as caught:
        text_publication.read_file_preimage(target.parent, reference=uri)
    assert filesystem.file_access_error_detail(caught.value) == f"目标不是文件: {uri}"


def test_unregistered_error_cannot_publish_even_when_it_looks_like_file_access(source, diagnostic_sink):
    target, uri = source
    original = OSError(f"文件访问失败: uri={uri}: stage=read, private-strerror: {target}")
    detail = filesystem.file_access_error_detail(original)
    assert "error=OSError" in detail and "errno=None" in detail
    assert uri not in detail and str(target.parent) not in detail and "private-strerror" not in detail


@pytest.mark.parametrize("number", [errno.EIO, None])
def test_known_file_access_keeps_reason_without_filename_and_preserves_existing_cause(source, diagnostic_sink, number):
    target, uri = source
    original = OSError(number, f"已知文件同步失败: {target}")
    cause = RuntimeError("原有文件系统原因")
    original.__cause__ = cause
    arguments = original.args
    with pytest.raises(OSError) as caught, filesystem.file_access(uri, "fsync"):
        raise original
    assert caught.value is original and original.__cause__ is cause and original.args == arguments
    detail = filesystem.file_access_error_detail(original)
    assert uri in detail and "stage=fsync" in detail and str(original) in detail
    if diagnostic_sink is not None:
        assert "diagnostic_sink_error=NotADirectoryError" in detail


def test_provider_file_shaped_error_keeps_source_protection(source, diagnostic_sink):
    target, uri = source
    original = failures.mark_provider_failure(PermissionError(errno.EACCES, "provider-key-secret", str(target)))
    with pytest.raises(PermissionError) as caught, filesystem.file_access(uri, "read"):
        raise original
    assert caught.value is original and original.filename == str(target)
    detail = filesystem.file_access_error_detail(original)
    assert "stage=execution-provider" in detail and "errno=13" in detail
    assert str(target) not in detail and "provider-key-secret" not in detail
