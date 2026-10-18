"""真实 fork 测试的单调期限和进程外看门狗。"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import secrets
import select
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Awaitable
from pathlib import Path
from typing import TypeVar

Result = TypeVar("Result")
FORK_TIMEOUT_SECONDS = 60.0


class ForkDeadlineExceeded(TimeoutError):
    pass


class ForkDeadlineWatchdog:
    """独立进程观察 deadline，避免事件循环和同步磁盘 I/O 阻塞计时。"""

    def __init__(self, artifacts_dir: Path) -> None:
        self.artifacts_dir = artifacts_dir.resolve()
        self._validate_artifacts_dir(self.artifacts_dir)
        suite_id = os.environ.get("BOXTEAM_TEST_SUITE")
        run_id = os.environ.get("BOXTEAM_TEST_RUN_ID")
        pgid = os.getpgrp()
        sid = os.getsid(0)
        parent_pid = os.getppid()
        if not suite_id or not run_id:
            raise RuntimeError("fork deadline watchdog 必须由正式 matrix runner 启动")
        if os.name != "posix" or pgid != sid or pgid <= 1:
            raise RuntimeError(
                f"拒绝保护非独立 matrix 测试进程组: pid={os.getpid()}, pgid={pgid}, sid={sid}"
            )
        leader_parent = _process_parent_pid(pgid)
        leader_parent_command = Path(f"/proc/{leader_parent}/cmdline").read_bytes()
        if b"tests/runner/run-tests.mjs" not in leader_parent_command:
            raise RuntimeError(
                "fork deadline watchdog 目标 PGID leader 必须由 tests/runner/run-tests.mjs 启动: "
                f"leader={pgid}, parent={leader_parent}, command={leader_parent_command!r}"
            )
        if os.getpgid(parent_pid) != pgid:
            raise RuntimeError("pytest 当前进程不属于其受保护的 matrix 子进程组")
        self._pgid = pgid
        self._lock = threading.Lock()
        self._process = subprocess.Popen(
            [
                sys.executable, str(Path(__file__).resolve()), "--watchdog",
                str(self.artifacts_dir), str(os.getpid()), str(pgid),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=None,
            text=True,
            bufsize=1,
            start_new_session=True,
        )

    @staticmethod
    def _validate_artifacts_dir(path: Path) -> None:
        output_root = (Path.cwd() / "out" / "tests").resolve()
        if not path.is_relative_to(output_root):
            raise ValueError(f"fork watchdog artifacts 必须位于正式 out/tests 下: {path}")
        path.mkdir(parents=True, exist_ok=True)

    def register_c3_sshd(self, process: subprocess.Popen[str], config_file: Path) -> None:
        identity = _read_c3_sshd_identity(self.artifacts_dir, process.pid, config_file)
        self._send({"action": "sshd_identity", "identity": identity})

    def start(self, token: str, target: str, operation_key: str, started_at: float, deadline: float) -> None:
        self._send({
            "action": "start", "token": token, "target": target,
            "operation_key": operation_key, "started_at": started_at, "deadline": deadline,
            "last_stage": "started",
        })

    def stage(self, token: str, stage: str) -> None:
        self._send({"action": "stage", "token": token, "last_stage": stage})

    def complete(self, token: str) -> None:
        self._send({"action": "complete", "token": token})

    def _send(self, message: dict[str, object]) -> None:
        with self._lock:
            if self._process.poll() is not None or self._process.stdin is None:
                raise RuntimeError("fork deadline sidecar 已退出，不能保证 operation deadline")
            self._process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self._process.stdin.flush()

    def close(self) -> None:
        with self._lock:
            if self._process.poll() is None and self._process.stdin is not None:
                self._process.stdin.write('{"action":"stop"}\n')
                self._process.stdin.flush()
                self._process.stdin.close()
        try:
            self._process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=3)

    def wait_for_termination(self) -> None:
        exit_code = self._process.wait()
        if exit_code != 137:
            raise RuntimeError(
                f"expired fork deadline watchdog exited without terminating its process group: exit={exit_code}"
            )


class ForkOperationDeadline:
    def __init__(
        self,
        target: str,
        operation_key: str,
        watchdog: ForkDeadlineWatchdog,
        *,
        _test_timeout_seconds: float | None = None,
    ) -> None:
        self.target = target
        self.operation_key = operation_key
        self._watchdog = watchdog
        self._timeout_seconds = FORK_TIMEOUT_SECONDS if _test_timeout_seconds is None else _test_timeout_seconds
        if self._timeout_seconds <= 0 or self._timeout_seconds > FORK_TIMEOUT_SECONDS:
            raise ValueError("测试 fork deadline 必须在 (0, 60] 秒内")
        self.started_at = time.monotonic()
        self.deadline = self.started_at + self._timeout_seconds
        self.last_stage = "started"
        self._token = secrets.token_hex(16)
        self._completed = False
        self._timed_out = False
        self._watchdog.start(self._token, target, operation_key, self.started_at, self.deadline)

    @classmethod
    def for_test(
        cls, target: str, operation_key: str, watchdog: ForkDeadlineWatchdog, timeout_seconds: float,
    ) -> ForkOperationDeadline:
        """仅测试短阈值使用；生产构造始终固定为60秒。"""
        return cls(target, operation_key, watchdog, _test_timeout_seconds=timeout_seconds)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    async def wait(self, awaitable: Awaitable[Result], stage: str) -> Result:
        self.set_stage(stage)
        remaining = self.remaining
        if remaining <= 0 and not self._completed:
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            raise self._timeout()
        task = asyncio.ensure_future(awaitable)
        done, _ = await asyncio.wait({task}, timeout=remaining if remaining > 0 else None)
        if task not in done:
            if self._completed:
                return await task
            task.add_done_callback(_report_detached_fork_failure)
            raise self._timeout()
        result = task.result()
        if self.remaining <= 0 and not self._completed:
            raise self._timeout()
        return result

    def set_stage(self, stage: str) -> None:
        self.last_stage = stage
        self._watchdog.stage(self._token, stage)

    def complete(self) -> None:
        if self._completed or self._timed_out:
            return
        self.last_stage = "backend_durable_completion_confirmed"
        self._completed = True
        self._watchdog.complete(self._token)

    def _timeout(self) -> ForkDeadlineExceeded:
        self._timed_out = True
        message = (
            "fork API deadline exceeded: "
            f"target={self.target}, operation/key={self.operation_key}, "
            f"last_stage={self.last_stage}, elapsed={self.elapsed:.3f}s, "
            "backend_state=unknown; 客户端等待超时不表示后端操作已取消或回滚"
        )
        return ForkDeadlineExceeded(message)


def _report_detached_fork_failure(task: asyncio.Task[object]) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        _write_watchdog_stderr(f"deadline 后 fork coroutine 失败: {error!r}\n")


class ForkDeadlineRegistry:
    """每个 pytest owner 共享相同 operation/key 的原始绝对预算。"""

    def __init__(self, artifacts_dir: Path) -> None:
        self.watchdog = ForkDeadlineWatchdog(artifacts_dir)
        self._active: dict[tuple[str, str], ForkOperationDeadline] = {}

    def for_operation(self, target: str, operation_key: str) -> ForkOperationDeadline:
        identity = (target, operation_key)
        deadline = self._active.get(identity)
        if deadline is None or deadline._completed:
            deadline = ForkOperationDeadline(target, operation_key, self.watchdog)
            self._active[identity] = deadline
        return deadline

    def complete(self, deadline: ForkOperationDeadline) -> None:
        if deadline._timed_out:
            return
        deadline.complete()
        self._active.pop((deadline.target, deadline.operation_key), None)

    def close(self) -> None:
        if any(deadline._timed_out and not deadline._completed for deadline in self._active.values()):
            self.watchdog.wait_for_termination()
            return
        self.watchdog.close()


def _write_exclusive_json(path: Path, payload: dict[str, object]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _watchdog_main(artifacts_dir: Path, parent_pid: int, protected_pgid: int) -> int:
    artifacts_dir = artifacts_dir.resolve()
    ForkDeadlineWatchdog._validate_artifacts_dir(artifacts_dir)
    active: dict[str, dict[str, object]] = {}
    sshd_identity: dict[str, object] | None = None
    source_fd = sys.stdin.fileno()
    input_buffer = bytearray()
    while True:
        now = time.monotonic()
        expired = next((
            (token, record) for token, record in active.items()
            if now >= float(record["deadline"])
        ), None)
        if expired is not None:
            token, record = expired
            elapsed = max(0.0, now - float(record["started_at"]))
            message = (
                "fork API deadline exceeded: "
                f"target={record['target']}, operation/key={record['operation_key']}, "
                f"last_stage={record['last_stage']}, elapsed={elapsed:.3f}s, "
                "backend_state=unknown; watchdog terminated the protected pytest process group"
            )
            path = artifacts_dir / f"fork-deadline-{time.time_ns()}-{token}.json"
            try:
                _write_exclusive_json(path, {
                    "target": record["target"], "operation_key": record["operation_key"],
                    "last_stage": record["last_stage"], "elapsed_seconds": round(elapsed, 3),
                    "backend_state": "unknown", "error": message,
                })
            except OSError as error:
                _write_watchdog_stderr(
                    f"fork deadline sidecar write/fsync failed: path={path}, error={error!r}\n"
                )
            _write_watchdog_stderr(message + "\n")
            try:
                _kill_owned_c3_sshd(sshd_identity)
            except RuntimeError as error:
                _write_watchdog_stderr(f"C3 SSHD 清理身份复核失败: {error}\n")
            _terminate_process_group(protected_pgid, parent_pid)
            return 137

        timeout = min(
            (max(0.0, float(record["deadline"]) - now) for record in active.values()),
            default=None,
        )
        readable, _, _ = select.select([source_fd], [], [], timeout)
        if not readable:
            continue
        chunk = os.read(source_fd, 65536)
        if not chunk:
            return 0
        input_buffer.extend(chunk)
        while True:
            newline = input_buffer.find(b"\n")
            if newline < 0:
                break
            line = bytes(input_buffer[:newline])
            del input_buffer[:newline + 1]
            if not line:
                continue
            message = json.loads(line)
            action = message["action"]
            if action == "stop":
                return 0
            if action == "sshd_identity":
                identity = message.get("identity")
                sshd_identity = identity if isinstance(identity, dict) else None
                continue
            token = str(message.get("token", ""))
            if action == "start":
                active[token] = message
            elif action == "stage":
                if token in active:
                    active[token]["last_stage"] = message["last_stage"]
            elif action == "complete":
                active.pop(token, None)
            else:
                raise ValueError(f"未知 fork watchdog action: {action!r}")


def _write_watchdog_stderr(message: str) -> None:
    try:
        sys.stderr.write(message)
        sys.stderr.flush()
    except (OSError, ValueError):
        try:
            os.write(2, message.encode("utf-8", errors="replace"))
        except (OSError, ValueError):
            return


def _read_c3_sshd_identity(
    artifacts_dir: Path, process_pid: int, config_file: Path,
) -> dict[str, object]:
    output_root = (Path.cwd() / "out" / "tests").resolve()
    artifacts_root = next((
        candidate for candidate in (artifacts_dir, *artifacts_dir.parents)
        if candidate.name == "artifacts" and candidate.is_relative_to(output_root)
    ), None)
    if artifacts_root is None:
        raise RuntimeError(f"C3 SSHD artifacts 必须位于正式 out/tests 下: {artifacts_dir}")
    ssh_root = artifacts_root / "c3-ssh"
    expected_config = (ssh_root / "sshd_config").resolve()
    if not expected_config.is_file() or config_file.resolve() != expected_config:
        raise RuntimeError(f"C3 SSH harness 缺少正式 config 身份文件: {ssh_root}")
    try:
        pid = process_pid
        process_root = Path(f"/proc/{pid}")
        command_line = process_root.joinpath("cmdline").read_bytes().split(b"\0")
        arguments = [part.decode(errors="replace") for part in command_line if part]
        config_index = arguments.index("-f") + 1
        actual_config = Path(arguments[config_index]).resolve()
        expected_root = ssh_root.resolve()
        process_stat = process_root.joinpath("stat").read_text().rsplit(")", 1)[1].split()
        start_time = process_stat[19]
        pgid = int(process_stat[2])
        sid = int(process_stat[3])
        uid = process_root.stat().st_uid
        executable = process_root.joinpath("exe").resolve()
        if (
            pid <= 1
            or
            actual_config != expected_config
            or not actual_config.is_relative_to(expected_root)
            or uid != os.geteuid()
            or pgid != pid
            or sid != pid
        ):
            raise RuntimeError(
                f"C3 SSHD 进程身份不匹配: pid={pid}, uid={uid}, pgid={pgid}, sid={sid}, "
                f"config={actual_config}, expected_config={expected_config}"
            )
    except (OSError, ValueError, IndexError) as error:
        raise RuntimeError(f"无法读取 C3 SSHD 进程身份: {ssh_root}") from error
    return {
        "pid": pid, "start_time": start_time, "uid": uid,
        "pgid": pgid, "sid": sid, "executable": str(executable), "arguments": arguments,
        "config_path": str(expected_config), "c3_root": str(expected_root),
    }


def _kill_owned_c3_sshd(identity: dict[str, object] | None) -> None:
    if identity is None:
        return
    try:
        pid = int(identity["pid"])
        process_root = Path(f"/proc/{pid}")
        process_stat = process_root.joinpath("stat").read_text().rsplit(")", 1)[1].split()
        command_line = [
            part.decode(errors="replace") for part in process_root.joinpath("cmdline").read_bytes().split(b"\0") if part
        ]
        executable = process_root.joinpath("exe").resolve()
        pgid = int(process_stat[2])
        sid = int(process_stat[3])
        output_root = (Path.cwd() / "out" / "tests").resolve()
        expected_root = Path(str(identity["c3_root"])).resolve()
        config_path = Path(str(identity["config_path"])).resolve()
        config_index = command_line.index("-f") + 1
        actual_config = Path(command_line[config_index]).resolve()
        if (
            process_stat[19] != identity["start_time"]
            or process_root.stat().st_uid != identity["uid"]
            or identity["uid"] != os.geteuid()
            or pgid != identity["pgid"]
            or sid != identity["sid"]
            or pgid != pid
            or sid != pid
            or str(executable) != identity["executable"]
            or command_line != identity["arguments"]
            or actual_config != config_path
            or expected_root.name != "c3-ssh"
            or expected_root.parent.name != "artifacts"
            or not expected_root.is_relative_to(output_root)
            or config_path != (expected_root / "sshd_config").resolve()
            or not config_path.is_relative_to(expected_root)
        ):
            raise RuntimeError(f"冻结的 C3 SSHD 进程身份不匹配: pid={pid}, identity={identity!r}")
    except (OSError, ValueError, IndexError, KeyError) as error:
        raise RuntimeError(f"无法复核冻结的 C3 SSHD 身份: {identity!r}") from error
    try:
        os.killpg(int(identity["pgid"]), signal.SIGKILL)
    except ProcessLookupError:
        pass


def _terminate_process_group(protected_pgid: int, parent_pid: int) -> None:
    if (
        protected_pgid <= 1
        or protected_pgid != os.getpgid(parent_pid)
        or protected_pgid != os.getsid(parent_pid)
    ):
        raise RuntimeError("watchdog 拒绝终止非独立的受保护 test process group")
    os.killpg(protected_pgid, signal.SIGTERM)
    time.sleep(0.5)
    try:
        os.killpg(protected_pgid, 0)
    except ProcessLookupError:
        return
    os.killpg(protected_pgid, signal.SIGKILL)


def _process_parent_pid(pid: int) -> int:
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return int(fields[1])


if __name__ == "__main__" and sys.argv[1:2] == ["--watchdog"]:
    raise SystemExit(_watchdog_main(Path(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])))
