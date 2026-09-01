from app.gateway.protocol.control import gateway_health_to_proto
from app.protocol.codecs.browser import browser_page_to_json, browser_page_to_proto
from app.protocol.codecs.terminal import (
    terminal_session_to_json,
    terminal_session_to_proto,
)
from app.protocol.generated.boxteam.terminal.v1 import terminal_pb2
from app.schemas.gateway import GatewayHealthDTO


def test_terminal_snapshot_round_trip_preserves_extended_json_fields() -> None:
    value = {
        "terminal_id": "terminal_123",
        "session_id": "session_123",
        "status": "running",
        "cols": 120,
        "rows": 32,
        "buffer": "hello\n",
        "last_command_status": "running",
    }

    encoded = terminal_session_to_proto(value)

    assert terminal_session_to_json(encoded) == value


def test_terminal_completed_snapshot_is_accepted_during_command_release() -> None:
    value = {
        "terminal_id": "terminal_completed",
        "session_id": "session_123",
        "status": "completed",
        "last_command_status": "completed",
        "last_command_exit_code": 0,
    }

    encoded = terminal_session_to_proto(value)

    assert encoded.status == terminal_pb2.TERMINAL_STATUS_EXITED
    assert terminal_session_to_json(encoded) == value


def test_browser_snapshot_round_trip_preserves_extended_json_fields() -> None:
    value = {
        "browser_id": "browser_123",
        "page_id": "page_123",
        "session_id": "session_123",
        "status": "running",
        "url": "https://example.com",
        "viewport": {"width": 1280, "height": 800},
        "pages": [{"page_id": "page_123", "active": True}],
    }

    encoded = browser_page_to_proto(value)

    assert browser_page_to_json(encoded) == value


def test_gateway_adapter_uses_only_gateway_protocol_fields() -> None:
    encoded = gateway_health_to_proto(
        GatewayHealthDTO(
            process_id=123,
            active_workspace_id="workspace_123",
            development_restart_available=True,
        ),
        gateway_id="gateway_123",
    )

    assert encoded.gateway_id == "gateway_123"
    assert encoded.active_workspace_id == "workspace_123"
    assert encoded.process_id == 123
