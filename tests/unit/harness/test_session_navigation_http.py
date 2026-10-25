from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import SimpleNamespace

import httpx
import pytest

from tests.support.session_navigation import http_operations as session_navigation_http


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

    result = await session_navigation_http.wait_for_navigation_operation(
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

    result = await session_navigation_http.wait_for_navigation_operation(
        client,  # type: ignore[arg-type]
        "op_delete",
        wait_for_settlement=wait_for_settlement,
    )

    assert result["settlement"] == "pending"
    assert client.poll_count == 1


@dataclass
class NavigationPoll:
    response: httpx.Response | None = None
    failure: Exception | None = None
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    sleep_cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    reads: int = 0


@pytest.fixture
async def navigation_poll() -> AsyncIterator[tuple[httpx.AsyncClient, NavigationPoll]]:
    poll = NavigationPoll()

    async def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/session-catalog/operations"
        assert request.url.params["operation_id"] == "op_delete"
        poll.reads += 1
        poll.entered.set()
        if poll.failure is not None:
            raise poll.failure
        if poll.response is not None:
            return poll.response
        try:
            await asyncio.Event().wait()
        finally:
            poll.cancelled.set()
        raise AssertionError("受控请求只能由取消结束")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://navigation-fixture",
    ) as client:
        yield client, poll


@pytest.fixture
def short_navigation_deadline(
    monkeypatch: pytest.MonkeyPatch,
    navigation_poll: tuple[httpx.AsyncClient, NavigationPoll],
) -> None:
    _, poll = navigation_poll

    async def observe_sleep(seconds: float) -> None:
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            poll.sleep_cancelled.set()
            raise

    monkeypatch.setattr(session_navigation_http, "NAVIGATION_OPERATION_WAIT_SECONDS", 0.02)
    monkeypatch.setattr(session_navigation_http, "asyncio", SimpleNamespace(
        timeout=asyncio.timeout, sleep=observe_sleep,
        get_running_loop=asyncio.get_running_loop,
    ))


@pytest.mark.parametrize("stage", ["request", "settlement_poll"])
async def test_wait_deadline_covers_requests_and_polling_sleep(
    navigation_poll: tuple[httpx.AsyncClient, NavigationPoll],
    short_navigation_deadline: None,
    stage: str,
) -> None:
    client, poll = navigation_poll
    if stage == "settlement_poll":
        poll.response = httpx.Response(200, json={"data": {"items": [
            _receipt(kind="delete_session", settlement="pending"),
        ]}})

    # 外层只为错误实现兜底，不能把它的超时误认为 helper 的成功行为。
    async with asyncio.timeout(1):
        with pytest.raises(AssertionError, match="物理 settlement 未到终态: op_delete") as failure:
            await session_navigation_http.wait_for_navigation_operation(
                client,
                "op_delete",
                wait_for_settlement=True,
            )

    assert isinstance(failure.value.__cause__, TimeoutError)
    assert poll.reads == 1
    assert poll.cancelled.is_set() == (stage == "request")
    assert poll.sleep_cancelled.is_set() == (stage == "settlement_poll")


@pytest.mark.parametrize("failure", [httpx.ReadError("连接读取失败"), TimeoutError("传输层超时")])
async def test_wait_preserves_transport_failures(
    navigation_poll: tuple[httpx.AsyncClient, NavigationPoll],
    failure: Exception,
) -> None:
    client, poll = navigation_poll
    poll.failure = failure
    with pytest.raises(type(failure)) as caught:
        await session_navigation_http.wait_for_navigation_operation(
            client,
            "op_delete",
        )
    assert caught.value is failure
    assert poll.reads == 1


async def test_wait_preserves_caller_cancellation(
    navigation_poll: tuple[httpx.AsyncClient, NavigationPoll],
) -> None:
    client, poll = navigation_poll
    task = asyncio.create_task(
        session_navigation_http.wait_for_navigation_operation(client, "op_delete")
    )
    try:
        async with asyncio.timeout(1):
            await poll.entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert poll.cancelled.is_set()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
