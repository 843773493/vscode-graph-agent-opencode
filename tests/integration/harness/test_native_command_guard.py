"""真实命令监督器的标准流、期限与后代回收，不启动产品服务。"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import psutil
import pytest

from tests.harness.python.run_context import TestRunContext


@pytest.fixture
def guard_case(request: pytest.FixtureRequest) -> Iterator[tuple[list[str], Path]]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    directory = context.artifacts_dir
    guard = "windows_job.py" if sys.platform == "win32" else "posix_job.py"
    command = [sys.executable, str(Path.cwd() / "app/runtime" / guard),
               "--memory-limit-kb", "262144"]
    try:
        yield command, directory
    finally:
        # 先由用例断言监督器清理结果，再由fixture兜底，不伪造保护成功。
        marker = directory / "processes.json"
        if marker.exists():
            for pid in json.loads(marker.read_text()):
                if _running(pid):
                    psutil.Process(pid).kill()


def _running(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def test_guard_preserves_stdin_stdout_stderr_and_workspace(guard_case) -> None:
    guard, directory = guard_case
    program = ("import json,os,sys; value=json.load(sys.stdin); "
               "print(json.dumps({'input':value,'cwd':os.getcwd()})); "
               "print('诊断',file=sys.stderr)")
    result = subprocess.run(
        [*guard, "--timeout-seconds", "5", "--", sys.executable, "-c", program],
        cwd=directory, input=b'{"evaluation":"one"}', capture_output=True, timeout=10, check=False,
    )
    (directory / "stdout.raw").write_bytes(result.stdout)
    (directory / "stderr.raw").write_bytes(result.stderr)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"input": {"evaluation": "one"}, "cwd": str(directory)}
    assert result.stderr


@pytest.mark.parametrize("mode", ["normal", "failure", "timeout", "interrupt"])
def test_guard_reaps_tree_after_all_exit_paths(guard_case, mode: str) -> None:
    if mode == "interrupt" and sys.platform == "win32":
        pytest.skip("Windows通过关闭Job回收；POSIX另验TERM信号")
    guard, directory = guard_case
    marker = directory / "processes.json"
    program = (
        "import json,os,pathlib,subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(45)']); "
        "pathlib.Path(sys.argv[1]).write_text(json.dumps([os.getpid(),child.pid])); "
        "time.sleep(45) if sys.argv[2] in ('timeout','interrupt') else None; "
        "sys.exit(7 if sys.argv[2]=='failure' else 0)"
    )
    with (directory / "output.raw").open("wb") as output:
        process = subprocess.Popen(
            [*guard, "--timeout-seconds", "1" if mode == "timeout" else "5", "--",
             sys.executable, "-c", program, str(marker), mode],
            stdin=subprocess.DEVNULL, stdout=output, stderr=output,
        )
        try:
            deadline = time.monotonic() + 3
            while not marker.exists():
                assert process.poll() is None
                assert time.monotonic() < deadline
                time.sleep(0.01)
            if mode == "interrupt":
                process.send_signal(signal.SIGTERM)
            code = process.wait(timeout=10)
            pids = json.loads(marker.read_text())
            deadline = time.monotonic() + 2
            while any(_running(pid) for pid in pids) and time.monotonic() < deadline:
                time.sleep(0.01)
            observed = {"exit_code": code, "live_after_guard_exit": [pid for pid in pids if _running(pid)]}
            (directory / "result.json").write_text(json.dumps(observed))
            assert code == {"normal": 0, "failure": 7, "timeout": 124, "interrupt": 130}[mode]
            assert not observed["live_after_guard_exit"], observed
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)


def test_guard_enforces_memory_limit(guard_case) -> None:
    guard, directory = guard_case
    # 留出Python启动所需空间；脚本分配超过64MiB应明确失败。
    guard[guard.index("262144")] = "65536"
    result = subprocess.run(
        [*guard, "--timeout-seconds", "5", "--", sys.executable, "-c",
         "import time; data=bytearray(128*1024**2); time.sleep(45)"],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=10, check=False,
    )
    (directory / "stdout.raw").write_bytes(result.stdout)
    (directory / "stderr.raw").write_bytes(result.stderr)
    assert result.returncode == (137 if sys.platform == "win32" else 1), result.stderr
    assert result.stderr
