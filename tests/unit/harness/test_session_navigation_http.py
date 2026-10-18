from __future__ import annotations

import httpx
import pytest

from tests.support.session_navigation_http import wait_for_navigation_operation


class _ReceiptClient:
    def __init__(self, receipts: list[dict[str, object]]) -> None:
        self._receipts = receipts
        self.poll_count = 0

    async def get(
        self,
        path: str,
        *,
        params: dict[str, str],
    ) -> httpx.Response:
        assert path == "/api/v1/session-catalog/operations"
        assert params == {"operation_id": "op_delete"}
        assert self.poll_count < len(self._receipts), "helper 轮询次数超过受控回执序列"
        receipt = self._receipts[self.poll_count]
        self.poll_count += 1
        return httpx.Response(
            200,
            json={"data": {"items": [receipt]}},
        )


def _receipt(*, kind: str, settlement: str) -> dict[str, object]:
    return {
        "operation_id": "op_delete",
        "kind": kind,
        "state": "committed",
        "settlement": settlement,
    }


@pytest.mark.asyncio
async def test_wait_for_delete_settlement_polls_past_pending_receipt() -> None:
    client = _ReceiptClient(
        [
            _receipt(kind="delete_session", settlement="pending"),
            _receipt(kind="delete_session", settlement="pending"),
            _receipt(kind="delete_session", settlement="settled"),
        ]
    )

    result = await wait_for_navigation_operation(
        client,  # type: ignore[arg-type]
        "op_delete",
        wait_for_settlement=True,
    )

    assert result["state"] == "committed"
    assert result["settlement"] == "settled"
    assert client.poll_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "wait_for_settlement"),
    [("delete_session", False), ("rename_node", True)],
)
async def test_pending_settlement_does_not_delay_default_or_non_delete_operations(
    kind: str,
    wait_for_settlement: bool,
) -> None:
    client = _ReceiptClient([_receipt(kind=kind, settlement="pending")])

    result = await wait_for_navigation_operation(
        client,  # type: ignore[arg-type]
        "op_delete",
        wait_for_settlement=wait_for_settlement,
    )

    assert result["settlement"] == "pending"
    assert client.poll_count == 1
