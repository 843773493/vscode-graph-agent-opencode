"""配置快照错误不携带原生来源地址，内部异常链仍保留系统诊断。"""

from __future__ import annotations

import errno
from dataclasses import replace
from pathlib import Path

import pytest

from app.core.config_sources import (
    parse_stable_config_file,
    read_stable_config_file,
    verify_stable_config_file,
)


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "workspace.jsonc"
    path.write_text('{"enabled":true}', encoding="utf-8")
    return path


@pytest.mark.parametrize("operation", ["resolve", "stat_before", "read", "stat_after"])
def test_os_failure_preserves_errno_stage_and_cause_without_native_filename(
    config_file: Path, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    original_stat, original_read, original_resolve = Path.stat, Path.read_bytes, Path.resolve
    stat_calls = 0
    resolving = False
    original_error = PermissionError(errno.EACCES, "Permission denied", str(config_file))

    def resolve(path: Path, *args, **kwargs) -> Path:
        nonlocal resolving
        if path == config_file and operation == "resolve":
            raise original_error
        # Path.resolve 自身可额外 stat；故障只注入稳定快照的两次状态读取。
        resolving = True
        try:
            return original_resolve(path, *args, **kwargs)
        finally:
            resolving = False

    def stat(path: Path, *args, **kwargs):
        nonlocal stat_calls
        if path == config_file and not resolving:
            stat_calls += 1
            if (operation, stat_calls) in {("stat_before", 1), ("stat_after", 2)}:
                raise original_error
        return original_stat(path, *args, **kwargs)

    def read(path: Path) -> bytes:
        if path == config_file and operation == "read":
            raise original_error
        return original_read(path)

    with monkeypatch.context() as failure:
        failure.setattr(Path, "resolve", resolve)
        failure.setattr(Path, "stat", stat)
        failure.setattr(Path, "read_bytes", read)
        with pytest.raises(PermissionError) as raised:
            read_stable_config_file(config_file)
    assert raised.value.__cause__ is original_error
    assert raised.value.errno == errno.EACCES
    assert raised.value.filename is None
    assert f"operation={operation}" in str(raised.value)
    assert "Permission denied" in str(raised.value)
    assert str(config_file.parent) not in str(raised.value)


def test_unstable_read_exhausts_bounded_attempts_without_publishing_snapshot(
    config_file: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_read = Path.read_bytes
    reads = 0

    def changing_read(path: Path) -> bytes:
        nonlocal reads
        content = original_read(path)
        if path == config_file:
            reads += 1
            path.write_bytes(content + b" ")
        return content

    monkeypatch.setattr(Path, "read_bytes", changing_read)
    with pytest.raises(RuntimeError, match="读取期间持续变化") as raised:
        read_stable_config_file(config_file, attempts=3)
    assert reads == 3
    assert str(config_file.parent) not in str(raised.value)


def test_changed_source_and_missing_payload_fail_without_native_path(config_file: Path) -> None:
    snapshot = read_stable_config_file(config_file)
    config_file.write_text('{"enabled":false}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="解析后发生变化") as changed:
        verify_stable_config_file(snapshot)
    with pytest.raises(RuntimeError, match="缺少文件内容") as missing:
        parse_stable_config_file(replace(snapshot, raw_bytes=None))
    assert str(config_file.parent) not in str(changed.value)
    assert str(config_file.parent) not in str(missing.value)
