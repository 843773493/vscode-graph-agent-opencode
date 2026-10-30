from __future__ import annotations

import errno
import logging
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

import app.gateway.auth as auth_module
import app.gateway.credentials as credentials_module
from app.core import file_permissions
from app.gateway.auth import get_gateway_local_token
from app.gateway.credentials import FederationCredentialStore
from app.gateway.federation.identity import load_or_create_signing_key, public_key_pem


@pytest.fixture
def windows_acl_calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(arguments: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if "/grant:r" in arguments:
            calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, stdout="", stderr="")

    monkeypatch.setattr(file_permissions, "_is_windows", lambda: True)
    monkeypatch.setattr(
        file_permissions,
        "_windows_acl_is_private",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(file_permissions.subprocess, "run", run)
    return calls


def test_gateway_local_token_sets_and_checks_private_acl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    windows_acl_calls: list[list[str]],
) -> None:
    gateway_root = tmp_path / "gateway"
    monkeypatch.setenv("BOXTEAM_GATEWAY_ROOT", str(gateway_root))

    first_token = get_gateway_local_token()
    second_token = get_gateway_local_token()

    credential_path = gateway_root / "credentials" / "local-token"
    assert first_token == second_token
    assert [arguments[1] for arguments in windows_acl_calls] == [
        str(credential_path),
        str(credential_path),
    ]


def test_federation_credential_store_secures_written_and_read_tokens(
    tmp_path: Path,
    windows_acl_calls: list[list[str]],
) -> None:
    credential_path = tmp_path / "gateway" / "federation.json"
    store = FederationCredentialStore(storage_path=credential_path)

    issued = store.issue(
        connection_id="rgw_test",
        peer_gateway_id="gateway_peer",
    )
    loaded = store.get("rgw_test")

    assert loaded.token == issued.token
    assert len(windows_acl_calls) == 2
    temporary_path = Path(windows_acl_calls[0][1])
    assert temporary_path.parent == credential_path.parent
    assert temporary_path != credential_path
    assert windows_acl_calls[1][1] == str(credential_path)


def test_federation_signing_key_secures_written_and_read_private_key(
    tmp_path: Path,
    windows_acl_calls: list[list[str]],
) -> None:
    first_key = load_or_create_signing_key(tmp_path)
    second_key = load_or_create_signing_key(tmp_path)

    assert public_key_pem(first_key) == public_key_pem(second_key)
    assert [arguments[1] for arguments in windows_acl_calls] == [
        str(tmp_path / "identity-signing-key.pem"),
        str(tmp_path / "identity-signing-key.pem"),
    ]


