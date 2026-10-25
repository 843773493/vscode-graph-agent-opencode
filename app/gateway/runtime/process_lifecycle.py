from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, TextIO

from app.gateway.runtime.process_groups import (
    ProcessStartMarker,
    _capture_posix_group_leader_start_marker,
    _posix_group_is_alive,
    _wait_until_closed,
)

logger = logging.getLogger(__name__)

WindowsProcessStartTimesByPid = dict[int, float]


def _windows_process_start_time(pid: int) -> float | None:
    import psutil

    try:
        return psutil.Process(pid).create_time()
    except psutil.NoSuchProcess:
        return None
    except psutil.Error as error:
        raise RuntimeError(
            f"读取 Windows 进程创建时间失败: pid={pid}: {error}"
        ) from error


def _capture_windows_process_tree(
    pid: int,
    *,
    expected_root_start_time: float | None,
) -> tuple[float | None, WindowsProcessStartTimesByPid]:
    import psutil

    children_by_parent: dict[int, list[tuple[int, float | None]]] = defaultdict(list)
    root_start_time: float | None = None
    try:
        for process in psutil.process_iter(attrs=["pid", "ppid", "create_time"]):
            info = process.info
            process_pid = info["pid"]
            parent_pid = info["ppid"]
            process_start_time = info["create_time"]
            if process_pid == pid:
                if process_start_time is None:
                    raise RuntimeError(f"无法核实 Windows 受管进程身份: pid={pid}")
                root_start_time = process_start_time
            if process_pid is not None and parent_pid is not None:
                children_by_parent[parent_pid].append((process_pid, process_start_time))
    except psutil.Error as error:
        raise RuntimeError(f"读取 Windows 进程树失败: pid={pid}: {error}") from error

    if (
        expected_root_start_time is not None
        and root_start_time is not None
        and root_start_time != expected_root_start_time
    ):
        raise RuntimeError(f"Windows 受管进程 PID 已被替换: pid={pid}")

    descendant_start_times: WindowsProcessStartTimesByPid = {}
    pending_parents = [pid]
    visited = {pid}
    while pending_parents:
        parent_pid = pending_parents.pop()
        for child_pid, child_start_time in children_by_parent[parent_pid]:
            if child_pid in visited:
                continue
            if child_start_time is None:
                raise RuntimeError(
                    "无法核实 Windows 受管进程树成员身份: "
                    f"pid={child_pid}, parent_pid={parent_pid}"
                )
            visited.add(child_pid)
            pending_parents.append(child_pid)
            descendant_start_times[child_pid] = child_start_time
    return root_start_time, descendant_start_times


def _windows_process_tree_is_alive(
    descendant_start_times: WindowsProcessStartTimesByPid,
) -> bool:
    import psutil

    for pid, expected_start_time in tuple(descendant_start_times.items()):
        try:
            current_start_time = psutil.Process(pid).create_time()
        except psutil.NoSuchProcess:
            descendant_start_times.pop(pid)
            continue
        except psutil.Error as error:
            raise RuntimeError(
                f"核实 Windows 进程树成员失败: pid={pid}: {error}"
            ) from error
        if current_start_time != expected_start_time:
            descendant_start_times.pop(pid)
    return bool(descendant_start_times)


def _terminate_windows_process_tree(
    descendant_start_times: WindowsProcessStartTimesByPid,
    *,
    force: bool,
) -> None:
    import psutil

    for pid, expected_start_time in tuple(descendant_start_times.items()):
        try:
            process = psutil.Process(pid)
            if process.create_time() != expected_start_time:
                descendant_start_times.pop(pid)
                continue
            if force:
                process.kill()
            else:
                process.terminate()
        except psutil.NoSuchProcess:
            descendant_start_times.pop(pid)
        except psutil.Error as error:
            raise RuntimeError(
                f"Windows 子进程终止失败: pid={pid}, force={force}: {error}"
            ) from error


