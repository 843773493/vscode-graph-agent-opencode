from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="验证 Linux session 与僵尸进程")

_PROCESS_PARENT = """
import os, signal, sys
read_fd, write_fd = os.pipe()
child = os.fork()
if child == 0:
    os.close(read_fd)
    os.setsid()
    if sys.argv[1] == 'ignore-term':
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    os.write(write_fd, b'ready')
    os.close(write_fd)
    if sys.argv[1] == 'already-exited':
        os._exit(0)
    while True:
        signal.pause()
os.close(write_fd)
os.read(read_fd, 5)
os.close(read_fd)
print(child, flush=True)
sys.stdin.readline()
os.kill(child, signal.SIGKILL)
os.waitpid(child, 0)
"""


@pytest.fixture
def unreaped_process() -> Iterator[Callable[[str], int]]:
    """父进程暂缓 waitpid，使停止后的子进程保持真实的 Z 状态。"""
    parents: list[subprocess.Popen[str]] = []

    def start(mode: str) -> int:
        parent = subprocess.Popen(
            [sys.executable, "-u", "-c", _PROCESS_PARENT, mode],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        parents.append(parent)
        assert parent.stdout is not None
        ready, _, _ = select.select([parent.stdout], [], [], 5)
        assert ready, "测试进程未完成 session 隔离"
        child_pid = int(parent.stdout.readline())
        if mode == "already-exited":
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                state = Path(f"/proc/{child_pid}/stat").read_text().rsplit(") ", 1)[1].split()[0]
                if state == "Z":
                    return child_pid
                time.sleep(0.01)
            pytest.fail("测试子进程未进入等待回收状态")
        return child_pid

    try:
        yield start
    finally:
        for parent in parents:
            stdout, stderr = parent.communicate("\n", timeout=5)
            assert parent.returncode == 0, f"测试父进程未回收子进程：{stdout}\n{stderr}"


@pytest.fixture
def release_process() -> Callable[..., dict[str, object]]:
    node = shutil.which("node")
    assert node is not None, "未找到 Terminal 使用的 Node 运行时"
    module = (Path.cwd() / "src/workspace-services/terminal/server/processes/terminalProcessLifecycle.js").as_uri()
    script = f"""
import {{ readProcessStat, terminateTerminalProcessTree }} from {json.dumps(module)};
const pid = Number(process.argv[1]);
const before = await readProcessStat(pid);
const result = await terminateTerminalProcessTree({{
  pid,
  processSessionId: before.processSessionId,
  processStartTime: before.processStartTime + Number(process.argv[2]),
}});
console.log(JSON.stringify({{ result, after: await readProcessStat(pid) }}));
"""

    def release(pid: int, *, stale_identity: bool = False) -> dict[str, object]:
        result = subprocess.run(
            [node, "--input-type=module", "-e", script, str(pid), str(int(stale_identity))],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        return json.loads(result.stdout)

    return release


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("already-exited", "missing"), ("term-exits", "terminated"), ("ignore-term", "force_killed")],
)
def test_release_does_not_wait_for_parent_to_reap_exited_process(
    mode: str,
    expected: str,
    unreaped_process: Callable[[str], int],
    release_process: Callable[..., dict[str, object]],
) -> None:
    pid = unreaped_process(mode)
    result = release_process(pid)

    assert result["result"] == expected
    assert result["after"]["state"] == "Z"
    # 对象仍在进程表中，但已无法执行；终端不能把回收责任算成停止失败。
    os.kill(pid, 0)


def test_release_preserves_live_process_when_start_identity_is_stale(
    unreaped_process: Callable[[str], int],
    release_process: Callable[..., dict[str, object]],
) -> None:
    pid = unreaped_process("term-exits")
    result = release_process(pid, stale_identity=True)

    assert result["result"] == "missing"
    assert result["after"]["state"] not in {"Z", "X", "x"}
    os.kill(pid, 0)
