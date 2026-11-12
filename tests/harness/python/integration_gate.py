from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import portalocker
import pytest
from _pytest.terminal import TerminalReporter

from tests.harness.performance.record import stage


def integration_gate_path(project_root: Path) -> Path:
    """同一 Git 仓库的 worktree 共用主工作树中的控制锁；源码归档使用显式根。"""
    root = project_root.resolve()
    if (root / ".git").exists():
        worktrees = subprocess.run(
            ["git", "-C", str(root), "worktree", "list", "--porcelain", "-z"],
            check=True, capture_output=True, encoding="utf-8", timeout=10,
        ).stdout
        first_record = worktrees.split("\0", 1)[0]
        if not first_record.startswith("worktree "):
            raise ValueError(f"Git 未返回主工作树位置: {first_record!r}")
        root = Path(first_record.removeprefix("worktree "))
        if not root.is_absolute():
            raise ValueError(f"Git 主工作树位置不是绝对路径: {first_record!r}")
    return root / "out/tests/harness/python/integration_gate/runtime/session.lock"


def integration_gate(lock_path: Path, *, timeout: float = 600) -> portalocker.Lock:
    """一次放行一个 pytest 进程，正常退出和进程终止均由 OS 释放锁。"""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # 锁文件不能删除，否则既有等待者和后来者可能锁住不同 inode。
    return portalocker.Lock(
        lock_path, timeout=timeout, raise_on_release_error=True,
    )


@pytest.fixture(scope="session", autouse=True)
def integration_test_gate(request: pytest.FixtureRequest) -> Iterator[None]:
    """集成/E2E 在任何模块装具之前取得跨进程名额；单测不占用。"""
    root = Path.cwd().resolve()
    if not any(
        item.path.is_relative_to(root / "tests" / layer)
        for item in request.session.items
        for layer in ("integration", "e2e")
    ):
        yield
        return
    lock_path = integration_gate_path(root)
    reporter = request.config.pluginmanager.getplugin("terminalreporter")
    if isinstance(reporter, TerminalReporter):
        reporter.write_line(f"集成/E2E 等待独占运行名额（最多600秒）: {lock_path}")
    lock = integration_gate(lock_path)
    try:
        with stage("integration.gate.wait", scope="session"):
            lock.acquire()
        if isinstance(reporter, TerminalReporter):
            reporter.write_line("集成/E2E 已取得运行名额；本仓库其他测试进程继续等待")
        yield
    finally:
        lock.release()
