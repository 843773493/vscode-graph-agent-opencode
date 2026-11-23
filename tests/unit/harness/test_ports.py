from __future__ import annotations

import errno
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

import portalocker
import pytest

from tests.support import ports
from tests.support.ports import TEST_PORT_RANGES, TestFilePorts, _candidate_starts


@pytest.fixture
def plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestFilePorts]:
    # 单测只注入租约和原生锁，不监听网络；真实互斥由 integration/harness 验证。
    @contextmanager
    def lock(*_args, **_kwargs):
        yield

    monkeypatch.setattr(ports.portalocker, "Lock", lock)
    monkeypatch.setattr(ports.E2EProcessLease, "acquire", Mock())
    monkeypatch.setattr(ports.E2EProcessLease, "release", Mock())
    value = TestFilePorts(tmp_path / "test_ports.py", tmp_path / "leases", ((33000, 33003),))
    try:
        yield value
    finally:
        value.close()


def test_candidates_visit_allowed_ranges_once_and_wrap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ports.secrets, "randbelow", lambda size: size - 1)
    assert list(_candidate_starts(((8800, 8802), (1024, 1025)), 1)) == [1025, 8800, 8801, 8802, 1024]
    assert list(_candidate_starts(((8800, 8802), (1024, 1025)), 2)) == [1024, 8800, 8801]
    assert list(_candidate_starts(((8800, 8802),), 4)) == []
    candidates = set(_candidate_starts(TEST_PORT_RANGES, 1))
    assert len(candidates) == sum(end - start + 1 for start, end in TEST_PORT_RANGES) - len(ports.TEST_BROWSER_BLOCKED_PORTS)
    assert candidates.isdisjoint(range(8000, 8800))
    assert candidates.isdisjoint(ports.TEST_BROWSER_BLOCKED_PORTS)
    assert min(candidates) == 1024 and max(candidates) == 65535
    # 连续池中间也不能夹入浏览器禁用端口。
    assert list(_candidate_starts(((10079, 10082),), 2)) == [10081]


def test_service_key_is_stable_without_arithmetic_offsets(plan: TestFilePorts, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ports, "_candidate_starts", lambda _ranges, _count: iter([33003, 33000]))
    first = plan.port(0)
    assert plan.port(0) == first == 33003
    # 第二项的候选先跳过已持有端口（真实锁行为另测）。
    monkeypatch.setattr(ports, "_candidate_starts", lambda _ranges, _count: iter([33000]))
    assert plan.port(20) == 33000
    assert ports.E2EProcessLease.acquire.call_count == 2
    with pytest.raises(ValueError, match="不能改变端口数量"):
        plan.port_range(0, count=2)
    with pytest.raises(ValueError, match="必须为正数"):
        plan.port_range("ssh", count=0)


def test_only_address_conflicts_are_skipped(plan: TestFilePorts, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ports, "_candidate_starts", lambda _ranges, _count: iter([33000, 33001]))
    seen = []

    def acquire(lease):
        seen.append(lease.ports)
        if lease.ports == (33000,):
            raise RuntimeError("已占用") from OSError(errno.EADDRINUSE, "占用")

    monkeypatch.setattr(ports.E2EProcessLease, "acquire", acquire)
    assert plan.port("gateway") == 33001
    assert seen == [(33000,), (33001,)]


@pytest.mark.parametrize("code", [errno.EACCES, None])
def test_other_acquire_errors_are_not_retried(plan: TestFilePorts, monkeypatch: pytest.MonkeyPatch, code: int | None) -> None:
    calls = []
    failure = RuntimeError("原租约错误")

    def acquire(lease):
        calls.append(lease.ports)
        if code is None:
            raise failure
        raise failure from OSError(code, "原原因")

    monkeypatch.setattr(ports.E2EProcessLease, "acquire", acquire)
    with pytest.raises(RuntimeError) as caught:
        plan.port("backend")
    assert caught.value is failure
    assert len(calls) == 1


def test_locked_candidate_does_not_create_lease(plan: TestFilePorts, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ports, "_candidate_starts", lambda _ranges, _count: iter([33000, 33001]))

    @contextmanager
    def lock(path, **_kwargs):
        if path.name == "33000.lock":
            raise portalocker.exceptions.AlreadyLocked("占用")
        yield

    monkeypatch.setattr(ports.portalocker, "Lock", lock)
    assert plan.port("gateway") == 33001
    assert ports.E2EProcessLease.acquire.call_count == 1


def test_exhaustion_is_bounded_and_releases_partial_group(plan: TestFilePorts, monkeypatch: pytest.MonkeyPatch) -> None:
    opened, closed = [], []

    @contextmanager
    def lock(path, **_kwargs):
        if path.name == "33003.lock":
            raise portalocker.exceptions.AlreadyLocked("占用")
        opened.append(path.name)
        try:
            yield
        finally:
            closed.append(path.name)

    monkeypatch.setattr(ports, "_candidate_starts", lambda _ranges, _count: iter([33002, 33003]))
    monkeypatch.setattr(ports.portalocker, "Lock", lock)
    with pytest.raises(RuntimeError, match="端口池已耗尽.*attempts=2"):
        plan.port_range("ssh", count=2)
    assert opened == closed == ["33002.lock"]
    assert ports.E2EProcessLease.acquire.call_count == 0


def test_file_plan_reuse_explicit_root_and_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "custom-tests"
    file = root / "e2e/test_example.py"
    file.parent.mkdir(parents=True)
    file.touch()
    monkeypatch.setattr(ports, "_lease_root", lambda _root: tmp_path / "leases")
    first = ports.e2e_ports_for_file(file, tests_root=root)
    assert ports.e2e_ports_for_file(file, tests_root=root) is first
    ports.release_ports_for_file(file)
    second = ports.e2e_ports_for_file(file, tests_root=root)
    assert second is not first
    ports.release_ports_for_file(file)
    with pytest.raises(ValueError, match="不在指定测试层或不存在"):
        ports.ports_for_file(file, test_layer="integration", tests_root=root)
