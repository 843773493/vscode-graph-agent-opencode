from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import TextIO

logger = logging.getLogger(__name__)

PROCESS_LOG_MAX_BYTES = 64 * 1024 * 1024
PROCESS_LOG_ROTATIONS = 3


@dataclass(frozen=True, slots=True)
class ProcessLogStore:
    root: Path

    def path_for(self, file_name: str) -> Path:
        if (
            not file_name
            or Path(file_name).name != file_name
            or PureWindowsPath(file_name).name != file_name
        ):
            logger.error("进程日志文件名非法: %r", file_name)
            raise ValueError("进程日志文件名非法")
        if not file_name.endswith(".log"):
            logger.error("进程日志后缀非法: %r", file_name)
            raise ValueError("进程日志必须使用 .log 后缀")
        return self.root / file_name

    def open(self, file_name: str) -> TextIO:
        log_path = self.path_for(file_name)
        stage = "create_directory"
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            stage = "directory_permissions"
            if os.name == "posix":
                self.root.chmod(0o700)
            if log_path.is_symlink():
                logger.error("进程日志文件不允许是符号链接: %s", log_path)
                raise RuntimeError(f"进程日志文件不允许是符号链接: log={file_name}")
            stage = "rotate"
            self._rotate_if_needed(log_path)
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            stage = "open"
            descriptor = os.open(log_path, flags, 0o600)
            try:
                stage = "file_permissions"
                if os.name == "posix":
                    os.fchmod(descriptor, 0o600)
                return os.fdopen(descriptor, "a", encoding="utf-8")
            except Exception:
                os.close(descriptor)
                raise
        except OSError as error:
            logger.exception("进程日志访问失败: root=%s log=%s stage=%s", self.root, file_name, stage)
            detail = f"进程日志访问失败: log={file_name}, stage={stage}, error_type={type(error).__name__}"
            if getattr(error, "winerror", None) is not None:
                detail += f", winerror={error.winerror}"
            raise type(error)(error.errno, detail) from error

    @staticmethod
    def _rotate_if_needed(log_path: Path) -> None:
        if not log_path.exists():
            return
        if log_path.stat().st_size < PROCESS_LOG_MAX_BYTES:
            return
        for index in range(PROCESS_LOG_ROTATIONS - 1, 0, -1):
            source = log_path.with_name(f"{log_path.name}.{index}")
            target = log_path.with_name(f"{log_path.name}.{index + 1}")
            if source.is_symlink() or target.is_symlink():
                logger.error("进程日志轮转目标不允许是符号链接: source=%s target=%s", source, target)
                raise RuntimeError(
                    f"进程日志轮转目标不允许是符号链接: source={source.name} target={target.name}"
                )
            if source.exists():
                source.replace(target)
        rotated_path = log_path.with_name(f"{log_path.name}.1")
        if rotated_path.is_symlink():
            logger.error("进程日志轮转目标不允许是符号链接: %s", rotated_path)
            raise RuntimeError(f"进程日志轮转目标不允许是符号链接: log={rotated_path.name}")
        log_path.replace(rotated_path)
