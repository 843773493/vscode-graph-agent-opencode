from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

_WINDOWS_PROCESS_TREE_PARENT = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, '-u', '-c', 'import time; time.sleep(600)'])
with open(sys.argv[1], 'w', encoding='ascii') as marker:
    marker.write(str(child.pid))
    marker.flush()
time.sleep(600)
"""


@dataclass(frozen=True, slots=True)
class WindowsProcessTree:
    root: subprocess.Popen[str]
    child_pid: int
    child_start_time: float


def _same_process_is_running(pid: int, start_time: float) -> bool:
    import psutil

    try:
        process = psutil.Process(pid)
        return process.create_time() == start_time and process.is_running()
    except psutil.NoSuchProcess:
        return False


def _wait_for_windows_process_exit(pid: int, start_time: float, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while _same_process_is_running(pid, start_time):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.05, remaining))
    return True


@pytest.fixture
def windows_process_tree(tmp_path: Path) -> Iterator[WindowsProcessTree]:
    """创建独立的父子进程树，并只回收本 fixture 启动的 PID 实例。"""
    import psutil

    child_pid_file = tmp_path / "child.pid"
    root = subprocess.Popen(
        [sys.executable, "-u", "-c", _WINDOWS_PROCESS_TREE_PARENT, str(child_pid_file)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    child_pid: int | None = None
    child_start_time: float | None = None
    try:
        deadline = time.monotonic() + 10
        while not child_pid_file.is_file():
            if root.poll() is not None:
                pytest.fail(f"测试父进程过早退出: exit_code={root.returncode}")
            if time.monotonic() >= deadline:
                pytest.fail("测试父进程未能启动子进程")
            time.sleep(0.02)

        child_pid = int(child_pid_file.read_text(encoding="ascii"))
        child_start_time = psutil.Process(child_pid).create_time()
        yield WindowsProcessTree(root, child_pid, child_start_time)
    finally:
        if (
            child_pid is not None
            and child_start_time is not None
            and _same_process_is_running(child_pid, child_start_time)
        ):
            psutil.Process(child_pid).kill()
        if root.poll() is None:
            try:
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(root.pid)],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
            except OSError:
                if root.poll() is None:
                    root.kill()
        if root.poll() is None:
            root.kill()
        root.wait(timeout=5)
        if (
            child_pid is not None
            and child_start_time is not None
            and not _wait_for_windows_process_exit(child_pid, child_start_time, 5)
        ):
            raise RuntimeError(f"测试子进程未能回收: pid={child_pid}")


@pytest.fixture
def release_process_tree() -> Callable[[int], dict[str, object]]:
    node = shutil.which("node")
    assert node is not None, "未找到 Terminal 使用的 Node 运行时"
    module = (
        Path.cwd()
        / "src/workspace-services/terminal/server/processes/terminalProcessLifecycle.js"
    ).as_uri()
    script = f"""
import {{ readProcessStat, terminateTerminalProcessTree }} from {json.dumps(module)};
const pid = Number(process.argv[1]);
const before = await readProcessStat(pid);
const result = await terminateTerminalProcessTree({{
  pid,
  processSessionId: before?.processSessionId ?? null,
  processStartTime: before?.processStartTime ?? null,
}});
console.log(JSON.stringify({{ result, before }}));
"""

    def release(pid: int) -> dict[str, object]:
        result = subprocess.run(
            [node, "--input-type=module", "-e", script, str(pid)],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        return json.loads(result.stdout)

    return release


@pytest.mark.skipif(sys.platform != "win32", reason="需要 Windows 原生 taskkill 进程树")
def test_windows_release_terminates_root_and_descendant(
    windows_process_tree: WindowsProcessTree,
    release_process_tree: Callable[[int], dict[str, object]],
) -> None:
    tree = windows_process_tree
    release_result = release_process_tree(tree.root.pid)

    assert release_result["result"] == "terminated"
    assert release_result["before"] is None
    assert tree.root.wait(timeout=5) is not None
    assert _wait_for_windows_process_exit(tree.child_pid, tree.child_start_time, 5)
