from __future__ import annotations

import os
import threading
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from app.core.session_tree.support import (
    FOLDER_MANIFEST_NAME,
    SESSION_ALLOCATION_MARKER_NAME,
    SESSION_ALLOCATION_TEMP_PREFIX,
    SESSION_CHILDREN_DIR_NAME,
    SESSION_MANIFEST_NAME,
    SessionPhysicalNode,
    SessionTreeOperationLock,
    _atomic_write_json_value,
    _navigation_signature,
    _parse_optional_datetime,
    _read_json_object,
    physical_display_segment,
    physical_segment,
    session_tree_operation_locked,
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
    """以目录索引为权威解析稳定会话与文件夹 ID。"""

    INDEX_SCHEMA_VERSION = 3

    def __init__(self, sessions_root: Path) -> None:
        self.sessions_root = sessions_root.resolve()
        index_root = self.sessions_root.parent / "navigation"
        marker_root = self.sessions_root.parent / "migrations"
        if self.sessions_root.name != "sessions":
            # TODO: 测试与嵌入式调用仍允许传入任意 sessions 根目录；规范化调用后删除。
            index_root = (
                self.sessions_root.parent
                / f".{self.sessions_root.name}-session-navigation"
            )
            marker_root = index_root
        self.index_path = index_root / "session-catalog-index.json"
        self.authority_marker_path = marker_root / "session-catalog-index-v3.json"
        self._nodes: dict[str, SessionPhysicalNode] = {}
        self._navigation_mtimes: dict[Path, tuple[int, int, int]] = {}
        self._lock = threading.RLock()
        self._session_tree_operation_lock = SessionTreeOperationLock(
            self.index_path.parent / ".session-catalog.lock"
        )
        self._loaded = False
        self._revision = 0
        self._deleting_subtrees: set[str] = set()
        self._physical_tree_error: str | None = None

    @session_tree_operation_locked
    def initialize(self) -> None:
        with self._lock:
            if self._loaded:
                return
            self.sessions_root.mkdir(parents=True, exist_ok=True)
            if self.authority_marker_path.is_file() and not self.index_path.is_file():
                raise RuntimeError(
                    "权威会话目录索引缺失，拒绝根据磁盘目录重建。"
                    f" index={self.index_path}, marker={self.authority_marker_path}"
                )
            if not self._has_authoritative_index_locked():
                # TODO: 旧物理树迁移窗口结束后删除扫描式导入入口。
                self._migrate_legacy_layout()
                self._migrate_physical_session_parents()
                self._migrate_legacy_session_locators()
                self._migrate_legacy_inline_attachments()
                legacy_nodes = self._scan_physical_tree_locked()
                self._migrate_nodes_to_id_paths_locked(legacy_nodes)
                self._nodes = self._nodes_from_records_locked(
                    self._records_from_nodes(legacy_nodes)
                )
                self._write_index_locked()
            self._write_authority_marker_locked()
            try:
                self._refresh_locked()
            except RuntimeError as error:
                if "检测到绕过软件修改会话目录结构" not in str(error):
                    raise
                # 只加载权威索引，让后端能够启动并对外报告可恢复的目录错误；
                # 普通目录 API 仍会由 _raise_if_physical_tree_invalid_locked 拒绝，
                # 运行时写入则使用专门的索引解析器，不会因无关孤儿节点失去终态。
                self._nodes = self._nodes_from_records_locked(
                    self._load_index_records_locked()
                )
                self._loaded = True
                self._revision += 1
                self._physical_tree_error = str(error)

    def invalidate(self) -> None:
        with self._lock:
            self._loaded = False
            self._nodes = {}
            self._navigation_mtimes = {}
            self._physical_tree_error = None

    @session_tree_operation_locked
    def refresh(self) -> list[SessionPhysicalNode]:
        with self._lock:
            if not self._loaded:
                self.initialize()
                self._raise_if_physical_tree_invalid_locked()
                return list(self._nodes.values())
            return self._refresh_locked()

    def _scan_physical_tree_locked(self) -> list[SessionPhysicalNode]:
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

    def _has_authoritative_index_locked(self) -> bool:
        if not self.index_path.exists():
            return False
        raw = _read_json_object(self.index_path)
        version = raw.get("schema_version")
        if version == self.INDEX_SCHEMA_VERSION:
            return True
        if version == 2:
            return False
        raise RuntimeError(
            "会话目录索引版本非法，拒绝根据磁盘目录猜测结构: "
            f"path={self.index_path}, schema_version={version}"
        )

    def _write_authority_marker_locked(self) -> None:
        if self.authority_marker_path.is_file():
            return
        _atomic_write_json_value(
            self.authority_marker_path,
            {
                "schema_version": self.INDEX_SCHEMA_VERSION,
                "status": "authoritative",
                "created_at": datetime.now(UTC).isoformat(),
            },
        )

    @staticmethod
    def _records_from_nodes(
        nodes: list[SessionPhysicalNode],
    ) -> list[dict[str, object]]:
        return [
            {
                "node_id": node.node_id,
                "kind": node.kind,
                "name": node.name,
                "parent_node_id": node.parent_node_id,
                "created_at": node.created_at.isoformat(),
                "updated_at": node.updated_at.isoformat(),
            }
            for node in sorted(nodes, key=lambda item: item.node_id)
        ]

    def _load_index_records_locked(self) -> list[dict[str, object]]:
        if not self.index_path.is_file():
            raise RuntimeError(f"权威会话目录索引缺失: {self.index_path}")
        try:
            raw = _read_json_object(self.index_path)
        except (OSError, ValueError) as error:
            raise RuntimeError(
                f"权威会话目录索引无法读取: {self.index_path}: {error}"
            ) from error
        if raw.get("schema_version") != self.INDEX_SCHEMA_VERSION:
            raise RuntimeError(
                "会话目录索引不是当前权威版本: "
                f"path={self.index_path}, schema_version={raw.get('schema_version')}"
            )
        records = raw.get("nodes")
        if not isinstance(records, list):
            raise TypeError(f"会话目录索引 nodes 必须是数组: {self.index_path}")
        normalized: list[dict[str, object]] = []
        seen: set[str] = set()
        for offset, value in enumerate(records):
            if not isinstance(value, dict):
                raise TypeError(
                    f"会话目录索引节点必须是对象: path={self.index_path}, offset={offset}"
                )
            node_id = value.get("node_id")
            kind = value.get("kind")
            name = value.get("name")
            parent_node_id = value.get("parent_node_id")
            if not isinstance(node_id, str) or not node_id:
                raise RuntimeError(f"会话目录索引节点 ID 非法: offset={offset}")
            if physical_segment("", node_id) != node_id:
                raise RuntimeError(f"会话目录索引节点 ID 不能作为路径段: {node_id}")
            if node_id in seen:
                raise RuntimeError(f"会话目录索引包含重复节点 ID: {node_id}")
            if kind not in {"folder", "session"}:
                raise RuntimeError(
                    f"会话目录索引节点类型非法: node_id={node_id}, kind={kind}"
                )
            if not isinstance(name, str) or not name:
                raise RuntimeError(f"会话目录索引节点显示名非法: node_id={node_id}")
            if parent_node_id is not None and not isinstance(parent_node_id, str):
                raise RuntimeError(f"会话目录索引父节点非法: node_id={node_id}")
            normalized.append({str(key): item for key, item in value.items()})
            seen.add(node_id)
        return normalized

    def _nodes_from_records_locked(
        self,
        records: list[dict[str, object]],
    ) -> dict[str, SessionPhysicalNode]:
        records_by_id = {str(record["node_id"]): record for record in records}
        nodes: dict[str, SessionPhysicalNode] = {}
        visiting: set[str] = set()

        def build(node_id: str) -> SessionPhysicalNode:
            existing = nodes.get(node_id)
            if existing is not None:
                return existing
            if node_id in visiting:
                raise RuntimeError(f"会话目录索引包含循环: {node_id}")
            record = records_by_id.get(node_id)
            if record is None:
                raise RuntimeError(f"会话目录索引节点不存在: {node_id}")
            visiting.add(node_id)
            parent_node_id = record.get("parent_node_id")
            if isinstance(parent_node_id, str):
                parent = build(parent_node_id)
                parent_path = (
                    parent.path / SESSION_CHILDREN_DIR_NAME
                    if parent.kind == "session"
                    else parent.path
                )
            else:
                parent_node_id = None
                parent_path = self.sessions_root
            created_at = _parse_optional_datetime(record.get("created_at"))
            updated_at = _parse_optional_datetime(record.get("updated_at"))
            if created_at is None or updated_at is None:
                raise RuntimeError(f"会话目录索引节点时间非法: node_id={node_id}")
            node = SessionPhysicalNode(
                node_id=node_id,
                kind=str(record["kind"]),
                path=parent_path / node_id,
                parent_node_id=parent_node_id,
                name=str(record["name"]),
                created_at=created_at,
                updated_at=updated_at,
            )
            nodes[node_id] = node
            visiting.remove(node_id)
            return node

        for node_id in records_by_id:
            build(node_id)
        return nodes

    def _migrate_nodes_to_id_paths_locked(
        self,
        legacy_nodes: list[SessionPhysicalNode],
    ) -> None:
        nodes = {node.node_id: node for node in legacy_nodes}
        children: dict[str | None, list[str]] = {}
        for node in legacy_nodes:
            children.setdefault(node.parent_node_id, []).append(node.node_id)

        def move_subtree(node_id: str, parent_path: Path) -> None:
            node = nodes[node_id]
            source = parent_path / node.path.name
            target = parent_path / physical_segment("", node_id)
            if source != target:
                if source.exists() and not target.exists():
                    os.replace(source, target)
                elif not source.exists() and target.exists():
                    pass
                else:
                    raise RuntimeError(
                        "旧会话目录迁移目标冲突: "
                        f"node_id={node_id}, source={source}, target={target}"
                    )
            child_parent = (
                target / SESSION_CHILDREN_DIR_NAME
                if node.kind == "session"
                else target
            )
            for child_id in sorted(children.get(node_id, [])):
                move_subtree(child_id, child_parent)

        for root_id in sorted(children.get(None, [])):
            move_subtree(root_id, self.sessions_root)

    def _write_index_locked(self) -> None:
        _atomic_write_json_value(
            self.index_path,
            {
                "schema_version": self.INDEX_SCHEMA_VERSION,
                "revision": self._revision + 1,
                "nodes": self._records_from_nodes(list(self._nodes.values())),
            },
        )

    def _relocate_index_subtree_locked(
        self,
        *,
        node_id: str,
        source: Path,
        target: Path,
        parent_node_id: str | None,
        name: str | None = None,
    ) -> None:
        for current_id, current in tuple(self._nodes.items()):
            if current_id == node_id:
                self._nodes[current_id] = replace(
                    current,
                    path=target,
                    parent_node_id=parent_node_id,
                    name=name if name is not None else current.name,
                    updated_at=datetime.now(UTC),
                )
                continue
            if current.path.is_relative_to(source):
                self._nodes[current_id] = replace(
                    current,
                    path=target / current.path.relative_to(source),
                )

    @session_tree_operation_locked
    def update_node_name(self, node_id: str, name: str) -> SessionPhysicalNode:
        with self._lock:
            if not name:
                raise ValueError("会话目录节点显示名不能为空")
            self._ensure_loaded()
            self._refresh_if_navigation_changed_locked()
            node = self.get_node(node_id)
            self._nodes[node_id] = replace(
                node,
                name=name,
                updated_at=datetime.now(UTC),
            )
            self._write_index_locked()
            self._refresh_locked()
            return self.get_node(node_id)

    def _validate_filesystem_locked(
        self,
        nodes: dict[str, SessionPhysicalNode],
    ) -> None:
        expected_children: dict[str | None, set[str]] = {}
        for node in nodes.values():
            expected_children.setdefault(node.parent_node_id, set()).add(node.node_id)
            if not node.path.is_dir() or node.path.is_symlink():
                raise RuntimeError(
                    "检测到绕过软件修改会话目录结构；权威索引声明的节点目录"
                    "不存在或不是普通目录。"
                    f" node_id={node.node_id}, expected_path={node.path}"
                )
            if node.path.name != node.node_id:
                raise RuntimeError(
                    "检测到绕过软件修改会话目录结构；物理目录名必须等于稳定 ID。"
                    f" node_id={node.node_id}, path={node.path}"
                )
            manifest_path = node.path / (
                FOLDER_MANIFEST_NAME if node.kind == "folder" else SESSION_MANIFEST_NAME
            )
            if not manifest_path.is_file():
                raise RuntimeError(
                    "检测到绕过软件修改会话目录结构；节点 manifest 缺失。"
                    f" node_id={node.node_id}, manifest={manifest_path}"
                )
            manifest = _read_json_object(manifest_path)
            manifest_id = manifest.get(
                "folder_id" if node.kind == "folder" else "session_id"
            )
            if manifest_id != node.node_id:
                raise RuntimeError(
                    "检测到绕过软件修改会话目录结构；manifest 稳定 ID 不匹配。"
                    f" node_id={node.node_id}, manifest_id={manifest_id}, path={manifest_path}"
                )
            if node.kind == "session":
                expected_parent_session_id = nearest_session_ancestor_from_nodes(
                    node.parent_node_id,
                    nodes,
                )
                if manifest.get("parent_session_id") != expected_parent_session_id:
                    raise RuntimeError(
                        "检测到绕过软件修改会话目录结构；会话 manifest 与权威索引"
                        "父关系不一致。"
                        f" session_id={node.node_id}, "
                        f"index_parent_session_id={expected_parent_session_id}, "
                        f"manifest_parent_session_id={manifest.get('parent_session_id')}"
                    )

        def validate_container(
            path: Path,
            parent_node_id: str | None,
            *,
            allowed_files: set[str] | None = None,
        ) -> None:
            expected = expected_children.get(parent_node_id, set())
            allowed = allowed_files or set()
            actual = {
                entry.name
                for entry in path.iterdir()
                if entry.name not in allowed
            }
            if actual != expected:
                raise RuntimeError(
                    "检测到绕过软件修改会话目录结构；权威索引与磁盘目录不一致。"
                    f" container={path}, expected={sorted(expected)}, actual={sorted(actual)}"
                )

        validate_container(self.sessions_root, None)
        for node in nodes.values():
            if node.kind == "folder":
                validate_container(
                    node.path,
                    node.node_id,
                    allowed_files={FOLDER_MANIFEST_NAME},
                )
                continue
            children_path = node.path / SESSION_CHILDREN_DIR_NAME
            expected = expected_children.get(node.node_id, set())
            if expected:
                if not children_path.is_dir() or children_path.is_symlink():
                    raise RuntimeError(
                        "检测到绕过软件修改会话目录结构；会话 children 目录与权威索引"
                        "不一致。"
                        f" session_id={node.node_id}, path={children_path}"
                    )
                validate_container(children_path, node.node_id)
            elif children_path.exists():
                validate_container(children_path, node.node_id)

    def _recover_stale_allocations_locked(
        self,
        nodes: dict[str, SessionPhysicalNode],
    ) -> None:
        """只回收软件留下的失效分配目录，不吸收未索引节点。"""
        containers = [self.sessions_root]
        for node in nodes.values():
            if node.kind == "folder":
                containers.append(node.path)
                continue
            children_path = node.path / SESSION_CHILDREN_DIR_NAME
            if children_path.is_dir() and not children_path.is_symlink():
                containers.append(children_path)
        for container in containers:
            if not container.is_dir() or container.is_symlink():
                continue
            for entry in tuple(container.iterdir()):
                marker = entry / SESSION_ALLOCATION_MARKER_NAME
                if entry.is_dir() and not entry.is_symlink() and marker.is_file():
                    recover_allocation_marker(
                        entry,
                        has_manifest=(entry / SESSION_MANIFEST_NAME).is_file(),
                    )

    def _refresh_locked(self) -> list[SessionPhysicalNode]:
        records = self._load_index_records_locked()
        nodes = self._nodes_from_records_locked(records)
        self._recover_stale_allocations_locked(nodes)
        self._validate_filesystem_locked(nodes)
        self._nodes = nodes
        self._physical_tree_error = None
        tracked_paths = [self.index_path, self.sessions_root]
        for node in nodes.values():
            tracked_paths.extend(
                [
                    node.path,
                    node.path
                    / (FOLDER_MANIFEST_NAME if node.kind == "folder" else SESSION_MANIFEST_NAME),
                ]
            )
            children_path = node.path / SESSION_CHILDREN_DIR_NAME
            if node.kind == "session" and children_path.is_dir():
                tracked_paths.append(children_path)
        self._navigation_mtimes = {
            path: _navigation_signature(path) for path in tracked_paths
        }
        self._loaded = True
        self._revision += 1
        return list(nodes.values())

    @property
    @session_tree_operation_locked
    def revision(self) -> int:
        """返回物理树进程内修订号，并先检查人工文件系统变更。"""
        with self._lock:
            self._ensure_loaded()
            self._refresh_if_navigation_changed_locked()
            self._raise_if_physical_tree_invalid_locked()
            return self._revision

    @session_tree_operation_locked
    def list_nodes(self, *, refresh: bool = False) -> list[SessionPhysicalNode]:
        with self._lock:
            if not self._loaded:
                self.initialize()
            if refresh:
                return self._refresh_locked()
            self._refresh_if_navigation_changed_locked()
            self._raise_if_physical_tree_invalid_locked()
            return list(self._nodes.values())

    @session_tree_operation_locked
    def list_authoritative_nodes(self) -> list[SessionPhysicalNode]:
        """只投影权威索引，不扫描或吸收未登记的物理目录。"""
        with self._lock:
            self._ensure_loaded()
            nodes = self._nodes_from_records_locked(
                self._load_index_records_locked()
            )
            self._nodes = nodes
            return list(nodes.values())

    @property
    @session_tree_operation_locked
    def physical_tree_error(self) -> str | None:
        """返回最近一次严格物理树校验错误，不触发新的扫描。"""
        with self._lock:
            self._ensure_loaded()
            return self._physical_tree_error

    @property
    @session_tree_operation_locked
    def authoritative_revision(self) -> int:
        """返回索引投影修订号，不要求物理树当前一致。"""
        with self._lock:
            self._ensure_loaded()
            return self._revision

    @session_tree_operation_locked
    def get_node(self, node_id: str) -> SessionPhysicalNode:
        with self._lock:
            self._ensure_loaded()
            self._refresh_node_if_changed_locked(node_id)
            self._raise_if_physical_tree_invalid_locked()
            node = self._nodes.get(node_id)
            if node is None:
                raise KeyError(f"物理会话节点不存在: {node_id}")
            return node

    @session_tree_operation_locked
    def resolve_session_node(self, session_id: str) -> Path:
        """通过稳定会话 ID 返回物理树中的绝对会话目录。"""
        node = self.get_node(session_id)
        if node.kind != "session":
            raise RuntimeError(f"节点不是会话: node_id={session_id}, path={node.path}")
        if not node.path.is_absolute():
            raise RuntimeError(
                "会话物理节点不是绝对路径: "
                f"session_id={session_id}, path={node.path}"
            )
        return node.path

    @session_tree_operation_locked
    def resolve_session_node_for_runtime(self, session_id: str) -> Path:
        """按索引定位运行时会话，但不扫描无关物理兄弟节点。

        事件、消息流和作业终态必须能够在目录树异常时完成收尾；否则一个
        无关的物理孤儿节点会让失败事件本身也无法落盘，最终留下永久运行态。
        这里仍校验索引、目标目录和目标 manifest，普通目录 API 继续使用严格
        的 ``resolve_session_node``，因此不会静默吸收物理树漂移。
        """
        with self._lock:
            nodes = self._nodes_from_records_locked(
                self._load_index_records_locked()
            )
            node = nodes.get(session_id)
            if node is None:
                raise KeyError(f"权威会话目录索引不存在: session_id={session_id}")
            if node.kind != "session":
                raise RuntimeError(
                    f"节点不是会话: node_id={session_id}, path={node.path}"
                )
            if not node.path.is_absolute():
                raise RuntimeError(
                    "会话物理节点不是绝对路径: "
                    f"session_id={session_id}, path={node.path}"
                )
            if not node.path.is_dir() or node.path.is_symlink():
                raise RuntimeError(
                    "检测到绕过软件修改会话目录结构；权威索引声明的运行时会话目录"
                    "不存在或不是普通目录: "
                    f"session_id={session_id}, path={node.path}"
                )
            if node.path.name != node.node_id:
                raise RuntimeError(
                    "检测到绕过软件修改会话目录结构；运行时会话目录名必须等于稳定 ID: "
                    f"session_id={session_id}, path={node.path}"
                )
            manifest_path = node.path / SESSION_MANIFEST_NAME
            manifest = _read_json_object(manifest_path)
            if manifest.get("session_id") != session_id:
                raise RuntimeError(
                    "检测到绕过软件修改会话目录结构；运行时会话 manifest 稳定 ID 不匹配: "
                    f"session_id={session_id}, manifest={manifest_path}"
                )
            expected_parent_session_id = nearest_session_ancestor_from_nodes(
                node.parent_node_id,
                nodes,
            )
            if manifest.get("parent_session_id") != expected_parent_session_id:
                raise RuntimeError(
                    "检测到绕过软件修改会话目录结构；运行时会话 manifest 与权威索引"
                    "父关系不一致: "
                    f"session_id={session_id}, "
                    f"expected_parent_session_id={expected_parent_session_id}, "
                    f"manifest_parent_session_id={manifest.get('parent_session_id')}"
                )
            return node.path

    @session_tree_operation_locked
    def resolve_folder_dir(self, folder_id: str) -> Path:
        node = self.get_node(folder_id)
        if node.kind != "folder":
            raise RuntimeError(f"节点不是会话文件夹: node_id={folder_id}, path={node.path}")
        return node.path


    @session_tree_operation_locked
    def relative_path(self, node_id: str) -> str:
        with self._lock:
            return self.get_node(node_id).path.relative_to(self.sessions_root).as_posix()

    @session_tree_operation_locked
    def child_nodes(self, node_id: str) -> list[SessionPhysicalNode]:
        with self._lock:
            self._ensure_loaded()
            self._refresh_if_navigation_changed_locked()
            return [
                node for node in self._nodes.values() if node.parent_node_id == node_id
            ]

    @session_tree_operation_locked
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

    @session_tree_operation_locked
    def nearest_session_ancestor(self, parent_node_id: str | None) -> str | None:
        with self._lock:
            self._ensure_loaded()
            self._raise_if_physical_tree_invalid_locked()
            return nearest_session_ancestor_from_nodes(
                parent_node_id,
                self._nodes,
            )
    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.initialize()

    def _raise_if_physical_tree_invalid_locked(self) -> None:
        if self._physical_tree_error is not None:
            raise RuntimeError(self._physical_tree_error)


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
