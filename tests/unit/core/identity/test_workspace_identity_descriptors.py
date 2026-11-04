"""身份文件创建失败时，原始描述符仍须由创建者收尾。"""

from __future__ import annotations

import asyncio
import errno
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Never

import pytest

from app.core import workspace_identity as identity


@pytest.fixture(params=["io", "cancel"])
def identity_create_failure(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    cleanup_fails: bool,
) -> Iterator[tuple[BaseException, OSError, list[int], list[int]]]:
    path = identity.workspace_identity_path(tmp_path)
    raw = (
        PermissionError(errno.EACCES, "身份流创建失败", str(path))
        if request.param == "io" else asyncio.CancelledError("身份创建取消")
    )
    cleanup_error = PermissionError(errno.EIO, "身份描述符关闭失败", str(path))
    opened: list[int] = []
    closed: list[int] = []
    real_close = os.close

    def fdopen(descriptor: int, mode: str, *, encoding: str) -> Never:
        assert mode == "w" and encoding == "utf-8"
        os.fstat(descriptor)  # 检查经真实O_EXCL创建、尚未交付stream的描述符。
        opened.append(descriptor)
        raise raw

    def close(descriptor: int) -> None:
        real_close(descriptor)
        if descriptor in opened:
            closed.append(descriptor)
            if cleanup_fails:
                raise cleanup_error

    monkeypatch.setattr(identity.os, "fdopen", fdopen)
    monkeypatch.setattr(identity.os, "close", close)
    try:
        yield raw, cleanup_error, opened, closed
    finally:
        # 失败实现也不能让测试自身泄漏真实描述符。
        for descriptor in opened:
            try:
                os.fstat(descriptor)
            except OSError as error:
                if error.errno != errno.EBADF:
                    raise
            else:
                real_close(descriptor)


@pytest.mark.parametrize("cleanup_fails", [False, True], ids=["close-ok", "close-failed"])
def test_failed_identity_stream_closes_owned_descriptor_and_preserves_causes(
    tmp_path: Path, identity_create_failure: tuple[BaseException, OSError, list[int], list[int]],
    cleanup_fails: bool,
) -> None:
    raw, cleanup_error, opened, closed = identity_create_failure
    expected_type = BaseExceptionGroup if cleanup_fails else type(raw)
    with pytest.raises(expected_type) as caught:
        identity.load_or_create_workspace_id(tmp_path)
    path = identity.workspace_identity_path(tmp_path)
    assert len(opened) == 1 and closed == opened
    with pytest.raises(OSError) as invalid_descriptor:
        os.fstat(opened[0])
    assert invalid_descriptor.value.errno == errno.EBADF

    primary = caught.value
    if cleanup_fails:
        assert isinstance(primary, BaseExceptionGroup)
        assert len(primary.exceptions) == 2
        failure, cleanup = primary.exceptions
        assert primary.__cause__ is failure
        assert isinstance(cleanup, PermissionError)
        assert cleanup.__cause__ is cleanup_error
        assert cleanup.errno == errno.EIO
        assert cleanup.filename is None and "stage=close_descriptor" in str(cleanup)
        assert str(path) not in str(cleanup) and "身份描述符关闭失败" not in str(cleanup)
        primary = failure
    if isinstance(raw, asyncio.CancelledError):
        assert primary is raw
    else:
        assert isinstance(primary, PermissionError)
        assert primary.__cause__ is raw and primary.errno == errno.EACCES
        assert primary.filename is None and "stage=flush" in str(primary)
    assert str(path) not in str(caught.value) and "身份流创建失败" not in str(caught.value)

    # 失败的候选文件不能公开成身份，也不能留下半写入的最终文件。
    assert not path.exists()
    assert list(path.parent.iterdir()) == []
