"""联邦凭据发布只收尾本次文件与描述符，失败不改变原授权。"""

from __future__ import annotations

import asyncio
import errno
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Self, TextIO, cast

import pytest

import app.gateway.credentials as credentials_module
from app.gateway.credentials import FederationCredential, FederationCredentialStore


@dataclass
class PublishCase:
    path: Path
    store: FederationCredentialStore
    credential: FederationCredential
    original: bytes
    handles: list[tuple[int, int, int]] = field(default_factory=list)


@dataclass
class WriteFault:
    error: BaseException
    temporaries: list[Path] = field(default_factory=list)
    close_error: BaseException | None = None


@pytest.fixture
def publish_case(tmp_path: Path) -> Iterator[PublishCase]:
    path = tmp_path / "credentials" / "federation.json"
    store = FederationCredentialStore(storage_path=path)
    credential = store.issue(connection_id="rgw_publish", peer_gateway_id="gateway_peer")
    case = PublishCase(path, store, credential, path.read_bytes())
    try:
        yield case
    finally:
        # 红测也不能遗留原实现泄漏的fd，只关闭身份仍匹配的本次句柄。
        for descriptor, device, inode in case.handles:
            try:
                stat = os.fstat(descriptor)
            except OSError:
                continue
            if (stat.st_dev, stat.st_ino) == (device, inode):
                os.close(descriptor)


@pytest.fixture
def inject_write_fault(publish_case: PublishCase) -> Callable[[pytest.MonkeyPatch, str, BaseException | None], WriteFault]:
    case = publish_case
    mkdir, mkstemp = Path.mkdir, credentials_module.tempfile.mkstemp
    fdopen, fsync, publish = os.fdopen, os.fsync, os.replace
    restrict = credentials_module.restrict_private_path_access

    def install(patch: pytest.MonkeyPatch, stage: str, failure: BaseException | None = None) -> WriteFault:
        fault = WriteFault(failure if failure is not None else OSError(errno.EIO, f"private-write-marker {case.path}", str(case.path)))

        def prepare(path: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False) -> None:
            if stage == "prepare" and path == case.path.parent:
                raise fault.error
            mkdir(path, mode, parents, exist_ok)

        def create_temp(*, prefix: str, dir: Path) -> tuple[int, str]:
            if stage == "create_temp":
                raise fault.error
            descriptor, name = mkstemp(prefix=prefix, dir=dir)
            stat = os.fstat(descriptor)
            case.handles.append((descriptor, stat.st_dev, stat.st_ino))
            fault.temporaries.append(Path(name))
            return descriptor, name

        class Stream:
            def __init__(self, raw: TextIO) -> None:
                self.raw = raw

            def __enter__(self) -> Self:
                return self

            def __exit__(self, *_: object) -> None:
                self.raw.close()
                if fault.close_error is not None:
                    raise fault.close_error
                if stage == "close":
                    raise fault.error

            def fileno(self) -> int:
                return self.raw.fileno()

            def write(self, value: str) -> int:
                if stage == "write":
                    self.raw.write(value[:1])
                    raise fault.error
                return self.raw.write(value)

            def flush(self) -> None:
                if stage == "flush":
                    raise fault.error
                self.raw.flush()

        def open_stream(descriptor: int, mode: str, *, encoding: str) -> TextIO:
            if stage == "open_stream":
                raise fault.error
            return cast(TextIO, Stream(cast(TextIO, fdopen(descriptor, mode, encoding=encoding))))

        def permissions(path: Path, *, descriptor: int | None = None) -> None:
            assert path in fault.temporaries and descriptor is not None
            if stage == "permissions":
                raise fault.error
            restrict(path, descriptor=descriptor)

        def sync(descriptor: int) -> None:
            if stage == "fsync":
                raise fault.error
            fsync(descriptor)

        def replace_file(source: Path, target: Path) -> None:
            assert source in fault.temporaries and target == case.path
            if stage == "publish":
                raise fault.error
            publish(source, target)

        patch.setattr(Path, "mkdir", prepare)
        patch.setattr(credentials_module.tempfile, "mkstemp", create_temp)
        patch.setattr(credentials_module.os, "fdopen", open_stream)
        patch.setattr(credentials_module, "restrict_private_path_access", permissions)
        patch.setattr(credentials_module.os, "fsync", sync)
        patch.setattr(credentials_module.os, "replace", replace_file)
        return fault

    return install


