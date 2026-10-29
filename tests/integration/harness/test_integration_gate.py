from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tests.harness.python.integration_gate import integration_gate_path

RepositoryFactory = Callable[[str], tuple[Path, Path]]
ProcessStarter = Callable[[Path, str, list[str]], tuple[subprocess.Popen[bytes], Path]]

_PROBE = '''from pathlib import Path
import time
import pytest

@pytest.fixture(scope="module", autouse=True)
def service_lifetime():
    output = Path.cwd() / "out"
    output.mkdir(exist_ok=True)
    (output / "started").write_text(str(time.monotonic()), encoding="utf-8")
    yield
    (output / "closed").write_text(str(time.monotonic()), encoding="utf-8")

def test_probe():
    deadline = time.monotonic() + 20
    while not (Path.cwd() / "out/release").exists():
        assert time.monotonic() < deadline, "父级探针未释放测试"
        time.sleep(0.025)
'''


@pytest.fixture
def gate_repositories(tmp_path: Path) -> RepositoryFactory:
    def prepare(layer: str) -> tuple[Path, Path]:
        main = tmp_path / "main repo"
        main.mkdir()
        tests = main / "tests"
        (tests / layer).mkdir(parents=True)
        (tests / "conftest.py").write_text(
            "pytest_plugins = ['tests.harness.python.integration_gate']\n",
            encoding="utf-8",
        )
        (tests / layer / "test_probe.py").write_text(_PROBE, encoding="utf-8")
        for command in (
            ["git", "init", "-q", str(main)],
            ["git", "-C", str(main), "add", "tests"],
            ["git", "-C", str(main), "-c", "user.name=Gate Test", "-c",
             "user.email=gate@example.invalid", "commit", "-qm", "test fixture"],
        ):
            subprocess.run(command, check=True, capture_output=True, timeout=10)
        linked = tmp_path / "linked repo"
        subprocess.run(
            ["git", "-C", str(main), "worktree", "add", "--detach", str(linked)],
            check=True, capture_output=True, timeout=10,
        )
        return main, linked

    return prepare


@pytest.fixture
def gate_processes(tmp_path: Path) -> Iterator[ProcessStarter]:
    processes: list[subprocess.Popen[bytes]] = []

    def start(root: Path, layer: str, options: list[str]) -> tuple[subprocess.Popen[bytes], Path]:
        output = tmp_path / f"pytest-{len(processes)}.log"
        with output.open("wb") as stream:
            process = subprocess.Popen(
                [sys.executable, "-m", "pytest", "-q", "-s", "-p", "no:cacheprovider",
                 "--import-mode=importlib", *options, f"tests/{layer}"],
                cwd=root, stdout=stream, stderr=subprocess.STDOUT,
                env=os.environ | {"PYTHONPATH": str(Path.cwd())},
            )
        processes.append(process)
        return process, output

    try:
        yield start
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)


def _wait_for(path: Path, process: subprocess.Popen[bytes], text: str | None = None) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if path.is_file() and (text is None or text in path.read_text(encoding="utf-8")):
            return
        assert process.poll() is None, f"子级 pytest 提前退出: {process.returncode}; {path}"
        time.sleep(0.025)
    pytest.fail(f"未观察到子级 pytest 状态: {path}; {text}")


def _assert_waiting(root: Path, process: subprocess.Popen[bytes], log: Path) -> None:
    _wait_for(log, process, "等待独占运行名额")
    # 已完成导入/收集后留出装具执行窗口，避免仅凭排队日志抢先判定阻塞。
    with pytest.raises(subprocess.TimeoutExpired):
        process.wait(timeout=0.3)
    assert not (root / "out/started").exists()


@pytest.mark.parametrize("layer", ["integration", "e2e"])
def test_worktrees_wait_before_service_fixtures_and_release_after_cleanup(
    gate_repositories: RepositoryFactory, gate_processes: ProcessStarter, layer: str,
) -> None:
    main, linked = gate_repositories(layer)
    assert integration_gate_path(main) == integration_gate_path(linked)
    first, first_log = gate_processes(main, layer, [])
    _wait_for(main / "out/started", first)
    second, second_log = gate_processes(linked, layer, [])
    _assert_waiting(linked, second, second_log)
    (main / "out/release").touch()
    assert first.wait(timeout=10) == 0, first_log.read_text(encoding="utf-8")
    _wait_for(linked / "out/started", second)
    assert float((main / "out/closed").read_text()) <= float((linked / "out/started").read_text())
    (linked / "out/release").touch()
    assert second.wait(timeout=10) == 0, second_log.read_text(encoding="utf-8")


def test_killed_pytest_releases_os_lock_for_waiting_worktree(
    gate_repositories: RepositoryFactory, gate_processes: ProcessStarter,
) -> None:
    main, linked = gate_repositories("integration")
    first, _ = gate_processes(main, "integration", [])
    _wait_for(main / "out/started", first)
    second, second_log = gate_processes(linked, "integration", [])
    _assert_waiting(linked, second, second_log)
    first.kill()
    assert first.wait(timeout=10) != 0
    _wait_for(linked / "out/started", second)
    assert not (main / "out/closed").exists()
    (linked / "out/release").touch()
    assert second.wait(timeout=10) == 0, second_log.read_text(encoding="utf-8")


def test_unit_only_pytest_does_not_take_integration_slot(
    gate_repositories: RepositoryFactory, gate_processes: ProcessStarter,
) -> None:
    main, linked = gate_repositories("unit")
    first, first_log = gate_processes(main, "unit", [])
    _wait_for(main / "out/started", first)
    second, second_log = gate_processes(linked, "unit", [])
    _wait_for(linked / "out/started", second)
    assert not (main / "out/closed").exists()
    for root in (main, linked):
        (root / "out/release").touch()
    for process, log in ((first, first_log), (second, second_log)):
        assert process.wait(timeout=10) == 0, log.read_text(encoding="utf-8")
        assert "等待独占运行名额" not in log.read_text(encoding="utf-8")


def test_xdist_workers_share_one_slot_without_deadlocking(
    gate_repositories: RepositoryFactory, gate_processes: ProcessStarter,
) -> None:
    main, _ = gate_repositories("integration")
    probe = _PROBE.replace('output = Path.cwd() / "out"',
                           'output = Path.cwd() / "out" / Path(__file__).stem')
    probe = probe.replace('output.mkdir(exist_ok=True)', 'output.mkdir(parents=True, exist_ok=True)')
    probe = probe[:probe.index("def test_probe():")] + 'def test_probe():\n    time.sleep(0.1)\n'
    for name in ("test_first", "test_second"):
        (main / "tests/integration" / f"{name}.py").write_text(probe, encoding="utf-8")
    (main / "tests/integration/test_probe.py").unlink()
    process, log = gate_processes(main, "integration", ["-n", "2", "--dist=loadfile"])
    assert process.wait(timeout=20) == 0, log.read_text(encoding="utf-8")
    intervals = sorted(
        (float((main / "out" / name / "started").read_text()),
         float((main / "out" / name / "closed").read_text()))
        for name in ("test_first", "test_second")
    )
    assert intervals[0][1] <= intervals[1][0]
