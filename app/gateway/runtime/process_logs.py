from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from app.core.file_permissions import restrict_private_path_access
from app.core.logging_contract import RuntimeLogContract, load_runtime_log_contract

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProcessLogStore:
    root: Path
    contract: RuntimeLogContract = field(default_factory=load_runtime_log_contract)

    def path_for(self, role: str, instance: str | int | None = None) -> Path:
        return self.contract.path_for(self.root, role, instance)

    def open(self, role: str, instance: str | int | None = None) -> TextIO:
        log_path = self.path_for(role, instance)
        file_name = log_path.name
        stage = "create_directory"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            stage = "directory_permissions"
            restrict_private_path_access(log_path.parent, is_directory=True)
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
                restrict_private_path_access(log_path, descriptor=descriptor)
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

    def _rotate_if_needed(self, log_path: Path) -> None:
        if not log_path.exists():
            return
        if log_path.stat().st_size < self.contract.max_bytes:
            return
        for index in range(self.contract.archives - 1, 0, -1):
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
