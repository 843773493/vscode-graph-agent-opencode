import base64
import errno
import json
import os
import time
from pathlib import Path

import pytest

from app.runtime.chatgpt_auth import (
    configure_litellm_chatgpt_auth_directory,
    ensure_chatgpt_oauth_ready,
    ensure_litellm_chatgpt_model_capabilities,
    is_chatgpt_oauth_provider,
)


def _set_test_home(monkeypatch, home: Path) -> None:
    monkeypatch.setenv("HOME", str(home))
    # TODO: Windows 的 pathlib.expanduser 使用 USERPROFILE，不读取 HOME。
    if os.name == "nt":
        monkeypatch.setenv("USERPROFILE", str(home))


def _access_token(expires_at: int, *, account_id: str | None = None) -> str:
    payload: dict[str, object] = {"exp": expires_at}
    if account_id is not None:
        payload["https://api.openai.com/auth"] = {
            "chatgpt_account_id": account_id,
        }
    claims = base64.urlsafe_b64encode(
        json.dumps(payload).encode()
    ).decode().rstrip("=")
    return f"header.{claims}.signature"


def _write_codex_auth(path: Path, *, access_token: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": access_token or _access_token(2_000_000_000),
                    "refresh_token": "codex-refresh",
                    "id_token": "codex-id",
                    "account_id": "account-1",
                }
            }
        ),
        encoding="utf-8",
    )


def test_chatgpt_auth_defaults_to_boxteam_user_directory(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("BOXTEAM_HOME", str(tmp_path / "boxteam-home"))
    monkeypatch.delenv("CHATGPT_TOKEN_DIR", raising=False)
    monkeypatch.delenv("CHATGPT_AUTH_FILE", raising=False)
    _set_test_home(monkeypatch, tmp_path / "home")

    token_dir = configure_litellm_chatgpt_auth_directory()

    assert token_dir == (tmp_path / "boxteam-home" / "auth" / "chatgpt").resolve()
    assert token_dir == Path(os.environ["CHATGPT_TOKEN_DIR"])


def test_chatgpt_auth_preserves_explicit_token_directory(
    monkeypatch,
    tmp_path: Path,
) -> None:
    explicit_dir = tmp_path / "custom-chatgpt-auth"
    monkeypatch.setenv("CHATGPT_TOKEN_DIR", str(explicit_dir))
    monkeypatch.delenv("CHATGPT_AUTH_FILE", raising=False)
    _set_test_home(monkeypatch, tmp_path / "home")

    token_dir = configure_litellm_chatgpt_auth_directory()

    assert token_dir == explicit_dir.resolve()


def test_chatgpt_auth_migrates_codex_tokens_once(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    source = home / ".codex" / "auth.json"
    _write_codex_auth(source)
    boxteam_home = tmp_path / "boxteam-home"
    _set_test_home(monkeypatch, home)
    monkeypatch.setenv("BOXTEAM_HOME", str(boxteam_home))
    monkeypatch.delenv("CHATGPT_TOKEN_DIR", raising=False)
    monkeypatch.delenv("CHATGPT_AUTH_FILE", raising=False)

    token_dir = configure_litellm_chatgpt_auth_directory()
    target = token_dir / "auth.json"
    migrated = json.loads(target.read_text(encoding="utf-8"))

    assert migrated == {
        "access_token": _access_token(2_000_000_000),
        "refresh_token": "codex-refresh",
        "id_token": "codex-id",
        "expires_at": 2_000_000_000,
        "account_id": "account-1",
    }
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o600
        assert token_dir.stat().st_mode & 0o777 == 0o700

    target.write_text('{"access_token": "litellm-owned"}\n', encoding="utf-8")
    _write_codex_auth(source, access_token=_access_token(2_100_000_000))
    configure_litellm_chatgpt_auth_directory()

    assert json.loads(target.read_text(encoding="utf-8")) == {
        "access_token": "litellm-owned"
    }


def test_chatgpt_auth_derives_missing_account_id_from_current_codex_jwt(
    monkeypatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    source = home / ".codex" / "auth.json"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": _access_token(2_000_000_000),
                    "refresh_token": "codex-refresh",
                    "id_token": _access_token(
                        2_000_000_000,
                        account_id="jwt-account",
                    ),
                }
            }
        ),
        encoding="utf-8",
    )
    _set_test_home(monkeypatch, home)
    monkeypatch.setenv("BOXTEAM_HOME", str(tmp_path / "boxteam-home"))
    monkeypatch.delenv("CHATGPT_TOKEN_DIR", raising=False)
    monkeypatch.delenv("CHATGPT_AUTH_FILE", raising=False)

    token_dir = configure_litellm_chatgpt_auth_directory()
    migrated = json.loads((token_dir / "auth.json").read_text(encoding="utf-8"))

    assert migrated["account_id"] == "jwt-account"


