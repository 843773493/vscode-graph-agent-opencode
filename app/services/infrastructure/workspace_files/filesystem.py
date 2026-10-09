"""原 operation 的内容清单、durable staging 与无覆盖发布证明。"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from typing import BinaryIO

from app.core.atomic_fs import fsync_directory, fsync_file
from app.services.infrastructure.workspace_files.runtime import (
    rename_entry_exclusive,
)
from app.services.infrastructure.workspace_files.store import canonical_json, digest


class FileEffectUnverifiedError(RuntimeError):
    """原发布效果无法由冻结 inode/内容证明核实，禁止盲目重放。"""


def content_manifest(path: Path) -> dict:
    if path.is_symlink():
        raise ValueError(f"暂不支持复制符号链接: {path}")
    if path.is_file():
        hasher = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                hasher.update(chunk)
                size += len(chunk)
        return {"kind": "file", "size": size, "hash": hasher.hexdigest()}
    if not path.is_dir():
        raise FileNotFoundError(f"复制来源不是文件或目录: {path}")
    children = {}
    for child in sorted(path.iterdir(), key=lambda item: item.name):
        children[child.name] = content_manifest(child)
    return {"kind": "directory", "children": children}


def captured_source(path: Path) -> dict:
    before = path.stat(follow_symlinks=False)
    manifest = content_manifest(path)
    after = path.stat(follow_symlinks=False)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise RuntimeError(f"捕获复制来源时源发生变化: {path}")
    return {"device": after.st_dev, "inode": after.st_ino, "mtime_ns": after.st_mtime_ns,
            "manifest": manifest, "manifest_hash": digest(manifest)}


def write_upload(path: Path, stream: BinaryIO) -> dict:
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
    if path.is_dir():
        for child in path.iterdir():
            sync_tree(child)
        fsync_directory(path)
    else:
        fsync_file(path)


def stage_step(*, stage_root: Path, operation_id: str, request_hash: str,
               source: Path | None, kind: str) -> tuple[Path, dict]:
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
        root = Path(step["stage_root"])
        if not root.exists():
            continue
        marker = root / "owner.json"
        if not marker.is_file() or marker.read_text(encoding="utf-8") != canonical_json({"operation_id": operation_id, "request_hash": request_hash}):
            raise RuntimeError(f"文件 operation 暂存清理的 owner 不一致: {root}")
        shutil.rmtree(root)
        fsync_directory(root.parent)
