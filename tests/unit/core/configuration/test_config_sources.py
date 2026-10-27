"""配置快照错误不携带原生来源地址，内部异常链仍保留系统诊断。"""

from __future__ import annotations

import errno
import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.core.config_sources import (
    ConfigSource,
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
@pytest.mark.parametrize("number", [errno.EACCES, errno.EIO, errno.ELOOP])
@pytest.mark.parametrize("fields", ["filename", "filename2", "errno_only", "winerror"])
def test_os_failure_preserves_errno_stage_and_cause_without_native_filename(
    config_file: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    operation: str, number: int, fields: str,
) -> None:
    original_stat, original_read, original_resolve = Path.stat, Path.read_bytes, Path.resolve
    stat_calls = 0
    resolving = False
    original_error = OSError(number, f"private-config-message: {config_file}")
    if fields == "filename":
        original_error.filename = str(config_file)
    elif fields == "filename2":
        original_error.filename2 = str(config_file)
    elif fields == "winerror":
        original_error.winerror = 5

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
        with pytest.raises(type(original_error)) as raised:
            read_stable_config_file(config_file)
    assert raised.value.__cause__ is original_error
    assert raised.value.errno == number
    assert raised.value.filename is None and raised.value.filename2 is None
    assert f"operation={operation}" in str(raised.value)
    assert str(config_file.parent) not in str(raised.value)
    assert "private-config-message" not in str(raised.value)
    assert f"error={type(original_error).__name__}" in str(raised.value)
    assert f"errno={number}" in str(raised.value)
    if fields == "winerror":
        assert "winerror=5" in str(raised.value)
    assert any(record.exc_info and record.exc_info[1] is original_error for record in caplog.records)
    assert str(config_file) in caplog.text and "private-config-message" in caplog.text


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


@pytest.mark.parametrize(("suffix", "parser_error"), [
    ('@}', "ValueError"),
    ('"secret-token" "another"}', "UnexpectedToken"),
    ('', "UnexpectedToken"),
    ('"\\q"}', "JSONDecodeError"),
])
def test_jsonc_parse_error_reports_position_without_document_or_token(
    config_file: Path, suffix: str, parser_error: str,
) -> None:
    content = '{\n"private": "secret-marker",\n"native": ' + json.dumps(str(config_file))
    config_file.write_text(content + ',\n"broken": ' + suffix, encoding="utf-8")

    with pytest.raises(ValueError, match="配置 JSONC 解析失败") as failure:
        parse_stable_config_file(read_stable_config_file(config_file))
    assert type(failure.value.__cause__).__name__ == parser_error
    message = str(failure.value)
    assert f"parser_error={parser_error}" in message
    assert "line=" in message and "column=" in message
    assert "secret-marker" not in message
    assert str(config_file.parent) not in message
    assert '"another"' not in message


def test_invalid_utf8_reports_byte_offset_without_document(config_file: Path) -> None:
    payload = b'{"private":"secret-marker","broken":"\xff"}'
    config_file.write_bytes(payload)
    with pytest.raises(ValueError, match="配置 UTF-8 解码失败") as failure:
        parse_stable_config_file(read_stable_config_file(config_file))
    cause = failure.value.__cause__
    assert isinstance(cause, UnicodeDecodeError)
    assert cause.object == payload
    byte_start = payload.index(b"\xff")
    assert f"byte_start={byte_start}" in str(failure.value)
    assert "secret-marker" not in str(failure.value)


def test_jsonc_comments_trailing_comma_and_unicode_keep_same_values(config_file: Path) -> None:
    config_file.write_text('{\n// 注释\n"值": [1, true,],\n}', encoding="utf-8")
    assert parse_stable_config_file(read_stable_config_file(config_file)) == {"值": [1, True]}


@pytest.mark.parametrize("uri", [
    "/private-config/workspace.jsonc", r"C:\private-config\workspace.jsonc",
    r"\\server\private-config\workspace.jsonc", "file:///private-config/workspace.jsonc",
    "workspace.jsonc", "", "boxteam://workspace/ws_a/resources/files/root/workspace.jsonc",
])
def test_source_constructor_and_replace_reject_native_or_wrong_kind_uri(
    uri: str, caplog: pytest.LogCaptureFixture,
) -> None:
    source = ConfigSource(uri=None, layer="runtime_override", precedence=4, loaded=True)
    with pytest.raises(ValueError) as created:
        ConfigSource(uri=uri, layer="inline", precedence=0, loaded=True)
    with pytest.raises(ValueError) as replaced:
        replace(source, uri=uri)
    assert str(created.value) == str(replaced.value)
    if uri:
        assert uri not in str(created.value)
    assert source.uri is None
    assert [record.args for record in caplog.records] == [(uri,), (uri,)]


@pytest.mark.parametrize("uri", [
    None, "boxteam://inline/boxteam_v1/resources/config/workspace_inline",
    "boxteam://gateway_remote/workspace/ws_a/resources/config/workspace_override",
])
def test_source_keeps_logical_uri_or_explicit_null_without_resolving(uri: str | None) -> None:
    source = ConfigSource(uri=uri, layer="inline", precedence=0, loaded=True)
    assert source.uri == uri and replace(source, loaded=False).uri == uri
