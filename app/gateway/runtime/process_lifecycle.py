from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, TextIO

from app.core.shielded_cleanup import run_cleanup_shielded
from app.gateway.runtime.process_groups import (
    ProcessStartMarker,
    _capture_posix_group_leader_start_marker,
    _posix_group_is_alive,
    _wait_until_closed,
)
from app.gateway.runtime.process_logs import ProcessLogFile, ProcessLogOwnership

logger = logging.getLogger(__name__)

WindowsProcessStartTimesByPid = dict[int, float]


async def wait_process_exit(pid: int) -> None:
    """等待已持有实例的系统退出通知；取消只释放等待，不终止进程。"""
    if os.name == "nt":
        import pywintypes
        import win32api
        import win32con
        import win32event

        try:
            process = win32api.OpenProcess(win32con.SYNCHRONIZE, False, pid)
        except pywintypes.error as error:
            if error.winerror == 87:  # 进程已退出，系统不再提供句柄。
                return
            raise
        cancelled = win32event.CreateEvent(None, True, False, None)
        waiting = asyncio.create_task(asyncio.to_thread(win32event.WaitForMultipleObjects,
            [process, cancelled], False, win32event.INFINITE))
        try:
            await asyncio.shield(waiting)
        finally:
            win32event.SetEvent(cancelled)
            # 保留句柄直至实际等待返回，接管释放时也不会遗留 executor 线程。
            async def release() -> None:
                try:
                    await waiting
                finally:
                    win32api.CloseHandle(cancelled)
                    win32api.CloseHandle(process)

            await run_cleanup_shielded(release)
        return

    import select

    loop = asyncio.get_running_loop()
    exited = loop.create_future()
    queue = None
    descriptor = None
    try:
        if sys.platform == "darwin":
            queue = select.kqueue()
            queue.control([select.kevent(pid, filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT, fflags=select.KQ_NOTE_EXIT)], 0)
            descriptor = queue.fileno()
        else:
            descriptor = os.pidfd_open(pid)
        def complete() -> None:
            if not exited.done():
                exited.set_result(None)

        loop.add_reader(descriptor, complete)
        await exited
    except ProcessLookupError:
        return
    finally:
        if descriptor is not None:
            loop.remove_reader(descriptor)
        if queue is not None:
            queue.close()
        elif descriptor is not None:
            os.close(descriptor)


class LoggedProcessCleanupError(RuntimeError):
    """启动后的回收失败仍携带实际进程和日志句柄，不能仅剩一个 PID。"""

    def __init__(self, process: subprocess.Popen[str], log_file: TextIO):
        super().__init__(f"受管进程启动失败且未完成回收: pid={process.pid}")
        self.process = process
        self.log_file = log_file


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
        # 先冻结创建时间，避免旧父子快照配上随后复用 PID 的新进程身份。
        process_infos = [
            process.info
            for process in psutil.process_iter(attrs=["pid", "create_time"])
        ]
        # TODO: psutil 提供公开批量父子关系接口后迁移；当前复用 children() 的快照，
        # 避免 Windows 每读取一个 ppid 都重新枚举整机进程。
        parent_pids = psutil._ppid_map()  # pyright: ignore[reportAttributeAccessIssue]
        for info in process_infos:
            process_pid = info["pid"]
            parent_pid = parent_pids.get(process_pid)
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


def _verify_windows_service_process(
    root_pid: int, root_start_time: float, pid: int, *,
    expected_service_start_time: float | None = None,
) -> tuple[float, WindowsProcessStartTimesByPid]:
    actual_root_start_time, descendants = _capture_windows_process_tree(
        root_pid, expected_root_start_time=root_start_time,
    )
    if actual_root_start_time != root_start_time:
        raise RuntimeError(f"Windows 受管进程根已退出: pid={root_pid}")
    service_start_time = root_start_time if pid == root_pid else descendants.get(pid)
    if service_start_time is None:
        raise RuntimeError(f"服务进程不属于受管进程树: pid={pid}, root_pid={root_pid}")
    if (expected_service_start_time is not None and service_start_time != expected_service_start_time
        or _windows_process_start_time(pid) != service_start_time
        or _windows_process_start_time(root_pid) != root_start_time):
        raise RuntimeError(f"Windows 服务进程身份在确认期间已变化: pid={pid}, root_pid={root_pid}")
    return service_start_time, descendants


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


