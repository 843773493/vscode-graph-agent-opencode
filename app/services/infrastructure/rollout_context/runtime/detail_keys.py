"""session-scoped context detail redaction key owner。"""

from __future__ import annotations

import os
import secrets
import stat
from dataclasses import dataclass, replace
from pathlib import Path

from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    DetailUnavailableError,
)


@dataclass(frozen=True, slots=True)
class PreparedDetailKey:
    session_id: str
    thread_id: str
    path: Path
    value: bytes
    is_new: bool


class ContextDetailKeyStore:
    """只负责 session detail redaction key 的安全读取与首次创建。"""

    def __init__(self, resolver: SessionCatalogPathResolver) -> None:
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

    def prepare(self, session_id: str, thread_id: str, *, create: bool) -> PreparedDetailKey:
        """冻结既有 key 或新 key；不创建目录或文件。"""
        if not isinstance(session_id, str) or not session_id or "\x00" in session_id:
            raise ValueError("session_id 必须是非空且不含 NUL 的字符串")
        if not isinstance(thread_id, str) or not thread_id or "\x00" in thread_id:
            raise ValueError("thread_id 必须是非空且不含 NUL 的字符串")
        thread_root = self._resolver.resolve_thread_node(session_id, thread_id)
        self._require_directory(thread_root, label="thread node")
        rollout = thread_root / "rollout"
        rollout_info = self._lstat_no_follow(rollout, label="rollout detail 父目录")
        if rollout_info is None:
            if not create:
                raise DetailUnavailableError("redaction key 不存在")
            return PreparedDetailKey(
                session_id, thread_id, rollout / ".context-redaction-key", secrets.token_bytes(32), True,
            )
        if rollout_info is None or not stat.S_ISDIR(rollout_info.st_mode):
            raise DetailUnavailableError("rollout detail 父目录不是安全普通目录")
        try:
            rollout.resolve(strict=True).relative_to(thread_root.resolve(strict=True))
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
            return PreparedDetailKey(session_id, thread_id, key_path, key, False)
        if not create:
            raise DetailUnavailableError("redaction key 不存在")
        return PreparedDetailKey(session_id, thread_id, key_path, secrets.token_bytes(32), True)

    def _publish_new(self, prepared: PreparedDetailKey) -> Path | None:
        thread_root = self._resolver.resolve_thread_node(prepared.session_id, prepared.thread_id)
        self._require_directory(thread_root, label="thread node")
        rollout = thread_root / "rollout"
        if prepared.path != rollout / ".context-redaction-key":
            raise DetailUnavailableError("prepared redaction key owner/path 不一致")
        rollout_info = self._lstat_no_follow(rollout, label="rollout detail 父目录")
        if rollout_info is None:
            rollout.mkdir(exist_ok=True)
        self._require_directory(rollout, label="rollout detail 父目录")
        if not prepared.is_new:
            if self.prepare(prepared.session_id, prepared.thread_id, create=False).value != prepared.value:
                raise DetailUnavailableError("redaction key 在冻结后被改写")
            return None
        try:
            descriptor = os.open(
                prepared.path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except FileExistsError:
            return None
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(prepared.value)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            prepared.path.unlink()
            raise
        return prepared.path

    def publish(self, prepared: PreparedDetailKey) -> Path | None:
        """只排他发布已冻结的新 key；返回本次创建的文件供事务回滚。"""
        created = self._publish_new(prepared)
        if created is None and self.prepare(
            prepared.session_id, prepared.thread_id, create=False
        ).value != prepared.value:
            raise DetailUnavailableError("prepared redaction key 与已发布 key 冲突")
        return created

    def resolve(self, prepared: PreparedDetailKey) -> PreparedDetailKey:
        """独立写入保留首次并发创建时读取获胜 key 的合同。"""
        if prepared.is_new and self._publish_new(prepared) is None:
            # 独立首次写入服从排他发布的获胜者；seal 则必须保持已冻结的 key。
            return self.prepare(prepared.session_id, prepared.thread_id, create=False)
        return replace(prepared, is_new=False)

    def get(self, session_id: str, thread_id: str, *, create: bool) -> bytes:
        return self.resolve(self.prepare(session_id, thread_id, create=create)).value


__all__ = ["ContextDetailKeyStore", "PreparedDetailKey"]
