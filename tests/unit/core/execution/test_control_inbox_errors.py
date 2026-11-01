"""控制状态拒绝公开原因、原始诊断和文件保全。"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path

import pytest

from app.core.turn_execution_scope import AgentControlInbox


@pytest.fixture(params=["open", "fsync", "replace"])
def control_write_failure(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[], AbstractContextManager[OSError]]:
    @contextmanager
    def fail_write() -> Iterator[OSError]:
        failure = OSError("控制日志写入失败")

        def reject(*_args: object, **_kwargs: object) -> None:
            raise failure

        with monkeypatch.context() as patch:
            if request.param == "fsync":
                patch.setattr("app.core.turn_execution_scope.os.fsync", reject)
            else:
                patch.setattr(Path, str(request.param), reject)
            yield failure

    return fail_write


@pytest.fixture(params=["/private/control-state", r"C:\private\control-state", r"\\host\private\control-state"])
def invalid_location(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.mark.parametrize("fault", ["shape", "session", "command"])
def test_state_rejection_preserves_cause_and_file_without_publishing_locations(
    tmp_path: Path,
    invalid_location: str,
    fault: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "control.json"
    payload = [] if fault == "shape" else (
        {"session_id": invalid_location} if fault == "session" else
        {"session_id": "ses_owner", "commands": [{invalid_location: "bad-field"}]}
    )
    body = json.dumps(payload).encode("utf-8")
    path.write_bytes(body)
    error_type = RuntimeError if fault == "session" else TypeError
    with caplog.at_level(logging.ERROR), pytest.raises(error_type) as raised:
        AgentControlInbox("stream_owner", session_id="ses_owner", state_path=path)

    error = raised.value
    assert type(error) is error_type
    assert "artifact=turn_control_state" in str(error)
    assert "stage=turn-control-load" in str(error)
    assert str(path) not in str(error)
    assert invalid_location not in str(error)
    cause = error.__cause__
    assert type(cause) is error_type
    assert str(path) in "\n".join(cause.__notes__)
    if fault != "shape":
        assert invalid_location in str(cause)
    assert any(record.exc_info and record.exc_info[1] is cause for record in caplog.records)
    assert str(path) in caplog.text
    assert path.read_bytes() == body


def test_rejected_state_can_be_repaired_without_changing_command_identity(tmp_path: Path) -> None:
    path = tmp_path / "control.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(TypeError):
        AgentControlInbox("stream_owner", session_id="ses_owner", state_path=path)
    path.unlink()
    inbox = AgentControlInbox("stream_owner", session_id="ses_owner", state_path=path)
    command = inbox.accept(command_id="cmd_owner", kind="interrupt", idempotency_key="idem_owner")

    restored = AgentControlInbox("stream_owner", session_id="ses_owner", state_path=path)

    assert restored.snapshot() == [command]
    assert restored.accept(command_id="cmd_duplicate", kind="interrupt", idempotency_key="idem_owner") == command
    assert restored._pending.empty()


@pytest.mark.parametrize("has_existing", [False, True])
def test_accept_write_failure_is_not_published_and_retry_is_durable(
    tmp_path: Path,
    control_write_failure: Callable[[], AbstractContextManager[OSError]],
    has_existing: bool,
) -> None:
    path = tmp_path / "control.json"
    inbox = AgentControlInbox("stream_owner", session_id="ses_owner", state_path=path)
    if has_existing:
        original = inbox.accept(command_id="cmd_original", kind="steer", idempotency_key="idem_original")
        assert inbox._pending.get_nowait() == original
    before = inbox.snapshot()
    body = path.read_bytes() if path.exists() else None

    with control_write_failure() as failure, pytest.raises(OSError) as caught:
        inbox.accept(command_id="cmd_retry", kind="interrupt", idempotency_key="idem_retry")

    assert caught.value is failure
    assert inbox.snapshot() == before
    assert inbox.get("cmd_retry") is None
    assert inbox._pending.empty()
    assert (path.read_bytes() if path.exists() else None) == body
    assert AgentControlInbox("stream_owner", session_id="ses_owner", state_path=path).snapshot() == before

    accepted = inbox.accept(command_id="cmd_retry", kind="interrupt", idempotency_key="idem_retry")
    assert accepted.control_seq == len(before) + 1
    assert inbox._pending.get_nowait() == accepted
    assert inbox.accept(command_id="cmd_duplicate", kind="interrupt", idempotency_key="idem_retry") == accepted
    assert inbox._pending.empty()
    restored = AgentControlInbox("stream_owner", session_id="ses_owner", state_path=path)
    assert restored.snapshot() == [*before, accepted]
    assert restored._pending.empty()


@pytest.mark.parametrize("status", ["consumed", "rejected"])
def test_mark_write_failure_keeps_durable_status_until_retry(
    tmp_path: Path,
    control_write_failure: Callable[[], AbstractContextManager[OSError]],
    status: str,
) -> None:
    path = tmp_path / "control.json"
    inbox = AgentControlInbox("stream_owner", session_id="ses_owner", state_path=path)
    command = inbox.accept(command_id="cmd_owner", kind="approval.result", idempotency_key="idem_owner")
    body = path.read_bytes()

    with control_write_failure() as failure, pytest.raises(OSError) as caught:
        inbox.mark(command.command_id, status)

    assert caught.value is failure
    assert inbox.get(command.command_id) == command
    assert inbox.snapshot() == [command]
    assert path.read_bytes() == body
    assert AgentControlInbox("stream_owner", session_id="ses_owner", state_path=path).snapshot() == [command]

    updated = inbox.mark(command.command_id, status)
    assert updated.status == status
    assert updated.control_seq == command.control_seq
    restored = AgentControlInbox("stream_owner", session_id="ses_owner", state_path=path)
    assert restored.snapshot() == [updated]
    assert restored._pending.empty()
