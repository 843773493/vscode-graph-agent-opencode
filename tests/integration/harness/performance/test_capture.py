"""一个真实紧循环证明进程外采样可存活；HTTP与流观察不偷读正文。"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from tests.harness.performance.observers import (
    observe_http,
    observe_loop,
    observe_model_stream,
    observe_sqlite,
)
from tests.harness.python.process_output import spawn_logged_process
from tests.harness.python.run_context import TestRunContext
from tests.support.processes.workspace import terminate_process


@pytest.fixture
def capture_context(request):
    return TestRunContext.from_test_file(Path(request.node.path)).prepare()


def test_external_sampling_survives_busy_process_and_keeps_exit_evidence(capture_context):
    context = capture_context
    directory = context.artifacts_dir / "busy-capture"
    directory.mkdir()
    child, _ = spawn_logged_process(
        [sys.executable, str(Path.cwd() / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")),
         "--memory-limit-kb", "262144", "--timeout-seconds", "0.6", "--", sys.executable,
         "-c", "allocation = bytearray(8 * 1024 * 1024)\nwhile True: pass"],
        cwd=Path.cwd(), env=os.environ.copy(), artifacts_dir=directory, log_name="busy",
    )
    sampler, logs = spawn_logged_process(
        [sys.executable, "-m", "tests.harness.performance.resources", "--pid", str(child.pid),
         "--directory", str(directory), "--interval-ms", "50", "--max-seconds", "10"],
        cwd=Path.cwd(), env=os.environ.copy(), artifacts_dir=directory, log_name="sampler",
    )
    try:
        path = directory / "resources.jsonl"
        deadline = time.monotonic() + 5
        while not path.exists() or len(path.read_text(encoding="utf-8").splitlines()) < 2:
            assert time.monotonic() < deadline, [path.read_text(encoding="utf-8") for path in logs]
            time.sleep(.05)
        assert child.wait(timeout=3) == 124
        (directory / "stop-sampling").write_text("stop\n")
        assert sampler.wait(timeout=5) == 0, [path.read_text(encoding="utf-8") for path in logs]
        summary = json.loads((directory / "resources-summary.json").read_text(encoding="utf-8"))
        assert summary["sample_count"] >= 2
        assert summary["observed_peak_rss_sum_bytes"] >= 8 * 1024 * 1024
        assert child.returncode == 124
    finally:
        terminate_process(child)
        terminate_process(sampler)


async def test_observers_keep_stream_semantics_and_notice_loop_block(perf):
    if perf.mode not in {"core", "diagnostic"}:
        pytest.skip("该场景验证 core 观察器")
    delivered = []

    async def model():
        for text in ("一", "二"):
            delivered.append(text)
            yield SimpleNamespace(content=text, usage_metadata={"input_tokens": 3, "output_tokens": 2})

    async with observe_loop(perf, interval=.01):
        time.sleep(.06)  # noqa: ASYNC251 — 故意阻塞以证明loop lag探针能识别真实同步阻塞。
        await asyncio.sleep(.02)
        output = [chunk.content async for chunk in observe_model_stream(model(), perf, provider="simulated")]
    assert output == delivered == ["一", "二"]
    closed = asyncio.Event()
    waiting = asyncio.Event()

    async def cancelled_source():
        try:
            yield SimpleNamespace(content="可见文本")
            waiting.set()
            await asyncio.Event().wait()
        finally:
            closed.set()

    async def consume():
        async for _ in observe_model_stream(cancelled_source(), perf, provider="simulated"):
            pass

    task = asyncio.create_task(consume())
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()

    with observe_sqlite(perf), sqlite3.connect(":memory:") as connection:
        connection.execute("CREATE TABLE items (value INTEGER)")
        connection.executemany("INSERT INTO items VALUES (?)", [(1,), (2,)])
        cursor = connection.execute("SELECT value FROM items ORDER BY value")
        assert next(cursor) == (1,)
        assert cursor.fetchone() == (2,)
        assert cursor.fetchone() is None
        connection.rollback()
        assert connection.execute("SELECT count(*) FROM items").fetchone() == (0,)

    attempts = 0

    async def respond(request):
        nonlocal attempts
        attempts += 1
        return httpx.Response(503 if attempts == 1 else 200, content=b"abc", headers={"x-request-id": "original-request"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with observe_http(client, perf):
            first = await client.get("https://test.invalid/value?token=must-not-be-recorded")
            assert first.status_code == 503
            response = await client.get("https://test.invalid/value?token=must-not-be-recorded")
        assert response.content == b"abc"
        assert response.headers["x-request-id"] == "original-request"
    rows = [json.loads(line) for line in perf.path.read_text(encoding="utf-8").splitlines()]
    lag = next(row for row in rows if row["stage"] == "asyncio.loop_lag")
    assert lag["max_ms"] >= 30
    assert "must-not-be-recorded" not in perf.path.read_text(encoding="utf-8")
    requests = [row for row in rows if row["event"] == "end" and row["stage"] == "http.attempt"]
    assert [row["status_code"] for row in requests] == [503, 200]
    streams = [row for row in rows if row["event"] == "end" and row["stage"] == "model.stream"]
    assert [(row["status"], row["consumption"]) for row in streams] == [("passed", "complete"), ("cancelled", "ended_early")]
    assert streams[0]["output_tokens"] == 2
