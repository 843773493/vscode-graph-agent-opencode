from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO


@dataclass(frozen=True, slots=True)
class ProcessLogStore:
    root: Path

    def path_for(self, file_name: str) -> Path:
        if not file_name or Path(file_name).name != file_name:
            raise ValueError(f"进程日志文件名非法: {file_name!r}")
        if not file_name.endswith(".log"):
            raise ValueError(f"进程日志必须使用 .log 后缀: {file_name}")
        return self.root / file_name

    def open(self, file_name: str) -> TextIO:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            self.root.chmod(0o700)
        log_path = self.path_for(file_name)
        if log_path.is_symlink():
            raise RuntimeError(f"进程日志文件不允许是符号链接: {log_path}")
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(log_path, flags, 0o600)
        try:
            if os.name == "posix":
                os.fchmod(descriptor, 0o600)
            return os.fdopen(descriptor, "a", encoding="utf-8")
        except Exception:
            os.close(descriptor)
            raise
