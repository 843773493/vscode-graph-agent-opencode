"""用真实子进程核目标测试保护器，不启动产品服务或访问远端目标。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="必须在 Windows 上验 Job Object")


@pytest.fixture
def target_guard():
    spec = importlib.util.spec_from_file_location(
        "windows_target_guard", Path.cwd() / "tools/cross-platform-development-targets/windows/run-tests.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_target_guard_preserves_exit_code(target_guard):
    assert target_guard.run_command([sys.executable, "-c", "raise SystemExit(7)"]) == 7


def test_target_guard_stops_blocked_process(target_guard, capsys):
    assert target_guard.run_command([sys.executable, "-c", "import time; time.sleep(60)"], timeout_seconds=0.3) == 124
    assert "测试超时" in capsys.readouterr().err


def test_target_guard_stops_memory_growth(target_guard, capsys):
    command = [sys.executable, "-c", "import time; data=bytearray(128*1024**2); time.sleep(60)"]
    assert target_guard.run_command(command, timeout_seconds=10, memory_bytes=64 * 1024**2) == 137
    assert "内存超限" in capsys.readouterr().err


def test_target_guard_reaps_descendant_after_root_exits(target_guard, tmp_path):
    import psutil

    marker = tmp_path / "descendant.pid"
    command = [sys.executable, "-c", (
        "import pathlib,subprocess,sys; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid),encoding='ascii')"
    ), str(marker)]
    try:
        assert target_guard.run_command(command) == 0
        assert not psutil.pid_exists(int(marker.read_text(encoding="ascii")))
    finally:
        if marker.exists() and psutil.pid_exists(pid := int(marker.read_text(encoding="ascii"))):
            psutil.Process(pid).kill()


@pytest.mark.parametrize("failure_stage", ["notification", "assignment"])
def test_target_guard_reaps_root_when_observation_fails(target_guard, monkeypatch, failure_stage):
    import psutil

    roots = []
    create_process = target_guard.win32process.CreateProcess

    def capture_process(*args):
        result = create_process(*args)
        roots.append(psutil.Process(result[2]))
        return result

    def fail_notification(*args):
        raise OSError("注入 Job 通知读取失败")

    monkeypatch.setattr(target_guard.win32process, "CreateProcess", capture_process)
    if failure_stage == "notification":
        monkeypatch.setattr(target_guard, "_next_notification", fail_notification)
    else:
        monkeypatch.setattr(target_guard.win32job, "AssignProcessToJobObject", fail_notification)
    try:
        with pytest.raises(OSError, match="注入 Job 通知读取失败"):
            target_guard.run_command([sys.executable, "-c", "import time; time.sleep(60)"])
        assert len(roots) == 1 and not roots[0].is_running()
    finally:
        for process in roots:
            if process.is_running():
                process.kill()
