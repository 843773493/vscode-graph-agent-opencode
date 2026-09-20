"""只读读取 schema v3 会话目录权威，供一次性 catalog 迁移使用。

这个模块只处理迁移边界上的旧 JSON 索引和嵌套物理树。它不持有生产
resolver 的可变状态，也不执行任何旧布局、manifest、分配 marker 或物理
目录迁移；读取失败直接抛出，调用方必须保留原现场并终止迁移。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from app.core.path_segments import physical_segment
from app.core.session_catalog_legacy_layout import (
    FOLDER_MANIFEST_NAME,
    PHYSICAL_LAYOUT_VERSION,
    SESSION_CHILDREN_DIR_NAME,
    SESSION_MANIFEST_NAME,
    SessionPhysicalNode,
    nearest_session_ancestor_from_nodes,
    parse_optional_datetime,
    read_json_object,
)

__all__ = [
    "SessionCatalogLegacyReader",
    "SessionCatalogLegacyReaderError",
]


class SessionCatalogLegacyReaderError(RuntimeError):
    """旧 schema v3 权威或其物理投影不一致。"""


class SessionCatalogLegacyReader:
    """只读校验并投影旧 schema v3 索引与物理树。

    ``read`` 只读取索引声明的节点及其父子容器，不通过扫描磁盘发现节点。
    容器内出现未被索引声明的导航项会直接失败；会话节点自身的业务内容
    （例如 rollout 文件）不属于导航物理树，因此保留为不透明内容，不递归
    吸收。
    """

    INDEX_SCHEMA_VERSION = 3

    def __init__(self, sessions_root: Path, index_path: Path | None = None) -> None:
        if not isinstance(sessions_root, Path):
            raise TypeError(f"sessions_root 必须是 Path: {sessions_root!r}")
        if index_path is not None and not isinstance(index_path, Path):
            raise TypeError(f"index_path 必须是 Path: {index_path!r}")
        self.sessions_root = sessions_root.expanduser().resolve()
        self.index_path = (
            index_path.expanduser().resolve()
            if index_path is not None
            else self.sessions_root.parent
            / "navigation"
            / "session-catalog-index.json"
        )

    def read(self) -> list[SessionPhysicalNode]:
        """读取并严格校验旧权威，成功时返回索引顺序的节点投影。"""
        records = self._read_index_records()
        # 空权威树没有任何需要对账的物理节点；旧 resolver 会在此场景初始化
        # 根目录，迁移 reader 不能写盘，因此允许根目录尚未创建的空树继续。
        if not records and not self.sessions_root.exists():
            return []
        self._validate_sessions_root()
        nodes = self._build_nodes(records)
        self._validate_physical_tree(nodes)
        return list(nodes.values())

    def _validate_sessions_root(self) -> None:
        if not self.sessions_root.is_dir() or self.sessions_root.is_symlink():
            raise SessionCatalogLegacyReaderError(
                "旧会话物理根必须是普通目录: " f"path={self.sessions_root}"
            )

    def _read_index_records(self) -> list[dict[str, object]]:
        if not self.index_path.is_file() or self.index_path.is_symlink():
            raise SessionCatalogLegacyReaderError(
                f"旧权威会话目录索引缺失或不是普通文件: {self.index_path}"
            )
        try:
            raw = read_json_object(self.index_path)
        except (OSError, ValueError, TypeError) as error:
            raise SessionCatalogLegacyReaderError(
                f"旧权威会话目录索引无法读取: {self.index_path}: {error}"
            ) from error
        if raw.get("schema_version") != self.INDEX_SCHEMA_VERSION:
            raise SessionCatalogLegacyReaderError(
                "旧权威会话目录索引 schema 版本非法: "
                f"path={self.index_path}, schema_version={raw.get('schema_version')!r}, "
                f"expected={self.INDEX_SCHEMA_VERSION}"
            )
        records = raw.get("nodes")
        if not isinstance(records, list):
            raise SessionCatalogLegacyReaderError(
                f"旧权威会话目录索引 nodes 必须是数组: {self.index_path}"
            )

        normalized: list[dict[str, object]] = []
        seen: set[str] = set()
        for offset, value in enumerate(records):
            if not isinstance(value, dict):
                raise SessionCatalogLegacyReaderError(
                    "旧权威会话目录索引节点必须是对象: "
                    f"path={self.index_path}, offset={offset}"
                )
            node_id = value.get("node_id")
            kind = value.get("kind")
            name = value.get("name")
            parent_node_id = value.get("parent_node_id")
            if not isinstance(node_id, str) or not node_id:
                raise SessionCatalogLegacyReaderError(
                    f"旧权威会话目录索引节点 ID 非法: offset={offset}"
                )
            try:
                expected_segment = physical_segment(str(name or ""), node_id)
            except (TypeError, ValueError) as error:
                raise SessionCatalogLegacyReaderError(
                    "旧权威会话目录索引节点 ID 不能作为物理路径段: "
                    f"node_id={node_id!r}, offset={offset}"
                ) from error
            if expected_segment != node_id:
                raise SessionCatalogLegacyReaderError(
                    "旧权威会话目录索引节点物理路径段不稳定: "
                    f"node_id={node_id!r}, segment={expected_segment!r}"
                )
            if node_id in seen:
                raise SessionCatalogLegacyReaderError(
                    f"旧权威会话目录索引包含重复节点 ID: {node_id}"
                )
            if kind not in {"folder", "session"}:
                raise SessionCatalogLegacyReaderError(
                    f"旧权威会话目录索引节点类型非法: node_id={node_id}, kind={kind!r}"
                )
            if not isinstance(name, str) or not name:
                raise SessionCatalogLegacyReaderError(
                    f"旧权威会话目录索引节点显示名非法: node_id={node_id}"
                )
            if parent_node_id is not None and not isinstance(parent_node_id, str):
                raise SessionCatalogLegacyReaderError(
                    f"旧权威会话目录索引父节点非法: node_id={node_id}"
                )
            normalized.append({str(key): item for key, item in value.items()})
            seen.add(node_id)
        return normalized

    def _build_nodes(
        self, records: list[dict[str, object]]
    ) -> dict[str, SessionPhysicalNode]:
        records_by_id = {str(record["node_id"]): record for record in records}
        nodes: dict[str, SessionPhysicalNode] = {}
        visiting: set[str] = set()

        def build(node_id: str) -> SessionPhysicalNode:
            existing = nodes.get(node_id)
            if existing is not None:
                return existing
            if node_id in visiting:
                raise SessionCatalogLegacyReaderError(
                    f"旧权威会话目录索引包含循环: {node_id}"
                )
            record = records_by_id.get(node_id)
            if record is None:
                raise SessionCatalogLegacyReaderError(
                    f"旧权威会话目录索引节点不存在: {node_id}"
                )
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
            created_at = self._parse_index_datetime(
                record.get("created_at"), node_id=node_id, field="created_at"
            )
            updated_at = self._parse_index_datetime(
                record.get("updated_at"), node_id=node_id, field="updated_at"
            )
            try:
                path_segment = physical_segment(str(record["name"]), node_id)
            except (KeyError, TypeError, ValueError) as error:
                raise SessionCatalogLegacyReaderError(
                    f"旧权威会话目录索引节点物理路径段非法: node_id={node_id}"
                ) from error
            node = SessionPhysicalNode(
                node_id=node_id,
                kind=str(record["kind"]),
                path=parent_path / path_segment,
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

    @staticmethod
    def _parse_index_datetime(
        value: object, *, node_id: str, field: str
    ) -> datetime:
        try:
            parsed = parse_optional_datetime(value)
        except (TypeError, ValueError) as error:
            raise SessionCatalogLegacyReaderError(
                f"旧权威会话目录索引节点时间非法: node_id={node_id}, field={field}"
            ) from error
        if parsed is None:
            raise SessionCatalogLegacyReaderError(
                f"旧权威会话目录索引节点时间非法: node_id={node_id}, field={field}"
            )
        return parsed

    def _validate_physical_tree(
        self, nodes: dict[str, SessionPhysicalNode]
    ) -> None:
        expected_children: dict[str | None, set[str]] = {}
        paths: dict[Path, str] = {}
        for node in nodes.values():
            expected_children.setdefault(node.parent_node_id, set()).add(node.node_id)
            expected_segment = physical_segment(node.name, node.node_id)
            if node.path.name != expected_segment:
                raise SessionCatalogLegacyReaderError(
                    "旧会话节点物理目录名与稳定 ID 不一致: "
                    f"node_id={node.node_id}, path={node.path}"
                )
            previous_id = paths.get(node.path)
            if previous_id is not None:
                raise SessionCatalogLegacyReaderError(
                    "旧会话节点物理路径重复: "
                    f"path={node.path}, node_ids={previous_id},{node.node_id}"
                )
            paths[node.path] = node.node_id
            if not node.path.is_dir() or node.path.is_symlink():
                raise SessionCatalogLegacyReaderError(
                    "旧会话节点目录不存在或不是普通目录: "
                    f"node_id={node.node_id}, path={node.path}"
                )
            manifest_name = (
                FOLDER_MANIFEST_NAME
                if node.kind == "folder"
                else SESSION_MANIFEST_NAME
            )
            manifest_path = node.path / manifest_name
            if not manifest_path.is_file() or manifest_path.is_symlink():
                raise SessionCatalogLegacyReaderError(
                    "旧会话节点 manifest 缺失或不是普通文件: "
                    f"node_id={node.node_id}, manifest={manifest_path}"
                )
            try:
                manifest = read_json_object(manifest_path)
            except (OSError, ValueError, TypeError) as error:
                raise SessionCatalogLegacyReaderError(
                    f"旧会话节点 manifest 无法读取: {manifest_path}: {error}"
                ) from error
            self._validate_manifest(node, manifest, manifest_path)
            if node.kind == "session":
                expected_parent_session_id = nearest_session_ancestor_from_nodes(
                    node.parent_node_id,
                    nodes,
                )
                if manifest.get("parent_session_id") != expected_parent_session_id:
                    raise SessionCatalogLegacyReaderError(
                        "旧会话 manifest 与权威索引父关系不一致: "
                        f"session_id={node.node_id}, "
                        f"expected_parent_session_id={expected_parent_session_id}, "
                        f"manifest_parent_session_id={manifest.get('parent_session_id')}, "
                        f"manifest={manifest_path}"
                    )
                children_path = node.path / SESSION_CHILDREN_DIR_NAME
                nested_manifests = [
                    path
                    for manifest_name in (FOLDER_MANIFEST_NAME, SESSION_MANIFEST_NAME)
                    for path in node.path.rglob(manifest_name)
                    if path != manifest_path
                    and not path.is_relative_to(children_path)
                ]
                if nested_manifests:
                    raise SessionCatalogLegacyReaderError(
                        "旧会话导航子节点必须位于保留的 children 目录下: "
                        f"session_id={node.node_id}, manifests={nested_manifests}"
                    )

        self._validate_container(
            self.sessions_root,
            expected_children.get(None, set()),
            allowed_files=set(),
            description="sessions 根",
        )
        for node in nodes.values():
            if node.kind == "folder":
                self._validate_container(
                    node.path,
                    expected_children.get(node.node_id, set()),
                    allowed_files={FOLDER_MANIFEST_NAME},
                    description=f"folder={node.node_id}",
                )
                continue
            children_path = node.path / SESSION_CHILDREN_DIR_NAME
            expected = expected_children.get(node.node_id, set())
            if expected or children_path.exists():
                if not children_path.is_dir() or children_path.is_symlink():
                    raise SessionCatalogLegacyReaderError(
                        "旧会话 children 边界必须是真实目录: "
                        f"session_id={node.node_id}, path={children_path}"
                    )
                self._validate_container(
                    children_path,
                    expected,
                    allowed_files=set(),
                    description=f"session children={node.node_id}",
                )

    def _validate_manifest(
        self,
        node: SessionPhysicalNode,
        manifest: dict[str, object],
        manifest_path: Path,
    ) -> None:
        if node.kind == "folder":
            if manifest.get("schema_version") != PHYSICAL_LAYOUT_VERSION:
                raise SessionCatalogLegacyReaderError(
                    f"旧会话文件夹 manifest 版本非法: {manifest_path}"
                )
            if manifest.get("folder_id") != node.node_id:
                raise SessionCatalogLegacyReaderError(
                    "旧会话文件夹 manifest 稳定 ID 不匹配: "
                    f"node_id={node.node_id}, manifest={manifest_path}"
                )
            self._parse_manifest_datetime(manifest, "created_at", manifest_path)
            return
        if manifest.get("session_id") != node.node_id:
            raise SessionCatalogLegacyReaderError(
                "旧会话 manifest 稳定 ID 不匹配: "
                f"node_id={node.node_id}, manifest={manifest_path}"
            )
        if not isinstance(manifest.get("title"), str):
            raise SessionCatalogLegacyReaderError(
                f"旧会话 manifest 缺少合法 title: {manifest_path}"
            )
        self._parse_manifest_datetime(manifest, "created_at", manifest_path)
        self._parse_manifest_datetime(manifest, "updated_at", manifest_path)

    @staticmethod
    def _parse_manifest_datetime(
        manifest: dict[str, object], field: str, manifest_path: Path
    ) -> datetime:
        try:
            parsed = parse_optional_datetime(manifest.get(field))
        except (TypeError, ValueError) as error:
            raise SessionCatalogLegacyReaderError(
                f"旧会话 manifest 时间字段非法: field={field}, path={manifest_path}"
            ) from error
        if parsed is None:
            raise SessionCatalogLegacyReaderError(
                f"旧会话 manifest 缺少合法时间字段: field={field}, path={manifest_path}"
            )
        return parsed

    def _validate_container(
        self,
        path: Path,
        expected_children: set[str],
        *,
        allowed_files: set[str],
        description: str,
    ) -> None:
        if not path.is_dir() or path.is_symlink():
            raise SessionCatalogLegacyReaderError(
                f"旧会话物理容器不是普通目录: {description}, path={path}"
            )
        actual = {
            entry.name for entry in path.iterdir() if entry.name not in allowed_files
        }
        expected = {
            physical_segment("", node_id) for node_id in expected_children
        }
        if actual != expected:
            raise SessionCatalogLegacyReaderError(
                "旧会话权威索引与物理容器不一致: "
                f"{description}, path={path}, expected={sorted(expected)}, "
                f"actual={sorted(actual)}"
            )
