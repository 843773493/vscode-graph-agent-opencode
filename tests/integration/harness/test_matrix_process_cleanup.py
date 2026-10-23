"""真实矩阵入口的退出清理；只创建有限寿命的隔离Python进程。"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.harness.python.run_context import TestRunContext

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="本组真实进程状态核验使用Linux /proc")

WORKER = r"""
import json, os, signal, subprocess, sys, time
from pathlib import Path
role, mode, directory = sys.argv[1], sys.argv[2], Path(sys.argv[3])
deadline = time.monotonic() + 45
if role == 'descendant':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    (directory / 'ready.json').write_text(json.dumps({
        'pid': os.getpid(), 'leader': os.getppid(), 'pgid': os.getpgrp(), 'sid': os.getsid(0),
    }), encoding='utf-8')
else:
    (directory / 'leader.json').write_text(json.dumps({'pid': os.getpid(), 'pgid': os.getpgrp()}), encoding='utf-8')
    if mode in {'ignore_interrupt', 'repeat_interrupt'}:
        signal.signal(signal.SIGTERM, lambda *_: (directory / 'term-received').touch())
    subprocess.Popen([sys.executable, '-c', sys.argv[4], 'descendant', mode, str(directory)], stdin=subprocess.DEVNULL)
while time.monotonic() < deadline:
    if role == 'leader' and mode.startswith('normal') and (directory / 'finish').exists():
        sys.exit(7 if mode == 'normal_nonzero' else 0)
    time.sleep(0.01)
"""


def process_is_running(pid: int) -> bool:
    """僵尸已停止执行；回收其PID是父进程职责，不算仍运行的测试程序。"""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
    except FileNotFoundError:
        return False
    return fields[0] != "Z"


def wait_for_marker(path: Path, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 3
    while not path.exists():
        assert process.poll() is None, f"矩阵入口在标记写入前退出: {path}, exit={process.returncode}"
        assert time.monotonic() < deadline, f"等待隔离进程标记超时: {path}"
        time.sleep(0.01)


def stop_group(directory: Path) -> None:
    registration = directory / "leader.json"
    if not registration.exists():
        return
    owner = json.loads(registration.read_text(encoding="utf-8"))
    assert owner["pid"] == owner["pgid"] != os.getpgrp()
    try:
        os.killpg(owner["pgid"], signal.SIGKILL)
    except ProcessLookupError:
        pass


def stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.kill()
    process.wait(timeout=3)


@dataclass
class MatrixCase:
    mode: str
    directory: Path
    artifacts: Path
    runner: subprocess.Popen[bytes]
    unrelated: subprocess.Popen[bytes]
    facts: dict[str, int]


@pytest.fixture
def matrix_case(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[MatrixCase]:
    root = Path.cwd()
    mode = str(request.param)
    context = TestRunContext.from_test_file(Path(request.node.path))
    artifacts = context.artifacts_dir / f"run-{os.getpid()}" / request.node.name
    artifacts.mkdir(parents=True)
    matrix_directory = tmp_path / "tests/runner"
    matrix_directory.mkdir(parents=True)
    matrix = {"schemaVersion": 1, "suites": [{
        "id": "owned-process", "enabled": True, "dataLimitKb": 262144,
        "timeoutMs": 500 if mode == "timeout" else 40_000,
        "command": [sys.executable, "-c", WORKER, "leader", mode, str(tmp_path), WORKER],
    }]}
    (matrix_directory / "matrix.jsonc").write_text(json.dumps(matrix), encoding="utf-8")
    with ExitStack() as lifetime:
        unrelated = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(45)"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        lifetime.callback(stop_process, unrelated)
        stdout = lifetime.enter_context((artifacts / "stdout.log").open("wb"))
        stderr = lifetime.enter_context((artifacts / "stderr.log").open("wb"))
        runner = subprocess.Popen(
            ["bun", str(root / "tests/runner/run-tests.mjs"), "--suite=owned-process"],
            cwd=tmp_path, env={**os.environ, "BOXTEAM_PROJECT_ROOT": str(tmp_path)},
            stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, start_new_session=True,
        )
        # 两个释放动作独立登记，断言失败也不留下本用例的隔离进程。
        lifetime.callback(stop_group, tmp_path)
        lifetime.callback(stop_process, runner)
        wait_for_marker(tmp_path / "ready.json", runner)
        facts = json.loads((tmp_path / "ready.json").read_text(encoding="utf-8"))
        assert facts["leader"] == facts["pgid"] == facts["sid"] != os.getpgrp()
        yield MatrixCase(mode, tmp_path, artifacts, runner, unrelated, facts)


@pytest.mark.parametrize(("matrix_case", "expected_exit"), [
    pytest.param("normal", 0, id="normal-success"),
    pytest.param("normal_nonzero", 7, id="normal-failure"),
    pytest.param("timeout", 3, id="suite-timeout"),
    pytest.param("sigint", 130, id="sigint"),
    pytest.param("sigterm", 130, id="sigterm"),
    pytest.param("repeat_interrupt", 130, id="second-interrupt"),
    pytest.param("ignore_interrupt", 130, id="interrupt-grace-expired"),
], indirect=["matrix_case"])
def test_matrix_exit_stops_only_its_owned_process_group(matrix_case: MatrixCase, expected_exit: int) -> None:
    case = matrix_case
    started = time.monotonic()
    if case.mode.startswith("normal"):
        (case.directory / "finish").touch()
    elif case.mode != "timeout":
        case.runner.send_signal(signal.SIGTERM if case.mode == "sigterm" else signal.SIGINT)
        if case.mode == "repeat_interrupt":
            wait_for_marker(case.directory / "term-received", case.runner)
            assert case.runner.poll() is None
            case.runner.send_signal(signal.SIGINT)
    code = case.runner.wait(timeout=35 if case.mode == "ignore_interrupt" else 5)
    deadline = time.monotonic() + 1
    live = []
    while True:
        live = [pid for pid in (case.facts["leader"], case.facts["pid"]) if process_is_running(pid)]
        if not live or time.monotonic() >= deadline:
            break
        time.sleep(0.01)
    result = {
        "mode": case.mode, "exit_code": code, "elapsed_seconds": time.monotonic() - started,
        "facts": case.facts, "live_after_runner_exit": live,
        "unrelated_process_alive": case.unrelated.poll() is None,
    }
    (case.artifacts / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    assert code == expected_exit
    assert not live, result
    assert result["unrelated_process_alive"], "矩阵入口影响了另一个隔离进程组"
    if case.mode == "timeout":
        output = (case.artifacts / "stdout.log").read_text(encoding="utf-8")
        receipt = json.loads(output.strip())
        assert receipt["status"] == "TIMEOUT"
        assert receipt["timeoutMs"] == 500
