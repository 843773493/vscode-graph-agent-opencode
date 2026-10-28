"""身份文件真实读写及失败事实；位置仅留诊断，不进入调用方异常。"""

from __future__ import annotations

import errno
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import pytest

from app.core import workspace_identity as identity


@pytest.fixture(params=["read", "load", "bind"])
def identity_entry(request: pytest.FixtureRequest) -> Callable[[Path, str], str]:
    if request.param == "read":
        return lambda root, expected: identity.read_workspace_id(root)
    if request.param == "load":
        return lambda root, expected: identity.load_or_create_workspace_id(root)
    return lambda root, expected: identity.bind_workspace_id(root, expected)


@pytest.mark.parametrize("content,category", [
    (b'{"workspace_id":', RuntimeError),
    (b'[]', TypeError),
    (b'{}', TypeError),
    (b'{"workspace_id":12}', TypeError),
    (b'\xff', UnicodeDecodeError),
])
def test_invalid_identity_file_keeps_bytes_and_recovers_without_path_output(
    tmp_path: Path, identity_entry: Callable[[Path, str], str], content: bytes,
    category: type[Exception], caplog: pytest.LogCaptureFixture,
) -> None:
    expected = identity.load_or_create_workspace_id(tmp_path)
    path = identity.workspace_identity_path(tmp_path)
    original = path.read_bytes()
    path.write_bytes(content)
    for _ in range(2):
        with caplog.at_level(logging.ERROR, logger=identity.__name__), pytest.raises(category) as caught:
            identity_entry(tmp_path, expected)
        assert str(path) not in str(caught.value)
        assert "artifact=workspace_identity" in str(caught.value)
        assert str(path) in caplog.text
        assert path.read_bytes() == content
    path.write_bytes(original)
    assert identity_entry(tmp_path, expected) == expected
    assert path.read_bytes() == original


@pytest.mark.parametrize("stage", ["resolve", "read", "stat", "directory", "create", "flush"])
@pytest.mark.parametrize("native_path", ["/private/workspace-marker", r"C:\private\workspace-marker", r"\\server\private\workspace-marker"])
def test_native_identity_io_error_keeps_category_codes_cause_and_original_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    stage: str, native_path: str,
) -> None:
    expected = identity.load_or_create_workspace_id(tmp_path)
    path = identity.workspace_identity_path(tmp_path)
    original = path.read_bytes()
    if stage == "create":
        path.unlink()
    before = path.read_bytes() if path.exists() else None
    raw = PermissionError(errno.EACCES, f"原生身份访问失败: {native_path}", native_path)
    real_read, real_stat, real_mkdir = Path.read_text, Path.stat, Path.mkdir
    real_resolve = Path.resolve
    real_open = identity.os.open

    def read(target: Path, encoding: str | None = None, errors: str | None = None) -> str:
        if target == path:
            raise raw
        return real_read(target, encoding=encoding, errors=errors)

    def stat(target: Path, *args: object, **kwargs: object):
        if target == path:
            raise raw
        return real_stat(target, *args, **kwargs)

    def mkdir(target: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False) -> None:
        if target == path.parent:
            raise raw
        real_mkdir(target, mode=mode, parents=parents, exist_ok=exist_ok)

    def open_file(target, *args, **kwargs):
        if target == path:
            raise raw
        return real_open(target, *args, **kwargs)

    def flush(descriptor: int) -> None:
        raise raw

    def resolve(target: Path, strict: bool = False) -> Path:
        if target == tmp_path:
            raise raw
        return real_resolve(target, strict=strict)

    with monkeypatch.context() as patch:
        if stage == "resolve":
            patch.setattr(Path, "resolve", resolve)
        elif stage == "read":
            patch.setattr(Path, "read_text", read)
        elif stage == "stat":
            patch.setattr(Path, "stat", stat)
        elif stage == "directory":
            patch.setattr(Path, "mkdir", mkdir)
        elif stage == "create":
            patch.setattr(identity.os, "open", open_file)
        else:
            # 真正首次创建进入原fsync；失败后的既有创建行为保持，不由测试回填UUID。
            path.unlink()
            patch.setattr(identity.os, "fsync", flush)
        with caplog.at_level(logging.ERROR, logger=identity.__name__), pytest.raises(PermissionError) as caught:
            identity.load_or_create_workspace_id(tmp_path)
        projected = caught.value
        assert projected is not raw
        assert projected.__cause__ is raw
        assert projected.errno == raw.errno
        assert projected.filename is None and projected.filename2 is None
        assert str(path) not in str(projected) and native_path not in str(projected)
        assert f"stage={stage}" in str(projected)
        assert "artifact=workspace_identity" in str(projected)
        assert raw.filename in caplog.text
        assert any(record.exc_info is not None and record.exc_info[1] is raw for record in caplog.records)
    if stage != "flush":
        assert (path.read_bytes() if path.exists() else None) == before
    recovered = identity.load_or_create_workspace_id(tmp_path)
    assert identity.read_workspace_id(tmp_path) == recovered
    if stage not in {"create", "flush"}:
        assert recovered == expected and path.read_bytes() == original


