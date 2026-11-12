"""统一观察器只验证归属、故障透明和比较能力，不复制业务性能断言。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tests.harness.performance.record import Perf
from tests.harness.performance.report import compare, summarize
from tests.harness.performance.resources import run, snapshot
from tests.harness.python.run_context import TestRunContext


@pytest.fixture
def isolated_perf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("BOXTEAM_TEST_PERF_DIR", raising=False)
    context = TestRunContext.from_test_file(Path("tests/unit/harness/test_performance.py"), project_root=tmp_path)
    perf = Perf(context, mode="core")
    try:
        yield perf
    finally:
        perf.close()


@pytest.fixture
def failed_preparation(perf):
    with perf.stage("fixture.expected_failure"):
        raise RuntimeError("明确的fixture准备失败")


@pytest.mark.xfail(raises=RuntimeError, strict=True, reason="验证真实setup失败仍留证且不执行call")
def test_setup_failure_never_enters_call(failed_preparation):
    raise AssertionError("setup失败后不得进入call")


@pytest.mark.parametrize("operation", ["write", "flush"])
def test_close_io_failure_releases_stream_and_preserves_error(isolated_perf, monkeypatch, operation):
    perf = isolated_perf
    stream = perf._stream
    original = getattr(stream, operation)
    error = OSError("性能产物写入失败")
    failed = False

    def fail_once(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise error
        return original(*args, **kwargs)

    monkeypatch.setattr(stream, operation, fail_once)
    with pytest.raises(OSError) as failure:
        perf.close()
    assert failure.value is error
    assert stream.closed
    perf.close()


async def test_concurrent_stages_keep_parent_and_cancellation(isolated_perf, monkeypatch):
    perf = isolated_perf
    entered = asyncio.Event()

    async def waiting():
        with perf.stage("waiting"):
            entered.set()
            await asyncio.Event().wait()

    with perf.stage("parent"):
        task = asyncio.create_task(waiting())
        await entered.wait()
        with perf.stage("sibling", workload={"items": 3}):
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    with monkeypatch.context() as patch:
        def fail_record(*args, **kwargs):
            raise OSError("阶段开始记录失败")

        patch.setattr(perf, "record", fail_record)
        with pytest.raises(OSError), perf.stage("failed_start"):
            raise AssertionError("记录失败不得进入阶段")
    with perf.stage("after_failed_start"):
        pass
    original_record = perf.record
    body_error = ValueError("原业务错误")
    with monkeypatch.context() as patch:
        def fail_end(event, *args, **kwargs):
            if event == "end":
                raise OSError("阶段结束记录失败")
            return original_record(event, *args, **kwargs)

        patch.setattr(perf, "record", fail_end)
        with pytest.raises(ValueError) as failure, perf.stage("failed_end"):
            raise body_error
        assert failure.value is body_error
        assert "阶段结束记录失败" in " ".join(body_error.__notes__)
    perf.close()
    rows = [json.loads(line) for line in perf.path.read_text(encoding="utf-8").splitlines()]
    starts = {row["stage"]: row for row in rows if row["event"] == "start"}
    ends = {row["stage"]: row for row in rows if row["event"] == "end"}
    assert starts["waiting"]["parent_stage_id"] == starts["sibling"]["parent_stage_id"] == starts["parent"]["stage_id"]
    assert ends["waiting"]["status"] == "cancelled"
    assert ends["parent"]["status"] == "passed"
    assert starts["after_failed_start"]["parent_stage_id"] == starts["parent"]["parent_stage_id"]


def test_resource_capabilities_capacity_and_early_exit_are_explicit(tmp_path, monkeypatch):
    import psutil

    process = psutil.Process()

    def unsupported():
        raise NotImplementedError

    monkeypatch.setattr(process, "io_counters", unsupported, raising=False)
    assert snapshot(process)["io"] == {"status": "not_supported"}
    (tmp_path / "stop-sampling").touch()
    monkeypatch.setattr(psutil.Process, "children", lambda self, recursive: [])
    run(process.pid, tmp_path, .01, 1, 1)
    summary = json.loads((tmp_path / "resources-summary.json").read_text(encoding="utf-8"))
    assert summary["dropped_samples"] == 1
    assert summary["observed_peak_rss_sum_bytes"] > 0
    assert (tmp_path / "resources.jsonl").read_text(encoding="utf-8") == ""
    early = tmp_path / "exited"
    early.mkdir()
    original_process = psutil.Process

    def exited_process(pid=None):
        if pid is not None:
            raise psutil.NoSuchProcess(pid)
        return original_process()

    monkeypatch.setattr(psutil, "Process", exited_process)
    run(process.pid, early, .01, 1024, 1)
    summary = json.loads((early / "resources-summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "process_exited_before_sampling"
    assert summary["observed_peak_rss_sum_bytes"] is summary["cpu_observed_deltas"] is None


def test_report_comparison_preserves_raw_samples_and_rejects_workload_mismatch(isolated_perf):
    perf = isolated_perf
    with perf.stage("read", workload={"items": 8}):
        pass
    perf.close()
    summary = summarize(perf.directory)
    assert summary["stages"][0]["count"] == 1
    assert summary["stages"][0]["distribution"]["tail_confidence"] == "descriptive_only"
    assert compare(summary, summary)["comparable"]
    candidate = json.loads(json.dumps(summary))
    candidate["stages"][0]["workload"]["items"] = 16
    assert not compare(summary, candidate)["comparable"]
    assert summary["stages"][0]["workload"]["items"] == 8
    zero = json.loads(json.dumps(summary))
    zero["stages"][0]["distribution"]["p50_ms"] = 0
    assert compare(zero, summary)["changes"][0]["relative_delta"] is None
