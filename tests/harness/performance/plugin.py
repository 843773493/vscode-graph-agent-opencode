"""pytest 生命周期自动留证；阶段 fixture 不承担进程监督。"""
from __future__ import annotations

import cProfile
import os
import platform
from pathlib import Path

import pytest
import pytest_asyncio

from tests.harness.performance.record import Perf, active_perf, set_active
from tests.harness.performance.report import summarize
from tests.harness.python.run_context import TestRunContext

_session: Perf | None = None


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("performance")
    group.addoption("--perf", choices=("off", "basic", "core", "diagnostic"), default=os.getenv("BOXTEAM_TEST_PERF_MODE", "basic"))
    selected = os.getenv("BOXTEAM_TEST_PERF_TOOL")
    group.addoption("--perf-profile", choices=("cpu", "allocations"), default=selected if selected in {"cpu", "allocations"} else None,
                    help="仅在 diagnostic 档位显式追踪当前进程；不记录模型或HTTP正文")


def pytest_configure(config: pytest.Config) -> None:
    if config.getoption("perf_profile") and config.getoption("perf") != "diagnostic":
        raise pytest.UsageError("--perf-profile 需要 --perf=diagnostic")


def pytest_sessionstart(session: pytest.Session) -> None:
    global _session
    _session = Perf(TestRunContext.from_test_file(Path(__file__)), mode=session.config.getoption("perf"))
    set_active(_session)
    _session.record("session_start", "pytest.session", startup_scope="uv_interpreter_early_imports_not_decomposed",
                    python_version=platform.python_version(), pytest_version=pytest.__version__)


@pytest.hookimpl(wrapper=True)
def pytest_collection(session: pytest.Session) -> object:
    assert _session is not None
    with _session.stage("pytest.collection"):
        return (yield)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None) -> object:
    perf = Perf(TestRunContext.from_test_file(Path(item.path)), mode=item.config.getoption("perf"), test_id=item.nodeid)
    set_active(perf)
    try:
        return (yield)
    finally:
        try:
            perf.close()
        finally:
            set_active(_session)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo) -> object:
    report = yield
    perf = active_perf()
    if perf is not None:
        perf.record("test_report", f"pytest.{report.when}", status=report.outcome,
                    elapsed_ns=int(report.duration * 1e9), error_type=call.excinfo.typename if call.excinfo else None)
        if report.when == "setup" and report.outcome != "passed":
            perf.record("test_report", "pytest.call", status="not_run", reason=f"setup_{report.outcome}")
    return report


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> object:
    perf = active_perf()
    assert perf is not None
    profile = item.config.getoption("perf_profile")
    profiler = cProfile.Profile() if profile == "cpu" else None
    if profiler:
        profiler.enable()
    if profile == "allocations":
        import tracemalloc
        tracemalloc.start(10)
    try:
        with perf.stage("pytest.call"):
            return (yield)
    finally:
        if profiler:
            profiler.disable()
            profiler.dump_stats(str(perf.path.with_suffix(".pstats")))
        if profile == "allocations":
            import tracemalloc
            tracemalloc.take_snapshot().dump(str(perf.path.with_suffix(".tracemalloc")))
            tracemalloc.stop()
        if profile:
            perf.record("diagnostic", "python.profile", tool=profile, scope="pytest_call_current_process", path=str(perf.path.with_suffix(".pstats" if profiler else ".tracemalloc")))


@pytest.hookimpl(wrapper=True)
def pytest_runtest_setup(item: pytest.Item) -> object:
    perf = active_perf()
    assert perf is not None
    with perf.stage("pytest.setup"):
        return (yield)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None) -> object:
    perf = active_perf()
    assert perf is not None
    with perf.stage("pytest.teardown"):
        return (yield)


@pytest.fixture
def perf() -> Perf:
    current = active_perf()
    assert current is not None
    return current


@pytest.fixture
def perf_sqlite(perf):
    from tests.harness.performance.observers import observe_sqlite
    with observe_sqlite(perf):
        yield


@pytest_asyncio.fixture
async def perf_loop(perf):
    from tests.harness.performance.observers import observe_loop
    async with observe_loop(perf):
        yield


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if _session is not None:
        _session.record("session_end", "pytest.session", status=str(exitstatus), collected=session.testscollected)
        _session.close()
        if _session.mode != "off" and not os.getenv("BOXTEAM_TEST_PERF_DIR"):
            summarize(_session.directory)
        set_active(None)