# TODO: Windows 的 taskkill 优雅模式对子进程返回非零是正常的，必须交给 close() 的 /F 阶段收尾。
def _taskkill_process_tree(pid: int, *, force: bool) -> None:
    arguments = ["taskkill", "/T"]
    if force:
        arguments.append("/F")
    arguments.extend(["/PID", str(pid)])
    result = subprocess.run(
        arguments,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0 and force and _windows_process_is_alive(pid):
        raise RuntimeError(
            "Windows 进程树清理失败: "
            f"pid={pid}, force={force}, stderr={result.stderr.strip()}"
        )


def _windows_process_is_alive(pid: int) -> bool:
    """用系统进程 API 查询 Windows PID，避免周期性启动 tasklist 子进程。"""
    import psutil

    try:
        return psutil.pid_exists(pid)
    except psutil.Error as error:
        raise RuntimeError(f"Windows 进程状态查询失败: pid={pid}: {error}") from error


class ManagedProcessHandle(Protocol):
    def is_alive(self) -> bool: ...

    def request_terminate(self) -> None: ...

    def close(self, *, timeout_seconds: float = 8) -> None: ...

    def detach(self) -> None: ...


@dataclass(slots=True)
class ManagedProcess:
    process: subprocess.Popen[str]
    log_file: object
    close_failure: TimeoutError | None = field(default=None, init=False, repr=False)
    _leader_start_marker: ProcessStartMarker | None = field(
        default=None, init=False, repr=False
    )
    _group_closed: bool = field(default=False, init=False, repr=False)
    _windows_root_start_time: float | None = field(default=None, init=False, repr=False)
    _windows_descendant_start_times: WindowsProcessStartTimesByPid = field(
        default_factory=dict, init=False, repr=False
    )
    _windows_descendants_captured: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if os.name == "posix":
            self._leader_start_marker = _capture_posix_group_leader_start_marker(
                self.process.pid
            )
        elif os.name == "nt":
            self._windows_root_start_time = _windows_process_start_time(
                self.process.pid
            )

    def is_alive(self) -> bool:
        return self.process.poll() is None

    def request_terminate(self) -> None:
        if not self._close_target_is_alive():
            return
        try:
            self._terminate_group()
        except ProcessLookupError:
            return

    def close(self, *, timeout_seconds: float = 8) -> None:
        if self.close_failure is not None:
            if self._close_target_is_alive():
                self._kill_group()
                self._wait_closed(timeout_seconds)
            raise self.close_failure
        if self._close_target_is_alive():
            self.request_terminate()
            try:
                self._wait_closed(timeout_seconds)
            except subprocess.TimeoutExpired as error:
                self.close_failure = TimeoutError(
                    "受管进程组未完成优雅关闭，已进入强制回收: "
                    f"pid={self.process.pid}, timeout_seconds={timeout_seconds}"
                )
                self.close_failure.__cause__ = error
                self._kill_group()
                self._wait_closed(timeout_seconds)
                raise self.close_failure from error
        # 关闭失败仍由原 owner 持有进程与日志句柄，避免重试丢失诊断输出。
        close = getattr(self.log_file, "close", None)
        if callable(close):
            close()

    def detach(self) -> None:
        """Gateway 重启时放弃进程所有权，不向独立进程组发送信号。"""
        close = getattr(self.log_file, "close", None)
        if callable(close):
            close()

    def _terminate_group(self) -> None:
        if os.name == "posix":
            os.killpg(self.process.pid, signal.SIGTERM)
        elif self.process.poll() is None:
            _taskkill_process_tree(self.process.pid, force=False)
        else:
            _terminate_windows_process_tree(
                self._windows_descendant_start_times,
                force=False,
            )

    def _kill_group(self) -> None:
        if not self._close_target_is_alive():
            return
        if os.name == "posix":
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
        else:
            if self.process.poll() is None:
                _taskkill_process_tree(self.process.pid, force=True)
            _terminate_windows_process_tree(
                self._windows_descendant_start_times,
                force=True,
            )

    def _close_target_is_alive(self) -> bool:
        root_alive = self.process.poll() is None
        if os.name != "posix":
            if not self._windows_descendants_captured:
                root_start_time, descendant_start_times = _capture_windows_process_tree(
                    self.process.pid,
                    expected_root_start_time=self._windows_root_start_time,
                )
                if self._windows_root_start_time is None:
                    self._windows_root_start_time = root_start_time
                self._windows_descendant_start_times.update(descendant_start_times)
                self._windows_descendants_captured = True
            return root_alive or _windows_process_tree_is_alive(
                self._windows_descendant_start_times
            )
        if not self._group_closed:
            self._group_closed = not _posix_group_is_alive(
                self.process.pid, self._leader_start_marker
            )
        return not self._group_closed

    def _wait_closed(self, timeout_seconds: float) -> None:
        deadline = time.monotonic() + timeout_seconds
        self.process.wait(timeout=timeout_seconds)
        # 根 wait 与剩余组成员共用原预算；根退出不能代表同组子进程已退出。
        if os.name == "posix" and not _wait_until_closed(
            self._close_target_is_alive, deadline=deadline
        ):
            raise subprocess.TimeoutExpired(
                f"owned process group pid={self.process.pid}", timeout_seconds
            )
        if os.name == "nt" and not _wait_until_closed(
            self._close_target_is_alive, deadline=deadline
        ):
            raise subprocess.TimeoutExpired(
                f"owned process tree pid={self.process.pid}", timeout_seconds
            )


@dataclass(slots=True)
class AdoptedManagedProcess:
    """新 Gateway 通过已验证的健康接口重新接管存活的辅助服务。"""

    pid: int
    close_failure: TimeoutError | None = field(default=None, init=False, repr=False)
    _leader_start_marker: ProcessStartMarker | None = field(
        default=None, init=False, repr=False
    )
    _group_closed: bool = field(default=False, init=False, repr=False)
    _windows_root_start_time: float | None = field(default=None, init=False, repr=False)
    _windows_descendant_start_times: WindowsProcessStartTimesByPid = field(
        default_factory=dict, init=False, repr=False
    )
    _windows_descendants_captured: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.pid <= 0:
            raise ValueError(f"接管进程 PID 必须为正整数: {self.pid}")
        if os.name == "posix":
            self._leader_start_marker = _capture_posix_group_leader_start_marker(
                self.pid
            )
        elif os.name == "nt":
            self._windows_root_start_time = _windows_process_start_time(self.pid)

    def is_alive(self) -> bool:
        return self._is_alive()

    def request_terminate(self) -> None:
        if not self._close_target_is_alive():
            return
        try:
            if os.name == "posix":
                os.killpg(self.pid, signal.SIGTERM)
            elif self._is_alive():
                _taskkill_process_tree(self.pid, force=False)
            else:
                _terminate_windows_process_tree(
                    self._windows_descendant_start_times,
                    force=False,
                )
        except ProcessLookupError:
            return

    def close(self, *, timeout_seconds: float = 8) -> None:
        if self.close_failure is None:
            self.request_terminate()
            deadline = time.monotonic() + timeout_seconds
            if _wait_until_closed(self._close_target_is_alive, deadline=deadline):
                return
            self.close_failure = TimeoutError(
                "已接管进程组未完成优雅关闭，已进入强制回收: "
                f"pid={self.pid}, timeout_seconds={timeout_seconds}"
            )
        if self._close_target_is_alive():
            try:
                if os.name == "posix":
                    os.killpg(self.pid, signal.SIGKILL)
                else:
                    if self._is_alive():
                        _taskkill_process_tree(self.pid, force=True)
                    _terminate_windows_process_tree(
                        self._windows_descendant_start_times,
                        force=True,
                    )
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + timeout_seconds
            if not _wait_until_closed(self._close_target_is_alive, deadline=deadline):
                raise RuntimeError(
                    f"已接管进程组强制终止后仍未退出: pid={self.pid}"
                ) from self.close_failure
        # 强制回收不能证明 unload 已完成，后续关闭也不得洗掉这一失败事实。
        raise self.close_failure

    def detach(self) -> None:
        return

    def _is_alive(self) -> bool:
        if os.name == "nt":
            if self._windows_root_start_time is not None:
                return _windows_process_tree_is_alive(
                    {self.pid: self._windows_root_start_time}
                )
            return _windows_process_is_alive(self.pid)
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError as error:
            raise PermissionError(f"无权检查已接管辅助服务 PID: {self.pid}") from error
        return True

    def _close_target_is_alive(self) -> bool:
        if os.name != "posix":
            if not self._windows_descendants_captured:
                root_start_time, descendant_start_times = _capture_windows_process_tree(
                    self.pid,
                    expected_root_start_time=self._windows_root_start_time,
                )
                if self._windows_root_start_time is None:
                    self._windows_root_start_time = root_start_time
                self._windows_descendant_start_times.update(descendant_start_times)
                self._windows_descendants_captured = True
            return self._is_alive() or _windows_process_tree_is_alive(
                self._windows_descendant_start_times
            )
        if not self._group_closed:
            self._group_closed = not _posix_group_is_alive(
                self.pid, self._leader_start_marker
            )
        return not self._group_closed


def _spawn_logged_process(
    command: list[str],
    *,
    log_file: TextIO,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.Popen[str]:
    try:
        return subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdout=log_file,
            stderr=log_file,
            text=True,
            start_new_session=os.name == "posix",
        )
    except Exception as error:
        # 只记可执行文件和 cwd，不把命令参数或环境中的凭据复制到日志。
        logger.exception("受管进程启动失败: executable=%s cwd=%s", command[:1], cwd)
        cleanup_note = None
        try:
            log_file.close()
        except Exception as cleanup_error:
            logger.exception("受管进程启动失败后日志关闭失败")
            cleanup_note = (
                "受管进程启动失败后日志关闭失败: "
                f"error_type={type(cleanup_error).__name__}"
            )
            if isinstance(cleanup_error, OSError):
                cleanup_note += f", errno={cleanup_error.errno}"
            error.add_note(cleanup_note)
        if not isinstance(error, OSError):
            raise
        detail = f"受管进程启动失败: stage=spawn, error_type={type(error).__name__}"
        if getattr(error, "winerror", None) is not None:
            detail += f", winerror={error.winerror}"
        public_error = type(error)(error.errno, detail)
        if cleanup_note is not None:
            public_error.add_note(cleanup_note)
        raise public_error from error
