from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from app.core.identifier import create_prefixed_id
from app.core.session_tree.metadata_migration import (
    SessionMetadataMigrationSupport,
)
from app.core.session_tree.nodes import write_folder_manifest
from app.core.session_tree.support import (
    FOLDER_MANIFEST_NAME,
    SESSION_ALLOCATION_MARKER_NAME,
    SESSION_ALLOCATION_TEMP_PREFIX,
    SESSION_MANIFEST_NAME,
    SessionPhysicalNode,
    _atomic_write_bytes,
    _atomic_write_json,
    _process_identity,
    _read_json_object,
    physical_segment,
)


class SessionPathMutationSupport(SessionMetadataMigrationSupport):
    """封装物理会话树的分配、移动与递归删除事务。"""

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
            write_folder_manifest(
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
                write_folder_manifest(
                    node.path,
                    folder_id=folder_id,
                    created_at=node.created_at,
                )
                raise
            self._refresh_locked()
