"""按需观察已有调用边界；不复制被测实现，不预读数据库或网络正文。"""
from __future__ import annotations

import asyncio
import inspect
import sqlite3
from collections.abc import AsyncIterator, Iterator, Sized
from contextlib import asynccontextmanager, contextmanager
from functools import wraps
from time import perf_counter_ns

import pytest

from tests.harness.performance.record import Perf, active_perf
from tests.harness.performance.report import distribution


@contextmanager
def observe_calls(owner: object, names: tuple[str, ...], perf: Perf | None = None) -> Iterator[None]:
    perf = perf or active_perf()
    if perf is None or perf.mode not in {"core", "diagnostic"}:
        yield
        return
    with pytest.MonkeyPatch.context() as patch:
        for name in names:
            original = getattr(owner, name)
            label = f"method.{getattr(owner, '__name__', type(owner).__name__)}.{name}"

            def measure(original, label):
                @wraps(original)
                def measured(*args, **kwargs):
                    with perf.stage(label) as result:
                        value = original(*args, **kwargs)
                        result["returned_present"] = value is not None
                        result["returned_count"] = len(value) if isinstance(value, Sized) else None
                        return value

                @wraps(original)
                async def measured_async(*args, **kwargs):
                    with perf.stage(label):
                        return await original(*args, **kwargs)

                return measured_async if inspect.iscoroutinefunction(original) else measured

            patch.setattr(owner, name, measure(original, label))
        yield


@asynccontextmanager
async def observe_loop(perf: Perf, interval: float = .05) -> AsyncIterator[None]:
    if perf.mode not in {"core", "diagnostic"}:
        yield
        return
    values: list[float] = []
    count, maximum = 0, 0.0

    async def poll() -> None:
        nonlocal count, maximum
        loop = asyncio.get_running_loop()
        while True:
            expected = loop.time() + interval
            await asyncio.sleep(interval)
            lag = max(0, loop.time() - expected) * 1000
            count += 1
            maximum = max(maximum, lag)
            if len(values) < 1024:
                values.append(lag)

    task = asyncio.create_task(poll(), name="test-performance-loop-lag")
    await asyncio.sleep(0)
    try:
        yield
    finally:
        # 让刚解除的同步阻塞先结算一次，再停止观察，不把最后的阻塞漏掉。
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        perf.record("metric", "asyncio.loop_lag", sample_count=count, max_ms=maximum,
                    sample_interval_ms=interval * 1000, retained_samples=len(values),
                    p50_ms=distribution(values)["p50_ms"], p95_ms=distribution(values)["p95_ms"],
                    samples_truncated=count > len(values))


def checkpoint_resources(perf: Perf, label: str, round: int) -> None:
    """只在固定轮次边界取资源快照；持续采样仍由runner唯一负责。"""
    if perf.mode not in {"core", "diagnostic"}:
        return
    import os

    import psutil

    from tests.harness.performance.resources import sample_tree

    samples = sample_tree(psutil.Process(), {}, discover=True)
    for sample in samples:
        perf.record("metric", "resource.checkpoint", label=label, round=round,
                    observed_pid=sample["pid"], created_at=sample["created_at"],
                    rss_bytes=sample.get("rss_bytes"), threads=sample.get("threads"),
                    handles=sample.get("handles") if isinstance(sample.get("handles"), int) else None,
                    status=sample.get("status", "collected"), process_count=len(samples),
                    scope="current_process" if sample["pid"] == os.getpid() else "descendant_shared")


@contextmanager
def observe_http(client, perf: Perf) -> Iterator[None]:
    """非流式 send 记录完整消费；流式 send 只记录响应头，绝不偷读正文。"""
    original = client.send

    async def send(request, *args, **kwargs):
        with perf.stage("http.attempt", workload={"method": request.method, "stream": kwargs.get("stream", False)}) as result:
            response = await original(request, *args, **kwargs)
            result.update(status_code=response.status_code, request_id=response.headers.get("x-request-id"),
                          boundary="response_headers" if kwargs.get("stream") else "body_consumed",
                          received_bytes=None if kwargs.get("stream") else response.num_bytes_downloaded)
            return response

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(client, "send", send)
        yield


