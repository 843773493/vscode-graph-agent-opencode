"""文件形态拒绝发生在 SQLite 打开前，原路径仅进入原生诊断。"""

from __future__ import annotations

import errno
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.services.infrastructure.rollout_context.storage.primitives import (
    _RolloutSQLiteConnection,
)


@pytest.fixture(params=[(False, False), (True, False), (True, True)], ids=["writer", "reader", "frozen"])
def connection_mode(request: pytest.FixtureRequest) -> tuple[bool, bool]:
    return request.param


@pytest.fixture
def index_path(tmp_path: Path) -> Path:
    path = tmp_path / "rollout" / "index.sqlite"
    path.parent.mkdir()
    with _RolloutSQLiteConnection.open(path, read_only=False, frozen_source=False) as connection:
        connection.execute("CREATE TABLE marker(value TEXT NOT NULL)")
        connection.execute("INSERT INTO marker VALUES ('原始正文 Ω')")
    return path


@pytest.fixture(params=["root_symlink", "root_file", "index_symlink", "index_directory"])
def invalid_index(
    index_path: Path, tmp_path: Path, request: pytest.FixtureRequest,
) -> Iterator[tuple[Path, Path, str]]:
    shape = request.param
    if "symlink" in shape and os.name == "nt":
        pytest.skip("Windows 符号链接权限由实机验收覆盖")
    target = index_path.parent if shape.startswith("root_") else index_path
    backup = tmp_path / "原始数据库"
    target.rename(backup)
    if "symlink" in shape:
        target.symlink_to(backup, target_is_directory=shape.startswith("root_"))
        reason = "rollout 目录不能是符号链接" if shape.startswith("root_") else "rollout index.sqlite 不能是符号链接"
    elif shape == "root_file":
        target.write_text("原始阻挡文件")
        reason = "rollout 路径必须是普通目录"
    else:
        target.mkdir()
        reason = "rollout index.sqlite 必须是普通文件"
    try:
        yield index_path, target, reason
    finally:
        if target.is_symlink() or target.is_file():
            target.unlink()
        else:
            target.rmdir()
        backup.rename(target)
        with _RolloutSQLiteConnection.open(index_path, read_only=True, frozen_source=False) as connection:
            assert connection.execute("SELECT value FROM marker").fetchone() == ("原始正文 Ω",)


def test_structural_rejection_preserves_source_and_does_not_open_sqlite(
    invalid_index: tuple[Path, Path, str], connection_mode: tuple[bool, bool],
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    index, invalid, reason = invalid_index
    before = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file() and not path.is_symlink()}
    opened = []
    original = sqlite3.connect

    def connect(database, *args, **kwargs):
        if database != ":memory:":
            opened.append(database)
        return original(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    read_only, frozen = connection_mode
    with pytest.raises(RuntimeError, match=reason) as caught:
        _RolloutSQLiteConnection.open(index, read_only=read_only, frozen_source=frozen)
    assert "artifact=rollout_index stage=path_validation" in str(caught.value)
    assert str(index) not in str(caught.value) and str(invalid) not in str(caught.value)
    assert str(invalid) in caplog.text
    assert not opened
    assert {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file() and not path.is_symlink()} == before


def test_native_inspection_failure_preserves_type_and_recovers(
    index_path: Path, connection_mode: tuple[bool, bool],
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    original = Path.is_symlink
    failure = PermissionError(errno.EACCES, f"原生检查失败 {index_path}", str(index_path))
    before = index_path.read_bytes()

    def is_symlink(path):
        if path == index_path:
            raise failure
        return original(path)

    read_only, frozen = connection_mode
    with monkeypatch.context() as patch:
        patch.setattr(Path, "is_symlink", is_symlink)
        with pytest.raises(PermissionError) as caught:
            _RolloutSQLiteConnection.open(index_path, read_only=read_only, frozen_source=frozen)
    assert caught.value.__cause__ is failure
    assert caught.value.errno == errno.EACCES and caught.value.filename is None
    assert "artifact=rollout_index stage=path_validation" in str(caught.value)
    assert str(index_path) not in str(caught.value) and str(index_path) in caplog.text
    assert index_path.read_bytes() == before
    with _RolloutSQLiteConnection.open(index_path, read_only=read_only, frozen_source=frozen) as connection:
        assert connection.execute("SELECT value FROM marker").fetchone() == ("原始正文 Ω",)
        assert connection.execute("PRAGMA query_only").fetchone() == (int(read_only),)
        assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert connection.execute("PRAGMA busy_timeout").fetchone() == (30000,)