def test_chatgpt_oauth_provider_requires_explicit_auth_shape() -> None:
    assert is_chatgpt_oauth_provider(
        {"auth": {"type": "oauth", "method": "chatgpt"}}
    )
    assert not is_chatgpt_oauth_provider({"auth": {"type": "oauth"}})


def test_expired_chatgpt_auth_is_replaced_by_valid_codex_auth(
    monkeypatch,
    tmp_path: Path,
) -> None:
    now = int(time.time())
    home = tmp_path / "home"
    boxteam_home = tmp_path / "boxteam-home"
    _set_test_home(monkeypatch, home)
    monkeypatch.setenv("BOXTEAM_HOME", str(boxteam_home))
    monkeypatch.delenv("CHATGPT_TOKEN_DIR", raising=False)
    monkeypatch.delenv("CHATGPT_AUTH_FILE", raising=False)
    token_dir = configure_litellm_chatgpt_auth_directory()
    target = token_dir / "auth.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "access_token": _access_token(now - 60),
                "refresh_token": "stale-refresh",
                "id_token": "stale-id",
                "expires_at": now - 60,
                "account_id": "stale-account",
            }
        ),
        encoding="utf-8",
    )
    _write_codex_auth(
        home / ".codex" / "auth.json",
        access_token=_access_token(now + 3600),
    )

    ensure_chatgpt_oauth_ready(token_dir)

    refreshed = json.loads(target.read_text(encoding="utf-8"))
    assert refreshed["access_token"] == _access_token(now + 3600)
    assert refreshed["refresh_token"] == "codex-refresh"
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o600


def test_expired_chatgpt_auth_fails_fast_without_valid_codex_auth(
    monkeypatch,
    tmp_path: Path,
) -> None:
    now = int(time.time())
    home = tmp_path / "home"
    token_dir = tmp_path / "chatgpt-auth"
    token_dir.mkdir()
    (token_dir / "auth.json").write_text(
        json.dumps(
            {
                "access_token": _access_token(now - 60),
                "expires_at": now - 60,
            }
        ),
        encoding="utf-8",
    )
    _set_test_home(monkeypatch, home)

    try:
        ensure_chatgpt_oauth_ready(token_dir)
    except RuntimeError as error:
        assert "codex login" in str(error)
    else:
        raise AssertionError("过期凭据必须快速失败")


@pytest.fixture(params=["target", "migration", "refresh"])
def auth_read_case(request, monkeypatch, tmp_path):
    home = tmp_path / "home"
    _set_test_home(monkeypatch, home)
    token_dir = tmp_path / "private-auth-location"
    target = token_dir / "auth.json"
    source = home / ".codex/auth.json"
    monkeypatch.setenv("CHATGPT_TOKEN_DIR", str(token_dir))
    monkeypatch.delenv("CHATGPT_AUTH_FILE", raising=False)
    _write_codex_auth(source)
    if request.param != "migration":
        token_dir.mkdir()
        target.write_text(json.dumps({
            "access_token": _access_token(2_000_000_000),
            "expires_at": 0 if request.param == "refresh" else 2_000_000_000,
        }), encoding="utf-8")
    if request.param == "target":
        source = target
    invoke = (configure_litellm_chatgpt_auth_directory if request.param == "migration"
              else lambda: ensure_chatgpt_oauth_ready(token_dir))
    artifact = "chatgpt_auth" if request.param == "target" else "codex_auth"
    return source, target, invoke, artifact


