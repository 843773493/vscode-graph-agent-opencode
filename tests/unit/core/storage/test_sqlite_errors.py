"""SQLite 运行时错误保留数值事实、异常身份与诊断原文。"""

from __future__ import annotations

import ctypes
import errno
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from app.core import sqlite_state
from app.core.atomic_fs import native_io_path


@pytest.mark.parametrize("name", ["native.sqlite", "数据库.sqlite"])
@pytest.mark.parametrize("component", ["main", "-journal"])
def test_budget_failure_reports_exact_boundary_without_database_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, name: str, component: str,
) -> None:
    database = tmp_path / name
    main_bytes = len(str(native_io_path(database.resolve())).encode("utf-8"))
    overflowing_bytes = main_bytes + (len(component) if component != "main" else 0)
    maximum = overflowing_bytes - 1
    monkeypatch.setattr(sqlite_state, "sqlite_vfs_max_pathname", lambda: maximum)
    with pytest.raises(ValueError) as caught:
        sqlite_state.validate_sqlite_path_budget(database)
    detail = str(caught.value)
    assert "SQLite 绝对路径超出当前默认 VFS 预算" in detail
    assert "artifact=sqlite_runtime stage=path_budget" in detail
    assert f"component={component}, bytes={overflowing_bytes}, mxPathname={maximum}" in detail
    assert f"sqlite_version={sqlite3.sqlite_version}" in detail
    assert str(database) not in detail and str(database) in caplog.text
    assert not database.exists()


def test_wrong_path_type_does_not_echo_native_input() -> None:
    value = "/本机原始路径/database.sqlite"
    with pytest.raises(TypeError, match="database_path 必须是 Path") as caught:
        sqlite_state.validate_sqlite_path_budget(value)  # type: ignore[arg-type]
    assert value not in str(caught.value)


def test_native_resolve_failure_keeps_errno_and_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    database = tmp_path / "index.sqlite"
    failure = PermissionError(errno.EACCES, f"原生错误 {database}", str(database))
    original = Path.resolve

    def resolve(path, *args, **kwargs):
        if path == database:
            raise failure
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(PermissionError) as caught:
        sqlite_state.validate_sqlite_path_budget(database)
    assert caught.value.__cause__ is failure
    assert caught.value.errno == errno.EACCES and caught.value.filename is None
    assert "stage=path_resolve" in str(caught.value)
    assert str(database) not in str(caught.value) and str(database) in caplog.text
    assert not database.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX 符号链接环")
def test_symlink_loop_preserves_native_type_without_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    database = tmp_path / "loop.sqlite"
    database.symlink_to(database.name)
    with pytest.raises((RuntimeError, OSError)) as caught:
        sqlite_state.validate_sqlite_path_budget(database)
    assert type(caught.value) is type(caught.value.__cause__)
    assert "stage=path_resolve" in str(caught.value)
    assert str(database) not in str(caught.value) and str(database) in caplog.text
    assert database.is_symlink()


@pytest.fixture
def extension_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    extension = tmp_path / "原生运行库.so"
    monkeypatch.setattr(sqlite_state._sqlite3, "__file__", str(extension))
    monkeypatch.setattr(sqlite_state._sqlite3, "__spec__", SimpleNamespace(origin="extension"))
    # 只替换原生库边界，不用另一份系统 SQLite 假冒当前解释器的 VFS。
    monkeypatch.setattr(sqlite_state, "os", SimpleNamespace(name="posix"))
    return extension


def test_extension_load_failure_keeps_original_error(
    extension_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    failure = OSError(errno.ENOENT, f"原生加载错误 {extension_path}", str(extension_path))
    monkeypatch.setattr(ctypes, "CDLL", Mock(side_effect=failure))
    with pytest.raises(RuntimeError, match="stage=vfs_extension") as caught:
        sqlite_state.sqlite_vfs_max_pathname()
    assert caught.value.__cause__ is failure and "errno=2" in str(caught.value)
    assert str(extension_path) not in str(caught.value) and str(extension_path) in caplog.text


@pytest.mark.parametrize("state", ["missing-symbol", "missing-vfs", "invalid-header"])
def test_vfs_contract_failures_keep_reason_and_header_facts(
    extension_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, state: str,
) -> None:
    if state == "missing-symbol":
        native = SimpleNamespace()
        expected = "未导出 sqlite3_vfs_find"
    else:
        header = sqlite_state._SQLiteVFSHeader(1, 0, 0)
        native = SimpleNamespace(sqlite3_vfs_find=Mock(return_value=(
            ctypes.pointer(header) if state == "invalid-header" else None
        )))
        expected = "头部字段非法" if state == "invalid-header" else "未注册默认 VFS"
    monkeypatch.setattr(ctypes, "CDLL", Mock(return_value=native))
    with pytest.raises(RuntimeError, match=expected) as caught:
        sqlite_state.sqlite_vfs_max_pathname()
    if state == "invalid-header":
        assert "iVersion=1, szOsFile=0, mxPathname=0" in str(caught.value)
    if state == "missing-symbol":
        assert isinstance(caught.value.__cause__, AttributeError)
    assert str(extension_path) not in str(caught.value) and str(extension_path) in caplog.text


def test_builtin_probe_failure_preserves_sqlite_code_and_disables_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    probe = tmp_path / "sqlite_vfs_probe.so"
    failure = sqlite3.OperationalError(f"原生探针错误 {probe}")
    failure.sqlite_errorcode = sqlite3.SQLITE_ERROR
    failure.sqlite_errorname = "SQLITE_ERROR"
    connection = Mock()
    connection.load_extension.side_effect = failure
    monkeypatch.setattr(sqlite_state._sqlite3, "__spec__", SimpleNamespace(origin="built-in"))
    monkeypatch.setattr(sqlite_state, "files", lambda package: tmp_path)
    monkeypatch.setattr(sqlite3, "connect", Mock(return_value=connection))
    with pytest.raises(sqlite3.OperationalError, match="stage=vfs_probe") as caught:
        sqlite_state.sqlite_vfs_max_pathname()
    assert type(caught.value) is sqlite3.OperationalError and caught.value.__cause__ is failure
    assert caught.value.sqlite_errorcode == failure.sqlite_errorcode
    assert caught.value.sqlite_errorname == failure.sqlite_errorname
    assert str(probe) not in str(caught.value) and str(probe) in caplog.text
    assert connection.enable_load_extension.call_args_list == [call(True), call(False)]
    connection.close.assert_called_once_with()
