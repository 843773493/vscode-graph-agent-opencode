"""日志真实写入失败不能替换凭据主错或阻断原发布收尾。"""

from __future__ import annotations

import errno
import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

import app.gateway.credentials as credentials_module
from app.gateway.credentials import FederationCredentialStore, load_or_create_gateway_id


class FailingLogHandler(logging.Handler):
    def __init__(self, destination: Path) -> None:
        super().__init__(logging.ERROR)
        self.destination = destination
        self.records: list[logging.LogRecord] = []
        self.errors: list[OSError] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        try:
            with self.destination.open("a", encoding="utf-8"):
                pass
        except OSError as error:
            self.errors.append(error)
            raise


@pytest.fixture
def failed_sink(tmp_path: Path) -> Iterator[FailingLogHandler]:
    blocked = tmp_path / "blocked-log-directory"
    blocked.write_text("原阻塞文件", encoding="utf-8")
    handler = FailingLogHandler(blocked / "service.log")
    credentials_module.logger.addHandler(handler)
    try:
        yield handler
    finally:
        credentials_module.logger.removeHandler(handler)
        handler.close()


@pytest.mark.parametrize("damage", ["permissions", "json", "utf8", "version"])
def test_read_failure_keeps_primary_type_cause_coordinates_and_credentials(
    tmp_path: Path, failed_sink: FailingLogHandler, damage: str,
) -> None:
    if damage == "permissions" and os.name == "nt":
        pytest.skip("POSIX 私有文件权限；Windows ACL 由对应实机验收")
    path = tmp_path / "federation.json"
    store = FederationCredentialStore(storage_path=path)
    credential = store.issue(connection_id="rgw_diagnostic", peer_gateway_id="gateway_peer")
    original = path.read_bytes()
    expected: type[Exception] = ValueError
    stage = "decode"
    if damage == "permissions":
        path.chmod(0o644)
        expected, stage = PermissionError, "permissions"
    elif damage == "json":
        path.write_bytes(b'{"secret":"' + credential.token.encode() + b'", invalid}')
        expected = json.JSONDecodeError
    elif damage == "utf8":
        path.write_bytes(credential.token.encode() + b"\xff")
        expected = UnicodeDecodeError
    else:
        path.write_text(json.dumps({"schema_version": credential.token}))
        stage = "validate"
    damaged = path.read_bytes()
    with pytest.raises(expected) as caught:
        store.verify(credential.token)
    error = caught.value
    cause = error.__cause__
    assert type(error) is expected and type(cause) is expected
    assert f"stage={stage}" in str(error)
    assert f"diagnostic_sink_error={type(failed_sink.errors[0]).__name__}" in str(error)
    assert str(path) not in str(error) and credential.token not in str(error)
    assert str(failed_sink.destination) not in str(error)
    assert cause is not None
    assert str(failed_sink.destination) in "\n".join(cause.__notes__)
    assert len(failed_sink.records) == len(failed_sink.errors) == 1
    assert credential.token not in failed_sink.records[0].getMessage()
    assert (failed_sink.records[0].exc_info is not None) == (damage == "permissions")
    for attribute in ("errno", "winerror", "encoding", "object", "start", "end", "doc", "pos", "lineno", "colno"):
        if hasattr(cause, attribute):
            assert getattr(error, attribute) == getattr(cause, attribute)
    assert path.read_bytes() == damaged
    path.write_bytes(original)
    path.chmod(0o600)
    assert store.verify(credential.token) == credential


@pytest.mark.parametrize("stage", ["open_stream", "publish"])
def test_write_failure_keeps_authorization_and_releases_original_temp_owner(
    tmp_path: Path, failed_sink: FailingLogHandler, monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    path = tmp_path / "federation.json"
    store = FederationCredentialStore(storage_path=path)
    credential = store.issue(connection_id="rgw_existing", peer_gateway_id="gateway_peer")
    original = path.read_bytes()
    primary = OSError(errno.EIO, f"原生发布故障 {path}", str(path))
    descriptors: list[int] = []

    def fail_open(descriptor: int, *args: object, **kwargs: object) -> None:
        descriptors.append(descriptor)
        raise primary

    def fail_publish(*args: object) -> None:
        raise primary

    with monkeypatch.context() as patch:
        patch.setattr(credentials_module.os, "fdopen" if stage == "open_stream" else "replace",
                      fail_open if stage == "open_stream" else fail_publish)
        with pytest.raises(OSError) as caught:
            store.issue(connection_id="rgw_rejected", peer_gateway_id="gateway_new")
    assert caught.value.__cause__ is primary and caught.value.errno == errno.EIO
    assert f"stage={stage}" in str(caught.value)
    assert "diagnostic_sink_error=" in str(caught.value)
    assert str(path) not in str(caught.value) and str(failed_sink.destination) not in str(caught.value)
    assert str(failed_sink.destination) in "\n".join(primary.__notes__)
    assert path.read_bytes() == original and not list(tmp_path.glob(".federation.json.*"))
    for descriptor in descriptors:
        with pytest.raises(OSError) as closed:
            os.fstat(descriptor)
        assert closed.value.errno == errno.EBADF
    assert store.verify(credential.token) == credential
    assert store.issue(connection_id="rgw_rejected", peer_gateway_id="gateway_new").connection_id == "rgw_rejected"


def test_identity_decode_failure_keeps_original_identity_and_explicit_retry(
    tmp_path: Path, failed_sink: FailingLogHandler,
) -> None:
    path = tmp_path / "identity.json"
    identity = load_or_create_gateway_id(path)
    original = path.read_bytes()
    damaged = b'{"gateway_id": invalid}'
    path.write_bytes(damaged)
    with pytest.raises(json.JSONDecodeError) as caught:
        load_or_create_gateway_id(path)
    cause = caught.value.__cause__
    assert isinstance(cause, json.JSONDecodeError)
    assert caught.value.doc == cause.doc and caught.value.pos == cause.pos
    assert "artifact=gateway_identity" in str(caught.value) and "stage=decode" in str(caught.value)
    assert "diagnostic_sink_error=" in str(caught.value)
    assert str(path) not in str(caught.value) and str(failed_sink.destination) not in str(caught.value)
    assert str(failed_sink.destination) in "\n".join(cause.__notes__)
    assert failed_sink.records[0].exc_info is None and path.read_bytes() == damaged
    path.write_bytes(original)
    assert load_or_create_gateway_id(path) == identity
