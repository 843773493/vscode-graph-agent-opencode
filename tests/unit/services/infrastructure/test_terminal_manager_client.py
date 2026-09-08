from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.terminal_manager_client import TerminalManagerClient


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return b'{"data": {}}'


@pytest.mark.asyncio
async def test_delete_terminal_normalizes_nested_terminal_snapshot() -> None:
    client = TerminalManagerClient(backend_url="http://terminal.test")

    async def fake_request(
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        assert method == "DELETE"
        assert path == "/api/terminals/term_1"
        assert payload is None
        return {
            "data": {
                "deleted": True,
                "terminal_id": "term_1",
                "terminal": {
                    "terminal_id": "term_1",
                    "session_id": "session_1",
                    "status": "deleted",
                },
            }
        }

    client._json_request = fake_request  # type: ignore[method-assign]

    result = await client.delete_terminal("term_1")

    assert result["deleted"] is True
    assert result["terminal_id"] == "term_1"
    assert result["terminal"] == {
        "terminal_id": "term_1",
        "session_id": "session_1",
        "status": "deleted",
    }


def test_terminal_backend_url_is_resolved_from_current_config_snapshot() -> None:
    config_service = Mock(spec=ConfigService)
    current_url = ["http://terminal-a"]
    config_service.get_terminal_backend_url.side_effect = (
        lambda: current_url[0]
    )
    client = TerminalManagerClient(config_service=config_service)

    with patch(
        "app.services.infrastructure.terminal_manager_client.urlopen",
        return_value=_Response(),
    ) as mocked_urlopen:
        client._json_request_sync("GET", "/api/terminals", None)
        current_url[0] = "http://terminal-b"
        client._json_request_sync("GET", "/api/terminals", None)

    assert [
        call.args[0].full_url for call in mocked_urlopen.call_args_list
    ] == [
        "http://terminal-a/api/terminals",
        "http://terminal-b/api/terminals",
    ]
