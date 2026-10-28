"""稳定Gateway身份在原文件owner处拒绝，不复制位置或切换身份。"""

from __future__ import annotations

import asyncio
import errno
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Self, TextIO

import pytest

import app.gateway.credentials as credentials_module
from app.gateway.credentials import load_or_create_gateway_id
from app.gateway.proxy_upstream import load_proxy_gateway_id

GATEWAY_ID = "gateway_" + "a" * 32


@pytest.fixture
def identity_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("BOXTEAM_GATEWAY_ROOT", str(tmp_path))
    return tmp_path / "identity.json"


@pytest.mark.parametrize("damage", ["path-value", "unc-value", "drive-value", "empty", "missing", "shape", "json", "utf8"])
def test_invalid_identity_preserves_file_and_retry_uses_repaired_value(
    identity_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, damage: str,
) -> None:
    value: object = {"gateway_id": str(identity_path)}
    match damage:
        case "unc-value":
            value = {"gateway_id": r"\\private-host\share\private-identity-marker"}
        case "drive-value":
            value = {"gateway_id": r"C:\private\private-identity-marker"}
        case "empty":
            value = {"gateway_id": ""}
        case "missing":
            value = {"other": str(identity_path)}
        case "shape":
            value = [str(identity_path)]
    damaged = json.dumps(value).encode()
    if damage == "json":
        damaged = b'{"gateway_id": ' + json.dumps(str(identity_path)).encode() + b','
    elif damage == "utf8":
        damaged = str(identity_path).encode() + b"\xff"
    identity_path.write_bytes(damaged)

    def reject_generation(_: int) -> str:
        raise AssertionError("损坏身份不得生成替代ID")

    with monkeypatch.context() as patch:
        patch.setattr(credentials_module.secrets, "token_hex", reject_generation)
        with pytest.raises(ValueError) as caught:
            load_proxy_gateway_id()
    stage = "decode" if damage in {"json", "utf8"} else "validate"
    assert "artifact=gateway_identity" in str(caught.value)
    assert f"stage={stage}" in str(caught.value)
    assert str(identity_path) not in str(caught.value)
    assert "private-identity-marker" not in str(caught.value)
    assert str(identity_path) in caplog.text
    assert caught.value.__cause__ is not None
    assert type(caught.value) is type(caught.value.__cause__)
    assert identity_path.read_bytes() == damaged
    identity_path.write_text(json.dumps({"gateway_id": GATEWAY_ID}), encoding="utf-8")
    assert load_proxy_gateway_id() == GATEWAY_ID


@pytest.mark.parametrize("stage", ["stat", "read", "prepare", "create", "open_stream", "write", "close"])
def test_native_identity_failure_keeps_error_and_original_target(
    identity_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, stage: str,
) -> None:
    original = OSError(errno.EIO, f"native-identity-marker {identity_path}", str(identity_path))
    if stage in {"stat", "read"}:
        identity_path.write_text(json.dumps({"gateway_id": GATEWAY_ID}), encoding="utf-8")
    original_bytes = identity_path.read_bytes() if identity_path.exists() else None
    original_open = Path.open
    native_open, native_fdopen = os.open, os.fdopen
    handles: list[tuple[int, int, int]] = []

    def fail_stat(path: Path) -> bool:
        assert path == identity_path
        raise original

    def fail_read(
        path: Path, mode: str = "r", buffering: int = -1, encoding: str | None = None,
        errors: str | None = None, newline: str | None = None,
    ) -> IO[str] | IO[bytes]:
        if path == identity_path:
            raise original
        return original_open(path, mode, buffering, encoding, errors, newline)

    def fail_prepare(path: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False) -> None:
        assert path == identity_path.parent
        raise original

    def open_identity(path: Path, flags: int, mode: int = 0o777) -> int:
        assert path == identity_path and flags & os.O_EXCL
        if stage == "create":
            raise original
        descriptor = native_open(path, flags, mode)
        stat = os.fstat(descriptor)
        handles.append((descriptor, stat.st_dev, stat.st_ino))
        return descriptor

    class Stream:
        def __init__(self, raw: IO[str]) -> None:
            self.raw = raw

        def write(self, value: str) -> int:
            if stage == "write":
                self.raw.write(value[:1])
                raise original
            return self.raw.write(value)

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_: object) -> None:
            self.raw.close()
            if stage == "close":
                raise original

    def open_stream(descriptor: int, mode: str, *, encoding: str) -> Stream:
        if stage == "open_stream":
            raise original
        return Stream(native_fdopen(descriptor, mode, encoding=encoding))

    try:
        with monkeypatch.context() as patch:
            if stage == "stat":
                patch.setattr(Path, "exists", fail_stat)
            elif stage == "read":
                patch.setattr(Path, "open", fail_read)
            elif stage == "prepare":
                patch.setattr(Path, "mkdir", fail_prepare)
            else:
                patch.setattr(credentials_module.os, "open", open_identity)
                patch.setattr(credentials_module.os, "fdopen", open_stream)
            with pytest.raises(OSError) as caught:
                load_or_create_gateway_id(identity_path)
        for descriptor, _, _ in handles:
            with pytest.raises(OSError) as closed:
                os.fstat(descriptor)
            assert closed.value.errno == errno.EBADF
        assert caught.value.__cause__ is original
        assert caught.value.errno == errno.EIO and caught.value.filename is None
        assert "artifact=gateway_identity" in str(caught.value)
        assert f"stage={stage}" in str(caught.value)
        assert str(identity_path) not in str(caught.value)
        assert "native-identity-marker" not in str(caught.value)
        assert str(identity_path) in caplog.text
        if original_bytes is not None:
            assert identity_path.read_bytes() == original_bytes
        if stage in {"open_stream", "write", "close"}:
            # 原直接排他创建保留失败原件，不自动删除或生成另一身份。
            assert identity_path.is_file()
        elif original_bytes is None:
            assert not identity_path.exists()
    finally:
        # 红测只清理身份仍匹配的原fd，不能关闭已被其它owner复用的句柄。
        for descriptor, device, inode in handles:
            try:
                stat = os.fstat(descriptor)
            except OSError:
                continue
            if (stat.st_dev, stat.st_ino) == (device, inode):
                os.close(descriptor)


