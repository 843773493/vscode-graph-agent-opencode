import pytest

from app.gateway.protocol.control import gateway_health_to_proto
from app.protocol.codecs.browser import browser_page_to_json, browser_page_to_proto
from app.protocol.codecs.terminal import (
    terminal_session_to_json,
    terminal_session_to_proto,
)
from app.protocol.generated.boxteam.terminal.v1 import terminal_pb2
from app.schemas.gateway.diagnostics import GatewayHealthDTO


def test_terminal_snapshot_round_trip_preserves_extended_json_fields() -> None:
    value = {
        "terminal_id": "terminal_123",
        "workspace_id": "00000000-0000-4000-8000-000000000001",
        "cwd_relative": ".",
        "transport_owner_revision": 9_007_199_254_740_993,
        "command_start_sequence": 9_007_199_254_740_993,
        "status": "running",
        "cols": 120,
        "rows": 32,
        "buffer": "hello\n",
        "last_command_status": "running",
    }

    encoded = terminal_session_to_proto(value)

    decoded = terminal_session_to_json(encoded)
    assert isinstance(decoded["transport_owner_revision"], int)
    assert isinstance(decoded["command_start_sequence"], int)
    assert decoded == value


def test_terminal_completed_snapshot_is_accepted_during_command_release() -> None:
    value = {
        "terminal_id": "terminal_completed",
        "workspace_id": "00000000-0000-4000-8000-000000000001",
        "transport_owner_revision": 1,
        "status": "completed",
        "last_command_status": "completed",
        "last_command_exit_code": 0,
    }

    encoded = terminal_session_to_proto(value)

    assert encoded.status == terminal_pb2.TERMINAL_STATUS_EXITED
    decoded = terminal_session_to_json(encoded)
    assert isinstance(decoded["transport_owner_revision"], int)
    assert decoded == value


def test_browser_snapshot_round_trip_preserves_extended_json_fields() -> None:
    value = {
        "browser_id": "browser_123",
        "page_id": "page_123",
        "workspace_id": "00000000-0000-4000-8000-000000000001",
        "transport_owner_revision": 9_007_199_254_740_993,
        "reconciliation_status": "verified",
        "status": "running",
        "url": "https://example.com",
        "viewport": {"width": 1280, "height": 800},
        "pages": [{"page_id": "page_123", "active": True}],
    }

    encoded = browser_page_to_proto(value)

    decoded = browser_page_to_json(encoded)
    assert isinstance(decoded["transport_owner_revision"], int)
    assert decoded == value


def test_gateway_adapter_uses_only_gateway_protocol_fields() -> None:
    encoded = gateway_health_to_proto(
        GatewayHealthDTO(
            gateway_id="gateway_123",
            process_id=123,
            active_workspace_id="workspace_123",
            development_restart_available=True,
        ),
    )

    assert encoded.gateway_id == "gateway_123"
    assert encoded.active_workspace_id == "workspace_123"
    assert encoded.process_id == 123

@pytest.mark.parametrize("exit_code", [0, 9, None])
def test_terminal_exit_codes_keep_integer_type_and_presence_through_wire(exit_code: int | None) -> None:
    value = {
        "terminal_id": "term_exit_presence",
        "workspace_id": "00000000-0000-4000-8000-000000000001",
        "transport_owner_revision": 1,
        "status": "running" if exit_code is None else "completed",
        "exit_code": exit_code, "last_command_exit_code": exit_code,
    }
    encoded = terminal_session_to_proto(value)
    wire = terminal_pb2.TerminalSession.FromString(encoded.SerializeToString())
    decoded = terminal_session_to_json(wire)
    for field in ("exit_code", "last_command_exit_code"):
        assert wire.HasField(field) is (exit_code is not None)
        assert decoded[field] == exit_code
        if exit_code is not None:
            assert isinstance(decoded[field], int)
