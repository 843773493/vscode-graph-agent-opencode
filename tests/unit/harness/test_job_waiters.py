from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from tests.support import api_waiters


@dataclass
class JobApi:
    client: httpx.AsyncClient
    states: dict[str, list[str]]
    reads: list[str]


@pytest.fixture
async def job_api() -> AsyncIterator[JobApi]:
    states: dict[str, list[str]] = {}
    reads: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        job_id = request.url.path.removeprefix("/api/v1/jobs/")
        reads.append(job_id)
        sequence = states[job_id]
        status = sequence.pop(0) if len(sequence) > 1 else sequence[0]
        return httpx.Response(200, json={"data": {
            "job_id": job_id,
            "status": status,
            "error_message": f"{job_id} 失败详情",
        }})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://job-fixture",
    ) as client:
        yield JobApi(client, states, reads)


@pytest.fixture
def poll_sleep(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    sleep = AsyncMock()
    monkeypatch.setattr(api_waiters, "asyncio", SimpleNamespace(sleep=sleep))
    return sleep


async def test_completed_jobs_return_without_a_final_polling_delay(
    job_api: JobApi,
    poll_sleep: AsyncMock,
) -> None:
    job_api.states.update({"a": ["completed"], "b": ["succeeded"]})
    results = await api_waiters.wait_for_jobs_done_concurrently(
        job_api.client, ["a", "b"], max_attempts=1,
    )

    assert set(results) == {"a", "b"}
    assert {data["job_id"] for data in results.values()} == {"a", "b"}
    assert sorted(job_api.reads) == ["a", "b"]
    poll_sleep.assert_not_awaited()


async def test_only_pending_jobs_are_polled_again_and_sleep_is_between_rounds(
    job_api: JobApi,
    poll_sleep: AsyncMock,
) -> None:
    job_api.states.update({"a": ["completed"], "b": ["running", "completed"]})
    results = await api_waiters.wait_for_jobs_done_concurrently(
        job_api.client, ["a", "b"], max_attempts=2,
    )

    assert set(results) == {"a", "b"}
    assert job_api.reads.count("a") == 1
    assert job_api.reads.count("b") == 2
    poll_sleep.assert_awaited_once_with(1)


@pytest.mark.parametrize("multiple", [False, True])
async def test_exhausted_poll_rounds_do_not_add_a_final_sleep_or_false_success(
    multiple: bool,
    job_api: JobApi,
    poll_sleep: AsyncMock,
) -> None:
    job_api.states["pending"] = ["running"]
    with pytest.raises(pytest.fail.Exception, match="pending.*超时|超时.*pending"):
        if multiple:
            await api_waiters.wait_for_jobs_done_concurrently(
                job_api.client, ["pending"], max_attempts=2,
            )
        else:
            await api_waiters.wait_for_job_done(job_api.client, "pending", max_attempts=2)

    assert job_api.reads == ["pending", "pending"]
    poll_sleep.assert_awaited_once_with(1)


@pytest.mark.parametrize("status", ["failed", "cancelled", "timed_out"])
async def test_failed_job_preserves_identity_and_reason_without_waiting(
    status: str,
    job_api: JobApi,
    poll_sleep: AsyncMock,
) -> None:
    job_api.states["broken"] = [status]
    with pytest.raises(pytest.fail.Exception, match="broken.*broken 失败详情"):
        await api_waiters.wait_for_jobs_done_concurrently(job_api.client, ["broken"])
    assert job_api.reads == ["broken"]
    poll_sleep.assert_not_awaited()


async def test_no_jobs_requires_neither_requests_nor_waiting(
    job_api: JobApi,
    poll_sleep: AsyncMock,
) -> None:
    assert await api_waiters.wait_for_jobs_done_concurrently(job_api.client, []) == {}
    assert not job_api.reads
    poll_sleep.assert_not_awaited()
