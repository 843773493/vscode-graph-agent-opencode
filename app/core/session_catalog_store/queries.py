"""catalog 只读查询一条垂直链路。

承载子节点计数/分页、节点投影读取、面包屑、最近 session 祖先、后代
session ID、按 main thread 反查、全表一致性校验与 locator 绝对路径解析。

错误分类沿用宿主约定：``TypeError`` 输入类型错误、``ValueError`` 输入形态
非法、``KeyError`` 目标行不存在、``RuntimeError`` 语义冲突。
"""

from __future__ import annotations

from pathlib import Path

from app.core.session_catalog_store._schema import (
    _LOCATOR_PREFIX,
    _NODE_COLUMNS,
)
from app.core.session_catalog_store.contracts import SessionCatalogNode
from app.core.session_catalog_store.validators import (
    validate_path_budget,
    validate_storage_relative_locator,
)


class CatalogQueriesMixin:
    """catalog 只读查询方法族（唯一实现点）。"""

    def count_children(self, node_id: str) -> int:
        """返回直接子节点数。"""
        with self.read_transaction() as connection:
            self._require_node(connection, node_id)
            row = connection.execute(
                "SELECT COUNT(*) FROM nodes WHERE parent_node_id = ?",
                (node_id,),
            ).fetchone()
            return int(row[0])

    def get_node(self, node_id: str) -> SessionCatalogNode:
        """返回节点投影；不存在抛 KeyError。"""
        with self.read_transaction() as connection:
            return self._node_from_row(self._require_node(connection, node_id))

    def list_children(
        self,
        parent_node_id: str | None,
        *,
        limit: int,
        cursor: str | None = None,
    ) -> tuple[list[SessionCatalogNode], str | None, bool]:
        """按 node_id 游标稳定分页返回直接子节点。

        ``parent_node_id`` 为 None 时返回根级节点；``cursor`` 是上一页最后
        一个 node_id，下一页从其之后开始。
        """
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError(f"limit 必须是正整数: {limit!r}")
        with self.read_transaction() as connection:
            if parent_node_id is not None:
                self._require_node(connection, parent_node_id)
            if cursor is None:
                rows = connection.execute(
                    f"SELECT {_NODE_COLUMNS} FROM nodes "
                    "WHERE parent_node_id IS ? ORDER BY node_id LIMIT ?",
                    (parent_node_id, limit + 1),
                ).fetchall()
            else:
                rows = connection.execute(
                    f"SELECT {_NODE_COLUMNS} FROM nodes "
                    "WHERE parent_node_id IS ? AND node_id > ? "
                    "ORDER BY node_id LIMIT ?",
                    (parent_node_id, cursor, limit + 1),
                ).fetchall()
            has_more = len(rows) > limit
            items = [self._node_from_row(row) for row in rows[:limit]]
            next_cursor = items[-1].node_id if has_more and items else None
            return items, next_cursor, has_more

    def breadcrumb(self, node_id: str) -> list[SessionCatalogNode]:
        """返回从根到该节点（含自身）的节点链。"""
        with self.read_transaction() as connection:
            chain: list[SessionCatalogNode] = []
            visited: set[str] = set()
            current_id: str | None = node_id
            while current_id is not None:
                if current_id in visited:
                    raise RuntimeError(f"会话目录包含循环: {current_id}")
                visited.add(current_id)
                row = self._require_node(connection, current_id)
                chain.append(self._node_from_row(row))
                current_id = row["parent_node_id"]
            chain.reverse()
            return chain

    def nearest_session_ancestor(self, node_id: str) -> str | None:
        """返回最近的 session 祖先 ID（不含自身；传 parent 语义）。

        从父节点开始向上找第一个 kind 为 session 的祖先。
        """
        with self.read_transaction() as connection:
            row = self._require_node(connection, node_id)
            current_id = row["parent_node_id"]
            visited: set[str] = set()
            while current_id is not None:
                if current_id in visited:
                    raise RuntimeError(f"会话目录包含循环: {current_id}")
                visited.add(current_id)
                current_row = self._require_node(connection, current_id)
                if current_row["kind"] == "session":
                    return str(current_row["node_id"])
                current_id = current_row["parent_node_id"]
            return None

    def descendant_session_ids(self, node_id: str) -> list[str]:
        """递归 CTE 返回全部后代 session ID（不含自身，按 node_id 排序）。"""
        with self.read_transaction() as connection:
            self._require_node(connection, node_id)
            rows = connection.execute(
                """
                WITH RECURSIVE descendants(node_id, kind) AS (
                    SELECT node_id, kind FROM nodes WHERE parent_node_id = ?
                    UNION
                    SELECT n.node_id, n.kind FROM nodes n
                    JOIN descendants d ON n.parent_node_id = d.node_id
                )
                SELECT node_id FROM descendants WHERE kind = 'session'
                ORDER BY node_id
                """,
                (node_id,),
            ).fetchall()
            return [str(row[0]) for row in rows]

    def get_session_by_main_thread(
        self,
        workspace_id: str,
        main_thread_id: str,
    ) -> SessionCatalogNode:
        """按 (workspace_id, main_thread_id) 返回唯一 session 节点。"""
        with self.read_transaction() as connection:
            row = connection.execute(
                f"SELECT {_NODE_COLUMNS} FROM nodes "
                "WHERE workspace_id = ? AND main_thread_id = ? AND kind = 'session'",
                (workspace_id, main_thread_id),
            ).fetchone()
            if row is None:
                raise KeyError(
                    "main_thread_id 对应的 session 不存在: "
                    f"workspace_id={workspace_id}, main_thread_id={main_thread_id}"
                )
            return self._node_from_row(row)

    def verify_workspace_consistency(self) -> None:
        """全表校验：父节点存在、父子同 workspace、无环；违反抛 RuntimeError。"""
        with self.read_transaction() as connection:
            rows = connection.execute(
                "SELECT node_id, parent_node_id, workspace_id FROM nodes"
            ).fetchall()
        by_id = {str(row["node_id"]): row for row in rows}
        for row in rows:
            parent_node_id = row["parent_node_id"]
            if parent_node_id is None:
                continue
            parent = by_id.get(str(parent_node_id))
            if parent is None:
                raise RuntimeError(
                    "会话目录父节点缺失: "
                    f"node_id={row['node_id']}, parent_node_id={parent_node_id}"
                )
            if str(parent["workspace_id"]) != str(row["workspace_id"]):
                raise RuntimeError(
                    "会话目录父子节点 workspace 不一致: "
                    f"node_id={row['node_id']}, "
                    f"node_workspace={row['workspace_id']}, "
                    f"parent_node_id={parent_node_id}, "
                    f"parent_workspace={parent['workspace_id']}"
                )
        for row in rows:
            visited: set[str] = set()
            current = row
            while current["parent_node_id"] is not None:
                current_id = str(current["node_id"])
                if current_id in visited:
                    raise RuntimeError(f"会话目录包含循环: {current_id}")
                visited.add(current_id)
                current = by_id[str(current["parent_node_id"])]

    def resolve_session_locator(self, locator: str) -> Path:
        """解析 locator 为 sessions_root 下的绝对路径；先过形态与预算校验。"""
        validate_storage_relative_locator(locator)
        relative = locator[len(_LOCATOR_PREFIX):]
        validate_path_budget(self.sessions_root, relative)
        return self.sessions_root / relative