def test_creation_collision_never_replaces_winning_identity(identity_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = FileExistsError(errno.EEXIST, str(identity_path), str(identity_path))

    def publish_winner(path: Path, flags: int, mode: int = 0o777) -> int:
        assert path == identity_path and flags & os.O_EXCL and mode == 0o600
        identity_path.write_text(json.dumps({"gateway_id": GATEWAY_ID}), encoding="utf-8")
        raise original

    with monkeypatch.context() as patch:
        patch.setattr(credentials_module.os, "open", publish_winner)
        with pytest.raises(FileExistsError) as caught:
            load_or_create_gateway_id(identity_path)
    assert caught.value.__cause__ is original
    assert str(identity_path) not in str(caught.value)
    assert load_proxy_gateway_id() == GATEWAY_ID


def test_existing_identity_close_failure_keeps_cause(identity_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    identity_path.write_text(json.dumps({"gateway_id": GATEWAY_ID}), encoding="utf-8")
    original = OSError(errno.EIO, str(identity_path), str(identity_path))
    original_open = Path.open

    @contextmanager
    def close_failure(path: Path, mode: str = "r", buffering: int = -1, encoding: str | None = None,
                      errors: str | None = None, newline: str | None = None) -> Iterator[IO[str]]:
        with original_open(path, mode, buffering, encoding, errors, newline) as stream:
            yield stream
        raise original

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", close_failure)
        with pytest.raises(OSError) as caught:
            load_or_create_gateway_id(identity_path)
    assert caught.value.__cause__ is original
    assert str(identity_path) not in str(caught.value)
    assert load_proxy_gateway_id() == GATEWAY_ID


def test_successful_identity_creation_is_stable_and_exclusive(identity_path: Path) -> None:
    first = load_or_create_gateway_id(identity_path)
    original = identity_path.read_bytes()
    assert first.startswith("gateway_") and len(first) == 40
    assert load_proxy_gateway_id() == first
    assert identity_path.read_bytes() == original == (json.dumps({"gateway_id": first}) + "\n").encode()
    if os.name != "nt":
        assert identity_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("fail_close", [False, True])
def test_stream_handoff_failure_retains_primary_and_cleanup_causes(
    identity_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool, fail_close: bool,
) -> None:
    primary = asyncio.CancelledError("cancel identity creation") if cancel else OSError(errno.EIO, str(identity_path))
    cleanup = OSError(errno.EIO, str(identity_path))
    native_open, native_close = os.open, os.close
    handles: list[int] = []

    def create(path: Path, flags: int, mode: int) -> int:
        descriptor = native_open(path, flags, mode)
        handles.append(descriptor)
        return descriptor

    def open_stream(descriptor: int, mode: str, *, encoding: str) -> TextIO:
        raise primary

    def close(descriptor: int) -> None:
        native_close(descriptor)
        if fail_close:
            raise cleanup

    with monkeypatch.context() as patch:
        patch.setattr(credentials_module.os, "open", create)
        patch.setattr(credentials_module.os, "fdopen", open_stream)
        patch.setattr(credentials_module.os, "close", close)
        with pytest.raises(BaseException) as caught:
            load_or_create_gateway_id(identity_path)
    error = caught.value
    if fail_close:
        assert isinstance(error, BaseExceptionGroup)
        assert len(error.exceptions) == 2
        assert error.exceptions[1].__cause__ is cleanup
        assert str(identity_path) not in str(error.exceptions[1])
        error = error.exceptions[0]
    if cancel:
        assert error is primary
    else:
        assert error.__cause__ is primary
        assert "stage=open_stream" in str(error)
        assert str(identity_path) not in str(error)
    assert len(handles) == 1
    with pytest.raises(OSError) as closed:
        os.fstat(handles[0])
    assert closed.value.errno == errno.EBADF
    assert identity_path.read_bytes() == b""
