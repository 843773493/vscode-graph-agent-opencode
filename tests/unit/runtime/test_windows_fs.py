"""真实 pywin32 错误保留公共文件系统的异常类别、路径与原因。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.core.atomic_fs import native_io_path
from app.runtime import windows_fs
from app.runtime.log_file_identity import (
    runtime_log_file_identity,
    runtime_log_path_identity,
)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="需要真实 Windows pywin32")


@pytest.mark.parametrize("operation", ["flush", "query_acl", "set_acl", "rename"])
def test_missing_target_preserves_native_error_contract(tmp_path: Path, operation: str) -> None:
    import pywintypes

    missing = native_io_path(tmp_path / "missing" / "target")
    with pytest.raises(FileNotFoundError) as raised:
        if operation == "flush":
            windows_fs.flush_directory(missing)
        elif operation == "query_acl":
            windows_fs.private_acl_is_current(missing, is_directory=False)
        elif operation == "set_acl":
            windows_fs.set_private_acl(missing, is_directory=False)
        else:
            windows_fs.rename_entry_exclusive(native_io_path(tmp_path / "source"), missing)
    assert raised.value.winerror in {2, 3}
    assert raised.value.filename == str(missing)
    assert isinstance(raised.value.__cause__, pywintypes.error)


def test_readonly_handle_denial_remains_a_permission_error(tmp_path: Path) -> None:
    import pywintypes

    path = native_io_path(tmp_path / "readonly")
    path.write_bytes(b"protected")
    path.chmod(0o400)
    try:
        with pytest.raises(PermissionError) as raised:
            windows_fs.flush_directory(path)
        assert raised.value.winerror == 5 and raised.value.filename == str(path)
        assert isinstance(raised.value.__cause__, pywintypes.error)
        assert path.read_bytes() == b"protected"
    finally:
        path.chmod(0o600)


def test_long_process_directory_without_short_names_is_explicitly_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import errno

    import win32api

    directory = tmp_path
    while len(str(directory)) < 280:
        directory /= "long-directory"
    monkeypatch.setattr(win32api, "GetShortPathName", lambda value: value)
    with pytest.raises(OSError) as raised:
        windows_fs.process_working_directory(directory)
    assert raised.value.errno == errno.ENAMETOOLONG
    assert raised.value.filename == str(directory)


def test_native_process_path_keeps_the_same_long_directory(tmp_path: Path) -> None:
    directory = tmp_path
    while len(str(directory)) < 220:
        directory /= "long-directory"
    directory.mkdir(parents=True)

    shortened = windows_fs.native_process_path(directory)

    assert Path(shortened).samefile(directory)


def test_exclusive_rename_keeps_both_files_on_conflict(tmp_path: Path) -> None:
    import pywintypes

    source, target = native_io_path(tmp_path / "source"), native_io_path(tmp_path / "target")
    source.write_bytes(b"source")
    target.write_bytes(b"target")
    with pytest.raises(FileExistsError) as raised:
        windows_fs.rename_entry_exclusive(source, target)
    assert raised.value.winerror == 183 and raised.value.filename == str(target)
    assert isinstance(raised.value.__cause__, pywintypes.error)
    assert source.read_bytes() == b"source" and target.read_bytes() == b"target"


def test_log_file_identity_matches_reopened_handle_and_rejects_another_file(tmp_path: Path) -> None:
    path, other = tmp_path / "actual.log", tmp_path / "other.log"
    path.write_bytes(b"actual")
    other.write_bytes(b"other")
    with path.open("rb") as stream:
        descriptor = stream.fileno()
        identity = runtime_log_file_identity(descriptor)
        assert runtime_log_path_identity(path) == identity
        assert runtime_log_path_identity(other) != identity
    with pytest.raises(OSError):
        windows_fs.file_identity(descriptor)