@pytest.mark.parametrize("concurrent", [False, True])
def test_binding_mismatch_exposes_only_expected_and_actual_uuid(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch, concurrent: bool,
) -> None:
    actual = identity.load_or_create_workspace_id(tmp_path)
    expected = str(uuid4())
    path = identity.workspace_identity_path(tmp_path)
    original = path.read_bytes()
    if concurrent:
        real_stat = Path.stat

        def stat(target: Path, *args, **kwargs):
            if target == path:
                raise FileNotFoundError(errno.ENOENT, "只让原stat观察不到并发创建")
            return real_stat(target, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", stat)
    with caplog.at_level(logging.ERROR, logger=identity.__name__), pytest.raises(RuntimeError) as caught:
        identity.bind_workspace_id(tmp_path, expected)
    assert f"expected={expected}, actual={actual}" in str(caught.value)
    assert str(path) not in str(caught.value)
    assert str(path) in caplog.text
    assert path.read_bytes() == original


def test_non_file_identity_is_never_replaced_or_reported_as_missing(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = identity.workspace_identity_path(tmp_path)
    path.mkdir(parents=True)
    marker = path / "marker"
    marker.write_text("keep")
    with caplog.at_level(logging.ERROR, logger=identity.__name__), pytest.raises(RuntimeError) as caught:
        identity.load_or_create_workspace_id(tmp_path)
    assert str(path) not in str(caught.value) and "不是文件" in str(caught.value)
    assert str(path) in caplog.text
    assert marker.read_text() == "keep"


def test_concurrent_creator_read_failure_keeps_read_stage_and_direct_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    expected = identity.load_or_create_workspace_id(tmp_path)
    path = identity.workspace_identity_path(tmp_path)
    original = path.read_bytes()
    raw = PermissionError(errno.EACCES, "并发创建后的身份读取拒绝", str(path))
    real_stat = Path.stat
    real_read = Path.read_text

    def stat(target: Path, *args, **kwargs):
        if target == path:
            raise FileNotFoundError(errno.ENOENT, "只让原stat观察不到并发创建")
        return real_stat(target, *args, **kwargs)

    def read(target: Path, encoding: str | None = None, errors: str | None = None) -> str:
        if target == path:
            raise raw
        return real_read(target, encoding=encoding, errors=errors)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "stat", stat)
        patch.setattr(Path, "read_text", read)
        with caplog.at_level(logging.ERROR, logger=identity.__name__), pytest.raises(PermissionError) as caught:
            identity.load_or_create_workspace_id(tmp_path)
        assert caught.value.__cause__ is raw
        assert "stage=read" in str(caught.value) and "stage=create" not in str(caught.value)
        assert str(path) not in str(caught.value)
        assert len([record for record in caplog.records if record.name == identity.__name__]) == 1
    assert path.read_bytes() == original
    assert identity.load_or_create_workspace_id(tmp_path) == expected


@pytest.mark.skipif(sys.platform != "linux", reason="真实Linux符号链接循环；不替代Windows原生验证")
def test_native_symlink_loop_preserves_failure_and_creates_no_identity(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    left, right = tmp_path / "left", tmp_path / "right"
    left.symlink_to(right)
    right.symlink_to(left)
    with caplog.at_level(logging.ERROR, logger=identity.__name__), pytest.raises(RuntimeError) as caught:
        identity.load_or_create_workspace_id(left)
    assert str(left) not in str(caught.value) and str(right) not in str(caught.value)
    assert "artifact=workspace_identity" in str(caught.value) and "stage=resolve" in str(caught.value)
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert str(left) in caplog.text or str(right) in caplog.text
    assert left.readlink() == right and right.readlink() == left
    assert sorted(path.name for path in tmp_path.iterdir()) == ["left", "right"]
