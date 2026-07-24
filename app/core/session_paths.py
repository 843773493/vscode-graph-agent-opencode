from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.core.identifier import create_prefixed_id
from app.core.legacy_inline_attachment_migration import (
    materialize_legacy_inline_attachments,
)


FOLDER_MANIFEST_NAME = ".boxteam-folder.json"
SESSION_MANIFEST_NAME = "session.json"
SESSION_CHILDREN_DIR_NAME = "children"
SESSION_ALLOCATION_MARKER_NAME = ".boxteam-session-allocating.json"
SESSION_ALLOCATION_TEMP_PREFIX = ".boxteam-session-allocating-"
PHYSICAL_LAYOUT_VERSION = 1
_INVALID_SEGMENT_CHARS = re.compile(r"[<>:\"/\\|?*\x00-\x1f]")
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


@dataclass(frozen=True, slots=True)
class SessionPhysicalNode:
    node_id: str
    kind: str
    path: Path
    parent_node_id: str | None
    name: str
    created_at: datetime
    updated_at: datetime


class SessionPathResolver:
    """从工作区物理目录树解析稳定会话与文件夹 ID。"""

    def __init__(self, sessions_root: Path) -> None:
        self.sessions_root = sessions_root.resolve()
        self._nodes: dict[str, SessionPhysicalNode] = {}
        self._navigation_mtimes: dict[Path, tuple[int, int, int]] = {}
        self._lock = threading.RLock()
        self._loaded = False
        self._revision = 0
        self._deleting_subtrees: set[str] = set()

    def initialize(self) -> None:
        with self._lock:
            self.sessions_root.mkdir(parents=True, exist_ok=True)
            self._migrate_legacy_layout()
            self._migrate_physical_session_parents()
            self._migrate_legacy_session_locators()
            self._migrate_legacy_inline_attachments()
            self.refresh()

    def invalidate(self) -> None:
        with self._lock:
            self._loaded = False
            self._nodes = {}
            self._navigation_mtimes = {}

    def refresh(self) -> list[SessionPhysicalNode]:
        with self._lock:
            return self._refresh_locked()

    def _refresh_locked(self) -> list[SessionPhysicalNode]:
        self.sessions_root.mkdir(parents=True, exist_ok=True)
        nodes: dict[str, SessionPhysicalNode] = {}
        paths_by_id: dict[str, list[Path]] = {}

        def visit(parent_path: Path, parent_node_id: str | None) -> None:
            for child in sorted(parent_path.iterdir(), key=lambda item: item.name.casefold()):
                if not child.is_dir() or child.is_symlink():
                    continue
                folder_manifest = child / FOLDER_MANIFEST_NAME
                session_manifest = child / SESSION_MANIFEST_NAME
                allocation_marker = child / SESSION_ALLOCATION_MARKER_NAME
                if allocation_marker.exists() and self._recover_allocation_marker(
                    child,
                    has_manifest=session_manifest.exists(),
                ):
                    continue
                if folder_manifest.exists() and session_manifest.exists():
                    raise RuntimeError(
                        f"物理节点同时包含文件夹与会话 manifest: {child}"
                    )
                if folder_manifest.exists():
                    node = self._read_folder_node(
                        child,
                        folder_manifest,
                        parent_node_id,
                    )
                elif session_manifest.exists():
                    node = self._read_session_node(
                        child,
                        session_manifest,
                        parent_node_id,
                    )
                elif allocation_marker.exists():
                    # 当前活跃创建窗口尚未写出 session.json。
                    continue
                elif child.name.startswith(SESSION_ALLOCATION_TEMP_PREFIX):
                    if any(child.iterdir()):
                        raise RuntimeError(
                            "会话临时分配目录缺少 marker 且包含数据: "
                            f"path={child}"
                        )
                    child.rmdir()
                    continue
                else:
                    raise RuntimeError(
                        "会话物理目录包含未托管子目录，拒绝静默忽略: "
                        f"path={child}"
                    )

                paths_by_id.setdefault(node.node_id, []).append(child)
                if node.node_id not in nodes:
                    nodes[node.node_id] = node
                if node.kind == "folder":
                    visit(child, node.node_id)
                else:
                    children_path = child / SESSION_CHILDREN_DIR_NAME
                    nested_manifests = [
                        path
                        for manifest_name in (FOLDER_MANIFEST_NAME, SESSION_MANIFEST_NAME)
                        for path in child.rglob(manifest_name)
                        if path != session_manifest
                        and not path.is_relative_to(children_path)
                    ]
                    if nested_manifests:
                        raise RuntimeError(
                            "会话导航子节点必须位于保留的 children 目录下: "
                            f"session_id={node.node_id}, manifests={nested_manifests}"
                        )
                    if children_path.exists():
                        if not children_path.is_dir() or children_path.is_symlink():
                            raise RuntimeError(
                                "会话 children 边界必须是真实目录: "
                                f"session_id={node.node_id}, path={children_path}"
                            )
                        visit(children_path, node.node_id)

        visit(self.sessions_root, None)
        duplicates = {
            node_id: paths
            for node_id, paths in paths_by_id.items()
            if len(paths) > 1
        }
        if duplicates:
            details = "; ".join(
                f"{node_id}={','.join(str(path) for path in paths)}"
                for node_id, paths in sorted(duplicates.items())
            )
            raise RuntimeError(f"物理会话目录存在重复稳定 ID: {details}")
        for node in nodes.values():
            if node.parent_node_id is None:
                continue
            parent = nodes.get(node.parent_node_id)
            if parent is None:
                raise RuntimeError(
                    f"物理会话节点父节点不存在: node_id={node.node_id}, "
                    f"parent={node.parent_node_id}"
                )
            if node.kind != "session":
                continue
            expected_parent_session_id = self._nearest_session_ancestor_from_nodes(
                node.parent_node_id,
                nodes,
            )
            declared_parent_session_id = _read_json_object(
                node.path / SESSION_MANIFEST_NAME
            ).get("parent_session_id")
            if declared_parent_session_id != expected_parent_session_id:
                raise RuntimeError(
                    "会话 parent_session_id 与物理祖先不一致: "
                    f"session_id={node.node_id}, declared={declared_parent_session_id}, "
                    f"physical={expected_parent_session_id}, path={node.path}"
                )
        self._nodes = nodes
        self._navigation_mtimes = {
            path: _navigation_signature(path)
            for path in (
                self.sessions_root,
                *(node.path for node in nodes.values()),
                *(
                    node.path / SESSION_CHILDREN_DIR_NAME
                    for node in nodes.values()
                    if node.kind == "session"
                    and (node.path / SESSION_CHILDREN_DIR_NAME).is_dir()
                ),
                *(
                    node.path
                    / (
                        FOLDER_MANIFEST_NAME
                        if node.kind == "folder"
                        else SESSION_MANIFEST_NAME
                    )
                    for node in nodes.values()
                ),
            )
        }
        self._loaded = True
        self._revision += 1
        return list(nodes.values())

    @property
    def revision(self) -> int:
        """返回物理树进程内修订号，并先检查人工文件系统变更。"""
        with self._lock:
            self._ensure_loaded()
            self._refresh_if_navigation_changed_locked()
            return self._revision

    def list_nodes(self, *, refresh: bool = False) -> list[SessionPhysicalNode]:
        with self._lock:
            if refresh or not self._loaded:
                return self._refresh_locked()
            self._refresh_if_navigation_changed_locked()
            return list(self._nodes.values())

    def get_node(self, node_id: str) -> SessionPhysicalNode:
        with self._lock:
            self._ensure_loaded()
            self._refresh_node_if_changed_locked(node_id)
            node = self._nodes.get(node_id)
            if node is None:
                raise KeyError(f"物理会话节点不存在: {node_id}")
            return node

    def resolve_session_dir(self, session_id: str) -> Path:
        node = self.get_node(session_id)
        if node.kind != "session":
            raise RuntimeError(f"节点不是会话: node_id={session_id}, path={node.path}")
        return node.path

    def resolve_folder_dir(self, folder_id: str) -> Path:
        node = self.get_node(folder_id)
        if node.kind != "folder":
            raise RuntimeError(f"节点不是会话文件夹: node_id={folder_id}, path={node.path}")
        return node.path

    def allocate_session_dir(
        self,
        *,
        session_id: str,
        title: str,
        parent_node_id: str | None = None,
    ) -> Path:
        with self._lock:
            self._ensure_loaded()
            self._refresh_if_navigation_changed_locked()
            if session_id in self._nodes:
                raise FileExistsError(f"会话稳定 ID 已存在: {session_id}")
            self._assert_parent_mutable(parent_node_id)
            parent_path = self._parent_path(parent_node_id)
            target = parent_path / physical_segment(title, session_id)
            if target.exists():
                raise FileExistsError(f"会话物理目录目标已存在: {target}")
            temporary = Path(
                tempfile.mkdtemp(
                    prefix=SESSION_ALLOCATION_TEMP_PREFIX,
                    dir=parent_path,
                )
            )
            try:
                _atomic_write_json(
                    temporary / SESSION_ALLOCATION_MARKER_NAME,
                    {
                        "session_id": session_id,
                        "created_at": datetime.now(UTC).isoformat(),
                        "pid": os.getpid(),
                        "process_identity": _process_identity(os.getpid()),
                    },
                )
                os.rename(temporary, target)
                return target
            except BaseException:
                self.abandon_session_allocation(temporary)
                raise

    def register_session(
        self,
        session_id: str,
        session_dir: Path,
    ) -> SessionPhysicalNode:
        with self._lock:
            resolved_session_dir = session_dir.resolve()
            if not resolved_session_dir.is_relative_to(self.sessions_root):
                raise RuntimeError(
                    "注册会话目录越出 sessions 根目录: "
                    f"session_id={session_id}, path={resolved_session_dir}"
                )
            marker_path = resolved_session_dir / SESSION_ALLOCATION_MARKER_NAME
            marker = _read_json_object(marker_path)
            if marker.get("session_id") != session_id:
                raise RuntimeError(
                    "注册会话时分配标记 ID 不匹配: "
                    f"expected={session_id}, marker={marker}"
                )
            manifest_path = resolved_session_dir / SESSION_MANIFEST_NAME
            if not manifest_path.is_file():
                raise RuntimeError(f"注册会话时缺少 session.json: {manifest_path}")
            manifest = _read_json_object(manifest_path)
            if manifest.get("session_id") != session_id:
                raise RuntimeError(
                    "注册会话时 manifest ID 不匹配: "
                    f"expected={session_id}, actual={manifest.get('session_id')}"
                )
            marker_path.unlink()
            self._refresh_locked()
            return self.get_node(session_id)

    def abandon_session_allocation(self, session_dir: Path) -> None:
        """回收尚未形成有效 session.json 的会话目录。"""
        with self._lock:
            marker = session_dir / SESSION_ALLOCATION_MARKER_NAME
            marker.unlink(missing_ok=True)
            if session_dir.exists() and not any(session_dir.iterdir()):
                session_dir.rmdir()

    def create_folder(
        self,
        *,
        name: str,
        parent_node_id: str | None,
        folder_id: str | None = None,
        created_at: datetime | None = None,
    ) -> SessionPhysicalNode:
        with self._lock:
            self._ensure_loaded()
            self._refresh_if_navigation_changed_locked()
            resolved_folder_id = folder_id or create_prefixed_id("fld")
            if resolved_folder_id in self._nodes:
                raise FileExistsError(f"会话文件夹稳定 ID 已存在: {resolved_folder_id}")
            self._assert_parent_mutable(parent_node_id)
            parent_path = self._parent_path(parent_node_id)
            target = parent_path / physical_segment(name, resolved_folder_id)
            target.mkdir(parents=False, exist_ok=False)
            timestamp = created_at or datetime.now(UTC)
            self._write_folder_manifest(
                target,
                folder_id=resolved_folder_id,
                created_at=timestamp,
            )
            self._refresh_locked()
            return self.get_node(resolved_folder_id)

    def move_node(
        self,
        *,
        node_id: str,
        parent_node_id: str | None,
        name: str | None = None,
    ) -> SessionPhysicalNode:
        with self._lock:
            self._ensure_loaded()
            node = self.get_node(node_id)
            self._assert_node_mutable(node_id)
            self._assert_parent_mutable(parent_node_id)
            if parent_node_id == node_id:
                raise ValueError(f"节点不能移动到自身下: {node_id}")
            if parent_node_id is not None:
                self._assert_not_descendant(node_id, parent_node_id)
            if node.kind == "session":
                declared_parent_session_id = _read_json_object(
                    node.path / SESSION_MANIFEST_NAME
                ).get("parent_session_id")
                target_parent_session_id = self.nearest_session_ancestor(
                    parent_node_id
                )
                if declared_parent_session_id != target_parent_session_id:
                    raise ValueError(
                        "会话跨父会话移动必须同时更新 parent_session_id: "
                        f"session_id={node_id}, declared={declared_parent_session_id}, "
                        f"target={target_parent_session_id}"
                    )
            parent_path = self._parent_path(parent_node_id)
            target_name = physical_segment(name or node.name, node.node_id)
            target = parent_path / target_name
            if target == node.path:
                return node
            if target.exists():
                raise FileExistsError(f"会话物理节点目标已存在: {target}")
            os.replace(node.path, target)
            self._refresh_locked()
            return self.get_node(node_id)

    def relocate_session(
        self,
        *,
        session_id: str,
        parent_node_id: str | None,
        name: str,
        manifest: dict[str, object],
    ) -> SessionPhysicalNode:
        """把会话子树与 manifest 作为一个受锁操作迁移。"""
        with self._lock:
            self._ensure_loaded()
            node = self.get_node(session_id)
            if node.kind != "session":
                raise RuntimeError(f"节点不是会话: {session_id}")
            if manifest.get("session_id") != session_id:
                raise ValueError(
                    "迁移会话 manifest ID 不匹配: "
                    f"expected={session_id}, actual={manifest.get('session_id')}"
                )
            self._assert_node_mutable(session_id)
            self._assert_parent_mutable(parent_node_id)
            if parent_node_id == session_id:
                raise ValueError("会话不能移动到自身下")
            if parent_node_id is not None:
                self._assert_not_descendant(session_id, parent_node_id)
            expected_parent_session_id = self.nearest_session_ancestor(parent_node_id)
            if manifest.get("parent_session_id") != expected_parent_session_id:
                raise ValueError(
                    "会话 manifest 父节点与目标物理祖先不一致: "
                    f"session_id={session_id}, manifest={manifest.get('parent_session_id')}, "
                    f"physical={expected_parent_session_id}"
                )

            parent_path = self._parent_path(parent_node_id)
            target = parent_path / physical_segment(name, session_id)
            if target.exists() and target != node.path:
                raise FileExistsError(f"会话物理节点目标已存在: {target}")

            source = node.path
            original_manifest = (source / SESSION_MANIFEST_NAME).read_bytes()
            moved = target != source
            try:
                if moved:
                    os.replace(source, target)
                _atomic_write_json(target / SESSION_MANIFEST_NAME, manifest)
                self._refresh_locked()
                return self.get_node(session_id)
            except BaseException:
                if moved and target.exists() and not source.exists():
                    os.replace(target, source)
                _atomic_write_bytes(source / SESSION_MANIFEST_NAME, original_manifest)
                self._refresh_locked()
                raise

    def expected_session_parents_after_folder_move(
        self,
        *,
        folder_id: str,
        parent_node_id: str | None,
    ) -> dict[str, str | None]:
        """计算文件夹子树移动后每个会话应声明的最近物理父会话。"""
        with self._lock:
            self._ensure_loaded()
            self._refresh_if_navigation_changed_locked()
            folder = self.get_node(folder_id)
            if folder.kind != "folder":
                raise ValueError(f"节点不是会话文件夹: {folder_id}")
            if parent_node_id == folder_id:
                raise ValueError(f"节点不能移动到自身下: {folder_id}")
            if parent_node_id is not None:
                self._assert_not_descendant(folder_id, parent_node_id)
            subtree_ids = self._subtree_node_ids(folder_id)
            external_parent_session_id = self.nearest_session_ancestor(parent_node_id)
            expected: dict[str, str | None] = {}
            for node_id in subtree_ids:
                node = self._nodes[node_id]
                if node.kind != "session":
                    continue
                ancestor_id = node.parent_node_id
                nearest_internal_session_id: str | None = None
                while ancestor_id is not None and ancestor_id in subtree_ids:
                    ancestor = self._nodes[ancestor_id]
                    if ancestor.kind == "session":
                        nearest_internal_session_id = ancestor.node_id
                        break
                    ancestor_id = ancestor.parent_node_id
                expected[node.node_id] = (
                    nearest_internal_session_id or external_parent_session_id
                )
            return expected

    def relocate_folder_tree(
        self,
        *,
        folder_id: str,
        parent_node_id: str | None,
        name: str,
        session_manifests: dict[str, dict[str, object]],
    ) -> SessionPhysicalNode:
        """原子移动文件夹子树，并同步其会话 manifest 的物理父关系。"""
        with self._lock:
            self._ensure_loaded()
            self._refresh_if_navigation_changed_locked()
            folder = self.get_node(folder_id)
            if folder.kind != "folder":
                raise ValueError(f"节点不是会话文件夹: {folder_id}")
            self._assert_node_mutable(folder_id)
            self._assert_parent_mutable(parent_node_id)
            expected_parents = self.expected_session_parents_after_folder_move(
                folder_id=folder_id,
                parent_node_id=parent_node_id,
            )
            if set(session_manifests) != set(expected_parents):
                raise ValueError(
                    "文件夹移动提供的会话 manifest 集合不完整: "
                    f"expected={sorted(expected_parents)}, "
                    f"actual={sorted(session_manifests)}"
                )
            for session_id, expected_parent_id in expected_parents.items():
                manifest = session_manifests[session_id]
                if manifest.get("session_id") != session_id:
                    raise ValueError(
                        "文件夹移动会话 manifest ID 不匹配: "
                        f"expected={session_id}, actual={manifest.get('session_id')}"
                    )
                if manifest.get("parent_session_id") != expected_parent_id:
                    raise ValueError(
                        "文件夹移动会话 manifest 父节点不匹配: "
                        f"session_id={session_id}, "
                        f"manifest={manifest.get('parent_session_id')}, "
                        f"physical={expected_parent_id}"
                    )

            parent_path = self._parent_path(parent_node_id)
            target = parent_path / physical_segment(name, folder_id)
            if target.exists() and target != folder.path:
                raise FileExistsError(f"会话物理节点目标已存在: {target}")
            source = folder.path
            session_relative_paths = {
                session_id: self._nodes[session_id].path.relative_to(source)
                for session_id in expected_parents
            }
            original_manifests = {
                session_id: (
                    self._nodes[session_id].path / SESSION_MANIFEST_NAME
                ).read_bytes()
                for session_id in expected_parents
            }
            moved = target != source
            try:
                if moved:
                    os.replace(source, target)
                for session_id, manifest in session_manifests.items():
                    _atomic_write_json(
                        target / session_relative_paths[session_id] / SESSION_MANIFEST_NAME,
                        manifest,
                    )
                self._refresh_locked()
                return self.get_node(folder_id)
            except BaseException:
                if moved and target.exists() and not source.exists():
                    os.replace(target, source)
                for session_id, content in original_manifests.items():
                    _atomic_write_bytes(
                        source / session_relative_paths[session_id] / SESSION_MANIFEST_NAME,
                        content,
                    )
                self._refresh_locked()
                raise

    def begin_subtree_delete(self, folder_id: str) -> None:
        """冻结文件夹子树的创建和移动，直到递归删除完成或失败。"""
        with self._lock:
            self._ensure_loaded()
            folder = self.get_node(folder_id)
            if folder.kind != "folder":
                raise RuntimeError(f"节点不是会话文件夹: {folder_id}")
            for active_folder_id in self._deleting_subtrees:
                if self._node_is_within(folder_id, active_folder_id) or self._node_is_within(
                    active_folder_id,
                    folder_id,
                ):
                    raise RuntimeError(
                        "会话文件夹子树已有删除操作: "
                        f"requested={folder_id}, active={active_folder_id}"
                    )
            self._deleting_subtrees.add(folder_id)

    def finish_subtree_delete(self, folder_id: str) -> None:
        with self._lock:
            if folder_id not in self._deleting_subtrees:
                raise RuntimeError(f"会话文件夹子树删除锁不存在: {folder_id}")
            self._deleting_subtrees.remove(folder_id)

    def delete_folder(
        self,
        folder_id: str,
        *,
        deleting_subtree_id: str | None = None,
    ) -> None:
        with self._lock:
            node = self.get_node(folder_id)
            if node.kind != "folder":
                raise RuntimeError(f"节点不是会话文件夹: {folder_id}")
            if deleting_subtree_id is None:
                self._assert_node_mutable(folder_id)
            elif (
                deleting_subtree_id not in self._deleting_subtrees
                or not self._node_is_within(folder_id, deleting_subtree_id)
            ):
                raise RuntimeError(
                    "递归删除内部请求没有持有对应子树锁: "
                    f"folder_id={folder_id}, subtree={deleting_subtree_id}"
                )
            children = [
                item
                for item in self._nodes.values()
                if item.parent_node_id == folder_id
            ]
            if children:
                child_ids = ",".join(sorted(item.node_id for item in children))
                raise RuntimeError(
                    "会话文件夹非空，拒绝删除: "
                    f"folder_id={folder_id}, children={child_ids}"
                )
            marker = node.path / FOLDER_MANIFEST_NAME
            unmanaged_entries = [
                entry for entry in node.path.iterdir() if entry != marker
            ]
            if unmanaged_entries:
                raise RuntimeError(
                    "会话文件夹包含未托管内容，拒绝删除: "
                    f"folder_id={folder_id}, entries="
                    f"{','.join(str(entry) for entry in unmanaged_entries)}"
                )
            marker.unlink()
            try:
                node.path.rmdir()
            except BaseException:
                self._write_folder_manifest(
                    node.path,
                    folder_id=folder_id,
                    created_at=node.created_at,
                )
                raise
            self._refresh_locked()

    def relative_path(self, node_id: str) -> str:
        with self._lock:
            return self.get_node(node_id).path.relative_to(self.sessions_root).as_posix()

    def child_nodes(self, node_id: str) -> list[SessionPhysicalNode]:
        with self._lock:
            self._ensure_loaded()
            self._refresh_if_navigation_changed_locked()
            return [
                node for node in self._nodes.values() if node.parent_node_id == node_id
            ]

    def descendant_session_ids(self, node_id: str, *, include_self: bool = False) -> list[str]:
        with self._lock:
            self._ensure_loaded()
            self._refresh_if_navigation_changed_locked()
            if node_id not in self._nodes:
                raise KeyError(f"物理会话节点不存在: {node_id}")
            result: list[str] = []
            pending = [node_id]
            visited: set[str] = set()
            while pending:
                current_id = pending.pop()
                if current_id in visited:
                    raise RuntimeError(f"物理目录索引包含循环: {current_id}")
                visited.add(current_id)
                current = self._nodes[current_id]
                if current.kind == "session" and (include_self or current_id != node_id):
                    result.append(current_id)
                pending.extend(
                    child.node_id
                    for child in self._nodes.values()
                    if child.parent_node_id == current_id
                )
            return sorted(result)

    def _subtree_node_ids(self, node_id: str) -> set[str]:
        if node_id not in self._nodes:
            raise KeyError(f"物理会话节点不存在: {node_id}")
        result: set[str] = set()
        pending = [node_id]
        while pending:
            current_id = pending.pop()
            if current_id in result:
                raise RuntimeError(f"物理目录索引包含循环: {current_id}")
            result.add(current_id)
            pending.extend(
                child.node_id
                for child in self._nodes.values()
                if child.parent_node_id == current_id
            )
        return result

    def nearest_session_ancestor(self, parent_node_id: str | None) -> str | None:
        with self._lock:
            self._ensure_loaded()
            return self._nearest_session_ancestor_from_nodes(
                parent_node_id,
                self._nodes,
            )

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.initialize()

    def _recover_allocation_marker(
        self,
        session_dir: Path,
        *,
        has_manifest: bool,
    ) -> bool:
        """恢复崩溃遗留标记；返回 True 表示整个空分配目录已回收。"""
        marker_path = session_dir / SESSION_ALLOCATION_MARKER_NAME
        marker = _read_json_object(marker_path)
        session_id = marker.get("session_id")
        pid = marker.get("pid")
        if not isinstance(session_id, str) or not session_id:
            raise RuntimeError(f"会话分配标记缺少 session_id: {marker_path}")
        if not isinstance(pid, int) or pid <= 0:
            raise RuntimeError(f"会话分配标记缺少合法 pid: {marker_path}")
        if _process_matches_identity(pid, marker.get("process_identity")):
            return False
        if has_manifest:
            manifest = _read_json_object(session_dir / SESSION_MANIFEST_NAME)
            if manifest.get("session_id") != session_id:
                raise RuntimeError(
                    "崩溃遗留会话分配标记与 manifest ID 不一致: "
                    f"marker={session_id}, manifest={manifest.get('session_id')}, "
                    f"path={session_dir}"
                )
            marker_path.unlink()
            return False
        unmanaged_entries = [entry for entry in session_dir.iterdir() if entry != marker_path]
        if unmanaged_entries:
            raise RuntimeError(
                "发现崩溃遗留且包含未完成数据的会话分配目录，拒绝静默忽略: "
                f"session_id={session_id}, path={session_dir}, "
                f"entries={','.join(str(entry) for entry in unmanaged_entries)}"
            )
        marker_path.unlink()
        session_dir.rmdir()
        return True

    def _refresh_if_navigation_changed_locked(self) -> None:
        for path, previous_signature in self._navigation_mtimes.items():
            try:
                current_signature = _navigation_signature(path)
            except FileNotFoundError:
                self._refresh_locked()
                return
            if current_signature != previous_signature:
                self._refresh_locked()
                return

    def _refresh_node_if_changed_locked(self, node_id: str) -> None:
        """单节点访问只做 O(1) 校验，变化时再扫描整个物理树。"""
        node = self._nodes.get(node_id)
        if node is None:
            self._refresh_locked()
            return
        manifest_path = node.path / (
            FOLDER_MANIFEST_NAME if node.kind == "folder" else SESSION_MANIFEST_NAME
        )
        for path in (node.path, manifest_path):
            previous_signature = self._navigation_mtimes.get(path)
            try:
                current_signature = _navigation_signature(path)
            except FileNotFoundError:
                self._refresh_locked()
                return
            if current_signature != previous_signature:
                self._refresh_locked()
                return

    def _parent_path(self, parent_node_id: str | None) -> Path:
        if parent_node_id is None:
            return self.sessions_root
        parent = self.get_node(parent_node_id)
        if parent.kind == "session":
            children_path = parent.path / SESSION_CHILDREN_DIR_NAME
            children_path.mkdir(exist_ok=True)
            return children_path
        return parent.path

    @staticmethod
    def _nearest_session_ancestor_from_nodes(
        parent_node_id: str | None,
        nodes: dict[str, SessionPhysicalNode],
    ) -> str | None:
        current_id = parent_node_id
        visited: set[str] = set()
        while current_id is not None:
            if current_id in visited:
                raise RuntimeError(f"物理目录索引包含循环: {current_id}")
            visited.add(current_id)
            current = nodes.get(current_id)
            if current is None:
                raise RuntimeError(f"物理会话节点父节点不存在: {current_id}")
            if current.kind == "session":
                return current.node_id
            current_id = current.parent_node_id
        return None

    def _assert_parent_mutable(self, parent_node_id: str | None) -> None:
        if parent_node_id is None:
            return
        self._assert_node_mutable(parent_node_id)

    def _assert_node_mutable(self, node_id: str) -> None:
        for folder_id in self._deleting_subtrees:
            if self._node_is_within(node_id, folder_id):
                raise RuntimeError(
                    "会话节点位于正在递归删除的文件夹中: "
                    f"node_id={node_id}, folder_id={folder_id}"
                )

    def _node_is_within(self, node_id: str, folder_id: str) -> bool:
        current = self._nodes.get(node_id)
        visited: set[str] = set()
        while current is not None:
            if current.node_id == folder_id:
                return True
            if current.node_id in visited:
                raise RuntimeError(f"物理目录索引包含循环: {current.node_id}")
            visited.add(current.node_id)
            current = (
                self._nodes.get(current.parent_node_id)
                if current.parent_node_id is not None
                else None
            )
        return False

    def _assert_not_descendant(self, node_id: str, candidate_parent_id: str) -> None:
        current: SessionPhysicalNode | None = self.get_node(candidate_parent_id)
        visited: set[str] = set()
        while current is not None:
            if current.node_id == node_id:
                raise ValueError(
                    f"移动会形成物理目录循环: node_id={node_id}, parent={candidate_parent_id}"
                )
            if current.node_id in visited:
                raise RuntimeError(f"物理目录索引包含循环: {current.node_id}")
            visited.add(current.node_id)
            current = (
                self._nodes.get(current.parent_node_id)
                if current.parent_node_id is not None
                else None
            )

    def _read_folder_node(
        self,
        path: Path,
        manifest_path: Path,
        parent_node_id: str | None,
    ) -> SessionPhysicalNode:
        raw = _read_json_object(manifest_path)
        if raw.get("schema_version") != PHYSICAL_LAYOUT_VERSION:
            raise RuntimeError(f"不支持的会话文件夹 manifest 版本: {manifest_path}")
        folder_id = raw.get("folder_id")
        if not isinstance(folder_id, str) or not folder_id:
            raise RuntimeError(f"会话文件夹 manifest 缺少 folder_id: {manifest_path}")
        created_at = _parse_datetime(raw.get("created_at"), manifest_path)
        stat = path.stat()
        return SessionPhysicalNode(
            node_id=folder_id,
            kind="folder",
            path=path,
            parent_node_id=parent_node_id,
            name=display_name_from_segment(path.name, folder_id),
            created_at=created_at,
            updated_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
        )

    def _read_session_node(
        self,
        path: Path,
        manifest_path: Path,
        parent_node_id: str | None,
    ) -> SessionPhysicalNode:
        raw = _read_json_object(manifest_path)
        session_id = raw.get("session_id")
        title = raw.get("title")
        if not isinstance(session_id, str) or not session_id:
            raise RuntimeError(f"会话 manifest 缺少 session_id: {manifest_path}")
        if not isinstance(title, str):
            raise RuntimeError(f"会话 manifest 缺少 title: {manifest_path}")
        return SessionPhysicalNode(
            node_id=session_id,
            kind="session",
            path=path,
            parent_node_id=parent_node_id,
            name=title,
            created_at=_parse_datetime(raw.get("created_at"), manifest_path),
            updated_at=_parse_datetime(raw.get("updated_at"), manifest_path),
        )

    @staticmethod
    def _write_folder_manifest(
        folder_path: Path,
        *,
        folder_id: str,
        created_at: datetime,
    ) -> None:
        _atomic_write_json(
            folder_path / FOLDER_MANIFEST_NAME,
            {
                "schema_version": PHYSICAL_LAYOUT_VERSION,
                "folder_id": folder_id,
                "created_at": created_at.isoformat(),
            },
        )

    def _migrate_physical_session_parents(self) -> None:
        """把既有 manifest 父子关系一次性转换为真实会话子树。"""
        migration_dir = self.sessions_root.parent / "migrations"
        migration_path = migration_dir / "session-physical-parents-v2.json"
        if migration_path.exists():
            record = _read_json_object(migration_path)
            if record.get("status") == "completed":
                return

        migration_dir.mkdir(parents=True, exist_ok=True)
        lock_path = migration_dir / "session-physical-parents-v2.lock"
        lock_descriptor = self._acquire_migration_lock(lock_path)
        try:
            self._write_migration_lock_owner(lock_descriptor)
            operations: list[dict[str, str]] = []
            started_at = datetime.now(UTC).isoformat()
            _atomic_write_json(
                migration_path,
                {
                    "schema_version": 2,
                    "status": "executing",
                    "started_at": started_at,
                    "operations": operations,
                },
            )
            try:
                self._migrate_physical_session_parents_locked(
                    operations=operations,
                    migration_path=migration_path,
                )
            except Exception as error:
                _atomic_write_json(
                    migration_path,
                    {
                        "schema_version": 2,
                        "status": "failed",
                        "started_at": started_at,
                        "failed_at": datetime.now(UTC).isoformat(),
                        "operations": operations,
                        "error": f"{type(error).__name__}: {error}",
                    },
                )
                raise
            _atomic_write_json(
                migration_path,
                {
                    "schema_version": 2,
                    "status": "completed",
                    "started_at": started_at,
                    "completed_at": datetime.now(UTC).isoformat(),
                    "operations": operations,
                },
            )
        finally:
            self._release_migration_lock(lock_descriptor)

    def _migrate_physical_session_parents_locked(
        self,
        *,
        operations: list[dict[str, str]],
        migration_path: Path,
    ) -> None:
        initial_paths = self._discover_session_paths()
        parent_by_session: dict[str, str | None] = {}
        workspace_by_session: dict[str, str | None] = {}
        for session_id, path in initial_paths.items():
            raw = _read_json_object(path / SESSION_MANIFEST_NAME)
            parent_id = raw.get("parent_session_id")
            if parent_id is not None and not isinstance(parent_id, str):
                raise RuntimeError(
                    "会话 parent_session_id 必须是字符串或 null: "
                    f"session_id={session_id}, value={parent_id!r}"
                )
            parent_by_session[session_id] = parent_id
            if raw.get("kind") == "context_fork" and not isinstance(
                raw.get("context_source_session_id"),
                str,
            ):
                if parent_id is None:
                    raise RuntimeError(
                        "旧 context_fork 会话缺少可迁移的上下文来源: "
                        f"session_id={session_id}"
                    )
                raw["context_source_session_id"] = parent_id
                _atomic_write_json(path / SESSION_MANIFEST_NAME, raw)
                self._append_migration_operation(
                    operations,
                    migration_path,
                    operation="record_context_source",
                    node_id=session_id,
                    source=str(path / SESSION_MANIFEST_NAME),
                    target=str(path / SESSION_MANIFEST_NAME),
                )
            workspace_id = raw.get("workspace_id")
            workspace_by_session[session_id] = (
                workspace_id if isinstance(workspace_id, str) else None
            )

        for session_id, parent_id in parent_by_session.items():
            if parent_id is None:
                continue
            if parent_id not in initial_paths:
                raise RuntimeError(
                    "会话父节点不存在，无法迁移物理子树: "
                    f"session_id={session_id}, parent_session_id={parent_id}"
                )
            child_workspace = workspace_by_session[session_id]
            parent_workspace = workspace_by_session[parent_id]
            if (
                child_workspace is not None
                and parent_workspace is not None
                and child_workspace != parent_workspace
            ):
                raise RuntimeError(
                    "跨工作区父子会话不能迁移为物理子树: "
                    f"session_id={session_id}, child_workspace={child_workspace}, "
                    f"parent_workspace={parent_workspace}"
                )

        placed: set[str] = set()
        visiting: set[str] = set()

        def place(session_id: str) -> None:
            if session_id in placed:
                return
            if session_id in visiting:
                raise RuntimeError(f"会话父子关系包含循环: {session_id}")
            visiting.add(session_id)
            parent_id = parent_by_session[session_id]
            if parent_id is not None:
                place(parent_id)

            current_paths = self._discover_session_paths()
            source = current_paths[session_id]
            actual_parent_id = self._nearest_physical_session_id(
                source,
                current_paths,
            )
            if actual_parent_id != parent_id:
                if actual_parent_id is not None:
                    raise RuntimeError(
                        "既有物理父会话与 manifest 不一致，拒绝猜测迁移: "
                        f"session_id={session_id}, declared={parent_id}, "
                        f"physical={actual_parent_id}"
                    )
                if parent_id is not None:
                    parent_path = current_paths[parent_id]
                    children_path = parent_path / SESSION_CHILDREN_DIR_NAME
                    children_path.mkdir(exist_ok=True)
                    raw = _read_json_object(source / SESSION_MANIFEST_NAME)
                    title = raw.get("title")
                    if not isinstance(title, str):
                        raise RuntimeError(f"旧会话标题无效: {source}")
                    target = children_path / physical_segment(title, session_id)
                    if target.exists():
                        raise FileExistsError(f"迁移子会话目标已存在: {target}")
                    os.replace(source, target)
                    self._append_migration_operation(
                        operations,
                        migration_path,
                        operation="move_child_session",
                        node_id=session_id,
                        source=str(source),
                        target=str(target),
                    )
            visiting.remove(session_id)
            placed.add(session_id)

        for session_id in sorted(initial_paths):
            place(session_id)

    def _nearest_physical_session_id(
        self,
        session_path: Path,
        session_paths: dict[str, Path],
    ) -> str | None:
        ids_by_path = {
            path.resolve(): session_id for session_id, path in session_paths.items()
        }
        current = session_path.resolve().parent
        while current != self.sessions_root:
            session_id = ids_by_path.get(current)
            if session_id is not None:
                return session_id
            if not current.is_relative_to(self.sessions_root):
                raise RuntimeError(f"会话目录越出 sessions 根目录: {session_path}")
            current = current.parent
        return None

    # TODO: 完成旧布局发布迁移窗口后，删除对 session-folders.json 的读取入口。
    def _migrate_legacy_layout(self) -> None:
        boxteam_root = self.sessions_root.parent
        migration_dir = boxteam_root / "migrations"
        migration_path = migration_dir / "session-physical-layout-v1.json"
        if migration_path.exists():
            record = _read_json_object(migration_path)
            if record.get("status") == "completed":
                return

        migration_dir.mkdir(parents=True, exist_ok=True)
        lock_path = migration_dir / "session-physical-layout-v1.lock"
        lock_descriptor = self._acquire_migration_lock(lock_path)
        try:
            self._write_migration_lock_owner(lock_descriptor)
            self._migrate_legacy_layout_locked(migration_path)
        finally:
            self._release_migration_lock(lock_descriptor)

    @staticmethod
    def _acquire_migration_lock(lock_path: Path) -> int:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if os.name == "nt":
                # TODO: Windows CI 覆盖 msvcrt 单字节非阻塞锁的跨进程行为。
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b" ")
                    os.fsync(descriptor)
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as error:
            os.close(descriptor)
            raise RuntimeError(
                f"会话物理目录迁移已被另一个进程占用: {lock_path}"
            ) from error
        return descriptor

    @staticmethod
    def _write_migration_lock_owner(descriptor: int) -> None:
        encoded = json.dumps(
            {
                "pid": os.getpid(),
                "started_at": datetime.now(UTC).isoformat(),
                "process_identity": _process_identity(os.getpid()),
            },
            ensure_ascii=False,
        ).encode("utf-8")
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.ftruncate(descriptor, 0)
        os.write(descriptor, encoded)
        os.fsync(descriptor)

    @staticmethod
    def _release_migration_lock(descriptor: int) -> None:
        try:
            if os.name == "nt":
                # TODO: Windows CI 覆盖 msvcrt 单字节锁释放行为。
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def _migrate_legacy_layout_locked(self, migration_path: Path) -> None:
        if migration_path.exists():
            record = _read_json_object(migration_path)
            if record.get("status") == "completed":
                return
        boxteam_root = self.sessions_root.parent
        migration_dir = migration_path.parent

        legacy_navigation_path = boxteam_root / "navigation" / "session-folders.json"
        legacy_navigation = (
            _read_json_object(legacy_navigation_path)
            if legacy_navigation_path.exists()
            else {"folders": [], "session_parents": {}}
        )
        raw_folders = legacy_navigation.get("folders", [])
        raw_session_parents = legacy_navigation.get("session_parents", {})
        if not isinstance(raw_folders, list) or not isinstance(raw_session_parents, dict):
            raise RuntimeError(f"旧会话导航格式无效: {legacy_navigation_path}")
        self._validate_legacy_navigation(
            raw_folders=raw_folders,
            raw_session_parents=raw_session_parents,
            session_paths=self._discover_session_paths(),
        )

        existing_record = (
            _read_json_object(migration_path) if migration_path.exists() else None
        )
        raw_existing_operations = (
            existing_record.get("operations", []) if existing_record is not None else []
        )
        if not isinstance(raw_existing_operations, list):
            raise RuntimeError(f"会话物理目录迁移 operations 无效: {migration_path}")
        operations = [
            {str(key): str(value) for key, value in item.items()}
            for item in raw_existing_operations
            if isinstance(item, dict)
        ]
        started_at = (
            existing_record.get("started_at")
            if existing_record is not None
            else datetime.now(UTC).isoformat()
        )
        if not isinstance(started_at, str):
            started_at = datetime.now(UTC).isoformat()
        _atomic_write_json(
            migration_path,
            {
                "schema_version": 1,
                "status": "executing",
                "started_at": started_at,
                "operations": operations,
            },
        )
        try:
            self._migrate_nodes(
                raw_folders=raw_folders,
                raw_session_parents=raw_session_parents,
                operations=operations,
                migration_path=migration_path,
            )
            self.refresh()
            archived_navigation: str | None = None
            if legacy_navigation_path.exists():
                archive_path = migration_dir / "session-folders-v1.json"
                if archive_path.exists():
                    raise FileExistsError(f"旧会话导航归档目标已存在: {archive_path}")
                os.replace(legacy_navigation_path, archive_path)
                archived_navigation = str(archive_path)
            _atomic_write_json(
                migration_path,
                {
                    "schema_version": 1,
                    "status": "completed",
                    "started_at": started_at,
                    "completed_at": datetime.now(UTC).isoformat(),
                    "operations": operations,
                    "archived_navigation": archived_navigation,
                },
            )
        except Exception as error:
            _atomic_write_json(
                migration_path,
                {
                    "schema_version": 1,
                    "status": "failed",
                    "started_at": started_at,
                    "failed_at": datetime.now(UTC).isoformat(),
                    "operations": operations,
                    "error": f"{type(error).__name__}: {error}",
                },
            )
            raise

    def _migrate_legacy_session_locators(self) -> None:
        migration_dir = self.sessions_root.parent / "migrations"
        migration_path = migration_dir / "session-stable-locators-v1.json"
        if migration_path.exists():
            record = _read_json_object(migration_path)
            if record.get("status") == "completed":
                return
        migration_dir.mkdir(parents=True, exist_ok=True)
        lock_path = migration_dir / "session-stable-locators-v1.lock"
        lock_descriptor = self._acquire_migration_lock(lock_path)
        try:
            self._write_migration_lock_owner(lock_descriptor)
            rewritten_files: list[str] = []
            started_at = datetime.now(UTC).isoformat()
            try:
                for session_id, session_path in self._discover_session_paths().items():
                    rewritten_files.extend(
                        self._rewrite_session_locator_files(session_id, session_path)
                    )
            except Exception as error:
                _atomic_write_json(
                    migration_path,
                    {
                        "schema_version": 1,
                        "status": "failed",
                        "started_at": started_at,
                        "failed_at": datetime.now(UTC).isoformat(),
                        "rewritten_files": rewritten_files,
                        "error": f"{type(error).__name__}: {error}",
                    },
                )
                raise
            _atomic_write_json(
                migration_path,
                {
                    "schema_version": 1,
                    "status": "completed",
                    "started_at": started_at,
                    "completed_at": datetime.now(UTC).isoformat(),
                    "rewritten_files": rewritten_files,
                },
            )
        finally:
            self._release_migration_lock(lock_descriptor)

    def _rewrite_session_locator_files(
        self,
        session_id: str,
        session_path: Path,
        *,
        attachment_locators: dict[str, str] | None = None,
    ) -> list[str]:
        rewritten: list[str] = []
        for path in sorted(session_path.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            relative_path = path.relative_to(session_path)
            top_level = relative_path.parts[0]
            if top_level == SESSION_CHILDREN_DIR_NAME:
                continue
            if not (
                relative_path == Path("pending_requests.json")
                or top_level in {"message_history", "checkpoints"}
            ):
                continue
            changed = False
            if path.suffix == ".json":
                value = json.loads(path.read_text(encoding="utf-8"))
                value, changed = _rewrite_legacy_locator_value(
                    value,
                    session_id,
                    attachment_locators=attachment_locators,
                )
                if changed:
                    _atomic_write_json_value(path, value)
            elif path.suffix == ".jsonl":
                output_lines: list[str] = []
                for line_number, raw_line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(),
                    start=1,
                ):
                    if not raw_line.strip():
                        continue
                    try:
                        value = json.loads(raw_line)
                    except json.JSONDecodeError as error:
                        raise RuntimeError(
                            "迁移稳定会话定位符时遇到损坏 JSONL: "
                            f"path={path}, line={line_number}"
                        ) from error
                    value, line_changed = _rewrite_legacy_locator_value(
                        value,
                        session_id,
                        attachment_locators=attachment_locators,
                    )
                    changed = changed or line_changed
                    output_lines.append(
                        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                    )
                if changed:
                    _atomic_write_text(path, "\n".join(output_lines) + "\n")
            elif (
                top_level == "checkpoints"
                and path.parent.name == "blobs"
                and path.suffix == ".bin"
            ):
                changed = _rewrite_checkpoint_blob(
                    path,
                    session_id,
                    attachment_locators=attachment_locators,
                )
            if changed:
                rewritten.append(str(path))
        return rewritten

    def _migrate_legacy_inline_attachments(self) -> None:
        migration_dir = self.sessions_root.parent / "migrations"
        migration_path = migration_dir / "session-inline-attachments-v1.json"
        if migration_path.exists():
            record = _read_json_object(migration_path)
            if record.get("status") == "completed":
                return
        migration_dir.mkdir(parents=True, exist_ok=True)
        lock_path = migration_dir / "session-inline-attachments-v1.lock"
        lock_descriptor = self._acquire_migration_lock(lock_path)
        try:
            self._write_migration_lock_owner(lock_descriptor)
            started_at = datetime.now(UTC).isoformat()
            rewritten_files: list[str] = []
            materialized: dict[str, int] = {}
            try:
                for session_id, session_path in self._discover_session_paths().items():
                    locators = materialize_legacy_inline_attachments(
                        session_id=session_id,
                        session_path=session_path,
                    )
                    materialized[session_id] = len(locators)
                    rewritten_files.extend(
                        self._rewrite_session_locator_files(
                            session_id,
                            session_path,
                            attachment_locators=locators,
                        )
                    )
            except Exception as error:
                _atomic_write_json(
                    migration_path,
                    {
                        "schema_version": 1,
                        "status": "failed",
                        "started_at": started_at,
                        "failed_at": datetime.now(UTC).isoformat(),
                        "materialized": materialized,
                        "rewritten_files": rewritten_files,
                        "error": f"{type(error).__name__}: {error}",
                    },
                )
                raise
            _atomic_write_json(
                migration_path,
                {
                    "schema_version": 1,
                    "status": "completed",
                    "started_at": started_at,
                    "completed_at": datetime.now(UTC).isoformat(),
                    "materialized": materialized,
                    "rewritten_files": rewritten_files,
                },
            )
        finally:
            self._release_migration_lock(lock_descriptor)

    @staticmethod
    def _validate_legacy_navigation(
        *,
        raw_folders: list[object],
        raw_session_parents: dict[object, object],
        session_paths: dict[str, Path],
    ) -> None:
        """在任何物理目录变更前验证旧导航图可以转换为文件夹树。"""
        folders: dict[str, str | None] = {}
        session_parents: dict[str, str] = {}
        for raw_folder in raw_folders:
            if not isinstance(raw_folder, dict):
                raise RuntimeError("旧会话文件夹记录必须是 object")
            folder_id = raw_folder.get("folder_id")
            if not isinstance(folder_id, str) or not folder_id:
                raise RuntimeError("旧会话文件夹记录缺少 folder_id")
            if folder_id in folders:
                raise RuntimeError(f"旧会话文件夹 ID 重复: {folder_id}")
            if folder_id in session_paths:
                raise RuntimeError(f"旧文件夹 ID 与会话 ID 冲突: {folder_id}")
            name = raw_folder.get("name")
            if not isinstance(name, str) or not name.strip():
                raise RuntimeError(f"旧会话文件夹名称无效: {folder_id}")
            raw_parent = raw_folder.get("parent_folder_id")
            if raw_parent is not None and (
                not isinstance(raw_parent, str) or not raw_parent
            ):
                raise RuntimeError(f"旧会话文件夹父节点无效: {folder_id}")
            folders[folder_id] = raw_parent
            raw_session_ids = raw_folder.get("session_ids", [])
            if not isinstance(raw_session_ids, list):
                raise RuntimeError(f"旧会话文件夹 session_ids 无效: {folder_id}")
            for session_id in raw_session_ids:
                if not isinstance(session_id, str) or not session_id:
                    raise RuntimeError(f"旧会话 ID 无效: folder_id={folder_id}")
                if session_id in session_parents:
                    raise RuntimeError(f"旧导航中会话存在多个父节点: {session_id}")
                session_parents[session_id] = folder_id

        for session_id, parent_id in raw_session_parents.items():
            if not isinstance(session_id, str) or not isinstance(parent_id, str):
                raise RuntimeError("旧 session_parents 必须是字符串映射")
            if session_id in session_parents:
                raise RuntimeError(f"旧导航中会话存在多个父节点: {session_id}")
            session_parents[session_id] = parent_id

        for folder_id, parent_id in folders.items():
            if parent_id is not None and parent_id not in folders:
                relation_kind = "会话" if parent_id in session_paths else "不存在节点"
                raise RuntimeError(
                    "旧会话文件夹只能挂在文件夹下: "
                    f"folder_id={folder_id}, parent_id={parent_id}, parent_kind={relation_kind}"
                )
        for session_id, parent_id in session_parents.items():
            if session_id not in session_paths:
                raise RuntimeError(f"旧会话导航引用不存在的会话: {session_id}")
            if parent_id not in folders:
                raise RuntimeError(
                    "旧会话只能挂在文件夹下: "
                    f"session_id={session_id}, parent_id={parent_id}"
                )

        visited: set[str] = set()
        visiting: set[str] = set()

        def visit(folder_id: str) -> None:
            if folder_id in visited:
                return
            if folder_id in visiting:
                raise RuntimeError(f"旧会话文件夹关系包含循环: {folder_id}")
            visiting.add(folder_id)
            parent_id = folders[folder_id]
            if parent_id is not None:
                visit(parent_id)
            visiting.remove(folder_id)
            visited.add(folder_id)

        for folder_id in folders:
            visit(folder_id)

    def _migrate_nodes(
        self,
        *,
        raw_folders: list[object],
        raw_session_parents: dict[object, object],
        operations: list[dict[str, str]],
        migration_path: Path,
    ) -> None:
        folders: dict[str, dict[str, object]] = {}
        parent_by_node: dict[str, str] = {}
        for raw_folder in raw_folders:
            if not isinstance(raw_folder, dict):
                raise RuntimeError("旧会话文件夹记录必须是 object")
            folder_id = raw_folder.get("folder_id")
            if not isinstance(folder_id, str) or not folder_id:
                raise RuntimeError("旧会话文件夹记录缺少 folder_id")
            if folder_id in folders:
                raise RuntimeError(f"旧会话文件夹 ID 重复: {folder_id}")
            folders[folder_id] = raw_folder
            parent = raw_folder.get("parent_folder_id")
            if isinstance(parent, str):
                parent_by_node[folder_id] = parent
            session_ids = raw_folder.get("session_ids", [])
            if not isinstance(session_ids, list):
                raise RuntimeError(f"旧会话文件夹 session_ids 无效: {folder_id}")
            for session_id in session_ids:
                if not isinstance(session_id, str):
                    raise RuntimeError(f"旧会话 ID 无效: folder_id={folder_id}")
                if session_id in parent_by_node:
                    raise RuntimeError(f"旧导航中会话存在多个父节点: {session_id}")
                parent_by_node[session_id] = folder_id
        for session_id, parent_id in raw_session_parents.items():
            if not isinstance(session_id, str) or not isinstance(parent_id, str):
                raise RuntimeError("旧 session_parents 必须是字符串映射")
            if session_id in parent_by_node:
                raise RuntimeError(f"旧导航中会话存在多个父节点: {session_id}")
            parent_by_node[session_id] = parent_id

        session_paths = self._discover_session_paths()
        folder_paths: dict[str, Path] = {}
        visiting: set[str] = set()

        def place(node_id: str) -> Path:
            if node_id in folder_paths:
                return folder_paths[node_id]
            if node_id in session_paths and not self._is_legacy_flat_path(session_paths[node_id]):
                return session_paths[node_id]
            if node_id in visiting:
                raise RuntimeError(f"旧会话导航包含循环: {node_id}")
            visiting.add(node_id)
            parent_id = parent_by_node.get(node_id)
            parent_path = self.sessions_root if parent_id is None else place(parent_id)

            if node_id in folders:
                raw_folder = folders[node_id]
                name = raw_folder.get("name")
                if not isinstance(name, str) or not name.strip():
                    raise RuntimeError(f"旧会话文件夹名称无效: {node_id}")
                target = parent_path / physical_segment(name, node_id)
                if target.exists():
                    manifest_path = target / FOLDER_MANIFEST_NAME
                    if not manifest_path.exists():
                        raise FileExistsError(f"迁移文件夹目标已存在且无 manifest: {target}")
                    existing_id = _read_json_object(manifest_path).get("folder_id")
                    if existing_id != node_id:
                        raise RuntimeError(f"迁移文件夹目标 ID 冲突: {target}")
                else:
                    target.mkdir()
                    created_at = _parse_optional_datetime(raw_folder.get("created_at"))
                    self._write_folder_manifest(
                        target,
                        folder_id=node_id,
                        created_at=created_at or datetime.now(UTC),
                    )
                    self._append_migration_operation(
                        operations,
                        migration_path,
                        operation="create_folder",
                        node_id=node_id,
                        source="",
                        target=str(target),
                    )
                folder_paths[node_id] = target
                visiting.remove(node_id)
                return target

            source = session_paths.get(node_id)
            if source is None:
                raise RuntimeError(f"旧会话导航引用不存在的会话: {node_id}")
            raw_session = _read_json_object(source / SESSION_MANIFEST_NAME)
            title = raw_session.get("title")
            if not isinstance(title, str):
                raise RuntimeError(f"旧会话标题无效: {source}")
            target = parent_path / physical_segment(title, node_id)
            if source != target:
                if target.exists():
                    raise FileExistsError(f"迁移会话目标已存在: {target}")
                os.replace(source, target)
                session_paths[node_id] = target
                self._append_migration_operation(
                    operations,
                    migration_path,
                    operation="move_session",
                    node_id=node_id,
                    source=str(source),
                    target=str(target),
                )
            visiting.remove(node_id)
            return target

        for folder_id in sorted(folders):
            place(folder_id)
        for session_id in sorted(session_paths):
            place(session_id)

    def _discover_session_paths(self) -> dict[str, Path]:
        paths_by_id: dict[str, list[Path]] = {}
        for manifest_path in self.sessions_root.rglob(SESSION_MANIFEST_NAME):
            if manifest_path.is_symlink():
                continue
            raw = _read_json_object(manifest_path)
            session_id = raw.get("session_id")
            if not isinstance(session_id, str) or not session_id:
                raise RuntimeError(f"会话 manifest 缺少 session_id: {manifest_path}")
            paths_by_id.setdefault(session_id, []).append(manifest_path.parent)
        duplicates = {
            node_id: paths for node_id, paths in paths_by_id.items() if len(paths) > 1
        }
        if duplicates:
            details = "; ".join(
                f"{node_id}={','.join(str(path) for path in paths)}"
                for node_id, paths in sorted(duplicates.items())
            )
            raise RuntimeError(f"迁移前发现重复会话 ID: {details}")
        return {node_id: paths[0] for node_id, paths in paths_by_id.items()}

    def _is_legacy_flat_path(self, path: Path) -> bool:
        return path.parent == self.sessions_root and path.name == _read_json_object(
            path / SESSION_MANIFEST_NAME
        ).get("session_id")

    @staticmethod
    def _append_migration_operation(
        operations: list[dict[str, str]],
        migration_path: Path,
        *,
        operation: str,
        node_id: str,
        source: str,
        target: str,
    ) -> None:
        operations.append(
            {
                "operation": operation,
                "node_id": node_id,
                "source": source,
                "target": target,
            }
        )
        record = _read_json_object(migration_path)
        record["operations"] = operations
        _atomic_write_json(migration_path, record)