def test_auth_missing_fields_keep_location_in_diagnostics(auth_read_case, caplog):
    source, target, invoke, artifact = auth_read_case
    original = source.read_bytes()
    source.write_bytes(b"{}\n")
    target_before = target.read_bytes() if target.exists() else None
    category = ValueError if artifact == "chatgpt_auth" else TypeError

    with pytest.raises(category) as caught:
        invoke()

    assert f"artifact={artifact}, stage=validate" in str(caught.value)
    assert str(source) not in str(caught.value)
    assert str(source) in caplog.text
    assert source.read_bytes() == b"{}\n"
    assert (target.read_bytes() if target.exists() else None) == target_before
    source.write_bytes(original)
    invoke()
    assert json.loads(target.read_bytes())["access_token"] == _access_token(2_000_000_000)


@pytest.mark.parametrize("category,code,native", [
    (PermissionError, errno.EACCES, "/private-auth-marker/auth.json"),
    (FileNotFoundError, errno.ENOENT, r"C:\private-auth-marker\auth.json"),
    (OSError, errno.EIO, r"\\private-auth-marker\share\auth.json"),
])
def test_auth_read_errors_keep_native_cause_and_retry(
    auth_read_case, monkeypatch, caplog, category, code, native,
):
    source, target, invoke, artifact = auth_read_case
    source_before = source.read_bytes()
    target_before = target.read_bytes() if target.exists() else None
    original_read = Path.read_text
    failure = category(code, f"读取失败 {native}", native)

    def read_text(path, *args, **kwargs):
        if path == source:
            raise failure
        return original_read(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", read_text)
        with pytest.raises(category) as caught:
            invoke()

    error = caught.value
    assert type(error) is category
    assert error.errno == code and error.filename is None
    assert getattr(error, "winerror", None) == getattr(failure, "winerror", None)
    assert error.__cause__ is failure
    assert f"artifact={artifact}, stage=read" in str(error)
    assert native not in str(error) and str(source) not in str(error)
    assert str(source) in caplog.text and native in caplog.text
    assert any(record.exc_info and record.exc_info[1] is failure for record in caplog.records)
    assert source.read_bytes() == source_before
    assert (target.read_bytes() if target.exists() else None) == target_before
    invoke()
    assert json.loads(target.read_bytes())["access_token"] == _access_token(2_000_000_000)


def test_auth_json_error_keeps_original_parse_coordinates(auth_read_case, caplog):
    source, _, invoke, _ = auth_read_case
    source.write_bytes(b'{\n"access_token":\n}')
    with pytest.raises(json.JSONDecodeError) as caught:
        invoke()
    assert (caught.value.lineno, caught.value.colno) == (3, 1)
    assert str(source) not in str(caught.value)
    assert not caplog.records


@pytest.mark.parametrize("auth_read_case,stage", [
    *[(mode, stage) for mode in ("migration", "refresh")
      for stage in ("prepare", "create_temp", "permissions", "fsync", "publish")],
    ("migration", "cleanup"),
], indirect=["auth_read_case"])
def test_auth_publication_failure_preserves_files_and_closes_descriptor(
    auth_read_case, monkeypatch, caplog, stage,
):
    from app.runtime import chatgpt_auth

    source, target, invoke, _ = auth_read_case
    source_before = source.read_bytes()
    target_before = target.read_bytes() if target.exists() else None
    failure = (RuntimeError(f"ACL失败 private-publish-marker {target}") if stage == "permissions"
               else PermissionError(errno.EACCES, "private-publish-marker", str(target)))
    descriptors = []
    original_mkstemp = chatgpt_auth.tempfile.mkstemp
    original_mkdir = Path.mkdir
    original_unlink = Path.unlink
    original_permissions = chatgpt_auth.restrict_private_path_access

    def mkstemp(*args, **kwargs):
        if stage == "create_temp":
            raise failure
        fd, name = original_mkstemp(*args, **kwargs)
        descriptors.append(fd)
        return fd, name

    def mkdir(path, *args, **kwargs):
        if stage == "prepare" and path == target.parent:
            raise failure
        return original_mkdir(path, *args, **kwargs)

    def unlink(path, *args, **kwargs):
        if stage == "cleanup" and path.name.startswith(".auth."):
            raise failure
        return original_unlink(path, *args, **kwargs)

    def permissions(path, *, is_directory=False, descriptor=None):
        if stage == "permissions" and descriptor is not None:
            raise failure
        return original_permissions(path, is_directory=is_directory, descriptor=descriptor)

    def fail(*args, **kwargs):
        raise failure

    with monkeypatch.context() as patch:
        patch.setattr(chatgpt_auth.tempfile, "mkstemp", mkstemp)
        patch.setattr(Path, "mkdir", mkdir)
        patch.setattr(Path, "unlink", unlink)
        patch.setattr(chatgpt_auth, "restrict_private_path_access", permissions)
        if stage == "fsync":
            patch.setattr(os, "fsync", fail)
        elif stage == "publish":
            patch.setattr(os, "link" if target_before is None else "replace", fail)
        with pytest.raises(type(failure)) as caught:
            invoke()
    for descriptor in descriptors:
        with pytest.raises(OSError) as closed:
            os.fstat(descriptor)
        assert closed.value.errno == errno.EBADF
    error = caught.value
    expected_stage = "write" if stage in {"permissions", "fsync"} else stage
    assert f"artifact=chatgpt_auth, stage={expected_stage}" in str(error)
    assert type(error) is type(failure) and error.__cause__ is failure
    if isinstance(error, OSError):
        assert error.errno == errno.EACCES and error.filename is None
    assert "private-publish-marker" not in str(error) and str(target) not in str(error)
    assert str(target) in caplog.text and "private-publish-marker" in caplog.text
    assert "codex-refresh" not in caplog.text and "codex-id" not in caplog.text
    assert source.read_bytes() == source_before
    if stage == "cleanup":
        assert json.loads(target.read_bytes())["access_token"] == _access_token(2_000_000_000)
        assert len(list(target.parent.glob(".auth.*.tmp"))) == 1
    else:
        assert (target.read_bytes() if target.exists() else None) == target_before
        assert list(target.parent.glob(".auth.*.tmp")) == []
    invoke()
    assert json.loads(target.read_bytes())["access_token"] == _access_token(2_000_000_000)


@pytest.mark.parametrize("auth_read_case", ["migration", "refresh"], indirect=True)
def test_auth_failed_publication_and_cleanup_keep_both_causes(auth_read_case, monkeypatch):
    source, target, invoke, _ = auth_read_case
    target_before = target.read_bytes() if target.exists() else None
    source_before = source.read_bytes()
    publication_error = OSError(errno.EIO, "private-publish-error", str(target))
    cleanup_error = PermissionError(errno.EACCES, "private-cleanup-error", str(target.parent))
    original_unlink = Path.unlink

    def fail(*args, **kwargs):
        raise publication_error

    def unlink(path, *args, **kwargs):
        if path.name.startswith(".auth."):
            raise cleanup_error
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail)
        patch.setattr(Path, "unlink", unlink)
        with pytest.raises(ExceptionGroup) as caught:
            invoke()
    primary, cleanup = caught.value.exceptions
    assert primary.__cause__ is publication_error
    assert cleanup.__cause__ is cleanup_error
    assert caught.value.__cause__ is primary
    assert "private-" not in str(caught.value) and str(target) not in str(caught.value)
    assert source.read_bytes() == source_before
    assert (target.read_bytes() if target.exists() else None) == target_before
    invoke()
    assert json.loads(target.read_bytes())["access_token"] == _access_token(2_000_000_000)


