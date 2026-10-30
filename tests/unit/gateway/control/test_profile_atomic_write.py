"""profile原子写入保留未移交描述符的归属和原失败。"""

from __future__ import annotations

import errno
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.gateway.control import user_profile as profile_module
from app.gateway.control.user_profile import UserProfileStore
from tests.unit.gateway.test_user_profile import (
    user_profile_store as user_profile_store,  # noqa: PLC0414
)


class WriteInterrupted(BaseException):
    """不终止pytest的同步写入中断。"""


def _is_open(descriptor: int) -> bool:
    try:
        os.fstat(descriptor)
    except OSError as error:
        assert error.errno == errno.EBADF
        return False
    return True


@pytest.fixture
def allocations(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[int, Path]]]:
    original_mkstemp, original_close = profile_module.tempfile.mkstemp, os.close
    allocated: list[tuple[int, Path]] = []

    def mkstemp(*, prefix: str, dir: Path, text: bool) -> tuple[int, str]:
        descriptor, name = original_mkstemp(prefix=prefix, dir=dir, text=text)
        allocated.append((descriptor, Path(name)))
        return descriptor, name

    monkeypatch.setattr(profile_module.tempfile, "mkstemp", mkstemp)
    yield allocated
    # 失败反例也关闭本次仍存活的真实fd，不将泄漏留给后续用例。
    for descriptor, _ in allocated:
        if _is_open(descriptor):
            original_close(descriptor)


@pytest.mark.parametrize("failure_kind", ["io", "value", "interrupt"])
@pytest.mark.parametrize("cleanup", ["none", "close", "unlink", "both"])
def test_fdopen_failure_closes_unclaimed_descriptor_and_preserves_all_errors(
    user_profile_store: UserProfileStore, allocations: list[tuple[int, Path]],
    monkeypatch: pytest.MonkeyPatch, failure_kind: str, cleanup: str,
) -> None:
    path = user_profile_store.user_path("user-a") / "profile.jsonc"
    original = path.read_bytes()
    primary: BaseException = {
        "io": PermissionError(errno.EACCES, "原生stream打开失败", str(path)),
        "value": ValueError(f"stream构造拒绝: {path}"),
        "interrupt": WriteInterrupted(f"stream构造中断: {path}"),
    }[failure_kind]
    close_error = OSError(errno.EIO, "未移交描述符关闭失败", str(path))
    unlink_error = PermissionError(errno.EACCES, "临时路径清理失败", str(path))
    real_close, real_unlink = os.close, Path.unlink
    close_calls: list[int] = []

    def fail_open(descriptor: int, mode: str, *, encoding: str) -> None:
        assert allocations[-1][0] == descriptor and mode == "w" and encoding == "utf-8"
        raise primary

    def close(descriptor: int) -> None:
        if allocations and descriptor == allocations[-1][0]:
            close_calls.append(descriptor)
            if cleanup in ("close", "both"):
                raise close_error
        real_close(descriptor)

    def unlink(target: Path, *, missing_ok: bool = False) -> None:
        if allocations and target == allocations[-1][1] and cleanup in ("unlink", "both"):
            raise unlink_error
        real_unlink(target, missing_ok=missing_ok)

    with monkeypatch.context() as patch:
        patch.setattr(profile_module.os, "fdopen", fail_open)
        patch.setattr(profile_module.os, "close", close)
        patch.setattr(Path, "unlink", unlink)
        with pytest.raises(type(primary)) as caught:
            user_profile_store._write_profile(user_id="user-a", payload={"config_version": 1, "display_name": "拒绝发布"})
        assert caught.value is primary
        descriptor, temporary = allocations[-1]
        assert close_calls == [descriptor]
        assert _is_open(descriptor) == (cleanup in ("close", "both"))
        assert temporary.exists() == (cleanup in ("unlink", "both"))
        notes = "\n".join(getattr(primary, "__notes__", ()))
        assert (str(close_error) in notes) == (cleanup in ("close", "both"))
        assert (str(unlink_error) in notes) == (cleanup in ("unlink", "both"))
        assert path.read_bytes() == original
    temporary.unlink(missing_ok=True)
    if _is_open(descriptor):
        real_close(descriptor)
    user_profile_store._write_profile(user_id="user-a", payload={"config_version": 1, "display_name": "原入口重试"})
    assert user_profile_store.read_profile(user_id="user-a")["display_name"] == "原入口重试"
    assert not list(path.parent.glob(f".{path.name}.*"))


@pytest.mark.parametrize("stage", ["private", "fsync", "published"])
def test_stream_owns_descriptor_after_fdopen_without_second_raw_close(
    user_profile_store: UserProfileStore, allocations: list[tuple[int, Path]],
    monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    path = user_profile_store.user_path("user-a") / "profile.jsonc"
    original = path.read_bytes()
    failure = PermissionError(errno.EACCES, "stream内原生写入失败", str(path))
    real_close, real_private, real_fsync = os.close, profile_module.restrict_private_path_access, os.fsync
    raw_close_calls: list[int] = []

    def close(descriptor: int) -> None:
        if allocations and descriptor == allocations[-1][0]:
            raw_close_calls.append(descriptor)
        real_close(descriptor)

    def private(target: Path, *, descriptor: int) -> None:
        if stage == "private":
            raise failure
        real_private(target, descriptor=descriptor)

    def fsync(descriptor: int) -> None:
        if stage == "fsync":
            raise failure
        real_fsync(descriptor)

    monkeypatch.setattr(profile_module.os, "close", close)
    monkeypatch.setattr(profile_module, "restrict_private_path_access", private)
    monkeypatch.setattr(profile_module.os, "fsync", fsync)
    if stage == "published":
        user_profile_store._write_profile(user_id="user-a", payload={"config_version": 1, "display_name": "已发布"})
        assert user_profile_store.read_profile(user_id="user-a")["display_name"] == "已发布"
    else:
        with pytest.raises(PermissionError) as caught:
            user_profile_store._write_profile(user_id="user-a", payload={"config_version": 1, "display_name": "未发布"})
        assert caught.value is failure
        assert path.read_bytes() == original
    descriptor, temporary = allocations[-1]
    assert not _is_open(descriptor) and not temporary.exists()
    assert not raw_close_calls
