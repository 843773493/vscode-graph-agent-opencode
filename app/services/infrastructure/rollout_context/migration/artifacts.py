"""一次性导入的普通文件边界、原件快照和持久化审计。"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from uuid import uuid4

from app.domain.itemized.hashing import canonical_json_bytes


def require_safe_path(path: Path) -> None:
    """逐级拒绝符号链接；检查发生在任何 mkdir/open/rename 之前。"""
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"migration 路径必须是绝对规范路径: {path}")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise RuntimeError(f"migration 路径不能包含符号链接: {current}")
        if current.exists() and current != path and not current.is_dir():
            raise RuntimeError(f"migration 父路径不是普通目录: {current}")


def read_regular(path: Path) -> bytes:
    require_safe_path(path)
    if not path.is_file():
        raise RuntimeError(f"migration artifact 必须是普通文件: {path}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise RuntimeError(f"migration artifact 必须是无硬链接的普通文件: {path}")
        data = stream.read()
        after = os.fstat(stream.fileno())
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ino,
    ):
        raise RuntimeError(f"source-mismatch: migration 读取期间文件变化: {path}")
    return data


def artifact_manifest(root: Path) -> dict[str, dict[str, object]]:
    require_safe_path(root)
    if not root.is_dir():
        raise FileNotFoundError(f"migration artifact 目录不存在: {root}")
    result: dict[str, dict[str, object]] = {}
    for path in sorted(root.rglob("*")):
        require_safe_path(path)
        if path.is_dir():
            continue
        data = read_regular(path)
        result[path.relative_to(root).as_posix()] = {
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    return result


def sync_directory(path: Path) -> None:
    if os.name == "nt":
        # TODO: Windows 的目录 durability barrier 需要原生句柄接口和真实断电验收。
        raise RuntimeError("migration 原子安装暂不支持 Windows directory fsync")
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_private(path: Path, data: bytes) -> None:
    require_safe_path(path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def write_audit(root: Path, report: dict[str, object]) -> None:
    temporary = root / f"report-{uuid4().hex}.next.json"
    write_private(temporary, canonical_json_bytes(report) + b"\n")
    require_safe_path(root / "report.json")
    os.replace(temporary, root / "report.json")
    sync_directory(root)


def copy_source(source: Path, target: Path) -> dict[str, dict[str, object]]:
    """复制全部原始 artifact，未知文件也只保留在受保护审计中。"""
    manifest = artifact_manifest(source)
    target.mkdir(mode=0o700)
    for relative, identity in manifest.items():
        data = read_regular(source / relative)
        if hashlib.sha256(data).hexdigest() != identity["sha256"]:
            raise RuntimeError(f"source-mismatch: migration snapshot: {relative}")
        destination = target / relative
        destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        write_private(destination, data)
    if artifact_manifest(source) != manifest:
        raise RuntimeError("source-mismatch: migration snapshot 期间原件变化")
    for directory in sorted(
        (path for path in target.rglob("*") if path.is_dir()), reverse=True
    ):
        sync_directory(directory)
    sync_directory(target)
    return manifest


def empty_target_identity(root: Path) -> tuple[int, int] | None:
    require_safe_path(root)
    if not root.exists():
        return None
    if not root.is_dir() or any(root.iterdir()):
        raise RuntimeError(
            f"legacy migration target 必须是空 rollout；保留原件: {root}"
        )
    info = root.stat()
    return info.st_dev, info.st_ino


def install_directory(staging: Path, target: Path) -> None:
    """唯一安装点：所有文件和目录先 fsync，再一次 rename 发布整个 rollout。"""
    for path in sorted(staging.rglob("*"), reverse=True):
        require_safe_path(path)
        if path.is_dir():
            sync_directory(path)
        else:
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
    sync_directory(staging)
    require_safe_path(target)
    os.replace(staging, target)
    sync_directory(target.parent)
