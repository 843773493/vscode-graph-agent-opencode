from __future__ import annotations

import threading
from pathlib import Path

from app.core.session_tree.support import (
    FOLDER_MANIFEST_NAME,
    SESSION_ALLOCATION_MARKER_NAME,
    SESSION_ALLOCATION_TEMP_PREFIX,
    SESSION_CHILDREN_DIR_NAME,
    SESSION_MANIFEST_NAME,
    SessionPhysicalNode,
    _navigation_signature,
    _read_json_object,
    physical_display_segment,
    physical_segment,
    validate_generator_physical_segment,
)

__all__ = [
    "FOLDER_MANIFEST_NAME",
    "SessionPathResolver",
    "SessionPhysicalNode",
    "physical_display_segment",
    "physical_segment",
    "validate_generator_physical_segment",
]
from app.core.session_tree.mutations import SessionPathMutationSupport
from app.core.session_tree.nodes import (
    nearest_session_ancestor_from_nodes,
    read_folder_node,
    read_session_node,
    recover_allocation_marker,
)


class SessionPathResolver(SessionPathMutationSupport):
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
                if allocation_marker.exists() and recover_allocation_marker(
                    child,
                    has_manifest=session_manifest.exists(),
                ):
                    continue
                if folder_manifest.exists() and session_manifest.exists():
                    raise RuntimeError(
                        f"物理节点同时包含文件夹与会话 manifest: {child}"
                    )
                if folder_manifest.exists():
                    node = read_folder_node(
                        child,
                        folder_manifest,
                        parent_node_id,
                    )
                elif session_manifest.exists():
                    node = read_session_node(
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
            expected_parent_session_id = nearest_session_ancestor_from_nodes(
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
            return nearest_session_ancestor_from_nodes(
                parent_node_id,
                self._nodes,
            )
    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.initialize()


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
