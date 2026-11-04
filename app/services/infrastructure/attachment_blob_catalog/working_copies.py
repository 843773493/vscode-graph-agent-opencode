"""同一附件 owner 的工作副本锁、初始基线与按需重建。"""

from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from datetime import datetime
from pathlib import Path

import portalocker

from app.core.atomic_fs import fsync_directory, native_io_path
from app.core.file_permissions import restrict_private_path_access
from app.core.session_lifecycle_gate import OperationHolderGate, _CrossProcessFileLock
from app.services.infrastructure.attachment_blob_catalog.catalog import (
    AttachmentBlobCatalog,
)
from app.services.infrastructure.attachment_blob_catalog.directories import (
    _read_file_version,
    protect_original,
    remove_directory,
    save_file_version,
    verify_content,
)
from app.services.infrastructure.attachment_blob_catalog.locator import (
    ingest_staging_relative_locator,
    resolve_attachment_directory,
    resolve_attachment_file,
    resolve_file_version,
    resolve_staging_path,
    validate_attachment_directory_relative_locator,
)
from app.services.infrastructure.attachment_blob_catalog.records import (
    AttachmentFileVersion,
)
from app.services.infrastructure.workspace_files.mutation_lock import (
    workspace_file_mutation_lock,
)

_held_directories = threading.local()


class AttachmentDirectoryHolderGate:
    """稳定目录 ID 独占原 OS holder，避免无关目录分片碰撞后丢失结清事件。"""

    def __init__(self, sessions_root: Path) -> None:
        self._sessions_root = sessions_root

    def lock_for(self, attachment_id: str, *, wait: bool = True) -> _CrossProcessFileLock:
        validate_attachment_directory_relative_locator("2000/01/01/" + attachment_id)
        return OperationHolderGate(self._sessions_root, namespace=f"attachment_directory_{attachment_id}").lock_for(
            attachment_id, wait=wait,
        )