@pytest.mark.skipif(os.name != "nt", reason="需要真实 Windows DACL")
@pytest.mark.parametrize("is_directory", [False, True], ids=["file", "directory"])
def test_windows_private_path_acl_uses_real_dacl_and_fast_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    is_directory: bool,
) -> None:
    path = tmp_path / ("private-directory" if is_directory else "private-file")
    if is_directory:
        path.mkdir()
    else:
        path.write_text("private", encoding="utf-8")

    file_permissions.restrict_private_path_access(path, is_directory=is_directory)

    assert file_permissions._windows_acl_is_private(path, is_directory=is_directory)

    def reject_acl_rewrite(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("已符合预期 DACL 时不得再次启动 icacls")

    monkeypatch.setattr(file_permissions.subprocess, "run", reject_acl_rewrite)
    file_permissions.restrict_private_path_access(path, is_directory=is_directory)


class LocalTokenDiagnostics(logging.Handler):
    def __init__(self, destination: Path | None) -> None:
        super().__init__(logging.ERROR)
        self.destination = destination
        self.records: list[logging.LogRecord] = []
        self.errors: list[OSError] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        if self.destination is not None:
            try:
                self.destination.write_text(record.getMessage())
            except OSError as error:
                self.errors.append(error)
                raise


@pytest.fixture(params=[False, True], ids=["log-ok", "log-failed"])
def local_token_diagnostics(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[LocalTokenDiagnostics]:
    blocker = tmp_path / "blocked-diagnostic-directory"
    blocker.write_text("阻塞日志目录")
    handler = LocalTokenDiagnostics(blocker / "service.log" if request.param else None)
    credentials_module.logger.addHandler(handler)
    try:
        yield handler
    finally:
        credentials_module.logger.removeHandler(handler)
        handler.close()


@pytest.mark.parametrize("damage", ["empty", "utf8", "directory", "permissions", "permission-missing"])
def test_local_token_read_errors_preserve_credentials_and_explicit_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    local_token_diagnostics: LocalTokenDiagnostics, damage: str,
) -> None:
    if damage == "permissions" and os.name == "nt":
        pytest.skip("POSIX 权限故障；Windows DACL 由实机验收")
    root = tmp_path / "gateway"
    monkeypatch.setenv("BOXTEAM_GATEWAY_ROOT", str(root))
    token = get_gateway_local_token()
    path = root / "credentials/local-token"
    original = path.read_bytes()
    expected: type[Exception]
    stage: str
    if damage == "empty":
        path.write_bytes(b" \n")
        expected, stage = ValueError, "validate"
    elif damage == "utf8":
        path.write_bytes(token.encode() + b"\xff")
        expected, stage = UnicodeDecodeError, "decode"
    elif damage == "directory":
        path.unlink()
        path.mkdir()
        expected, stage = IsADirectoryError, "open"
    elif damage == "permissions":
        path.chmod(0o644)
        expected, stage = PermissionError, "permissions"
    else:
        expected, stage = FileNotFoundError, "permissions"
    damaged = path.read_bytes() if damage != "directory" else None
    missing = FileNotFoundError(errno.ENOENT, "权限核验失败", str(path))

    def fail_permission(*_args: object, **_kwargs: object) -> None:
        raise missing

    with monkeypatch.context() as patch:
        if damage == "permission-missing":
            patch.setattr(auth_module, "require_private_file_access", fail_permission)
        with pytest.raises(expected) as caught:
            get_gateway_local_token()
    error = caught.value
    cause = error.__cause__
    assert type(error) is expected and type(cause) is expected
    assert "artifact=local_token" in str(error) and f"stage={stage}" in str(error)
    assert str(path) not in str(error) and token not in str(error)
    if damage == "permission-missing":
        assert cause is missing
    assert len(local_token_diagnostics.records) == 1
    record = local_token_diagnostics.records[0]
    assert str(path) in record.getMessage() and token not in record.getMessage()
    assert (record.exc_info is not None) == isinstance(cause, (OSError, RuntimeError))
    if local_token_diagnostics.destination is not None:
        assert "diagnostic_sink_error=NotADirectoryError" in str(error)
        assert str(local_token_diagnostics.destination) not in str(error)
        assert cause is not None
        assert str(local_token_diagnostics.destination) in "\n".join(cause.__notes__)
        assert len(local_token_diagnostics.errors) == 1
    else:
        assert "diagnostic_sink_error=" not in str(error)
    for attribute in ("errno", "encoding", "object", "start", "end"):
        if hasattr(cause, attribute):
            assert getattr(error, attribute) == getattr(cause, attribute)
    if damage == "directory":
        assert path.is_dir() and not list(path.iterdir())
        path.rmdir()
    else:
        assert path.read_bytes() == damaged
    path.write_bytes(original)
    path.chmod(0o600)
    assert get_gateway_local_token() == token and path.read_bytes() == original


@pytest.mark.parametrize("failure", ["fdopen", "permissions", "cancel"])
def test_local_token_create_failure_releases_original_descriptor_without_rotating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    local_token_diagnostics: LocalTokenDiagnostics, failure: str,
) -> None:
    root = tmp_path / "gateway"
    monkeypatch.setenv("BOXTEAM_GATEWAY_ROOT", str(root))
    path = root / "credentials/local-token"
    descriptors: list[int] = []
    primary = KeyboardInterrupt() if failure == "cancel" else OSError(errno.EIO, "凭据创建失败", str(path))
    original_open = os.open

    def track_open(path: Path, flags: int, mode: int) -> int:
        descriptor = original_open(path, flags, mode)
        descriptors.append(descriptor)
        return descriptor

    def fail(*_args: object, **_kwargs: object) -> None:
        raise primary

    with monkeypatch.context() as patch:
        patch.setattr(auth_module.os, "open", track_open)
        if failure == "permissions":
            patch.setattr(auth_module, "restrict_private_path_access", fail)
        else:
            patch.setattr(auth_module.os, "fdopen", fail)
        with pytest.raises(type(primary)) as caught:
            get_gateway_local_token()
    if failure == "cancel":
        assert caught.value is primary and not local_token_diagnostics.records
    else:
        assert isinstance(caught.value, OSError)
        assert caught.value.__cause__ is primary and caught.value.errno == errno.EIO
        assert "artifact=local_token" in str(caught.value)
        assert f"stage={'permissions' if failure == 'permissions' else 'open_stream'}" in str(caught.value)
        assert str(path) not in str(caught.value)
        assert len(local_token_diagnostics.records) == 1
    assert len(descriptors) == 1
    with pytest.raises(OSError) as closed:
        os.fstat(descriptors[0])
    assert closed.value.errno == errno.EBADF
    assert path.read_bytes() == b""
    # 失败文件由原 owner 保留供显式处理；下次不能悄悄覆盖或轮换 token。
    with pytest.raises(ValueError, match="本地凭据文件为空"):
        get_gateway_local_token()
    assert path.read_bytes() == b""
    path.unlink()
    token = get_gateway_local_token()
    assert token and get_gateway_local_token() == token


