"""原资源账本的文件错误不泄露位置，失败不改变真实占用事实。"""

from __future__ import annotations

import errno
import json
import logging
import os
from pathlib import Path

import pytest

from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)


@pytest.fixture(params=[False, True], ids=["log-ok", "log-failed"])
def ledger_log_failure(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
) -> OSError | None:
    if not request.param:
        return None
    failure = OSError("日志输出失败 /private/ledger-log-secret")
    original = logging.Logger.callHandlers

    def call_handlers(logger: logging.Logger, record: logging.LogRecord) -> None:
        original(logger, record)
        if "artifact=external_resource_leases" in record.getMessage():
            raise failure

    monkeypatch.setattr(logging.Logger, "callHandlers", call_handlers)
    return failure


@pytest.fixture
def ledger(tmp_path: Path) -> tuple[ExternalResourceLeaseLedger, Path]:
    path = tmp_path / "private" / "resources.json"
    owner = ExternalResourceLeaseLedger(state_path=path)
    owner.register_external(resource_id="terminal_original", kind="terminal", lifetime_scope="workspace")
    owner.acquire_operation(resource_id="terminal_original", turn_stream_id="holder", operation_id="original")
    return owner, path


@pytest.mark.parametrize("payload,reason", [
    ([], "必须是对象"),
    ({"records": "private-path"}, "records 必须是数组"),
    ({"leases": "private-path"}, "leases 必须是数组"),
    ({"records": [{}, "private-path"]}, "records[1] 必须是对象"),
    ({"leases": [{}, "private-path"]}, "leases[1] 必须是对象"),
])
def test_bad_envelope_reports_field_without_file_location(
    ledger: tuple[ExternalResourceLeaseLedger, Path], payload: object, reason: str,
    caplog: pytest.LogCaptureFixture, ledger_log_failure: OSError | None,
) -> None:
    owner, path = ledger
    before = path.read_bytes()
    damaged = json.dumps(payload).encode()
    path.write_bytes(damaged)
    with caplog.at_level("ERROR"), pytest.raises(TypeError) as raised:
        ExternalResourceLeaseLedger(state_path=path)
    detail = str(raised.value)
    assert str(path) not in detail and "private-path" not in detail
    assert reason in detail and "artifact=external_resource_leases" in detail and "stage=validate" in detail
    assert str(path) in caplog.text and path.read_bytes() == damaged
    assert isinstance(raised.value.__cause__, TypeError)
    if ledger_log_failure is not None:
        assert "diagnostic_sink_error=OSError" in detail
        assert any(str(ledger_log_failure) in note for note in raised.value.__cause__.__notes__)
    else:
        assert "diagnostic_sink_error" not in detail
    assert "ledger-log-secret" not in detail
    path.write_bytes(before)
    cold = ExternalResourceLeaseLedger(state_path=path)
    assert cold.get("terminal_original") == owner.get("terminal_original")
    assert cold.leases_for_resource("terminal_original") == owner.leases_for_resource("terminal_original")


@pytest.mark.parametrize("stage", ["read", "mkdir", "open", "fsync", "replace"])
def test_native_failure_preserves_original_lease_and_explicit_retry(
    ledger: tuple[ExternalResourceLeaseLedger, Path], stage: str,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    ledger_log_failure: OSError | None,
) -> None:
    owner, path = ledger
    original_bytes = path.read_bytes()
    original_record = owner.get("terminal_original")
    original_leases = owner.leases_for_resource("terminal_original")
    native = PermissionError(errno.EACCES, "原生拒绝", str(path))
    with caplog.at_level("ERROR"), monkeypatch.context() as fault:
        if stage == "fsync":
            def fail_sync(_fd: int) -> None:
                raise native
            fault.setattr(os, "fsync", fail_sync)
        else:
            method = {"read": "read_text", "mkdir": "mkdir", "open": "open", "replace": "replace"}[stage]
            target = path if stage == "read" else path.parent if stage == "mkdir" else path.with_suffix(".tmp")
            original_method = getattr(Path, method)

            def fail_target(current: Path, *args: object, **kwargs: object) -> object:
                if current == target:
                    raise native
                return original_method(current, *args, **kwargs)

            fault.setattr(Path, method, fail_target)
        with pytest.raises(PermissionError) as raised:
            if stage == "read":
                ExternalResourceLeaseLedger(state_path=path)
            else:
                owner.acquire_operation(resource_id="terminal_original", turn_stream_id="holder", operation_id="new")
    error = raised.value
    assert str(path) not in str(error) and error.filename is None
    assert error.errno == errno.EACCES and error.__cause__ is native
    assert f"stage={'read' if stage == 'read' else 'write'}" in str(error)
    assert str(path) in caplog.text and "原生拒绝" in caplog.text
    if ledger_log_failure is not None:
        assert "diagnostic_sink_error=OSError" in str(error)
        assert any(str(ledger_log_failure) in note for note in native.__notes__)
    else:
        assert "diagnostic_sink_error" not in str(error)
    assert "ledger-log-secret" not in str(error)
    assert path.read_bytes() == original_bytes and owner.get("terminal_original") == original_record
    assert owner.leases_for_resource("terminal_original") == original_leases
    cold = ExternalResourceLeaseLedger(state_path=path)
    assert cold.leases_for_resource("terminal_original") == original_leases
    retried = owner.acquire_operation(resource_id="terminal_original", turn_stream_id="holder", operation_id="new")
    assert owner.acquire_operation(resource_id="terminal_original", turn_stream_id="holder", operation_id="new") == retried
    assert ExternalResourceLeaseLedger(state_path=path).get_lease(retried.lease_id) == retried


def test_directory_cannot_replace_a_missing_ledger(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "private" / "resources.json"
    path.mkdir(parents=True)
    sentinel = path / "unrelated"
    sentinel.write_bytes(b"untouched")
    with caplog.at_level("ERROR"), pytest.raises(OSError) as raised:
        ExternalResourceLeaseLedger(state_path=path)
    assert str(path) not in str(raised.value)
    assert isinstance(raised.value.__cause__, OSError) and str(path) in caplog.text
    assert sentinel.read_bytes() == b"untouched"
    sentinel.unlink()
    path.rmdir()
    assert ExternalResourceLeaseLedger(state_path=path).get("absent") is None


def test_memory_and_not_yet_created_ledgers_keep_their_contract(tmp_path: Path) -> None:
    path = tmp_path / "not-created" / "resources.json"
    assert ExternalResourceLeaseLedger(state_path=path).get("absent") is None
    assert not path.parent.exists()
    owner = ExternalResourceLeaseLedger()
    owner.register_external(resource_id="memory", kind="mcp_connection", lifetime_scope="workspace")
    assert owner.get("memory") is not None
