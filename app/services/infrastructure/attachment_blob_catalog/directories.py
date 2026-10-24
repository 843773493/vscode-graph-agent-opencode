"""附件目录的完整性、冻结复制与物理删除；所有路径仍经原 locator。"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from pathlib import Path

from app.core.atomic_fs import fsync_directory
from app.core.file_permissions import restrict_private_path_access
from app.services.infrastructure.attachment_blob_catalog.locator import (
    resolve_attachment_file,
)
from app.services.infrastructure.attachment_blob_catalog.records import (
    DirectorySnapshotFile,
)


def file_version(path: Path, relative_path: str) -> DirectorySnapshotFile:
    before = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise RuntimeError("附件目录只允许普通文件和目录，不能冻结 symlink 或特殊节点")
    digest = hashlib.sha256()
    length = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            length += len(chunk)
    after = path.stat(follow_symlinks=False)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise RuntimeError("附件文件在冻结期间发生变化，不能发布混合版本")
    return DirectorySnapshotFile(relative_path, "sha256:" + digest.hexdigest(), length)


def verify_content(content: bytes | Path, digest: str, length: int, attachment_id: str) -> None:
    if isinstance(content, Path):
        actual = file_version(content, content.name)
        actual_digest, actual_length = actual.digest, actual.length
    else:
        actual_digest, actual_length = "sha256:" + hashlib.sha256(content).hexdigest(), len(content)
    if (actual_digest, actual_length) != (digest, length):
        raise RuntimeError(f"附件正文与冻结版本不一致：attachment_id={attachment_id}, expected={digest}/{length}, actual={actual_digest}/{actual_length}")


def directory_manifest(root: Path) -> tuple[tuple[DirectorySnapshotFile, ...], tuple[str, ...]]:
    if root.is_symlink() or not root.is_dir():
        raise FileNotFoundError("附件目录缺失或不是普通目录")
    files, directories = [], []
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        directory = Path(current)
        for name in sorted(directory_names):
            relative = (directory / name).relative_to(root).as_posix()
            path = resolve_attachment_file(root, tuple(relative.split("/")))
            if not path.is_dir():
                raise RuntimeError("附件目录在冻结期间发生变化")
            directories.append(relative)
        for name in sorted(file_names):
            relative = (directory / name).relative_to(root).as_posix()
            path = resolve_attachment_file(root, tuple(relative.split("/")))
            files.append(file_version(path, relative))
    return tuple(sorted(files, key=lambda file: file.relative_path)), tuple(sorted(directories))


def copy_directory_files(source: Path, target: Path, files: tuple[DirectorySnapshotFile, ...], directories: tuple[str, ...]) -> None:
    """只复制冻结清单，重复执行只复验已完成的本 intent 文件，不覆盖损坏成品。"""
    if tuple(sorted({file.relative_path for file in files})) != tuple(file.relative_path for file in files) or tuple(sorted(set(directories))) != directories:
        raise RuntimeError("附件目录冻结清单有重复或顺序不规范")
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    restrict_private_path_access(target, is_directory=True)
    for directory in directories:
        path = resolve_attachment_file(target, tuple(directory.split("/")))
        path.mkdir(exist_ok=True, mode=0o700)
        restrict_private_path_access(path, is_directory=True)
    for file in files:
        original = resolve_attachment_file(source, tuple(file.relative_path.split("/")))
        destination = resolve_attachment_file(target, tuple(file.relative_path.split("/")))
        verify_content(original, file.digest, file.length, file.relative_path)
        if destination.exists():
            verify_content(destination, file.digest, file.length, file.relative_path)
            continue
        with original.open("rb") as reader, destination.open("xb") as writer:
            restrict_private_path_access(destination, descriptor=writer.fileno())
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        verify_content(destination, file.digest, file.length, file.relative_path)
    if directory_manifest(target) != (files, directories):
        raise RuntimeError("附件目录复制结果与冻结清单不一致")
    for directory in sorted(directories, key=lambda value: value.count("/"), reverse=True):
        fsync_directory(resolve_attachment_file(target, tuple(directory.split("/"))))
    fsync_directory(target)
    fsync_directory(target.parent)


def protect_original(root: Path) -> None:
    """只在 live 目录发布前保护原件；冻结 capture 仍由原 owner 校验和清理。"""
    original = resolve_attachment_file(root, ("original",))
    for file in original.iterdir():
        if file.is_file():
            # 本入口只处理发布前的目录。Windows 重试须先撤下只读位取得
            # 可写句柄，再保护并同步同一文件，不能跳过失败后的 durability。
            if os.name == "nt":
                file.chmod(0o600)
            with file.open("r+b" if os.name == "nt" else "rb") as stream:
                file.chmod(0o400)
                os.fsync(stream.fileno())
    original.chmod(0o500)
    fsync_directory(original)


def remove_directory(path: Path) -> None:
    if path.is_symlink():
        raise RuntimeError("附件目录删除不能经过符号链接")
    if not path.exists():
        return
    original = path / "original"
    if original.is_symlink():
        raise RuntimeError("附件原件目录不能是符号链接")
    if original.is_dir():
        original.chmod(0o700)
        # Windows 的只读位也阻止删除文件；仅恢复已核对的普通原件。
        for file in original.iterdir():
            if not file.is_symlink() and file.is_file():
                file.chmod(0o600)
    shutil.rmtree(path)
    fsync_directory(path.parent)
