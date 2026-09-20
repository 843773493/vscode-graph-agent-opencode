"""一次性 catalog 迁移读取所需的旧物理布局模型。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

FOLDER_MANIFEST_NAME = ".boxteam-folder.json"
SESSION_MANIFEST_NAME = "session.json"
SESSION_CHILDREN_DIR_NAME = "children"
PHYSICAL_LAYOUT_VERSION = 1


@dataclass(frozen=True, slots=True)
class SessionPhysicalNode:
    """旧嵌套物理树中的导航节点投影。"""

    node_id: str
    kind: str
    path: Path
    parent_node_id: str | None
    name: str
    created_at: datetime
    updated_at: datetime


def read_json_object(path: Path) -> dict[str, object]:
    """读取并严格要求 JSON 根值为对象。"""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError(f"JSON 文件必须是 object: {path}")
    return {str(key): value for key, value in raw.items()}


def parse_datetime(value: object, path: Path) -> datetime:
    """解析必填 ISO 时间字段。"""
    parsed = parse_optional_datetime(value)
    if parsed is None:
        raise RuntimeError(f"manifest 缺少合法时间字段: {path}")
    return parsed


def parse_optional_datetime(value: object) -> datetime | None:
    """解析可选 ISO 时间字段，缺失值返回 None。"""
    if not isinstance(value, str) or not value:
        return None
    return datetime.fromisoformat(value)


def nearest_session_ancestor_from_nodes(
    parent_node_id: str | None,
    nodes: dict[str, SessionPhysicalNode],
) -> str | None:
    """沿旧导航父链查找最近的会话祖先，并拒绝循环或悬空父节点。"""
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


__all__ = [
    "FOLDER_MANIFEST_NAME",
    "PHYSICAL_LAYOUT_VERSION",
    "SESSION_CHILDREN_DIR_NAME",
    "SESSION_MANIFEST_NAME",
    "SessionPhysicalNode",
    "nearest_session_ancestor_from_nodes",
    "parse_datetime",
    "parse_optional_datetime",
    "read_json_object",
]