@pytest.mark.parametrize("auth_read_case", ["migration"], indirect=True)
def test_auth_migration_does_not_replace_concurrent_winner(auth_read_case, monkeypatch):
    _, target, invoke, _ = auth_read_case
    winner = b'{"access_token":"concurrent-winner","expires_at":2000000000}\n'
    original_link = os.link

    def link(source, destination):
        target.write_bytes(winner)
        return original_link(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(os, "link", link)
        invoke()
    assert target.read_bytes() == winner
    assert list(target.parent.glob(".auth.*.tmp")) == []


def test_gpt_56_capabilities_register_only_while_litellm_is_missing_support(
    monkeypatch,
) -> None:
    registered: list[dict] = []
    monkeypatch.setattr(
        "litellm.utils.supports_native_streaming",
        lambda **_: False,
    )
    monkeypatch.setattr("litellm.register_model", registered.append)

    assert ensure_litellm_chatgpt_model_capabilities("gpt-5.6-luna") is True
    assert registered[0]["chatgpt/gpt-5.6-luna"]["supports_native_streaming"] is True

    registered.clear()
    monkeypatch.setattr(
        "litellm.utils.supports_native_streaming",
        lambda **_: True,
    )
    assert ensure_litellm_chatgpt_model_capabilities("gpt-5.6-luna") is False
    assert registered == []