def assert_handles_closed(case: PublishCase) -> None:
    for descriptor, _, _ in case.handles:
        with pytest.raises(OSError) as caught:
            os.fstat(descriptor)
        assert caught.value.errno == errno.EBADF


@pytest.mark.parametrize("method", ["issue", "put", "remove"])
@pytest.mark.parametrize("stage", ["prepare", "create_temp", "open_stream", "permissions", "write", "flush", "fsync", "close", "publish"])
def test_publish_failure_preserves_original_authorization_and_can_retry(
    publish_case: PublishCase,
    inject_write_fault: Callable[[pytest.MonkeyPatch, str, BaseException | None], WriteFault],
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, method: str, stage: str,
) -> None:
    case = publish_case
    replacement = replace(case.credential, token="replacement-secret")

    def apply() -> None:
        match method:
            case "issue":
                case.store.issue(connection_id="rgw_new", peer_gateway_id="gateway_new")
            case "put":
                case.store.put(replacement)
            case "remove":
                case.store.remove(case.credential.connection_id)

    with monkeypatch.context() as patch:
        fault = inject_write_fault(patch, stage, None)
        with pytest.raises(OSError) as caught:
            apply()
    assert_handles_closed(case)
    assert caught.value.__cause__ is fault.error
    assert caught.value.errno == errno.EIO
    assert caught.value.filename is None
    assert "artifact=federation_credentials" in str(caught.value)
    assert f"stage={stage}" in str(caught.value)
    assert str(case.path) not in str(caught.value)
    assert "private-write-marker" not in str(caught.value)
    assert str(case.path) in caplog.text
    assert case.credential.token not in caplog.text
    assert "replacement-secret" not in caplog.text
    assert case.path.read_bytes() == case.original
    assert all(not path.exists() for path in fault.temporaries)
    assert case.store.verify(case.credential.token) == case.credential
    apply()
    if method == "put":
        assert case.store.get(case.credential.connection_id) == replacement
    elif method == "remove":
        assert case.store.list_all() == ()
    else:
        assert len(case.store.list_all()) == 2