# TODO(windows-graceful-stop): 未完成就绪／身份核验的无窗口服务仍走 taskkill /T，
# 期限后 /F 回收须报告失败；已验证 Backend／Browser／Terminal 统一走原认证 HTTP。
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
    @property
    def pid(self) -> int: ...

    def is_alive(self) -> bool: ...

    def request_terminate(self) -> None: ...

    def close(self, *, timeout_seconds: float = 8) -> None: ...

    def detach(self) -> None: ...


# TODO: 原句柄目前只核操作系统进程退出；Backend 非零退出码及接管进程的 lifespan
# 结果尚未统一上报。关闭 ACK 只表示接受请求，不证明应用清理成功。
@dataclass(slots=True)
class ManagedProcess:
    process: subprocess.Popen[str]
    log_file: object
    shutdown_request: Callable[[], None] | None = field(default=None, repr=False)
    _shutdown_requested: bool = field(default=False, init=False, repr=False)
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
    _service_pid: int | None = field(default=None, init=False, repr=False)
    _service_start_time: float | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if os.name == "posix":
            self._leader_start_marker = _capture_posix_group_leader_start_marker(
                self.process.pid
            )
        elif os.name == "nt":
            self._windows_root_start_time = _windows_process_start_time(
                self.process.pid
            )

    @property
    def pid(self) -> int:
        return self._service_pid if self._service_pid is not None else self.process.pid

    def confirm_service_identity(self, pid: int) -> None:
        """健康合同完整通过后，冻结原树中经过 OS 核验的实际服务身份。"""
        if self._service_pid is not None:
            if pid != self._service_pid or not self.is_alive():
                raise RuntimeError("已确认的受管服务进程身份已变化")
            return
        if self.process.poll() is not None:
            raise RuntimeError("受管进程在服务身份确认前已退出")
        service_start_time = None
        if os.name == "nt":
            if self._windows_root_start_time is None:
                raise RuntimeError("Windows 受管进程缺少启动根身份")
            service_start_time, descendants = _verify_windows_service_process(
                self.process.pid, self._windows_root_start_time, pid,
            )
            # 即使后续日志确认失败，原 owner 仍须记住已观察到的后代并负责回收。
            self._windows_descendant_start_times.update(descendants)
            if pid != self.process.pid and isinstance(self.log_file, ProcessLogFile):
                self.log_file.ownership.confirm_service_process(
                    pid, service_start_time, root_pid=self.process.pid,
                    root_created_at=self._windows_root_start_time,
                )
        elif pid != self.process.pid:
            raise RuntimeError("服务进程身份与受管进程根不匹配")
        self._service_pid, self._service_start_time = pid, service_start_time

    def is_alive(self) -> bool:
        if self._service_pid is not None and self._service_pid != self.process.pid:
            assert self._service_start_time is not None
            return _windows_process_tree_is_alive({self._service_pid: self._service_start_time})
        return self.process.poll() is None

    def request_terminate(self) -> None:
        if not self._close_target_is_alive():
            return
        if self.shutdown_request is not None:
            if not self._shutdown_requested:
                self.shutdown_request()
                self._shutdown_requested = True
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
        if isinstance(self.log_file, ProcessLogFile):
            self.log_file.release_ownership()

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
    log_ownership: ProcessLogOwnership | None = None
    shutdown_request: Callable[[], None] | None = field(default=None, repr=False)
    _shutdown_requested: bool = field(default=False, init=False, repr=False)
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
    _windows_root_pid: int = field(default=0, init=False, repr=False)
    _windows_service_start_time: float | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.pid <= 0:
            raise ValueError(f"接管进程 PID 必须为正整数: {self.pid}")
        if os.name == "posix":
            self._leader_start_marker = _capture_posix_group_leader_start_marker(
                self.pid
            )
        elif os.name == "nt":
            self._windows_root_pid = self.pid
            if self.log_ownership is None:
                self._windows_root_start_time = _windows_process_start_time(self.pid)
                self._windows_service_start_time = self._windows_root_start_time
            else:
                root_pid, root_start, service_start = self.log_ownership.process_tree_identity(self.pid)
                _, descendants = _verify_windows_service_process(
                    root_pid, root_start, self.pid, expected_service_start_time=service_start,
                )
                self._windows_root_pid, self._windows_root_start_time = root_pid, root_start
                self._windows_service_start_time = service_start
                self._windows_descendant_start_times.update(descendants)

    def is_alive(self) -> bool:
        return self._is_alive()

    def request_terminate(self) -> None:
        if not self._close_target_is_alive():
            return
        if self.shutdown_request is not None:
            if not self._shutdown_requested:
                self.shutdown_request()
                self._shutdown_requested = True
            return
        try:
            if os.name == "posix":
                os.killpg(self.pid, signal.SIGTERM)
            elif self._windows_root_is_alive():
                _taskkill_process_tree(self._windows_root_pid, force=False)
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
                if self.log_ownership is not None:
                    self.log_ownership.release()
                    self.log_ownership = None
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
                    if self._windows_root_is_alive():
                        _taskkill_process_tree(self._windows_root_pid, force=True)
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
            if self._windows_service_start_time is not None:
                return _windows_process_tree_is_alive(
                    {self.pid: self._windows_service_start_time}
                )
            return _windows_process_is_alive(self.pid)
        return self._close_target_is_alive()

    def _windows_root_is_alive(self) -> bool:
        if self._windows_root_start_time is not None:
            return _windows_process_tree_is_alive({self._windows_root_pid: self._windows_root_start_time})
        return _windows_process_is_alive(self._windows_root_pid)

    def _close_target_is_alive(self) -> bool:
        if os.name != "posix":
            if not self._windows_descendants_captured:
                root_start_time, descendant_start_times = _capture_windows_process_tree(
                    self._windows_root_pid,
                    expected_root_start_time=self._windows_root_start_time,
                )
                if self._windows_root_start_time is None:
                    self._windows_root_start_time = root_start_time
                self._windows_descendant_start_times.update(descendant_start_times)
                self._windows_descendants_captured = True
            return self._windows_root_is_alive() or _windows_process_tree_is_alive(
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
    detached: bool = False,
) -> subprocess.Popen[str]:
    process = None
    stage = "prepare_log"
    try:
        child_environment = dict(os.environ if env is None else env)
        child_environment.pop("BOXTEAM_RUNTIME_LOG_SINK", None)
        if isinstance(log_file, ProcessLogFile):
            log_file.begin_spawn()
            child_environment["BOXTEAM_RUNTIME_LOG_SINK"] = json.dumps(log_file.sink)
        stage = "spawn"
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=child_environment,
            stdin=subprocess.DEVNULL if detached else None,
            stdout=log_file,
            stderr=log_file,
            text=True,
            start_new_session=os.name == "posix",
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS)
            if detached and os.name == "nt" else 0,
        )
        stage = "bind_log"
        if isinstance(log_file, ProcessLogFile):
            log_file.ownership.bind_process(process.pid)
        return process
    except Exception as error:
        # 只记可执行文件和 cwd，不把命令参数或环境中的凭据复制到日志。
        logger.exception("受管进程启动失败: stage=%s executable=%s cwd=%s", stage, command[:1], cwd)
        cleanup_note = None
        try:
            if process is not None:
                ManagedProcess(process=process, log_file=log_file).close()
            else:
                if isinstance(log_file, ProcessLogFile):
                    log_file.release_ownership()
                log_file.close()
        except Exception as cleanup_error:
            logger.exception("受管进程启动失败后日志关闭失败")
            if process is not None:
                raise LoggedProcessCleanupError(process, log_file) from ExceptionGroup(
                    "受管进程启动与回收失败", [error, cleanup_error],
                )
            cleanup_note = (
                "受管进程启动失败后日志关闭失败: "
                f"error_type={type(cleanup_error).__name__}"
            )
            if isinstance(cleanup_error, OSError):
                cleanup_note += f", errno={cleanup_error.errno}"
            error.add_note(cleanup_note)
        if not isinstance(error, OSError):
            raise
        detail = f"受管进程启动失败: stage={stage}, error_type={type(error).__name__}"
        winerror = getattr(error, "winerror", None)
        if winerror is not None:
            detail += f", winerror={winerror}"
        public_error = type(error)(error.errno, detail)
        if cleanup_note is not None:
            public_error.add_note(cleanup_note)
        raise public_error from error
