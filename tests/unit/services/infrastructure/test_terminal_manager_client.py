from __future__ import annotations

import pytest

from app.services.infrastructure.terminal_manager_client import TerminalManagerClient


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
