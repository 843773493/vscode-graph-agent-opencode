from __future__ import annotations

import errno
import secrets
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from threading import RLock
from typing import ClassVar, Literal

import portalocker

from tests.harness.python.integration_gate import integration_gate_path
from tests.support.e2e_control.harness import E2EProcessLease

# 沿用测试允许范围，避开特权端口及 8000–8799 开发服务段。
TEST_PORT_RANGES = ((8800, 65535), (1024, 7999))
# WHATWG Fetch 的禁用端口在允许范围内的部分；空闲也不能用于浏览器 HTTP/WS。
# https://fetch.spec.whatwg.org/#port-blocking
TEST_BROWSER_BLOCKED_PORTS = frozenset({
    1719, 1720, 1723, 2049, 3659, 4045, 4190, 5060, 5061, 6000,
    6566, 6665, 6666, 6667, 6668, 6669, 6679, 6697, 10080,
})
_allocation_lock = RLock()


def _candidate_starts(ranges: tuple[tuple[int, int], ...], count: int) -> Iterator[int]:
    """随机起点后有界遍历；连续申请不能跨越范围边界。"""
    lengths = [max(0, end - start - count + 2) for start, end in ranges]
    total = sum(lengths)
    if not total:
        return
    first = secrets.randbelow(total)
    for step in range(total):
        index = (first + step) % total
        for (start, _end), length in zip(ranges, lengths, strict=True):
            if index < length:
                candidate = start + index
                if not any(candidate <= port < candidate + count for port in TEST_BROWSER_BLOCKED_PORTS):
                    yield candidate
                break
            index -= length


@lru_cache(maxsize=16)
def _lease_root(project_root: Path) -> Path:
    # 复用闸门的主 worktree 解析；不同 worktree 的测试共同协调端口。
    return integration_gate_path(project_root).with_name("ports")


@dataclass(slots=True)
class TestFilePorts:
    """一次测试运行的服务端口计划；key 只标识服务，不再表示算术偏移。"""

    __test__: ClassVar[bool] = False
    test_file: Path
    lease_root: Path
    ranges: tuple[tuple[int, int], ...] = TEST_PORT_RANGES
    _ports: dict[int | str, tuple[int, ...]] = field(default_factory=dict, init=False)
    _resources: ExitStack = field(default_factory=ExitStack, init=False, repr=False)

    def port(self, key: int | str) -> int:
        return self.port_range(key, count=1)[0]

    def port_range(self, key: int | str, *, count: int) -> tuple[int, ...]:
        """只有 SSH 隧道池等真实范围需求才连续申请，其余服务独立分配。"""
        with _allocation_lock:
            if count < 1:
                raise ValueError(f"测试端口数量必须为正数: {count}")
            if key in self._ports:
                ports = self._ports[key]
                if len(ports) != count:
                    raise ValueError(f"同一服务不能改变端口数量: key={key}, count={count}")
                return ports
            self.lease_root.mkdir(parents=True, exist_ok=True)
            attempts = 0
            for start in _candidate_starts(self.ranges, count):
                attempts += 1
                ports = tuple(range(start, start + count))
                with ExitStack() as resources:
                    try:
                        # REUSEADDR 不排斥另一个未监听的租约，必须同时持有原生文件锁。
                        # 锁文件不删除，避免等待者与后来者锁住不同 inode。
                        for port in ports:
                            resources.enter_context(portalocker.Lock(
                                self.lease_root / f"{port}.lock", timeout=0,
                            ))
                    except portalocker.exceptions.AlreadyLocked:
                        continue
                    lease = E2EProcessLease(self.lease_root / f"{start}.json", ports)
                    try:
                        lease.acquire()
                    except RuntimeError as error:
                        cause = error.__cause__
                        if isinstance(cause, OSError) and (
                            cause.errno == errno.EADDRINUSE or getattr(cause, "winerror", None) == 10048
                        ):
                            continue
                        raise
                    resources.callback(lease.release)
                    self._resources.enter_context(resources.pop_all())
                    self._ports[key] = ports
                    return ports
            raise RuntimeError(
                f"测试端口池已耗尽: file={self.test_file}, key={key}, "
                f"ranges={self.ranges}, count={count}, attempts={attempts}"
            )

    def close(self) -> None:
        """所有服务退出后释放租约；失败也继续释放其余端口。"""
        with _allocation_lock:
            self._resources.close()
            self._ports.clear()


_plans: dict[Path, TestFilePorts] = {}


def ports_for_file(
    test_file_path: Path,
    *,
    test_layer: Literal["integration", "e2e"],
    tests_root: Path,
) -> TestFilePorts:
    with _allocation_lock:
        root = tests_root.resolve()
        test_file = test_file_path.resolve()
        if not test_file.is_relative_to(root / test_layer) or not test_file.is_file():
            raise ValueError(f"{test_layer} 测试文件不在指定测试层或不存在: {test_file}")
        if test_file not in _plans:
            _plans[test_file] = TestFilePorts(test_file, _lease_root(Path.cwd().resolve()))
        return _plans[test_file]


def release_all_ports() -> None:
    # 引用其他文件的共享 fixture 也可能申请端口，最终由 session 兜底结清。
    with _allocation_lock, ExitStack() as cleanup:
        for plan in _plans.values():
            cleanup.callback(plan.close)
        _plans.clear()


def release_ports_for_file(test_file: Path) -> None:
    with _allocation_lock:
        plan = _plans.pop(test_file.resolve(), None)
        if plan is not None:
            plan.close()


def e2e_ports_for_file(test_file_path: Path, *, tests_root: Path | None = None) -> TestFilePorts:
    return ports_for_file(
        test_file_path, test_layer="e2e", tests_root=tests_root or Path.cwd() / "tests",
    )


def integration_ports_for_file(test_file_path: Path) -> TestFilePorts:
    return ports_for_file(
        test_file_path, test_layer="integration", tests_root=Path.cwd() / "tests",
    )
