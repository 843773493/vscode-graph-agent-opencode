"""附件在线访问与离线维护共用的跨进程互斥。"""

from __future__ import annotations

import os
from pathlib import Path
from threading import Lock
from types import TracebackType
from typing import Self

import portalocker


class AttachmentMaintenanceBusyError(RuntimeError):
    """附件目录仍被在线 owner 或离线维护占用。"""


class AttachmentMaintenanceLock:
    """在线 owner 持共享锁，离线维护持独占锁；冲突立即报告，不等待。

    锁文件位于附件根目录的同级，切换 catalog 或目录内容不能替换它。
    文件永不删除，进程退出由操作系统释放锁；锁文件存在不代表正在占用。
    """

    # TODO: schema 3 离线迁移取得独占后，仍须核对持久 operation 与外部任务终态，
    # 完成一致备份和可恢复切换；取得锁本身不证明遗留副作用已收敛。

    def __init__(self, attachments_root: Path, *, exclusive: bool) -> None:
        root = attachments_root.expanduser().resolve()
        self.path = root.with_name(f".{root.name}-maintenance.lock")
        self._exclusive = exclusive
        self._fd: int | None = None
        self._mutex = Lock()

    def acquire(self) -> None:
        with self._mutex:
            if self._fd is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            mode = portalocker.LOCK_EX if self._exclusive else portalocker.LOCK_SH
            try:
                portalocker.lock(fd, mode | portalocker.LOCK_NB)
            except portalocker.AlreadyLocked as error:
                os.close(fd)
                action = "离线维护" if self._exclusive else "在线访问"
                raise AttachmentMaintenanceBusyError(
                    f"附件目录无法{action}：仍有在线 owner 或离线维护占用；"
                    "停止相关进程并完成关闭后重试"
                ) from error
            except BaseException:
                os.close(fd)
                raise
            self._fd = fd

    def release(self) -> None:
        with self._mutex:
            fd = self._fd
            if fd is None:
                return
            self._fd = None
            try:
                portalocker.unlock(fd)
            finally:
                os.close(fd)

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(
        self,
        _type: type[BaseException] | None,
        _error: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.release()
