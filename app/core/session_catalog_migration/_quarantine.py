"""quarantine 隔离的耐久意图、内容证据与定点恢复。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import cast

from app.core.atomic_fs import fsync_directory as _fsync_directory

from ._constants import _JOURNAL_DIRECTORY_NAME, _ORPHANED_DIR_NAME
from ._contracts import _MigrationContext

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_QUARANTINE_INTENT_KEYS = frozenset(
    {
        "node_id",
        "kind",
        "old_relative_path",
        "target_relative_path",
        "tree",
        "tree_sha256",
    }
)


def _tree_sha256(tree: list[dict[str, object]]) -> str:
    encoded = json.dumps(
        tree, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_quarantine_intent(
    value: object,
    *,
    node_id: str,
    kind: str,
    old_relative_path: str,
) -> dict[str, object]:
    """校验 journal 中隔离证据的唯一结构与路径约束。"""
    if not isinstance(value, dict) or value.keys() != _QUARANTINE_INTENT_KEYS:
        raise ValueError("quarantine_intent 字段集合非法")
    if value.get("node_id") != node_id or value.get("kind") != kind:
        raise ValueError("quarantine_intent 节点 ID 或类型不匹配")
    if value.get("old_relative_path") != old_relative_path:
        raise ValueError("quarantine_intent 旧路径与 physical 记录不匹配")
    target_relative_path = PurePosixPath(
        _ORPHANED_DIR_NAME, _JOURNAL_DIRECTORY_NAME, node_id
    ).as_posix()
    if value.get("target_relative_path") != target_relative_path:
        raise ValueError("quarantine_intent 隔离目标路径不匹配")

    tree = value.get("tree")
    tree_sha256 = value.get("tree_sha256")
    if not isinstance(tree, list):
        raise TypeError("quarantine_intent.tree 必须是 list")
    if not isinstance(tree_sha256, str):
        raise TypeError("quarantine_intent.tree_sha256 必须是字符串")
    entries: dict[str, str] = {}
    paths: list[str] = []
    for offset, entry in enumerate(tree):
        if not isinstance(entry, dict):
            raise TypeError(f"quarantine_intent.tree[{offset}] 必须是 object")
        path = entry.get("path")
        entry_kind = entry.get("kind")
        if not isinstance(path, str) or entry_kind not in ("directory", "file"):
            raise ValueError(f"quarantine_intent.tree[{offset}] 路径或类型非法")
        if path == "":
            if entry_kind != "directory" or entry.keys() != {"path", "kind"}:
                raise ValueError("quarantine_intent.tree 根项必须是目录")
        else:
            candidate = PurePosixPath(path)
            if (
                candidate.is_absolute()
                or "\\" in path
                or candidate.as_posix() != path
                or any(part in ("", ".", "..") for part in candidate.parts)
            ):
                raise ValueError(f"quarantine_intent.tree[{offset}].path 非法")
            if entry_kind == "directory":
                if entry.keys() != {"path", "kind"}:
                    raise ValueError(f"quarantine_intent.tree[{offset}] 目录字段非法")
            else:
                size = entry.get("size")
                sha256 = entry.get("sha256")
                if (
                    entry.keys() != {"path", "kind", "size", "sha256"}
                    or not isinstance(size, int)
                    or isinstance(size, bool)
                    or size < 0
                    or not isinstance(sha256, str)
                    or _SHA256_PATTERN.fullmatch(sha256) is None
                ):
                    raise ValueError(f"quarantine_intent.tree[{offset}] 文件证据非法")
        if path in entries:
            raise ValueError(f"quarantine_intent.tree 路径重复: {path!r}")
        entries[path] = cast(str, entry_kind)
        paths.append(path)

    if not tree or entries.get("") != "directory" or paths != sorted(paths):
        raise ValueError("quarantine_intent.tree 缺少根目录或顺序非法")
    for path, entry_kind in entries.items():
        if not path:
            continue
        parent = PurePosixPath(path).parent.as_posix()
        if parent == ".":
            parent = ""
        if entries.get(parent) != "directory":
            raise ValueError(f"quarantine_intent.tree 父目录缺失: {path!r}")
    if (
        _SHA256_PATTERN.fullmatch(tree_sha256) is None
        or _tree_sha256(cast(list[dict[str, object]], tree)) != tree_sha256
    ):
        raise ValueError("quarantine_intent.tree_sha256 不匹配")
    return cast(dict[str, object], value)


class SessionCatalogMigratorQuarantineMixin:
    """隔离 physical 节点：intent 先落盘，目录再原子 rename。"""

    def _process_quarantine_node(
        self,
        node_id: str,
        record: dict[str, object],
        context: _MigrationContext,
        *,
        kind: str,
    ) -> None:
        stage = "物理迁移(quarantine 隔离)"
        old_path = self._old_path_for(record, stage=stage)
        target = self._quarantine_target_path(node_id)
        empty_target = False
        if old_path.name != node_id:
            raise self._fail(
                stage,
                f"旧位置叶名与 journal 节点 ID 不一致: "
                f"node_id={node_id!r}, path={old_path}",
            )

        if record["state"] == "pending":
            if not old_path.is_dir() or old_path.is_symlink():
                id_field = "folder_id" if kind == "folder" else "session_id"
                raise self._fail(
                    stage,
                    "旧位置目录已不存在且 journal 记 pending,无法证明"
                    f"(外部改动,拒绝继续): {id_field}={node_id}, path={old_path}",
                )
            if target.exists() or target.is_symlink():
                if not target.is_dir() or target.is_symlink():
                    raise self._fail(
                        stage,
                        "隔离目录已存在且非空(拒绝覆盖,保留审计): "
                        f"node_id={node_id}, target={target}",
                    )
                try:
                    empty_target = next(target.iterdir(), None) is None
                except OSError as error:
                    raise self._fail(
                        stage,
                        f"隔离目标目录无法枚举: node_id={node_id}, "
                        f"target={target}: {error}",
                    ) from error
                if not empty_target:
                    raise self._fail(
                        stage,
                        "隔离目录已存在且非空(拒绝覆盖,保留审计): "
                        f"node_id={node_id}, target={target}",
                    )
            self._prepare_quarantine_parent(node_id, stage=stage)
            if empty_target:
                try:
                    target.rmdir()
                except OSError as error:
                    raise self._fail(
                        stage,
                        f"既有空隔离目录无法移除: node_id={node_id}, "
                        f"target={target}: {error}",
                    ) from error
                _fsync_directory(target.parent)
            self._assert_same_filesystem(old_path, target.parent, stage=stage)
            tree = self._collect_quarantine_tree(old_path, node_id=node_id, stage=stage)
            relative = record.get("old_relative_path")
            if not isinstance(relative, str):
                raise self._fail(
                    stage,
                    f"physical 记录缺少 old_relative_path: node_id={node_id}",
                )
            intent: dict[str, object] = {
                "node_id": node_id,
                "kind": kind,
                "old_relative_path": relative,
                "target_relative_path": self._quarantine_target_relative_path(node_id),
                "tree": tree,
                "tree_sha256": _tree_sha256(tree),
            }
            self._validate_intent(intent, node_id, record, kind, stage=stage)
            record["quarantine_intent"] = intent
            record["state"] = "quarantine_intent"
            # durable intent 必须先于目录 rename，崩溃后才能核验两侧之一。
            self._write_journal_context(context, state="catalog_rebuilt", result=None)

        if record["state"] == "quarantine_isolated":
            self._verify_quarantine_isolated(
                node_id, record, kind=kind, stage=stage
            )
            return
        if record["state"] != "quarantine_intent":
            raise self._fail(
                stage,
                f"隔离节点状态非法: node_id={node_id}, state={record['state']!r}",
            )

        intent = self._validate_intent(
            record.get("quarantine_intent"), node_id, record, kind, stage=stage
        )
        source_present = old_path.exists() or old_path.is_symlink()
        target_present = target.exists() or target.is_symlink()
        if source_present == target_present:
            raise self._fail(
                stage,
                "隔离 intent 恢复时源/目标必须恰有一侧存在: "
                f"node_id={node_id}, source={old_path}, target={target}, "
                f"source_present={source_present}, target_present={target_present}",
            )
        if source_present:
            self._rename_quarantine_source(
                old_path, target, intent, node_id, record, context, kind, stage
            )
            return
        self._prepare_quarantine_parent(node_id, stage=stage)
        try:
            _fsync_directory(target.parent)
            _fsync_directory(old_path.parent)
        except OSError as error:
            raise self._fail(
                stage,
                "隔离恢复目录 durability fsync 失败: "
                f"node_id={node_id}, target_parent={target.parent}, "
                f"source_parent={old_path.parent}: {error}",
            ) from error
        self._verify_quarantine_tree(
            target, intent, node_id=node_id, record=record, kind=kind, stage=stage
        )
        record["state"] = "quarantine_isolated"
        self._write_journal_context(context, state="catalog_rebuilt", result=None)

    def _rename_quarantine_source(
        self,
        source: Path,
        target: Path,
        intent: dict[str, object],
        node_id: str,
        record: dict[str, object],
        context: _MigrationContext,
        kind: str,
        stage: str,
    ) -> None:
        self._prepare_quarantine_parent(node_id, stage=stage)
        self._assert_same_filesystem(source, target.parent, stage=stage)
        self._verify_quarantine_tree(
            source, intent, node_id=node_id, record=record, kind=kind, stage=stage
        )
        try:
            os.rename(source, target)
        except OSError as error:
            raise self._fail(
                stage,
                f"旧位置 rename 到隔离区失败: {source} -> {target}: {error}",
            ) from error
        _fsync_directory(target.parent)
        _fsync_directory(source.parent)
        self._verify_quarantine_tree(
            target, intent, node_id=node_id, record=record, kind=kind, stage=stage
        )
        record["state"] = "quarantine_isolated"
        self._write_journal_context(context, state="catalog_rebuilt", result=None)

    def _quarantine_target_relative_path(self, node_id: str) -> str:
        if PurePosixPath(node_id).name != node_id or node_id in (".", ".."):
            raise self._fail(
                "物理迁移(quarantine 隔离)",
                f"quarantine 节点 ID 不能作为隔离目录叶名: {node_id!r}",
            )
        return PurePosixPath(
            _ORPHANED_DIR_NAME, _JOURNAL_DIRECTORY_NAME, node_id
        ).as_posix()

    def _quarantine_target_path(self, node_id: str) -> Path:
        self._quarantine_target_relative_path(node_id)
        return self._resolved_orphaned_root / node_id

    def _prepare_quarantine_parent(self, node_id: str, *, stage: str) -> None:
        self._quarantine_target_relative_path(node_id)
        orphaned_root = self._resolved_sessions_root.parent / _ORPHANED_DIR_NAME
        for path in (orphaned_root, self._resolved_orphaned_root):
            parent = path.parent
            if parent.is_symlink() or not parent.is_dir():
                raise self._fail(
                    stage, f"隔离目标上级目录不是普通目录: path={parent}"
                )
            if path.is_symlink() or (path.exists() and not path.is_dir()):
                raise self._fail(stage, f"隔离目标父目录不是普通目录: path={path}")
            try:
                path.mkdir(exist_ok=True)
            except OSError as error:
                raise self._fail(
                    stage, f"隔离目标父目录无法创建: path={path}: {error}"
                ) from error
            if path.is_symlink() or not path.is_dir():
                raise self._fail(stage, f"隔离目标父目录不是普通目录: path={path}")
            try:
                # 确认已有目录项也同步到磁盘，再允许后续层级或节点 rename。
                _fsync_directory(parent)
            except OSError as error:
                raise self._fail(
                    stage,
                    f"隔离目标父目录 durability fsync 失败: path={parent}: {error}",
                ) from error

    def _assert_same_filesystem(
        self, source: Path, target_parent: Path, *, stage: str
    ) -> None:
        try:
            source_device = source.stat().st_dev
            target_device = target_parent.stat().st_dev
        except OSError as error:
            raise self._fail(
                stage,
                f"无法核对隔离 rename 的文件系统: source={source}, "
                f"target_parent={target_parent}: {error}",
            ) from error
        if source_device != target_device:
            raise self._fail(
                stage,
                "隔离源目录与目标目录不在同一文件系统,不能保证原子 rename: "
                f"source={source}, target_parent={target_parent}",
            )

    def _collect_quarantine_tree(
        self, directory: Path, *, node_id: str, stage: str
    ) -> list[dict[str, object]]:
        """枚举整个隔离树，保留空目录并拒绝 symlink/特殊文件。"""
        if not directory.is_dir() or directory.is_symlink():
            raise self._fail(
                stage, f"隔离内容根必须是普通目录: node_id={node_id}, path={directory}"
            )
        entries: list[dict[str, object]] = [{"path": "", "kind": "directory"}]
        pending = [directory]
        while pending:
            parent = pending.pop()
            try:
                children = sorted(parent.iterdir(), key=lambda item: item.name)
            except OSError as error:
                raise self._fail(
                    stage,
                    f"隔离目录无法枚举: node_id={node_id}, path={parent}: {error}",
                ) from error
            for child in children:
                relative = child.relative_to(directory).as_posix()
                try:
                    metadata = child.lstat()
                except OSError as error:
                    raise self._fail(
                        stage,
                        f"隔离目录项无法读取: node_id={node_id}, path={child}: {error}",
                    ) from error
                if stat.S_ISLNK(metadata.st_mode):
                    raise self._fail(
                        stage,
                        f"隔离树含符号链接,无法冻结内容证据: "
                        f"node_id={node_id}, path={child}",
                    )
                if stat.S_ISDIR(metadata.st_mode):
                    entries.append({"path": relative, "kind": "directory"})
                    pending.append(child)
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    raise self._fail(
                        stage, f"隔离树含非普通文件: node_id={node_id}, path={child}"
                    )
                entries.append(
                    self._quarantine_file_entry(
                        child, relative, node_id=node_id, stage=stage
                    )
                )
        return sorted(entries, key=lambda entry: str(entry["path"]))

    def _quarantine_file_entry(
        self, path: Path, relative: str, *, node_id: str, stage: str
    ) -> dict[str, object]:
        try:
            before = path.stat(follow_symlinks=False)
            digest = hashlib.sha256()
            size = 0
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
            after = path.stat(follow_symlinks=False)
        except OSError as error:
            raise self._fail(
                stage, f"隔离文件无法读取: node_id={node_id}, path={path}: {error}"
            ) from error
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) or size != before.st_size:
            raise self._fail(
                stage, f"隔离文件在冻结期间发生变化: node_id={node_id}, path={path}"
            )
        return {
            "path": relative,
            "kind": "file",
            "size": size,
            "sha256": digest.hexdigest(),
        }

    def _validate_intent(
        self,
        value: object,
        node_id: str,
        record: dict[str, object],
        kind: str,
        *,
        stage: str,
    ) -> dict[str, object]:
        relative = record.get("old_relative_path")
        if not isinstance(relative, str):
            raise self._fail(stage, f"physical 记录缺少旧路径: node_id={node_id}")
        try:
            return validate_quarantine_intent(
                value,
                node_id=node_id,
                kind=kind,
                old_relative_path=relative,
            )
        except (TypeError, ValueError) as error:
            raise self._fail(
                stage, f"隔离 intent 结构非法: node_id={node_id}: {error}"
            ) from error

    def _verify_quarantine_tree(
        self,
        directory: Path,
        intent: dict[str, object],
        *,
        node_id: str,
        record: dict[str, object],
        kind: str,
        stage: str,
    ) -> None:
        validated = self._validate_intent(
            intent, node_id, record, kind, stage=stage
        )
        expected_tree = validated["tree"]
        actual_tree = self._collect_quarantine_tree(
            directory, node_id=node_id, stage=stage
        )
        actual_sha = _tree_sha256(actual_tree)
        if actual_tree != expected_tree or actual_sha != validated["tree_sha256"]:
            raise self._fail(
                stage,
                "隔离目录与 durable intent 的完整内容证据不一致: "
                f"node_id={node_id}, path={directory}, "
                f"expected_sha256={validated['tree_sha256']}, actual_sha256={actual_sha}",
            )

    def _verify_quarantine_isolated(
        self,
        node_id: str,
        record: dict[str, object],
        *,
        kind: str,
        stage: str,
    ) -> None:
        old_path = self._old_path_for(record, stage=stage)
        target = self._quarantine_target_path(node_id)
        if old_path.exists() or old_path.is_symlink():
            raise self._fail(
                stage,
                f"隔离完成后旧位置重现,拒绝双份吸收: node_id={node_id}, "
                f"source={old_path}",
            )
        if not target.is_dir() or target.is_symlink():
            raise self._fail(
                stage,
                f"journal 记 quarantine_isolated 但隔离目录缺失或非法: "
                f"node_id={node_id}, target={target}",
            )
        intent = self._validate_intent(
            record.get("quarantine_intent"), node_id, record, kind, stage=stage
        )
        self._verify_quarantine_tree(
            target, intent, node_id=node_id, record=record, kind=kind, stage=stage
        )
