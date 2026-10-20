"""文本 owner 的原字节前置校验与单文件原子发布。"""

from __future__ import annotations

import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.core.hashing import sha256_hex


class WorkspaceFileConflictError(RuntimeError):
    """文件打开后已被其他写入者修改。"""


class FilePublicationUnverifiedError(RuntimeError):
    """已调用发布，但无法核实最终文件；调用者不得自动重放或回滚。"""

    def __init__(self, reference: str, expected_hash: str, observed_hash: str | None) -> None:
        self.reference = reference
        self.expected_hash = expected_hash
        self.observed_hash = observed_hash
        super().__init__(f"文件发布后无法核实，实际效果未知，请重新读取原目标: {reference}; "
                         f"expected_hash={expected_hash}; observed_hash={observed_hash}")


@dataclass(frozen=True, slots=True)
class FilePreimage:
    content: bytes
    metadata: os.stat_result

    @property
    def mode(self) -> int:
        return stat.S_IMODE(self.metadata.st_mode)

    @property
    def content_hash(self) -> str:
        return "sha256:" + sha256_hex(self.content)


def read_file_preimage(path: Path, *, reference: str, max_bytes: int | None = None) -> FilePreimage:
    before = path.lstat()
    if stat.S_ISLNK(before.st_mode):
        raise ValueError(f"不允许编辑符号链接: {reference}")
    if stat.S_ISDIR(before.st_mode):
        raise IsADirectoryError(f"目标不是文件: {reference}")
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"目标不是普通文件: {reference}")
    if max_bytes is not None and before.st_size > max_bytes:
        raise ValueError(f"文件过大，暂不预览: {reference} ({before.st_size} bytes)")
    with path.open("rb") as stream:
        content = stream.read() if max_bytes is None else stream.read(max_bytes + 1)
    after = path.lstat()
    if (before.st_dev, before.st_ino, before.st_mode, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_dev, after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns, after.st_ctime_ns,
    ):
        raise WorkspaceFileConflictError(f"文件在读取期间发生变化，请重新载入: {reference}")
    if max_bytes is not None and len(content) > max_bytes:
        raise ValueError(f"文件过大，暂不预览: {reference} ({len(content)} bytes)")
    return FilePreimage(content, after)


def publish_file_content(path: Path, content: bytes, *, before: FilePreimage, reference: str) -> FilePreimage:
    """调用者持有同一 Workspace mutation lock；外部 writer 仍可在核对后竞争。"""
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        # TODO：Windows 使用继承 ACL；不要把 POSIX mode bits 当作安全边界。
        if os.name != "nt":
            os.chmod(temporary_path, before.mode)
        try:
            latest = read_file_preimage(path, reference=reference, max_bytes=len(before.content))
        except (FileNotFoundError, IsADirectoryError, ValueError) as error:
            raise WorkspaceFileConflictError(f"文件在保存期间发生变化，请重新载入后再保存: {reference}") from error
        if latest.content != before.content:
            raise WorkspaceFileConflictError(f"文件在保存期间发生变化，请重新载入后再保存: {reference}")
        os.replace(temporary_path, path)
        temporary_path = None
        # 发布后不恢复旧内容；外部 writer 的新内容不能被自动回滚覆盖。
        try:
            observed = read_file_preimage(path, reference=reference, max_bytes=len(content))
        except (OSError, ValueError, WorkspaceFileConflictError) as error:
            raise FilePublicationUnverifiedError(reference, "sha256:" + sha256_hex(content), None) from error
        if observed.content != content:
            raise FilePublicationUnverifiedError(reference, "sha256:" + sha256_hex(content), observed.content_hash)
        return observed
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
