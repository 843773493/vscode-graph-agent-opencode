"""真实 pytest 子进程核对跨模块复用，构建命令使用显式替身。"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def workbench_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    tests = project / "tests"
    tests.mkdir(parents=True)
    (project / "src/clients/workbench").mkdir(parents=True)
    (tests / "conftest.py").write_text('''
import json
import os
import subprocess
from pathlib import Path
import pytest
from tests.harness.python import web_workbench

pytest_plugins = [
    "tests.harness.python.integration_gate",
    "tests.harness.python.web_workbench",
]

@pytest.fixture(scope="session", autouse=True)
def build_command_stub():
    original = subprocess.run

    def build(command, **kwargs):
        if command[:3] != ["bun", "run", "build"]:
            return original(command, **kwargs)
        calls = Path.cwd() / "build-calls.jsonl"
        with calls.open("a", encoding="utf-8") as output:
            output.write(json.dumps(command) + "\\n")
        kwargs["stdout"].write("显式构建替身，只应执行一次\\n")
        assert kwargs["timeout"] == 180
        mode = os.environ["WORKBENCH_BUILD_TEST_MODE"]
        if mode == "timeout":
            raise subprocess.TimeoutExpired(command, 180)
        if mode == "failure":
            return subprocess.CompletedProcess(command, 7)
        assets = Path(command[-1])
        (assets / "out").mkdir(parents=True)
        (assets / "version.json").write_text('{"commit":"fixture-upstream"}')
        if mode != "incomplete":
            (assets / "out/main.js").write_bytes(b"fixture build")
        return subprocess.CompletedProcess(command, 0)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(web_workbench.subprocess, "run", build)
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
    return project


@pytest.mark.parametrize("mode", ["success", "failure", "timeout", "incomplete"])
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
    assert result.returncode == (0 if mode == "success" else 1), log
    assert ("4 passed" if mode == "success" else "4 errors") in log, log
    calls = (project / "build-calls.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(calls) == 1, "共享 fixture 的失败也必须由 pytest 缓存，不能按模块重复构建"
    receipts = list((project / "out/tests/harness/python/web_workbench").rglob("workbench-build.json"))
    assert len(receipts) == 1
    receipt_path, = receipts
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["command"] == json.loads(calls[0])
    assert receipt["timeout_seconds"] == 180
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
        assert receipt["exit_code"] is None and "timed out after 180 seconds" in receipt["error"]
        assert "TimeoutExpired" in log
    else:
        assert receipt["exit_code"] == 0 and "FileNotFoundError" in log
    if mode != "success":
        assert not list(project.glob("test_*-*.json")), "构建失败不得进入用例或复用旧产物"
