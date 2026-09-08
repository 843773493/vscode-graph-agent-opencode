"""session-scoped context detail redaction key owner。"""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path
from typing import Protocol

from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    DetailUnavailableError,
)


class SessionPathResolver(Protocol):
    def resolve_session_node_for_runtime(self, session_id: str) -> Path: ...


class ContextDetailKeyStore:
    """只负责 session detail redaction key 的安全读取与首次创建。"""

    def __init__(self, resolver: SessionPathResolver) -> None:
        self._resolver = resolver

    @staticmethod
    def _lstat_no_follow(path: Path, *, label: str) -> os.stat_result | None:
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode):
            raise DetailUnavailableError(f"{label} 不能是符号链接: {path}")
        return info

    @classmethod
    def _require_directory(cls, path: Path, *, label: str) -> None:
        info = cls._lstat_no_follow(path, label=label)
        if info is None or not stat.S_ISDIR(info.st_mode):
            raise DetailUnavailableError(f"{label} 不是安全普通目录: {path}")

    def get(self, session_id: str, *, create: bool) -> bytes:
        if not isinstance(session_id, str) or not session_id or "\x00" in session_id:
            raise ValueError("session_id 必须是非空且不含 NUL 的字符串")
        session_root = self._resolver.resolve_session_node_for_runtime(session_id)
        self._require_directory(session_root, label="session node")
        rollout = session_root / "rollout"
        rollout_info = self._lstat_no_follow(rollout, label="rollout detail 父目录")
        if rollout_info is None:
            if not create:
                raise DetailUnavailableError("redaction key 不存在")
            try:
                rollout.mkdir()
            except FileExistsError:
                pass
            rollout_info = self._lstat_no_follow(
                rollout,
                label="rollout detail 父目录",
            )
        if rollout_info is None or not stat.S_ISDIR(rollout_info.st_mode):
            raise DetailUnavailableError("rollout detail 父目录不是安全普通目录")
        try:
            rollout.resolve(strict=True).relative_to(session_root.resolve(strict=True))
        except ValueError as error:
            raise DetailUnavailableError(
                "redaction key 路径越出 session node"
            ) from error
        key_path = rollout / ".context-redaction-key"
        key_info = self._lstat_no_follow(key_path, label="redaction key")
        if key_info is not None:
            if not stat.S_ISREG(key_info.st_mode):
                raise DetailUnavailableError("redaction key 不是普通文件")
            descriptor = os.open(
                key_path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise DetailUnavailableError("redaction key 不是普通文件")
                key = os.read(descriptor, 33)
            finally:
                os.close(descriptor)
            if len(key) != 32:
                raise DetailUnavailableError("redaction key 长度非法")
            return key
        if not create:
            raise DetailUnavailableError("redaction key 不存在")
        key = secrets.token_bytes(32)
        try:
            descriptor = os.open(
                key_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except FileExistsError:
            return self.get(session_id, create=False)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(key)
            stream.flush()
            os.fsync(stream.fileno())
        return key


__all__ = ["ContextDetailKeyStore", "SessionPathResolver"]