async def observe_model_stream(source: AsyncIterator, perf: Perf, *, provider: str) -> AsyncIterator:
    """沿消费者实际迭代记录模型流；不缓存 chunk，不写入提示词和输出正文。"""
    first_text = None
    first_chunk = None
    chunks = 0
    usage = None
    completed = False
    with perf.stage("model.stream", workload={"provider_kind": provider}) as result:
        started = perf_counter_ns()
        try:
            async for chunk in source:
                chunks += 1
                if first_chunk is None:
                    first_chunk = perf_counter_ns() - started
                content = getattr(chunk, "content", None)
                visible = bool(content) if isinstance(content, str) else (
                    any(isinstance(block, dict) and block.get("type") in {"text", "output_text"} and block.get("text") for block in content)
                    if isinstance(content, list) else False
                )
                if visible and first_text is None:
                    first_text = perf_counter_ns() - started
                current_usage = getattr(chunk, "usage_metadata", None)
                if current_usage:
                    usage = current_usage
                yield chunk
            completed = True
        finally:
            elapsed = perf_counter_ns() - started
            output_tokens = usage.get("output_tokens") if usage else None
            result.update(chunks=chunks, first_content_ns=first_text, first_chunk_ns=first_chunk,
                          observed_stream_ns=elapsed,
                          consumption="complete" if completed else "ended_early",
                          output_tokens_per_second=output_tokens / (elapsed / 1e9) if output_tokens is not None and elapsed else None,
                          rate_denominator="whole_observed_stream_seconds",
                          input_tokens=usage.get("input_tokens") if usage else None,
                          output_tokens=usage.get("output_tokens") if usage else None,
                          usage_status="collected" if usage else "not_available",
                          usage_source="last_chunk_with_usage", first_token_status="not_observed",
                          cached_input_tokens=(usage.get("input_token_details") or {}).get("cache_read") if usage else None)
            if hasattr(source, "aclose"):
                await source.aclose()


@contextmanager
def observe_sqlite(perf: Perf) -> Iterator[None]:
    if perf.mode not in {"core", "diagnostic"}:
        yield
        return
    counters = {"execute_count": 0, "execute_ns": 0, "consumed_rows": 0, "busy_count": 0, "custom_factory_connections_not_observed": 0}
    connect = sqlite3.connect

    class Cursor(sqlite3.Cursor):
        def execute(self, sql, parameters=()):
            started = perf_counter_ns()
            try:
                return super().execute(sql, parameters)
            except sqlite3.OperationalError as error:
                counters["busy_count"] += getattr(error, "sqlite_errorcode", 0) & 255 in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
                raise
            finally:
                counters["execute_count"] += 1
                counters["execute_ns"] += perf_counter_ns() - started

        def fetchone(self):
            row = super().fetchone()
            counters["consumed_rows"] += row is not None
            return row

        def fetchall(self):
            rows = super().fetchall()
            counters["consumed_rows"] += len(rows)
            return rows

        def fetchmany(self, *args, **kwargs):
            rows = super().fetchmany(*args, **kwargs)
            counters["consumed_rows"] += len(rows)
            return rows

        def __next__(self):
            row = super().__next__()
            counters["consumed_rows"] += 1
            return row

    class Connection(sqlite3.Connection):
        def cursor(self, factory=Cursor):
            return super().cursor(factory)

        def execute(self, sql, parameters=()):
            return self.cursor().execute(sql, parameters)

    def measured_connect(*args, **kwargs):
        factory = args[5] if len(args) > 5 else kwargs.get("factory", sqlite3.Connection)
        if factory is not sqlite3.Connection:
            counters["custom_factory_connections_not_observed"] += 1
            return connect(*args, **kwargs)
        if len(args) > 5:
            args = (*args[:5], Connection, *args[6:])
        else:
            kwargs["factory"] = Connection
        return connect(*args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(sqlite3, "connect", measured_connect)
        try:
            yield
        finally:
            perf.record("metric", "sqlite.aggregate", **counters, scope="current_test_observed_connections",
                        scan_rows_status="not_observed", executemany_status="not_observed", executescript_status="not_observed")