class WorkingCopiesMixin:
    _catalog: AttachmentBlobCatalog
    _workspace_root: Path
    _attachments_root: Path
    _directory_operations: AttachmentDirectoryHolderGate
    _clock: Callable[[], datetime]

    @contextmanager
    def directory_access(self, *attachment_ids: str, wait: bool = True) -> Iterator[None]:
        """同步文件访问复用原 holder 锁文件，允许同一线程的原 owner 嵌套解析。"""
        held: set[Path] = getattr(_held_directories, "paths", set())
        _held_directories.paths = held
        paths = sorted({self._directory_operations.lock_for(value).path for value in attachment_ids} - held)
        with ExitStack() as locks:
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                locks.enter_context(portalocker.Lock(path, mode="a+b", timeout=10 if wait else 0))
            held.update(paths)
            try:
                yield
            finally:
                held.difference_update(paths)

    def _directory_access_held(self, attachment_id: str) -> bool:
        return self._directory_operations.lock_for(attachment_id).path in getattr(_held_directories, "paths", set())

    def ensure_directory(self, attachment_id: str) -> Path:
        with self.directory_access(attachment_id):
            return self._ensure_held_directory(attachment_id)

    def _ensure_held_directory(self, attachment_id: str) -> Path:
        record = self._catalog.get_directory(attachment_id)
        if record is not None and record.availability == "available":
            root = resolve_attachment_directory(self._attachments_root, record.relative_locator)
            if not native_io_path(root).is_dir():
                raise FileNotFoundError(f"附件工作目录意外缺失：attachment_id={attachment_id}")
            return root
        with workspace_file_mutation_lock(self._workspace_root):
            return self._ensure_directory_locked(attachment_id)

    def _ensure_directory_locked(self, attachment_id: str) -> Path:
        record = self._catalog.get_directory(attachment_id)
        if record is None or record.availability == "tombstoned":
            raise FileNotFoundError(f"附件资源不存在：attachment_id={attachment_id}")
        root = resolve_attachment_directory(self._attachments_root, record.relative_locator)
        if record.availability == "available":
            if not native_io_path(root).is_dir():
                raise FileNotFoundError(f"附件工作目录意外缺失：attachment_id={attachment_id}")
            return root
        try:
            availability = record.availability
            if record.availability == "releasing":
                # 中断只允许继续删除已核实的基线残片；新内容必须保留并显式报告。
                if native_io_path(root).exists() and not self._matches_initial_baseline(attachment_id, root, partial=True):
                    raise RuntimeError("释放中断后的附件目录出现非基线内容，保留原目录供核实")
                self._require_saved_original(attachment_id)
                remove_directory(root)
                self._catalog.set_working_copy_state(attachment_id, expected="releasing", state="released", now=self._clock())
                availability = "released"
            original = self._require_saved_original(attachment_id)
            if availability == "released":
                self._catalog.set_working_copy_state(attachment_id, expected="released", state="rebuilding", now=self._clock())
            staging_id = "ing_" + hashlib.sha256(f"rebuild:{attachment_id}".encode()).hexdigest()[:32]
            staging = resolve_staging_path(self._attachments_root, ingest_staging_relative_locator(staging_id))
            if native_io_path(root).exists():
                if not self._matches_initial_baseline(attachment_id, root):
                    raise RuntimeError("重建中的已发布附件与初始基线不一致，保留原目录")
            else:
                remove_directory(staging)
                native_io_path(staging).mkdir(parents=True, mode=0o700)
                restrict_private_path_access(staging, is_directory=True)
                for name in ("original", "preview", "derived"):
                    child = native_io_path(staging / name)
                    child.mkdir(mode=0o700)
                    restrict_private_path_access(child, is_directory=True)
                for version in self._file_versions(attachment_id):
                    source = resolve_file_version(self._attachments_root, version.digest)
                    if version.role == "preview" and not native_io_path(source).exists():
                        continue
                    target = native_io_path(resolve_attachment_file(staging, tuple(version.relative_path.split("/"))))
                    with target.open("xb") as writer:
                        restrict_private_path_access(target, descriptor=writer.fileno())
                        actual = _read_file_version(source, version.relative_path, copy_to=writer)
                        if (actual.digest, actual.length) != (version.digest, version.length):
                            raise RuntimeError(f"附件可靠版本已损坏：attachment_id={attachment_id}")
                        writer.flush()
                        os.fsync(writer.fileno())
                verify_content(staging / "original/content", original.digest, original.length, attachment_id)
                protect_original(staging)
                for name in ("original", "preview", "derived"):
                    fsync_directory(staging / name)
                fsync_directory(staging)
                native_io_path(root.parent).mkdir(parents=True, exist_ok=True)
                os.rename(native_io_path(staging), native_io_path(root))
                fsync_directory(root.parent)
                fsync_directory(staging.parent)
            self._catalog.set_working_copy_state(attachment_id, expected="rebuilding", state="available", now=self._clock())
            return root
        except (OSError, RuntimeError, ValueError) as error:
            self._catalog.retain_working_copy(attachment_id, reason=f"重建未完成：{type(error).__name__}: {error}", now=self._clock())
            raise

    def _file_versions(self, attachment_id: str) -> tuple[AttachmentFileVersion, ...]:
        versions = []
        for path in ("original/content", "preview/512.webp"):
            version = self._catalog.file_version(attachment_id, path)
            if version is not None:
                versions.append(version)
        return tuple(versions)

    def _require_saved_original(self, attachment_id: str) -> AttachmentFileVersion:
        version = self._catalog.file_version(attachment_id, "original/content")
        if version is None:
            raise RuntimeError(f"附件缺少可靠原件版本：attachment_id={attachment_id}")
        verify_content(resolve_file_version(self._attachments_root, version.digest), version.digest, version.length, attachment_id)
        return version

    def _save_directory_versions(self, attachment_id: str, root: Path) -> None:
        for version in self._file_versions(attachment_id):
            content = resolve_attachment_file(root, tuple(version.relative_path.split("/")))
            if version.role == "preview" and not native_io_path(content).exists():
                continue
            save_file_version(self._attachments_root, content, digest=version.digest, length=version.length, attachment_id=attachment_id)
        self._require_saved_original(attachment_id)

    def _matches_initial_baseline(self, attachment_id: str, root: Path, *, partial: bool = False) -> bool:
        physical = native_io_path(root)
        if physical.is_symlink() or not physical.is_dir():
            raise FileNotFoundError("附件目录缺失或不是普通目录")
        versions = self._file_versions(attachment_id)
        children = {"": {"original", "preview", "derived"}, "original": {"content"},
                    "preview": {Path(value.relative_path).name for value in versions if value.role == "preview"}, "derived": set()}
        # 新路径已经证明有修改；不打开派生大文件、FIFO、symlink 或其它处理结果。
        for relative, expected in children.items():
            directory = native_io_path(root / relative)
            if not directory.exists():
                if partial:
                    continue
                return False
            if directory.is_symlink() or not directory.is_dir():
                return False
            actual = {path.name for path in directory.iterdir()}
            if actual - expected or (not partial and relative in {"", "original"} and actual != expected):
                return False
        for version in versions:
            path = resolve_attachment_file(root, tuple(version.relative_path.split("/")))
            if native_io_path(path).exists():
                actual = _read_file_version(path, version.relative_path)
                if (actual.digest, actual.length) != (version.digest, version.length):
                    return False
        return True