def test_local_token_create_and_descriptor_close_failures_keep_all_causes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, local_token_diagnostics: LocalTokenDiagnostics,
) -> None:
    monkeypatch.setenv("BOXTEAM_GATEWAY_ROOT", str(tmp_path / "gateway"))
    primary = OSError(errno.EIO, "原打开失败")
    cleanup = OSError(errno.EBADF, "原关闭失败")
    descriptors: list[int] = []
    original_close = os.close

    def fail_open(descriptor: int, *_args: object, **_kwargs: object) -> None:
        descriptors.append(descriptor)
        raise primary

    def fail_close(descriptor: int) -> None:
        assert descriptor == descriptors[0]
        raise cleanup

    try:
        with monkeypatch.context() as patch:
            patch.setattr(auth_module.os, "fdopen", fail_open)
            patch.setattr(auth_module.os, "close", fail_close)
            with pytest.raises(ExceptionGroup) as caught:
                get_gateway_local_token()
        failures = caught.value.exceptions
        assert len(failures) == 2
        assert failures[0].__cause__ is primary and failures[1].__cause__ is cleanup
        assert caught.value.__cause__ is failures[0]
        assert "stage=open_stream" in str(failures[0]) and "stage=close_descriptor" in str(failures[1])
        assert len(local_token_diagnostics.records) == 2
        if local_token_diagnostics.destination is not None:
            assert len(local_token_diagnostics.errors) == 2
            assert all("diagnostic_sink_error=" in str(error) for error in failures)
    finally:
        for descriptor in descriptors:
            original_close(descriptor)


def test_local_token_creation_race_never_replaces_the_winning_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, local_token_diagnostics: LocalTokenDiagnostics,
) -> None:
    root = tmp_path / "gateway"
    monkeypatch.setenv("BOXTEAM_GATEWAY_ROOT", str(root))
    path = root / "credentials/local-token"
    winner = b"credential-from-winning-creator\n"
    original_open = os.open

    def concurrent_create(path: Path, flags: int, mode: int) -> int:
        path.write_bytes(winner)
        path.chmod(0o600)
        return original_open(path, flags, mode)

    with monkeypatch.context() as patch:
        patch.setattr(auth_module.os, "open", concurrent_create)
        with pytest.raises(FileExistsError) as caught:
            get_gateway_local_token()
    assert caught.value.errno == errno.EEXIST and isinstance(caught.value.__cause__, FileExistsError)
    assert "artifact=local_token" in str(caught.value) and "stage=create" in str(caught.value)
    assert str(path) not in str(caught.value) and winner.decode().strip() not in str(caught.value)
    assert path.read_bytes() == winner and get_gateway_local_token() == winner.decode().strip()
