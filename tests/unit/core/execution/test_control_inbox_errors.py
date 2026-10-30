"""控制状态拒绝公开原因、原始诊断和文件保全。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from app.core.turn_execution_scope import AgentControlInbox


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
