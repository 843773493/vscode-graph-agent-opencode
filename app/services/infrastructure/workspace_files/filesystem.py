"""文件操作的内容清单、durable staging、无覆盖发布证明与下载访问边界。"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from app.core.atomic_fs import fsync_directory, fsync_file, native_io_path
from app.services.infrastructure.workspace_files.runtime import (
    rename_entry_exclusive,
)
from app.services.infrastructure.workspace_files.store import canonical_json, digest

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PreparedFileDownload:
    path: Path
    uri: str
    filename: str
    media_type: str
    metadata: os.stat_result
    temporary: bool = False


@contextmanager
def file_download_access(reference: str | None, stage: str) -> Iterator[None]:
    """下载的原生文件操作只发布 URI、实际阶段与系统错误码。"""
    try:
        yield
    except OSError as error:
        logger.exception("文件下载失败: uri=%s stage=%s", reference, stage)
        raise type(error)(
            error.errno,
            f"文件下载失败: uri={reference}, stage={stage}, error={type(error).__name__}, "
            f"errno={error.errno}, winerror={getattr(error, 'winerror', None)}",
        ) from error


class FileEffectUnverifiedError(RuntimeError):
    """原发布效果无法由冻结 inode/内容证明核实，禁止盲目重放。"""


def content_manifest(path: Path, *, validate: Callable[[Path], None] | None = None) -> dict:
    if validate is not None:
        validate(path)
    physical = native_io_path(path)
    if physical.is_symlink():
        raise ValueError(f"暂不支持复制符号链接: {path.name}")
    if physical.is_file():
        hasher = hashlib.sha256()
        size = 0
        with physical.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                hasher.update(chunk)
                size += len(chunk)
        return {"kind": "file", "size": size, "hash": hasher.hexdigest()}
    if not physical.is_dir():
        raise FileNotFoundError(f"复制来源不是文件或目录: {path.name}")
    children = {}
    for child in sorted(physical.iterdir(), key=lambda item: item.name):
        children[child.name] = content_manifest(path / child.name, validate=validate)
    return {"kind": "directory", "children": children}


def captured_source(path: Path, *, validate: Callable[[Path], None] | None = None) -> dict:
    physical = native_io_path(path)
    before = physical.stat(follow_symlinks=False)
    manifest = content_manifest(path, validate=validate)
    after = physical.stat(follow_symlinks=False)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise RuntimeError(f"捕获复制来源时源发生变化: {path.name}")
    return {"device": after.st_dev, "inode": after.st_ino, "mtime_ns": after.st_mtime_ns,
            "manifest": manifest, "manifest_hash": digest(manifest)}


def write_upload(path: Path, stream: BinaryIO) -> dict:
    path = native_io_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    hasher = hashlib.sha256()
    size = 0
    with path.open("xb") as output:
        while chunk := stream.read(1024 * 1024):
            output.write(chunk)
            hasher.update(chunk)
            size += len(chunk)
        output.flush()
        os.fsync(output.fileno())
    fsync_directory(path.parent)
    return {"size": size, "hash": hasher.hexdigest()}


def sync_tree(path: Path) -> None:
    path = native_io_path(path)
    if path.is_dir():
        for child in path.iterdir():
            sync_tree(child)
        fsync_directory(path)
    else:
        fsync_file(path)


def stage_step(*, stage_root: Path, operation_id: str, request_hash: str,
               source: Path | None, kind: str) -> tuple[Path, dict]:
    stage_root = native_io_path(stage_root)
    source = native_io_path(source) if source is not None else None
    marker = {"operation_id": operation_id, "request_hash": request_hash}
    if stage_root.exists():
        marker_path = stage_root / "owner.json"
        if not marker_path.is_file() or marker_path.read_text(encoding="utf-8") != canonical_json(marker):
            raise FileEffectUnverifiedError(f"原 staging 缺少精确 owner 证明: {stage_root}")
        # 只有本 operation 已登记、尚未发布的 staging 可以重建。
        shutil.rmtree(stage_root / "entry", ignore_errors=False) if (stage_root / "entry").is_dir() else (stage_root / "entry").unlink(missing_ok=True)
    else:
        stage_root.mkdir()
        with (stage_root / "owner.json").open("x", encoding="utf-8") as output:
            output.write(canonical_json(marker))
            output.flush()
            os.fsync(output.fileno())
        fsync_directory(stage_root)
        fsync_directory(stage_root.parent)
    entry = stage_root / "entry"
    if source is None:
        if kind == "directory":
            entry.mkdir()
        else:
            entry.touch(exist_ok=False)
    elif kind == "directory":
        shutil.copytree(source, entry)
    else:
        shutil.copy2(source, entry)
    sync_tree(entry)
    fsync_directory(stage_root)
    info = entry.stat(follow_symlinks=False)
    manifest = content_manifest(entry)
    directories = {}
    def capture_directory(path: Path, value: dict) -> None:
        if value["kind"] != "directory":
            return
        stat = path.stat(follow_symlinks=False)
        directories[str(path.relative_to(entry))] = {"device": stat.st_dev, "inode": stat.st_ino}
        for name, child in value["children"].items():
            capture_directory(path / name, child)
    capture_directory(entry, manifest)
    return entry, {"device": info.st_dev, "inode": info.st_ino, "manifest": manifest,
                   "directories": directories}


def verify_published(target: Path, proof: dict) -> bool:
    target = native_io_path(target)
    if not target.exists() and not target.is_symlink():
        return False
    if target.is_symlink():
        return False
    info = target.stat(follow_symlinks=False)
    return (info.st_dev, info.st_ino) == (proof["device"], proof["inode"]) and content_manifest(target) == proof["manifest"]


def publish_step(stage_entry: Path, target: Path, kind: str) -> None:
    # 文件与目录都移动原 inode；崩溃后原 staging 尚在才能证明未发布。
    rename_entry_exclusive(stage_entry, target)
    fsync_directory(target.parent)


def cleanup_steps(steps: list[dict], *, operation_id: str, request_hash: str) -> None:
    for step in steps:
        root = native_io_path(Path(step["stage_root"]))
        if not root.exists():
            continue
        marker = root / "owner.json"
        if not marker.is_file() or marker.read_text(encoding="utf-8") != canonical_json({"operation_id": operation_id, "request_hash": request_hash}):
            raise RuntimeError(f"文件 operation 暂存清理的 owner 不一致: {root}")
        shutil.rmtree(root)
        fsync_directory(root.parent)
