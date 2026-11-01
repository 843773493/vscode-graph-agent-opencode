"""真实 pytest 与构建树核对共享及回收；构建正文为显式替身。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import psutil
import pytest


def _owned_build_processes(project: Path) -> list[psutil.Process]:
    marker = project / "build-processes.json"
    if not marker.exists():
        return []
    result = []
    for identity in json.loads(marker.read_text()):
        try:
            process = psutil.Process(identity["pid"])
            if process.create_time() == identity["created_at"] and process.status() != psutil.STATUS_ZOMBIE:
                result.append(process)
        except psutil.NoSuchProcess:
            continue
    return result


@pytest.fixture
def workbench_project(tmp_path: Path) -> Iterator[Path]:
    project = tmp_path / "project"
    tests = project / "tests"
    tests.mkdir(parents=True)
    (project / "src/clients/workbench").mkdir(parents=True)
    runtime = project / "app/runtime"
    runtime.mkdir(parents=True)
    for guard in ("posix_job.py", "windows_job.py"):
        shutil.copyfile(Path.cwd() / "app/runtime" / guard, runtime / guard)
    (project / "build_fixture.py").write_text('''
import json
import os
import subprocess
import sys
import time
from pathlib import Path
import psutil

assets, marker, mode = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
marker.write_text(json.dumps([{"pid": pid, "created_at": psutil.Process(pid).create_time()}
                              for pid in (os.getpid(), child.pid)]))
print("显式构建替身，只应执行一次", flush=True)
if mode in ("timeout", "interrupted"):
    time.sleep(60)
if mode == "failure":
    sys.exit(7)
(assets / "out").mkdir(parents=True)
(assets / "version.json").write_text('{"commit":"fixture-upstream"}')
if mode != "incomplete":
    (assets / "out/main.js").write_bytes(b"fixture build")
''', encoding="utf-8")
    (tests / "conftest.py").write_text('''
import json
import os
import subprocess
import sys
import time
from pathlib import Path
import pytest
from tests.harness.python import web_workbench

pytest_plugins = [
    "tests.harness.python.integration_gate",
    "tests.harness.python.web_workbench",
]

@pytest.fixture(scope="session", autouse=True)
def build_command_stub():
    original = subprocess.Popen
    mode = os.environ["WORKBENCH_BUILD_TEST_MODE"]
    project = Path.cwd()

    def build(command, **kwargs):
        offset = command.index("--") + 1 if "--" in command else 0
        build_command = command[offset:]
        if build_command[:3] != ["bun", "run", "build"]:
            return original(command, **kwargs)
        calls = project / "build-calls.jsonl"
        with calls.open("a", encoding="utf-8") as output:
            output.write(json.dumps(build_command) + "\\n")
        marker = project / "build-processes.json"
        child_command = [sys.executable, str(project / "build_fixture.py"), build_command[-1], str(marker), mode]
        process = original([*command[:offset], *child_command], **kwargs)
        if mode == "interrupted":
            wait = process.wait
            first = True

            def interrupt_wait(timeout=None):
                nonlocal first
                if first:
                    first = False
                    deadline = time.monotonic() + 5
                    while not marker.exists():
                        assert process.poll() is None
                        assert time.monotonic() < deadline
                        time.sleep(0.01)
                    raise KeyboardInterrupt("构建调用者中断")
                return wait(timeout=timeout)

            process.wait = interrupt_wait
        return process

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(web_workbench, "WORKBENCH_BUILD_TIMEOUT_SECONDS", 3, raising=False)
        patch.setattr(web_workbench.subprocess, "Popen", build)
        yield
''', encoding="utf-8")
    for name in ("first", "second"):
        (tests / f"test_{name}.py").write_text('''
import json
from pathlib import Path
import pytest
from tests.harness.python.web_workbench import web_workbench_assets as web_workbench_assets

@pytest.mark.parametrize("invocation", [1, 2])
def test_assets(web_workbench_assets, invocation):
    assert (web_workbench_assets / "out/main.js").read_bytes() == b"fixture build"
    Path(f"{Path(__file__).stem}-{invocation}.json").write_text(json.dumps(str(web_workbench_assets)))
''', encoding="utf-8")
    try:
        yield project
    finally:
        # 先断言监督器的结果，再按PID及创建时刻兜底；不以兜底冒充产品回收。
        for process in _owned_build_processes(project):
            process.kill()


@pytest.mark.parametrize("mode", ["success", "failure", "timeout", "incomplete", "interrupted"])
def test_one_build_per_pytest_session_and_original_failures(
    workbench_project: Path, mode: str,
) -> None:
    project = workbench_project
    log_path = project / "pytest.log"
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--import-mode=importlib", "tests"]
    with log_path.open("x", encoding="utf-8") as output:
        result = subprocess.run(
            command, cwd=project, stdout=output, stderr=subprocess.STDOUT, timeout=30,
            env=os.environ | {
                "PYTHONPATH": str(Path.cwd()), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                "WORKBENCH_BUILD_TEST_MODE": mode,
            }, check=False,
        )
    (project / "pytest-result.json").write_text(json.dumps({
        "command": command, "cwd": str(project), "mode": mode,
        "exit_code": result.returncode, "log": str(log_path),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log = log_path.read_text(encoding="utf-8")
    deadline = time.monotonic() + 2
    while (live := _owned_build_processes(project)) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not live, f"构建已经退出，仍遗留父子进程: {[process.pid for process in live]}"
    assert result.returncode == {"success": 0, "interrupted": 2}.get(mode, 1), log
    if mode != "interrupted":
        assert ("4 passed" if mode == "success" else "4 errors") in log, log
    calls = (project / "build-calls.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(calls) == 1, "共享 fixture 的失败也必须由 pytest 缓存，不能按模块重复构建"
    receipts = list((project / "out/tests/harness/python/web_workbench").rglob("workbench-build.json"))
    assert len(receipts) == 1
    receipt_path, = receipts
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["command"] == json.loads(calls[0])
    assert receipt["timeout_seconds"] == 3
    assert receipt["memory_limit_kb"] == 4194304
    assert receipt["supervised_command"][-len(receipt["command"]):] == receipt["command"]
    assert receipt["elapsed_seconds"] >= 0
    assert "只应执行一次" in Path(receipt["log"]).read_text(encoding="utf-8")
    if mode == "success":
        assert receipt["exit_code"] == 0
        assert receipt["main_sha256"] == hashlib.sha256(b"fixture build").hexdigest()
        observed = [json.loads(path.read_text(encoding="utf-8")) for path in project.glob("test_*-*.json")]
        assert len(observed) == 4 and set(observed) == {receipt["assets"]}
        for name in ("first", "second"):
            module_receipts = list((project / f"out/tests/test_{name}").rglob("workbench-build.json"))
            assert len(module_receipts) == 1
            consumed = json.loads(module_receipts[0].read_text(encoding="utf-8"))
            assert consumed["shared_build_receipt"] == str(receipt_path)
            assert consumed["assets"] == receipt["assets"]
            assert consumed["main_sha256"] == receipt["main_sha256"]
            assert Path(consumed["log"]).read_bytes() == Path(receipt["log"]).read_bytes()
    elif mode == "failure":
        assert receipt["exit_code"] == 7 and "Workbench 构建失败" in log
    elif mode == "timeout":
        assert receipt["exit_code"] == receipt["supervisor_exit_code"] == 124
        assert "timed out after 3 seconds" in receipt["error"]
        assert "TimeoutExpired" in log
    elif mode == "interrupted":
        # Windows终止监督器后由Job句柄关闭回收；POSIX则由TERM处理器返回130。
        expected_exit = 1 if sys.platform == "win32" else 130
        assert receipt["exit_code"] is None and receipt["supervisor_exit_code"] == expected_exit
        assert "KeyboardInterrupt" in log
    else:
        assert receipt["exit_code"] == 0 and "FileNotFoundError" in log
    if mode != "success":
        assert not list(project.glob("test_*-*.json")), "构建失败不得进入用例或复用旧产物"