def physical_segment(name: str, stable_id: str) -> str:
    normalized = physical_display_segment(name)
    return f"{normalized}--{stable_id[-8:]}"


def physical_display_segment(name: str) -> str:
    """返回物理路径段的显示名部分，预留固定稳定 ID 后缀空间。"""
    normalized = unicodedata.normalize("NFKC", name).strip().rstrip(". ")
    normalized = _INVALID_SEGMENT_CHARS.sub("_", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip().rstrip(". ")
    if not normalized:
        normalized = "未命名"
    if normalized.upper() in _WINDOWS_RESERVED_NAMES:
        normalized = f"_{normalized}"
    max_name_length = 96 - len("--12345678")
    normalized = normalized[:max_name_length].rstrip(". ") or "未命名"
    return normalized


def validate_generator_physical_segment(value: str) -> None:
    """校验生成器路径模板渲染值；与物理路径规范化共用平台规则。"""
    if not value or value in {".", ".."}:
        raise ValueError(f"命名路径段非法: {value!r}")
    if _INVALID_SEGMENT_CHARS.search(value):
        raise ValueError(f"命名路径段包含跨平台非法字符: {value!r}")
    if Path(value).is_absolute():
        raise ValueError(f"命名路径段不能是绝对路径: {value!r}")
    stem = value.split(".", maxsplit=1)[0].upper()
    if stem in _WINDOWS_RESERVED_NAMES:
        raise ValueError(f"命名路径段是平台保留名称: {value!r}")
    if value.endswith((" ", ".")):
        raise ValueError(f"命名路径段不能以空格或点结尾: {value!r}")


def display_name_from_segment(segment: str, stable_id: str) -> str:
    for suffix in (f"--{stable_id}", f"--{stable_id[-8:]}"):
        if segment.endswith(suffix):
            return segment[: -len(suffix)] or "未命名"
    return segment


def _process_identity(pid: int) -> str | None:
    """在支持 `/proc` 的系统上读取可抵御 PID 重用的进程启动标识。"""
    stat_path = Path("/proc") / str(pid) / "stat"
    try:
        raw = stat_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    closing_parenthesis = raw.rfind(")")
    if closing_parenthesis < 0:
        raise RuntimeError(f"无法解析进程 stat: {stat_path}")
    fields_after_name = raw[closing_parenthesis + 2 :].split()
    if len(fields_after_name) <= 19:
        raise RuntimeError(f"进程 stat 缺少启动时间字段: {stat_path}")
    return fields_after_name[19]


def _process_matches_identity(pid: int, expected_identity: object) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if not isinstance(expected_identity, str) or not expected_identity:
        return True
    actual_identity = _process_identity(pid)
    return actual_identity is None or actual_identity == expected_identity


def _navigation_signature(path: Path) -> tuple[int, int, int]:
    stat = path.stat()
    return stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size


def _read_json_object(path: Path) -> dict[str, object]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError(f"JSON 文件必须是 object: {path}")
    return {str(key): value for key, value in raw.items()}


def _parse_datetime(value: object, path: Path) -> datetime:
    parsed = _parse_optional_datetime(value)
    if parsed is None:
        raise RuntimeError(f"manifest 缺少合法时间字段: {path}")
    return parsed


def _parse_optional_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _rewrite_legacy_locator_value(
    value: object,
    session_id: str,
    *,
    field_name: str | None = None,
    attachment_locators: dict[str, str] | None = None,
) -> tuple[object, bool]:
    if isinstance(value, str):
        if field_name not in {"file_id", "read_path", "artifact_path"}:
            return value, False
        if field_name == "file_id" and value.startswith("inline:"):
            if attachment_locators is None:
                return value, False
            locator = attachment_locators.get(value)
            if locator is None:
                raise RuntimeError(
                    "旧会话数据引用了无法从请求日志恢复的 inline 附件: "
                    f"session_id={session_id}, file_id={value!r}"
                )
            return locator, True
        return _rewrite_legacy_locator_string(
            value,
            session_id,
            field_name=field_name,
        )
    if isinstance(value, list):
        changed = False
        items: list[object] = []
        for item in value:
            rewritten, item_changed = _rewrite_legacy_locator_value(
                item,
                session_id,
                field_name=field_name,
                attachment_locators=attachment_locators,
            )
            items.append(rewritten)
            changed = changed or item_changed
        return items, changed
    if isinstance(value, tuple):
        rewritten, changed = _rewrite_legacy_locator_value(
            list(value),
            session_id,
            attachment_locators=attachment_locators,
        )
        if not isinstance(rewritten, list):
            raise TypeError("tuple 定位符迁移结果必须是 list")
        return tuple(rewritten), changed
    if isinstance(value, dict):
        changed = False
        result: dict[object, object] = {}
        for key, item in value.items():
            rewritten, item_changed = _rewrite_legacy_locator_value(
                item,
                session_id,
                field_name=key if isinstance(key, str) else None,
                attachment_locators=attachment_locators,
            )
            result[key] = rewritten
            changed = changed or item_changed
        return result, changed
    if hasattr(value, "model_fields") and hasattr(value, "model_copy"):
        changed = False
        updates: dict[str, object] = {}
        for name in value.__class__.model_fields:
            rewritten, field_changed = _rewrite_legacy_locator_value(
                getattr(value, name),
                session_id,
                field_name=name,
                attachment_locators=attachment_locators,
            )
            if field_changed:
                updates[name] = rewritten
                changed = True
        return value.model_copy(update=updates), changed
    return value, False


def _rewrite_legacy_locator_string(
    value: str,
    session_id: str,
    *,
    field_name: str | None,
) -> tuple[str, bool]:
    escaped_session_id = re.escape(session_id)
    absolute_pattern = re.compile(
        rf"(?:[A-Za-z]:)?(?:[/\\][^/\\\s\"'<>]+)*"
        rf"[/\\]\.boxteam[/\\]sessions[/\\]{escaped_session_id}[/\\]"
    )
    relative_pattern = re.compile(
        rf"(?<![\w.-])\.boxteam[/\\]sessions[/\\]{escaped_session_id}[/\\]"
    )
    replacement = (
        f"boxteam-session://{session_id}/"
        if field_name == "file_id"
        else f"/session-artifacts/{session_id}/"
    )
    updated = absolute_pattern.sub(replacement, value)
    updated = relative_pattern.sub(replacement, updated)
    return updated, updated != value


def _rewrite_checkpoint_blob(
    path: Path,
    session_id: str,
    *,
    attachment_locators: dict[str, str] | None = None,
) -> bool:
    raw = path.read_bytes()
    if not raw:
        return False
    try:
        value = json.loads(raw.decode("utf-8"))
        serialization = "json"
    except (UnicodeDecodeError, json.JSONDecodeError):
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

        serializer = JsonPlusSerializer()
        value = serializer.loads_typed(("msgpack", raw))
        serialization = "msgpack"
    rewritten, changed = _rewrite_legacy_locator_value(
        value,
        session_id,
        attachment_locators=attachment_locators,
    )
    if not changed:
        return False
    if serialization == "json":
        encoded = json.dumps(
            rewritten,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    else:
        serializer = JsonPlusSerializer()
        type_tag, encoded = serializer.dumps_typed(rewritten)
        if type_tag != "msgpack":
            raise RuntimeError(
                f"checkpoint blob 重写后序列化类型变化: path={path}, type={type_tag}"
            )
    _atomic_write_bytes(path, encoded)
    return True


def _atomic_write_json_value(path: Path, value: object) -> None:
    _atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _atomic_write_text(path: Path, value: str) -> None:
    _atomic_write_bytes(path, value.encode("utf-8"))


def _atomic_write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(value)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _atomic_write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
