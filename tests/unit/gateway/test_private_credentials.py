from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

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