@pytest.mark.parametrize("close_descriptor_fails", [False, True])
def test_cleanup_keeps_all_original_failures_and_only_owns_this_attempt(
    publish_case: PublishCase,
    inject_write_fault: Callable[[pytest.MonkeyPatch, str, BaseException | None], WriteFault],
    monkeypatch: pytest.MonkeyPatch, close_descriptor_fails: bool,
) -> None:
    case = publish_case
    cleanup_error = PermissionError(errno.EACCES, str(case.path), str(case.path))
    descriptor_error = OSError(errno.EIO, f"descriptor-private-marker {case.path}")
    original_unlink, original_close = Path.unlink, os.close

    with monkeypatch.context() as patch:
        fault = inject_write_fault(patch, "open_stream" if close_descriptor_fails else "publish", None)

        def reject_cleanup(path: Path, missing_ok: bool = False) -> None:
            if path in fault.temporaries:
                raise cleanup_error
            original_unlink(path, missing_ok=missing_ok)

        def reject_close(descriptor: int) -> None:
            original_close(descriptor)
            if any(descriptor == owned for owned, _, _ in case.handles):
                raise descriptor_error

        patch.setattr(Path, "unlink", reject_cleanup)
        if close_descriptor_fails:
            patch.setattr(credentials_module.os, "close", reject_close)
        with pytest.raises(ExceptionGroup) as caught:
            case.store.put(replace(case.credential, token="new-secret"))
    errors = caught.value.exceptions
    expected = [fault.error, descriptor_error, cleanup_error] if close_descriptor_fails else [fault.error, cleanup_error]
    assert [error.__cause__ for error in errors] == expected
    assert str(case.path) not in str(caught.value)
    assert all(str(case.path) not in str(error) for error in errors)
    assert_handles_closed(case)
    assert case.path.read_bytes() == case.original
    assert len(fault.temporaries) == 1
    orphan = fault.temporaries[0]
    assert orphan.is_file()
    orphan_bytes = orphan.read_bytes()
    case.store.put(replace(case.credential, token="retried-secret"))
    assert case.store.get(case.credential.connection_id).token == "retried-secret"
    assert orphan.read_bytes() == orphan_bytes


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_cancellation_keeps_identity_and_reports_cleanup_failure(
    publish_case: PublishCase,
    inject_write_fault: Callable[[pytest.MonkeyPatch, str, BaseException | None], WriteFault],
    monkeypatch: pytest.MonkeyPatch, cleanup_fails: bool,
) -> None:
    case = publish_case
    cancellation = asyncio.CancelledError("cancel-private-operation")
    cleanup_error = OSError(errno.EIO, str(case.path), str(case.path))
    original_unlink = Path.unlink
    with monkeypatch.context() as patch:
        fault = inject_write_fault(patch, "permissions", cancellation)

        def reject_cleanup(path: Path, missing_ok: bool = False) -> None:
            if path in fault.temporaries:
                raise cleanup_error
            original_unlink(path, missing_ok=missing_ok)

        if cleanup_fails:
            patch.setattr(Path, "unlink", reject_cleanup)
        with pytest.raises(BaseExceptionGroup if cleanup_fails else asyncio.CancelledError) as caught:
            case.store.remove(case.credential.connection_id)
    if cleanup_fails:
        assert isinstance(caught.value, BaseExceptionGroup)
        assert caught.value.exceptions[0] is cancellation
        assert caught.value.exceptions[1].__cause__ is cleanup_error
    else:
        assert caught.value is cancellation
        assert not fault.temporaries[0].exists()
    assert_handles_closed(case)
    assert case.path.read_bytes() == case.original
    assert case.store.verify(case.credential.token) == case.credential


def test_stream_close_failure_preserves_original_write_context(
    publish_case: PublishCase,
    inject_write_fault: Callable[[pytest.MonkeyPatch, str, BaseException | None], WriteFault],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = publish_case
    close_error = OSError(errno.EIO, str(case.path), str(case.path))
    with monkeypatch.context() as patch:
        fault = inject_write_fault(patch, "write", None)
        fault.close_error = close_error
        with pytest.raises(OSError) as caught:
            case.store.remove(case.credential.connection_id)
    assert caught.value.__cause__ is close_error
    assert close_error.__context__ is fault.error
    assert str(case.path) not in str(caught.value)
    assert_handles_closed(case)
    assert all(not path.exists() for path in fault.temporaries)
    assert case.path.read_bytes() == case.original


def test_encoding_failure_keeps_type_and_original_authorization(publish_case: PublishCase) -> None:
    case = publish_case
    with pytest.raises(UnicodeEncodeError) as caught:
        case.store.put(replace(case.credential, peer_gateway_id="gateway_\ud800"))
    assert isinstance(caught.value.__cause__, UnicodeEncodeError)
    assert "stage=write" in str(caught.value)
    assert "artifact=federation_credentials" in str(caught.value)
    assert str(case.path) not in str(caught.value)
    assert case.path.read_bytes() == case.original
    assert list(case.path.parent.iterdir()) == [case.path]
    assert case.store.verify(case.credential.token) == case.credential


def test_completed_publication_does_not_report_failure_from_an_absent_temporary(
    publish_case: PublishCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = publish_case
    original_unlink = Path.unlink

    def reject_absent_temporary(path: Path, missing_ok: bool = False) -> None:
        if path.parent == case.path.parent and path.name.startswith(f".{case.path.name}."):
            raise PermissionError(errno.EACCES, str(path), str(path))
        original_unlink(path, missing_ok=missing_ok)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", reject_absent_temporary)
        case.store.put(replace(case.credential, token="published-secret"))
    assert case.store.get(case.credential.connection_id).token == "published-secret"
    assert list(case.path.parent.iterdir()) == [case.path]
