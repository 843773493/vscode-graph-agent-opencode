from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, TextIO

from app.gateway.runtime.process_groups import (
    _capture_posix_group_starttime,
    _posix_group_is_alive,
    _wait_until_closed,
)


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
    if result.returncode != 0 and force:
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
    _leader_starttime: int | None = field(default=None, init=False, repr=False)
    _group_closed: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if os.name == "posix":
            self._leader_starttime = _capture_posix_group_starttime(self.process.pid)

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
        else:
            _taskkill_process_tree(self.process.pid, force=False)

    def _kill_group(self) -> None:
        if not self._close_target_is_alive():
            return
        if os.name == "posix":
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
        else:
            _taskkill_process_tree(self.process.pid, force=True)

    def _close_target_is_alive(self) -> bool:
        root_alive = self.process.poll() is None
        if os.name != "posix":
            return root_alive
        if not self._group_closed:
            self._group_closed = not _posix_group_is_alive(self.process.pid, self._leader_starttime)
        return not self._group_closed

    def _wait_closed(self, timeout_seconds: float) -> None:
        deadline = time.monotonic() + timeout_seconds
        self.process.wait(timeout=timeout_seconds)
        # 根 wait 与剩余组成员共用原预算；根退出不能代表同组子进程已退出。
        if os.name == "posix" and not _wait_until_closed(self._close_target_is_alive, deadline=deadline):
            raise subprocess.TimeoutExpired(f"owned process group pid={self.process.pid}", timeout_seconds)


@dataclass(slots=True)
class AdoptedManagedProcess:
    """新 Gateway 通过已验证的健康接口重新接管存活的辅助服务。"""

    pid: int
    close_failure: TimeoutError | None = field(default=None, init=False, repr=False)
    _leader_starttime: int | None = field(default=None, init=False, repr=False)
    _group_closed: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.pid <= 0:
            raise ValueError(f"接管进程 PID 必须为正整数: {self.pid}")
        if os.name == "posix":
            self._leader_starttime = _capture_posix_group_starttime(self.pid)

    def is_alive(self) -> bool:
        return self._is_alive()

    def request_terminate(self) -> None:
        if not self._close_target_is_alive():
            return
        try:
            if os.name == "posix":
                os.killpg(self.pid, signal.SIGTERM)
            else:
                _taskkill_process_tree(self.pid, force=False)
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
                    _taskkill_process_tree(self.pid, force=True)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + timeout_seconds
            if not _wait_until_closed(self._close_target_is_alive, deadline=deadline):
                raise RuntimeError(f"已接管进程组强制终止后仍未退出: pid={self.pid}") from self.close_failure
        # 强制回收不能证明 unload 已完成，后续关闭也不得洗掉这一失败事实。
        raise self.close_failure

    def detach(self) -> None:
        return

    def _is_alive(self) -> bool:
        if os.name == "nt":
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
            return self._is_alive()
        if not self._group_closed:
            self._group_closed = not _posix_group_is_alive(self.pid, self._leader_starttime)
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
    except Exception:
        log_file.close()
        raise
