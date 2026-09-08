"""只读识别本 audit 的 link→unlink 崩溃窗口，不接受任意硬链接。"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from app.services.infrastructure.rollout_context.migration.artifacts import (
    read_regular,
    require_safe_path,
)
from app.services.infrastructure.rollout_context.migration.schema_v3.model import (
    SchemaV3UpgradeError,
)


def read_audit_publication(path: Path) -> bytes:
    """只供审计自身文件使用；返回后仍必须校验 manifest/key，不能直接发布。"""
    require_safe_path(path)
    if not path.is_file() or path.lstat().st_nlink == 1:
        return read_regular(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 2:
            raise SchemaV3UpgradeError("schema-upgrade-audit-conflict: 非本次 publication link")
        raw = stream.read()
        after = os.fstat(stream.fileno())
    identity_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_nlink")
    if any(getattr(before, field) != getattr(after, field) for field in identity_fields):
        raise SchemaV3UpgradeError("schema-upgrade-audit-conflict: publication 读取期间变化")
    temporary = path.with_name(f".{path.name}.{hashlib.sha256(raw).hexdigest()}.next")
    require_safe_path(temporary)
    if not temporary.is_file():
        raise SchemaV3UpgradeError("schema-upgrade-audit-conflict: publication 临时 identity 缺失")
    info = temporary.lstat()
    if (info.st_dev, info.st_ino, info.st_nlink) != (before.st_dev, before.st_ino, 2):
        raise SchemaV3UpgradeError("schema-upgrade-audit-conflict: publication 临时 identity 冲突")
    # 不在读取/认证前 unlink。后续 persist/publish/verify 的 immutable_file
    # 只有在完整预检成功时才能清除这个已知第二名字。
    return raw
