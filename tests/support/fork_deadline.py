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
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from typing import TypeVar

import psutil

Result = TypeVar("Result")
FORK_TIMEOUT_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class _PosixProcessIdentity:
    process_start_marker: str
    process_group_id: int
    session_id: int
    effective_user_id: int
    executable_path: Path


@dataclass(frozen=True, slots=True)
class _WindowsProcessIdentity:
    process_start_marker: str
    parent_process_id: int
    parent_start_marker: str
    username: str
    executable_path: Path


class ForkDeadlineExceeded(TimeoutError):
    pass


class ForkDeadlineWatchdog:
    """独立进程观察 deadline，避免事件循环和同步磁盘 I/O 阻塞计时。"""

    def __init__(self, artifacts_dir: Path) -> None:
        self.artifacts_dir = artifacts_dir.resolve()
        self._validate_artifacts_dir(self.artifacts_dir)
        suite_id = os.environ.get("BOXTEAM_TEST_SUITE")
        run_id = os.environ.get("BOXTEAM_TEST_RUN_ID")
        if not suite_id or not run_id:
            raise RuntimeError("fork deadline watchdog 必须由正式 matrix runner 启动")
        protected_process_id = os.getpid()
        if sys.platform == "win32":
            if not _has_matrix_runner_ancestor(protected_process_id):
                raise RuntimeError(
                    "fork deadline watchdog 目标进程必须属于 tests/runner/run-tests.mjs"
                )
            process_group_options = {
                "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP,
            }
        elif os.name == "posix":
            process_group_id = os.getpgrp()
            session_id = os.getsid(0)
            if process_group_id != session_id or process_group_id <= 1:
                raise RuntimeError(
                    f"拒绝保护非独立 matrix 测试进程组: pid={protected_process_id}, "
                    f"pgid={process_group_id}, sid={session_id}"
                )
            leader_parent = _process_parent_pid(process_group_id)
            if sys.platform == "linux":
                leader_parent_command = Path(f"/proc/{leader_parent}/cmdline").read_bytes()
            else:
                leader_parent_command = b"\0".join(
                    argument.encode("utf-8", errors="surrogateescape")
                    for argument in psutil.Process(leader_parent).cmdline()
                )
            if b"tests/runner/run-tests.mjs" not in leader_parent_command:
                raise RuntimeError(
                    "fork deadline watchdog 目标 PGID leader 必须由 tests/runner/run-tests.mjs 启动: "
                    f"leader={process_group_id}, parent={leader_parent}, "
                    f"command={leader_parent_command!r}"
                )
            parent_pid = os.getppid()
            if os.getpgid(parent_pid) != process_group_id:
                raise RuntimeError("pytest 当前进程不属于其受保护的 matrix 子进程组")
            protected_process_id = process_group_id
            process_group_options = {"start_new_session": True}
        else:
            raise RuntimeError(f"fork deadline watchdog 不支持当前平台: {sys.platform}")
        self._active_tokens: set[str] = set()
        self._lock = threading.Lock()
        self._process = subprocess.Popen(
            [
                sys.executable, str(Path(__file__).resolve()), "--watchdog",
                str(self.artifacts_dir), str(os.getpid()), str(protected_process_id),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=None,
            text=True,
            encoding="utf-8",
            bufsize=1,
            **process_group_options,
        )

    @staticmethod
    def _validate_artifacts_dir(path: Path) -> None:
        output_root = (Path.cwd() / "out" / "tests").resolve()
        if not path.is_relative_to(output_root):
            raise ValueError(f"fork watchdog artifacts 必须位于正式 out/tests 下: {path}")
        path.mkdir(parents=True, exist_ok=True)

    def register_c3_sshd(self, process: subprocess.Popen[str], config_file: Path) -> None:
        identity = _read_c3_sshd_identity(self.artifacts_dir, process, config_file)
        self._send({"action": "sshd_identity", "identity": identity})

    def start(self, token: str, target: str, operation_key: str, started_at: float, deadline: float) -> None:
        self._send({
            "action": "start", "token": token, "target": target,
            "operation_key": operation_key, "started_at": started_at, "deadline": deadline,
            "last_stage": "started",
        })
        self._active_tokens.add(token)

    def stage(self, token: str, stage: str) -> None:
        self._send({"action": "stage", "token": token, "last_stage": stage})

    def complete(self, token: str) -> None:
        self._send({"action": "complete", "token": token})
        self._active_tokens.discard(token)

    def _send(self, message: dict[str, object]) -> None:
        with self._lock:
            if self._process.poll() is not None or self._process.stdin is None:
                raise RuntimeError("fork deadline sidecar 已退出，不能保证 operation deadline")
            self._process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self._process.stdin.flush()

    def close(self) -> None:
        if self._active_tokens:
            raise RuntimeError(
                f"拒绝关闭仍有 active fork deadline 的 watchdog: {sorted(self._active_tokens)}"
            )
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
        try:
            done, _ = await asyncio.wait({task}, timeout=remaining if remaining > 0 else None)
        except asyncio.CancelledError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            raise
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
        """结束本次观察；是否 durable 成功由业务调用方先行校验。"""
        if self._completed or self._timed_out:
            return
        self.last_stage = "observed_operation_finished"
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
        if any(not deadline._completed for deadline in self._active.values()):
            # 未完成的操作可能仍在后端运行；等待独立 watcher 到原绝对期限，
            # 由它写诊断并终止当前隔离运行组。不得发送 stop。
            self.watchdog.wait_for_termination()
            return
        self.watchdog.close()


def _write_exclusive_json(path: Path, payload: dict[str, object]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    if sys.platform != "win32":
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def _watchdog_main(
    artifacts_dir: Path,
    parent_pid: int,
    protected_process_id: int,
) -> int:
    artifacts_dir = artifacts_dir.resolve()
    ForkDeadlineWatchdog._validate_artifacts_dir(artifacts_dir)
    active: dict[str, dict[str, object]] = {}
    sshd_identity: dict[str, object] | None = None
    source_fd: int | None = sys.stdin.fileno()
    input_buffer = bytearray()
    messages: Queue[dict[str, object] | None | BaseException] | None = None
    if sys.platform == "win32":
        messages = Queue()

        def read_messages() -> None:
            for line in sys.stdin.buffer:
                try:
                    message: object = json.loads(line)
                    if not isinstance(message, dict):
                        raise TypeError("fork watchdog 控制消息必须是 JSON object")
                except (OSError, UnicodeDecodeError, TypeError, ValueError) as error:
                    messages.put(error)
                    return
                messages.put(message)
            messages.put(None)

        threading.Thread(target=read_messages, name="fork-deadline-input", daemon=True).start()

    def apply_message(message: dict[str, object]) -> bool:
        nonlocal sshd_identity
        action = message["action"]
        if action == "stop":
            if active:
                _write_watchdog_stderr("拒绝停止仍保护未终结 fork deadline 的 watchdog\n")
                return False
            return True
        if action == "sshd_identity":
            identity = message.get("identity")
            sshd_identity = identity if isinstance(identity, dict) else None
            return False
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
        return False

    while True:
        now = time.monotonic()
        expired = next((
            (token, record) for token, record in active.items()
            if now >= float(record["deadline"])
        ), None)
        if expired is not None:
            token, record = expired
            elapsed = max(0.0, now - float(record["started_at"]))
            protected_scope = "process tree" if sys.platform == "win32" else "process group"
            message = (
                "fork API deadline exceeded: "
                f"target={record['target']}, operation/key={record['operation_key']}, "
                f"last_stage={record['last_stage']}, elapsed={elapsed:.3f}s, "
                "backend_state=unknown; watchdog terminated the protected pytest "
                f"{protected_scope}"
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
            except (RuntimeError, psutil.Error) as error:
                _write_watchdog_stderr(f"C3 SSHD 清理身份复核失败: {error}\n")
            _terminate_process_group(protected_process_id, parent_pid)
            return 137

        timeout = min(
            (max(0.0, float(record["deadline"]) - now) for record in active.values()),
            default=None,
        )
        if messages is not None:
            try:
                message = messages.get(timeout=timeout)
            except Empty:
                continue
            if message is None:
                if not active:
                    return 0
                continue
            if isinstance(message, BaseException):
                raise message
            if apply_message(message):
                return 0
            continue

        readable, _, _ = select.select([source_fd] if source_fd is not None else [], [], [], timeout)
        if not readable:
            continue
        chunk = os.read(source_fd, 65536)
        if not chunk:
            if not active:
                return 0
            os.close(source_fd)
            source_fd = None
            continue
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
            if apply_message(message):
                return 0


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
    artifacts_dir: Path, process: subprocess.Popen[str], config_file: Path,
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
        pid = process.pid
        if not isinstance(process.args, list) or len(process.args) != 5:
            raise RuntimeError(f"C3 SSHD 启动命令格式不匹配: {process.args!r}")
        launch_arguments = [str(argument) for argument in process.args]
        launch_executable = Path(launch_arguments[0]).resolve()
        actual_config = Path(launch_arguments[4]).resolve()
        if launch_arguments[1:4] != ["-D", "-e", "-f"] or actual_config != expected_config:
            raise RuntimeError(
                f"C3 SSHD 启动命令身份不匹配: arguments={launch_arguments!r}, "
                f"expected_config={expected_config}"
            )
        expected_root = ssh_root.resolve()
        if sys.platform == "win32":
            process_identity = _windows_process_identity(pid)
            parent_process = psutil.Process(os.getpid())
            identity_matches = (
                process_identity.parent_process_id == os.getpid()
                and process_identity.parent_start_marker == _process_start_marker(parent_process)
                and process_identity.username == parent_process.username()
            )
        else:
            process_identity = _posix_process_identity(pid)
            identity_matches = (
                process_identity.effective_user_id == os.geteuid()
                and process_identity.process_group_id == pid
                and process_identity.session_id == pid
            )
        if (
            pid <= 1
            or process_identity.executable_path != launch_executable
            or actual_config != expected_config
            or not actual_config.is_relative_to(expected_root)
            or not identity_matches
        ):
            raise RuntimeError(
                f"C3 SSHD 进程身份不匹配: pid={pid}, identity={process_identity!r}, "
                f"config={actual_config}, expected_config={expected_config}"
            )
    except (OSError, ValueError, IndexError, psutil.Error) as error:
        raise RuntimeError(f"无法读取 C3 SSHD 进程身份: {ssh_root}") from error
    identity: dict[str, object] = {
        "pid": pid,
        "process_start_marker": process_identity.process_start_marker,
        "executable": str(process_identity.executable_path),
        "launch_arguments": launch_arguments,
        "config_path": str(expected_config), "c3_root": str(expected_root),
    }
    if isinstance(process_identity, _PosixProcessIdentity):
        identity.update({
            "uid": process_identity.effective_user_id,
            "pgid": process_identity.process_group_id,
            "sid": process_identity.session_id,
        })
    else:
        identity.update({
            "parent_process_id": process_identity.parent_process_id,
            "parent_start_marker": process_identity.parent_start_marker,
            "username": process_identity.username,
        })
    return identity


def _kill_owned_c3_sshd(identity: dict[str, object] | None) -> None:
    if identity is None:
        return
    try:
        pid = int(identity["pid"])
        output_root = (Path.cwd() / "out" / "tests").resolve()
        expected_root = Path(str(identity["c3_root"])).resolve()
        config_path = Path(str(identity["config_path"])).resolve()
        launch_arguments_value = identity["launch_arguments"]
        if (
            not isinstance(launch_arguments_value, list)
            or not all(isinstance(argument, str) for argument in launch_arguments_value)
            or len(launch_arguments_value) != 5
        ):
            raise RuntimeError(f"冻结的 C3 SSHD 启动命令无效: {identity!r}")
        launch_arguments = launch_arguments_value
        launch_executable = Path(launch_arguments[0]).resolve()
        actual_config = Path(launch_arguments[4]).resolve()
        if sys.platform == "win32":
            process_identity = _windows_process_identity(pid)
            parent_process_id = int(identity["parent_process_id"])
            parent_process = psutil.Process(parent_process_id)
            identity_matches = (
                process_identity.process_start_marker == identity["process_start_marker"]
                and process_identity.parent_process_id == parent_process_id
                and process_identity.parent_start_marker == identity["parent_start_marker"]
                and _process_start_marker(parent_process) == identity["parent_start_marker"]
                and process_identity.username == identity["username"]
                and process_identity.username == parent_process.username()
            )
        else:
            process_identity = _posix_process_identity(pid)
            identity_matches = (
                process_identity.process_start_marker == identity["process_start_marker"]
                and process_identity.effective_user_id == identity["uid"]
                and identity["uid"] == os.geteuid()
                and process_identity.process_group_id == identity["pgid"]
                and process_identity.session_id == identity["sid"]
                and process_identity.process_group_id == pid
                and process_identity.session_id == pid
            )
        if (
            not identity_matches
            or str(process_identity.executable_path) != identity["executable"]
            or launch_arguments[1:4] != ["-D", "-e", "-f"]
            or str(launch_executable) != identity["executable"]
            or actual_config != config_path
            or expected_root.name != "c3-ssh"
            or expected_root.parent.name != "artifacts"
            or not expected_root.is_relative_to(output_root)
            or config_path != (expected_root / "sshd_config").resolve()
            or not config_path.is_relative_to(expected_root)
        ):
            raise RuntimeError(f"冻结的 C3 SSHD 进程身份不匹配: pid={pid}, identity={identity!r}")
    except (OSError, ValueError, IndexError, KeyError, psutil.Error) as error:
        raise RuntimeError(f"无法复核冻结的 C3 SSHD 身份: {identity!r}") from error
    if sys.platform == "win32":
        _terminate_windows_process_tree(
            pid,
            expected_start_marker=str(identity["process_start_marker"]),
            expected_parent_process_id=int(identity["parent_process_id"]),
            expected_parent_start_marker=str(identity["parent_start_marker"]),
        )
    else:
        try:
            os.killpg(int(identity["pgid"]), signal.SIGKILL)
        except ProcessLookupError:
            pass


def _terminate_process_group(protected_process_id: int, parent_pid: int) -> None:
    if sys.platform == "win32":
        if protected_process_id != parent_pid:
            raise RuntimeError("watchdog 拒绝终止与 pytest PID 不同的 Windows 目标")
        watchdog = psutil.Process(os.getpid())
        if watchdog.ppid() != protected_process_id:
            raise RuntimeError("watchdog 拒绝终止不再是其直接父进程的 pytest")
        if not _has_matrix_runner_ancestor(protected_process_id):
            raise RuntimeError("watchdog 拒绝终止脱离 matrix runner 的 pytest 进程树")
        process = psutil.Process(protected_process_id)
        _terminate_windows_process_tree(
            protected_process_id,
            expected_start_marker=_process_start_marker(process),
        )
        return
    if (
        protected_process_id <= 1
        or protected_process_id != os.getpgid(parent_pid)
        or protected_process_id != os.getsid(parent_pid)
    ):
        raise RuntimeError("watchdog 拒绝终止非独立的受保护 test process group")
    os.killpg(protected_process_id, signal.SIGTERM)
    time.sleep(0.5)
    try:
        os.killpg(protected_process_id, 0)
    except ProcessLookupError:
        return
    os.killpg(protected_process_id, signal.SIGKILL)


def _terminate_windows_process_tree(
    process_id: int,
    *,
    expected_start_marker: str,
    expected_parent_process_id: int | None = None,
    expected_parent_start_marker: str | None = None,
) -> None:
    try:
        process = psutil.Process(process_id)
    except psutil.NoSuchProcess:
        return
    if _process_start_marker(process) != expected_start_marker:
        raise RuntimeError(f"拒绝终止已更换身份的 Windows 进程: pid={process_id}")
    if expected_parent_process_id is not None:
        parent = psutil.Process(expected_parent_process_id)
        if (
            process.ppid() != expected_parent_process_id
            or expected_parent_start_marker is None
            or _process_start_marker(parent) != expected_parent_start_marker
        ):
            raise RuntimeError(f"拒绝终止父进程身份不匹配的 Windows 进程: pid={process_id}")
    descendants = process.children(recursive=True)
    for descendant in descendants:
        try:
            descendant.create_time()
        except psutil.NoSuchProcess:
            continue
    owned = [descendant for descendant in reversed(descendants) if descendant.pid != os.getpid()]
    owned.append(process)
    for candidate in owned:
        try:
            if candidate.is_running():
                candidate.terminate()
        except psutil.NoSuchProcess:
            continue
    _, alive = psutil.wait_procs(owned, timeout=0.5)
    for candidate in alive:
        if candidate.pid == os.getpid():
            continue
        try:
            if candidate.is_running():
                candidate.kill()
        except psutil.NoSuchProcess:
            continue
    _, remaining = psutil.wait_procs(alive, timeout=3)
    if remaining:
        raise RuntimeError(
            "Windows 看门狗未能回收受管进程树: "
            f"pids={[candidate.pid for candidate in remaining]}"
        )


def _process_parent_pid(pid: int) -> int:
    if sys.platform == "linux":
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return int(fields[1])
    try:
        return psutil.Process(pid).ppid()
    except psutil.Error as error:
        raise RuntimeError(f"读取进程父 PID 失败: pid={pid}") from error


def _posix_process_identity(pid: int) -> _PosixProcessIdentity:
    if sys.platform == "linux":
        process_root = Path(f"/proc/{pid}")
        fields = process_root.joinpath("stat").read_text().rsplit(")", 1)[1].split()
        return _PosixProcessIdentity(
            process_start_marker=fields[19],
            process_group_id=int(fields[2]),
            session_id=int(fields[3]),
            effective_user_id=process_root.stat().st_uid,
            executable_path=process_root.joinpath("exe").resolve(),
        )
    process = psutil.Process(pid)
    return _PosixProcessIdentity(
        process_start_marker=repr(process.create_time()),
        process_group_id=os.getpgid(pid),
        session_id=os.getsid(pid),
        effective_user_id=process.uids().effective,
        executable_path=Path(process.exe()).resolve(),
    )


def _windows_process_identity(pid: int) -> _WindowsProcessIdentity:
    process = psutil.Process(pid)
    parent_process = psutil.Process(process.ppid())
    return _WindowsProcessIdentity(
        process_start_marker=_process_start_marker(process),
        parent_process_id=parent_process.pid,
        parent_start_marker=_process_start_marker(parent_process),
        username=process.username(),
        executable_path=Path(process.exe()).resolve(),
    )


def _process_start_marker(process: psutil.Process) -> str:
    return repr(process.create_time())


def _has_matrix_runner_ancestor(process_id: int) -> bool:
    process = psutil.Process(process_id)
    for ancestor in (process, *process.parents()):
        arguments = (
            argument.replace("\\", "/").casefold()
            for argument in ancestor.cmdline()
        )
        if any(
            argument == "tests/runner/run-tests.mjs"
            or argument.endswith("/tests/runner/run-tests.mjs")
            for argument in arguments
        ):
            return True
    return False


def _browser_driver_process_identity(
    process_id: int,
    watchdog_id: int | None = None,
    *,
    require_matrix_runner: bool = True,
) -> dict[str, object]:
    if process_id <= 1:
        raise RuntimeError(f"fork watchdog 拒绝无效的浏览器驱动 PID: pid={process_id}")
    if require_matrix_runner and not _has_matrix_runner_ancestor(process_id):
        raise RuntimeError(
            f"fork watchdog 拒绝不属于 matrix runner 的浏览器驱动: pid={process_id}"
        )
    if sys.platform == "win32":
        identity = _windows_process_identity(process_id)
        result: dict[str, object] = {
            "pid": process_id,
            "platform": "windows",
            "process_start_marker": identity.process_start_marker,
            "parent_process_id": identity.parent_process_id,
            "parent_start_marker": identity.parent_start_marker,
            "username": identity.username,
            "executable_path": str(identity.executable_path),
        }
    elif os.name == "posix":
        identity = _posix_process_identity(process_id)
        if identity.process_group_id != process_id or identity.session_id != process_id:
            raise RuntimeError(
                "fork watchdog 拒绝未隔离的 browser driver: "
                f"pid={process_id}, pgid={identity.process_group_id}, sid={identity.session_id}"
            )
        result = {
            "pid": process_id,
            "platform": "posix",
            "process_start_marker": identity.process_start_marker,
            "process_group_id": identity.process_group_id,
            "session_id": identity.session_id,
            "effective_user_id": identity.effective_user_id,
            "executable_path": str(identity.executable_path),
        }
    else:
        raise RuntimeError(f"fork watchdog 不支持当前平台: {sys.platform}")

    if watchdog_id is not None:
        if watchdog_id <= 1 or watchdog_id == process_id:
            raise RuntimeError(f"fork watchdog 进程身份无效: pid={watchdog_id}")
        watchdog = psutil.Process(watchdog_id)
        if watchdog.ppid() != process_id:
            raise RuntimeError(
                f"fork watchdog 必须是 browser driver 的直接子进程: "
                f"watchdog={watchdog_id}, parent={watchdog.ppid()}, driver={process_id}"
            )
        if os.name == "posix":
            watchdog_identity = _posix_process_identity(watchdog_id)
            if (
                watchdog_identity.process_group_id != watchdog_id
                or watchdog_identity.session_id != watchdog_id
            ):
                raise RuntimeError(
                    "fork watchdog 必须运行在独立 POSIX session: "
                    f"pid={watchdog_id}, pgid={watchdog_identity.process_group_id}, "
                    f"sid={watchdog_identity.session_id}"
                )
    return result


def _terminate_browser_driver(
    process_id: int,
    watchdog_id: int,
    expected_identity: dict[str, object],
) -> None:
    current_identity = _browser_driver_process_identity(
        process_id,
        watchdog_id,
        require_matrix_runner=False,
    )
    if sys.platform == "win32":
        identity_matches = all(
            current_identity[key] == expected_identity[key]
            for key in (
                "pid",
                "platform",
                "process_start_marker",
                "username",
                "executable_path",
            )
        )
    else:
        identity_matches = current_identity == expected_identity
    if not identity_matches:
        raise RuntimeError(f"拒绝终止身份已变化的 browser driver: pid={process_id}")
    if sys.platform == "win32":
        _terminate_windows_process_tree(
            process_id,
            expected_start_marker=str(expected_identity["process_start_marker"]),
        )
        return

    try:
        os.killpg(process_id, signal.SIGTERM)
    except ProcessLookupError as error:
        raise RuntimeError(
            f"browser driver process group 已消失: pid={process_id}"
        ) from error
    time.sleep(0.5)
    try:
        os.killpg(process_id, 0)
    except ProcessLookupError:
        return
    # POSIX 不会复用仍有成员的 process group ID；这里核实组仍存在后强制结束残余成员。
    os.killpg(process_id, signal.SIGKILL)


def _browser_driver_watchdog_helper(arguments: list[str]) -> int:
    action = arguments[0]
    process_id = int(arguments[1])
    if action == "--browser-driver-identity":
        result = _browser_driver_process_identity(process_id)
    elif action == "--browser-driver-validate":
        watchdog_id = int(arguments[2])
        expected_identity = json.loads(arguments[3])
        result = _browser_driver_process_identity(process_id, watchdog_id)
        if result != expected_identity:
            raise RuntimeError(f"browser driver 启动身份已变化: pid={process_id}")
    elif action == "--browser-driver-terminate":
        watchdog_id = int(arguments[2])
        expected_identity = json.loads(arguments[3])
        _terminate_browser_driver(process_id, watchdog_id, expected_identity)
        return 0
    else:
        raise ValueError(f"未知 browser driver watchdog 操作: {action}")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    arguments = sys.argv[1:]
    if arguments[:1] == ["--watchdog"]:
        raise SystemExit(
            _watchdog_main(Path(arguments[1]), int(arguments[2]), int(arguments[3]))
        )
    if arguments[:1] in (
        ["--browser-driver-identity"],
        ["--browser-driver-validate"],
        ["--browser-driver-terminate"],
    ):
        raise SystemExit(_browser_driver_watchdog_helper(arguments))
    raise SystemExit(f"未知 fork deadline watchdog 模式: {arguments[:1]}")
