from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.harness.python.run_context import TestRunContext
from tests.support import fork_deadline


@pytest.fixture
def c3_ssh_test_context(
    fork_deadline_test_context: TestRunContext,
) -> TestRunContext:
    context = fork_deadline_test_context
    ssh_root = context.artifacts_dir / "c3-ssh"
    ssh_root.mkdir(parents=True, exist_ok=True)
    (ssh_root / "sshd_config").write_text("Port 2222\n")
    return context


@pytest.fixture(scope="module")
def fork_deadline_test_context() -> TestRunContext:
    return TestRunContext.from_test_file(Path(__file__))


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc 身份解析")
def test_c3_linux_identity_uses_saved_launch_arguments_without_reading_cmdline(
    c3_ssh_test_context: TestRunContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = c3_ssh_test_context
    ssh_root = context.artifacts_dir / "c3-ssh"
    config_file = ssh_root / "sshd_config"
    pid = 42_001
    process_root = Path(f"/proc/{pid}")
    stat_file = process_root / "stat"
    cmdline_file = process_root / "cmdline"
    executable_file = process_root / "exe"
    executable = Path(sys.executable).resolve()
    start_ticks = "394396094"
    stat_fields = ["S", "1", str(pid), str(pid), *(["0"] * 15), start_ticks]
    launch_arguments = [str(executable), "-D", "-e", "-f", str(config_file)]
    process = SimpleNamespace(pid=pid, args=launch_arguments)
    original_read_text = Path.read_text
    original_read_bytes = Path.read_bytes
    original_stat = Path.stat
    original_resolve = Path.resolve

    def read_text(path: Path, *args: object, **kwargs: object) -> str:
        if path == stat_file:
            return f"{pid} (sshd) {' '.join(stat_fields)}"
        return original_read_text(path, *args, **kwargs)

    def read_bytes(path: Path, *args: object, **kwargs: object) -> bytes:
        if path == cmdline_file:
            raise AssertionError("身份复核不得读取已被 sshd 改写的 cmdline")
        return original_read_bytes(path, *args, **kwargs)

    def stat(path: Path, *args: object, **kwargs: object) -> os.stat_result:
        if path == process_root:
            return SimpleNamespace(st_uid=os.geteuid())  # type: ignore[return-value]
        return original_stat(path, *args, **kwargs)

    def resolve(path: Path, *args: object, **kwargs: object) -> Path:
        if path == executable_file:
            return executable
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setattr(Path, "stat", stat)
    monkeypatch.setattr(Path, "resolve", resolve)
    identity = fork_deadline._read_c3_sshd_identity(context.artifacts_dir, process, config_file)

    assert identity["launch_arguments"] == launch_arguments
    terminated: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(
        fork_deadline.os,
        "killpg",
        lambda pgid, sig: terminated.append((pgid, sig)),
    )
    fork_deadline._kill_owned_c3_sshd(identity)

    assert terminated == [(pid, signal.SIGKILL)]


def test_c3_non_linux_posix_identity_uses_psutil(
    c3_ssh_test_context: TestRunContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = c3_ssh_test_context
    ssh_root = context.artifacts_dir / "c3-ssh"
    config_file = ssh_root / "sshd_config"
    pid = 42_002
    executable = Path(sys.executable).resolve()
    launch_arguments = [str(executable), "-D", "-e", "-f", str(config_file)]
    process = SimpleNamespace(pid=pid, args=launch_arguments)
    create_time = 1_790_000_123.125
    process_probe = SimpleNamespace(
        create_time=lambda: create_time,
        uids=lambda: SimpleNamespace(effective=1000),
        exe=lambda: str(executable),
    )
    terminated: list[tuple[int, int]] = []
    monkeypatch.setattr(fork_deadline.sys, "platform", "darwin")
    monkeypatch.setattr(fork_deadline.psutil, "Process", lambda _pid: process_probe)
    monkeypatch.setattr(fork_deadline.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(fork_deadline.os, "getpgid", lambda _pid: pid, raising=False)
    monkeypatch.setattr(fork_deadline.os, "getsid", lambda _pid: pid, raising=False)
    monkeypatch.setattr(fork_deadline.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(
        fork_deadline.os,
        "killpg",
        lambda pgid, sig: terminated.append((pgid, sig)),
        raising=False,
    )

    identity = fork_deadline._read_c3_sshd_identity(
        context.artifacts_dir,
        process,
        config_file,
    )
    assert identity["process_start_marker"] == repr(create_time)
    assert identity["launch_arguments"] == launch_arguments

    fork_deadline._kill_owned_c3_sshd(identity)

    assert terminated == [(pid, 9)]


def test_c3_windows_identity_is_revalidated_before_process_tree_termination(
    c3_ssh_test_context: TestRunContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = c3_ssh_test_context
    ssh_root = context.artifacts_dir / "c3-ssh"
    config_file = ssh_root / "sshd_config"
    pid = 42_003
    executable = Path(sys.executable).resolve()
    launch_arguments = [str(executable), "-D", "-e", "-f", str(config_file)]
    process = SimpleNamespace(pid=pid, args=launch_arguments)
    start_time = [1_790_000_123.125]
    parent_probe = SimpleNamespace(
        pid=os.getpid(),
        create_time=lambda: 1_790_000_124.125,
        username=lambda: "test-user",
    )
    process_probe = SimpleNamespace(
        pid=pid,
        create_time=lambda: start_time[0],
        ppid=lambda: os.getpid(),
        username=lambda: "test-user",
        exe=lambda: str(executable),
    )
    terminated: list[tuple[int, dict[str, object]]] = []

    def get_process(process_id: int) -> SimpleNamespace:
        return parent_probe if process_id == os.getpid() else process_probe

    monkeypatch.setattr(fork_deadline.sys, "platform", "win32")
    monkeypatch.setattr(fork_deadline.psutil, "Process", get_process)
    monkeypatch.setattr(
        fork_deadline,
        "_terminate_windows_process_tree",
        lambda process_id, **identity: terminated.append((process_id, identity)),
    )

    identity = fork_deadline._read_c3_sshd_identity(
        context.artifacts_dir,
        process,
        config_file,
    )
    assert identity["process_start_marker"] == repr(start_time[0])
    assert identity["parent_process_id"] == os.getpid()
    assert identity["parent_start_marker"] == repr(parent_probe.create_time())

    fork_deadline._kill_owned_c3_sshd(identity)

    assert terminated == [(
        pid,
        {
            "expected_start_marker": repr(start_time[0]),
            "expected_parent_process_id": os.getpid(),
            "expected_parent_start_marker": repr(parent_probe.create_time()),
        },
    )]

    start_time[0] += 1
    with pytest.raises(RuntimeError, match="冻结的 C3 SSHD 进程身份不匹配"):
        fork_deadline._kill_owned_c3_sshd(identity)
    assert len(terminated) == 1


def test_watchdog_reads_windows_control_pipe_without_select(
    fork_deadline_test_context: TestRunContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = SimpleNamespace(
        buffer=SimpleNamespace(raw=[b'{"action":"stop"}\n']),
        fileno=lambda: 0,
    )
    monkeypatch.setattr(fork_deadline.sys, "platform", "win32")
    monkeypatch.setattr(fork_deadline.sys, "stdin", source)
    monkeypatch.setattr(
        fork_deadline.select,
        "select",
        lambda *_args: (_ for _ in ()).throw(AssertionError("Windows 管道不能交给 select")),
    )

    artifacts_dir = fork_deadline_test_context.artifacts_dir
    assert fork_deadline._watchdog_main(artifacts_dir, 1, 1) == 0


def test_watchdog_keeps_posix_select_control_pipe_path(
    fork_deadline_test_context: TestRunContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = SimpleNamespace(fileno=lambda: 29)
    monkeypatch.setattr(fork_deadline.sys, "platform", "linux")
    monkeypatch.setattr(fork_deadline.sys, "stdin", source)
    monkeypatch.setattr(fork_deadline.select, "select", lambda *_args: ([29], [], []))
    monkeypatch.setattr(fork_deadline.os, "read", lambda *_args: b'{"action":"stop"}\n')

    artifacts_dir = fork_deadline_test_context.artifacts_dir
    assert fork_deadline._watchdog_main(artifacts_dir, 1, 1) == 0


def test_windows_watchdog_timeout_requests_owned_process_tree_cleanup(
    fork_deadline_test_context: TestRunContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts_dir = fork_deadline_test_context.artifacts_dir
    now = time.monotonic()
    source = SimpleNamespace(
        buffer=SimpleNamespace(raw=[
            (
                json.dumps({
                    "action": "start",
                    "token": "expired",
                    "target": "target",
                    "operation_key": "operation",
                    "started_at": now - 2,
                    "deadline": now - 1,
                    "last_stage": "started",
                })
                + "\n"
            ).encode("utf-8")
        ]),
        fileno=lambda: 0,
    )
    terminated: list[tuple[int, int]] = []
    monkeypatch.setattr(fork_deadline.sys, "platform", "win32")
    monkeypatch.setattr(fork_deadline.sys, "stdin", source)
    monkeypatch.setattr(fork_deadline, "_write_exclusive_json", lambda *_args: None)
    monkeypatch.setattr(fork_deadline, "_write_watchdog_stderr", lambda *_args: None)
    monkeypatch.setattr(fork_deadline, "_terminate_process_group", lambda *args: terminated.append(args))
    monkeypatch.setattr(
        fork_deadline.select,
        "select",
        lambda *_args: (_ for _ in ()).throw(AssertionError("Windows 管道不能交给 select")),
    )

    assert fork_deadline._watchdog_main(artifacts_dir, 42, 42) == 137
    assert terminated == [(42, 42)]


def test_windows_process_tree_termination_skips_watchdog_and_kills_descendants_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actions: list[tuple[int, str]] = []

    class FakeProcess:
        def __init__(
            self,
            pid: int,
            start_time: float,
            children: list[FakeProcess] | None = None,
            *,
            resists_terminate: bool = False,
        ) -> None:
            self.pid = pid
            self._start_time = start_time
            self._children = children or []
            self._resists_terminate = resists_terminate
            self.running = True

        def create_time(self) -> float:
            return self._start_time

        def children(self, *, recursive: bool) -> list[FakeProcess]:
            assert recursive
            return self._children

        def is_running(self) -> bool:
            return self.running

        def terminate(self) -> None:
            actions.append((self.pid, "terminate"))
            if not self._resists_terminate:
                self.running = False

        def kill(self) -> None:
            actions.append((self.pid, "kill"))
            self.running = False

    watchdog = FakeProcess(os.getpid(), 100.0)
    child = FakeProcess(42_004, 101.0, resists_terminate=True)
    root = FakeProcess(42_005, 102.0, [child, watchdog])
    monkeypatch.setattr(
        fork_deadline.psutil,
        "Process",
        lambda process_id: root if process_id == root.pid else watchdog,
    )

    def wait_processes(processes: list[FakeProcess], **_kwargs) -> tuple[list[FakeProcess], list[FakeProcess]]:
        gone = [process for process in processes if not process.running]
        alive = [process for process in processes if process.running]
        return gone, alive

    monkeypatch.setattr(fork_deadline.psutil, "wait_procs", wait_processes)

    fork_deadline._terminate_windows_process_tree(
        root.pid,
        expected_start_marker=repr(root.create_time()),
    )

    assert actions == [
        (child.pid, "terminate"),
        (root.pid, "terminate"),
        (child.pid, "kill"),
    ]

    with pytest.raises(RuntimeError, match="已更换身份"):
        fork_deadline._terminate_windows_process_tree(
            root.pid,
            expected_start_marker="recycled-process-id",
        )
    assert actions[-1] == (child.pid, "kill")


def test_windows_watchdog_revalidates_matrix_owned_parent_before_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_id = 42_006
    parent_process = SimpleNamespace(create_time=lambda: 1_790_000_125.0)
    watchdog_process = SimpleNamespace(ppid=lambda: process_id)
    terminated: list[tuple[int, dict[str, object]]] = []

    def get_process(requested_process_id: int) -> SimpleNamespace:
        return watchdog_process if requested_process_id == os.getpid() else parent_process

    monkeypatch.setattr(fork_deadline.sys, "platform", "win32")
    monkeypatch.setattr(fork_deadline.psutil, "Process", get_process)
    monkeypatch.setattr(
        fork_deadline,
        "_has_matrix_runner_ancestor",
        lambda requested_process_id: requested_process_id == process_id,
    )
    monkeypatch.setattr(
        fork_deadline,
        "_terminate_windows_process_tree",
        lambda requested_process_id, **identity: terminated.append((requested_process_id, identity)),
    )

    fork_deadline._terminate_process_group(process_id, process_id)

    assert terminated == [
        (process_id, {"expected_start_marker": repr(parent_process.create_time())})
    ]


@pytest.mark.parametrize("fault", [None, "parent", "executable", "arguments", "matrix"])
def test_windows_watchdog_only_accepts_its_exact_venv_launcher(
    monkeypatch: pytest.MonkeyPatch, fault: str | None,
) -> None:
    protected_pid, launcher_pid = 42_010, 42_011
    arguments = ["fork_deadline.py", "--watchdog", "artifacts", str(protected_pid), str(protected_pid)]
    watchdog = SimpleNamespace(ppid=lambda: launcher_pid, cmdline=lambda: ["base-python", *arguments])
    launcher = SimpleNamespace(
        ppid=lambda: protected_pid + int(fault == "parent"),
        exe=lambda: str(Path(sys.executable).with_name("other-python")) if fault == "executable" else sys.executable,
        cmdline=lambda: [sys.executable, *arguments, *(["unexpected"] if fault == "arguments" else [])],
    )
    protected = SimpleNamespace(create_time=lambda: 1_790_000_126.0)
    processes = {os.getpid(): watchdog, launcher_pid: launcher, protected_pid: protected}
    terminated: list[int] = []
    monkeypatch.setattr(fork_deadline.sys, "platform", "win32")
    monkeypatch.setattr(fork_deadline.psutil, "Process", processes.__getitem__)
    monkeypatch.setattr(fork_deadline, "_has_matrix_runner_ancestor", lambda _pid: fault != "matrix")
    monkeypatch.setattr(fork_deadline, "_terminate_windows_process_tree", lambda pid, **_identity: terminated.append(pid))
    if fault is None:
        fork_deadline._terminate_process_group(protected_pid, protected_pid)
        assert terminated == [protected_pid]
    else:
        with pytest.raises(RuntimeError, match="watchdog 拒绝终止"):
            fork_deadline._terminate_process_group(protected_pid, protected_pid)
        assert terminated == []


def test_windows_watchdog_starts_in_its_own_process_group(
    fork_deadline_test_context: TestRunContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = SimpleNamespace(
        cmdline=lambda: ["bun", r"C:\repo\tests\runner\run-tests.mjs"]
    )
    test_process = SimpleNamespace(
        cmdline=lambda: ["python", "-m", "pytest"],
        parents=lambda: [runner],
    )
    spawned: list[tuple[list[str], dict[str, object]]] = []
    child = SimpleNamespace(poll=lambda: None, stdin=SimpleNamespace(write=lambda _data: None, flush=lambda: None))

    def popen(command: list[str], **options: object) -> SimpleNamespace:
        spawned.append((command, options))
        return child

    monkeypatch.setattr(fork_deadline.sys, "platform", "win32")
    monkeypatch.setattr(fork_deadline.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, raising=False)
    monkeypatch.setattr(fork_deadline.subprocess, "Popen", popen)
    monkeypatch.setattr(fork_deadline.psutil, "Process", lambda _pid: test_process)
    monkeypatch.setenv("BOXTEAM_TEST_SUITE", "unit-harness")
    monkeypatch.setenv("BOXTEAM_TEST_RUN_ID", "windows-watchdog-probe")

    artifacts_dir = fork_deadline_test_context.artifacts_dir
    watchdog = fork_deadline.ForkDeadlineWatchdog(artifacts_dir)

    assert watchdog._process is child
    assert spawned[0][1]["creationflags"] == 512
    assert "start_new_session" not in spawned[0][1]
