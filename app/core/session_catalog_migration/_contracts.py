"""迁移机公开 DTO、异常、journal 映射与拓扑序工具(逐字搬迁)。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from app.core.session_catalog_legacy_layout import SessionPhysicalNode

QuarantineReason = Literal["illegal_id", "illegal_date", "parent_quarantined"]


@dataclass(frozen=True, slots=True)
class QuarantinedNode:
    """被隔离的旧节点:不进 SQLite 目标树,原样保留在旧树中供审计。"""

    node_id: str
    reason: QuarantineReason


@dataclass(frozen=True, slots=True)
class SessionCatalogMigrationResult:
    """一次迁移的最终计数结果(与 completed journal 的 result 节同构)。"""

    migrated_session_nodes: int
    migrated_folder_nodes: int
    quarantined_nodes: tuple[QuarantinedNode, ...]
    journal_path: Path


@dataclass(frozen=True, slots=True)
class _FrozenNode:
    """journal 冻结的节点映射:重建 SQLite 树的唯一依据。

    folder 只冻结导航四元组;session 额外冻结 created_at、storage 相对
    locator 与已分配的 main_thread_id(恢复时复用,不重新生成)。
    """

    node_id: str
    kind: str
    parent_node_id: str | None
    display_name: str
    created_at: datetime | None
    storage_relative_locator: str | None
    main_thread_id: str | None


@dataclass
class _MigrationContext:
    """一次迁移运行的 journal 工作态(解析自 journal 或 fresh 构造)。

    ``physical`` 是可变 dict:物理迁移阶段逐动作更新并整体落盘;其余字段
    在一次运行内只读。"""

    backup: dict[str, object]
    frozen: list[_FrozenNode]
    quarantined: list[QuarantinedNode]
    migration_id: str
    physical: dict[str, object]


class SessionCatalogMigrationError(RuntimeError):
    """迁移 fail-closed 总类:旧权威不一致、journal 冲突、恢复无法证明、备份复验失败。"""


def _topological_order(nodes: list[SessionPhysicalNode]) -> list[SessionPhysicalNode]:
    """返回父先子后的确定性拓扑序;每轮按 node_id 排序保证结果稳定。"""
    remaining = sorted(nodes, key=lambda item: item.node_id)
    placed: set[str] = set()
    ordered: list[SessionPhysicalNode] = []
    while remaining:
        progressed: list[SessionPhysicalNode] = []
        deferred: list[SessionPhysicalNode] = []
        for node in remaining:
            if node.parent_node_id is None or node.parent_node_id in placed:
                progressed.append(node)
            else:
                deferred.append(node)
        if not progressed:
            # resolver 已保证无环;此处是防御性兜底,不静默吞掉。
            raise RuntimeError(
                "迁移冻结阶段无法推进拓扑序(疑似环): "
                f"node_ids={[item.node_id for item in deferred]}"
            )
        for node in progressed:
            ordered.append(node)
            placed.add(node.node_id)
        remaining = deferred
    return ordered


def _frozen_node_to_dict(item: _FrozenNode) -> dict[str, object]:
    """冻结节点 → journal JSON 记录(folder 不携带 session 专属字段)。"""
    payload: dict[str, object] = {
        "node_id": item.node_id,
        "kind": item.kind,
        "parent_node_id": item.parent_node_id,
        "display_name": item.display_name,
    }
    if item.kind == "session":
        payload["created_at"] = (
            item.created_at.isoformat() if item.created_at is not None else None
        )
        payload["storage_relative_locator"] = item.storage_relative_locator
        payload["main_thread_id"] = item.main_thread_id
    return payload


def _quarantined_to_dict(item: QuarantinedNode) -> dict[str, object]:
    return {"node_id": item.node_id, "reason": item.reason}


def _result_to_dict(result: SessionCatalogMigrationResult) -> dict[str, object]:
    return {
        "migrated_session_nodes": result.migrated_session_nodes,
        "migrated_folder_nodes": result.migrated_folder_nodes,
        "quarantined_nodes": [
            _quarantined_to_dict(item) for item in result.quarantined_nodes
        ],
    }
