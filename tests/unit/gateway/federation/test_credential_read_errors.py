"""联邦凭据读取失败保持认证拒绝、原件与诊断归属。"""

from __future__ import annotations

import errno
import json
import os
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, TextIO

import pytest

import app.gateway.credentials as credentials_module
from app.gateway.credentials import FederationCredential, FederationCredentialStore


@dataclass(frozen=True)
class CredentialCase:
    path: Path
    store: FederationCredentialStore
    credential: FederationCredential
    original: bytes


@pytest.fixture
def credential_case(tmp_path: Path) -> CredentialCase:
    path = tmp_path / "credentials" / "federation.json"
    store = FederationCredentialStore(storage_path=path)
    credential = store.issue(connection_id="rgw_read", peer_gateway_id="gateway_peer")
    return CredentialCase(path, store, credential, path.read_bytes())


@pytest.mark.parametrize("method", ["get", "verify", "list_valid", "list_all", "issue", "put", "remove"])
def test_all_credential_consumers_share_read_failure(
    credential_case: CredentialCase, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, method: str,
) -> None:
    case = credential_case
    original = PermissionError(errno.EACCES, f"native-private-marker {case.path}", str(case.path))
    descriptors: list[int] = []

    def reject(path: Path, *, descriptor: int | None = None) -> None:
        assert path == case.path
        assert descriptor is not None
        descriptors.append(descriptor)
        raise original

    with monkeypatch.context() as patch:
        patch.setattr(credentials_module, "require_private_file_access", reject)
        with pytest.raises(PermissionError) as caught:
            match method:
                case "get":
                    case.store.get(case.credential.connection_id)
                case "verify":
                    case.store.verify(case.credential.token)
                case "list_valid":
                    case.store.list_valid()
                case "list_all":
                    case.store.list_all()
                case "issue":
                    case.store.issue(connection_id="rgw_new", peer_gateway_id="gateway_new")
                case "put":
                    case.store.put(case.credential)
                case "remove":
                    case.store.remove(case.credential.connection_id)

    assert caught.value.__cause__ is original
    assert caught.value.errno == errno.EACCES
    assert caught.value.filename is None
    assert "artifact=federation_credentials" in str(caught.value)
    assert "stage=permissions" in str(caught.value)
    assert str(case.path) not in str(caught.value)
    assert "native-private-marker" not in str(caught.value)
    assert str(case.path) in caplog.text
    assert case.credential.token not in caplog.text
    assert case.path.read_bytes() == case.original
    assert len(descriptors) == 1
    with pytest.raises(OSError) as closed:
        os.fstat(descriptors[0])
    assert closed.value.errno == errno.EBADF
    assert case.store.get(case.credential.connection_id) == case.credential


@pytest.mark.parametrize("missing_after_open", [False, True], ids=["open-failed", "permission-file-missing"])
def test_native_read_failure_is_not_an_empty_credential_store(
    credential_case: CredentialCase, monkeypatch: pytest.MonkeyPatch, missing_after_open: bool,
) -> None:
    case = credential_case
    original = (FileNotFoundError if missing_after_open else OSError)(errno.EIO, str(case.path), str(case.path))
    original_open = Path.open

    def fail_open(
        path: Path, mode: str = "r", buffering: int = -1, encoding: str | None = None,
        errors: str | None = None, newline: str | None = None,
    ) -> IO[str] | IO[bytes]:
        if path == case.path:
            raise original
        return original_open(path, mode, buffering, encoding, errors, newline)

    def fail_permissions(path: Path, *, descriptor: int | None = None) -> None:
        assert path == case.path and descriptor is not None
        raise original

    with monkeypatch.context() as patch:
        if missing_after_open:
            patch.setattr(credentials_module, "require_private_file_access", fail_permissions)
        else:
            patch.setattr(Path, "open", fail_open)
        with pytest.raises(type(original)) as caught:
            case.store.list_all()
    assert caught.value.__cause__ is original
    assert str(case.path) not in str(caught.value)
    assert f"stage={'permissions' if missing_after_open else 'open'}" in str(caught.value)
    assert case.path.read_bytes() == case.original
    assert case.store.verify(case.credential.token) == case.credential


def test_close_failure_keeps_original_credentials_and_can_retry(
    credential_case: CredentialCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = credential_case
    original = OSError(errno.EIO, str(case.path), str(case.path))
    original_open = Path.open

    @contextmanager
    def close_failure(path: Path, mode: str, encoding: str | None) -> Iterator[TextIO]:
        assert path == case.path and mode == "r"
        with original_open(path, "r", encoding=encoding) as stream:
            yield stream
        raise original

    def fail_open(path: Path, mode: str = "r", *, encoding: str | None = None) -> AbstractContextManager[TextIO]:
        return close_failure(path, mode, encoding)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", fail_open)
        with pytest.raises(OSError) as caught:
            case.store.list_all()
    assert caught.value.__cause__ is original
    assert "stage=close" in str(caught.value)
    assert str(case.path) not in str(caught.value)
    assert case.path.read_bytes() == case.original
    assert case.store.verify(case.credential.token) == case.credential


@pytest.mark.parametrize("damage", ["version", "shape", "json", "utf8", "expiry", "credentials", "missing-field"])
def test_invalid_credentials_report_stage_without_copying_private_values(
    credential_case: CredentialCase, caplog: pytest.LogCaptureFixture, damage: str,
) -> None:
    case = credential_case
    payload = json.loads(case.original)
    match damage:
        case "version":
            payload["schema_version"] = str(case.path)
        case "shape":
            payload = [str(case.path)]
        case "expiry":
            payload["credentials"][0]["expires_at"] = str(case.path)
        case "credentials":
            payload["credentials"] = None
        case "missing-field":
            del payload["credentials"][0]["token"]
    damaged = json.dumps(payload).encode()
    if damage == "json":
        damaged = b'{"private": ' + json.dumps(str(case.path)).encode() + b','
    elif damage == "utf8":
        damaged = str(case.path).encode() + b"\xff"
    case.path.write_bytes(damaged)

    with pytest.raises((ValueError, TypeError, KeyError)) as caught:
        case.store.list_all()
    assert caught.value.__cause__ is not None
    assert type(caught.value) is type(caught.value.__cause__)
    assert "artifact=federation_credentials" in str(caught.value)
    assert f"stage={'decode' if damage in {'json', 'utf8'} else 'validate'}" in str(caught.value)
    assert str(case.path) not in str(caught.value)
    assert str(case.path) in caplog.text
    assert case.credential.token not in caplog.text
    assert case.path.read_bytes() == damaged
    case.path.write_bytes(case.original)
    assert case.store.verify(case.credential.token) == case.credential


def test_missing_credentials_remain_missing_without_creating_a_file(tmp_path: Path) -> None:
    path = tmp_path / "absent" / "federation.json"
    store = FederationCredentialStore(storage_path=path)
    assert store.list_all() == ()
    with pytest.raises(PermissionError, match="无效或已过期"):
        store.verify("unknown")
    assert not path.exists()
