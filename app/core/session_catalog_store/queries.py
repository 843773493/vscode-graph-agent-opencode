"""catalog 只读查询一条垂直链路。

承载子节点计数/分页、节点投影读取、面包屑、最近 session 祖先、后代
session ID、按 main thread 反查、全表一致性校验与 locator 绝对路径解析。

错误分类沿用宿主约定：``TypeError`` 输入类型错误、``ValueError`` 输入形态
非法、``KeyError`` 目标行不存在、``RuntimeError`` 语义冲突。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from app.core.session_catalog_store._schema import (
    _LOCATOR_PREFIX,
    _NODE_COLUMNS,
    _SESSION_LIST_FINGERPRINT_PARTS,
    _SESSION_LIST_INDEX_NAME,
)
from app.core.session_catalog_store.contracts import (
    CatalogMaintenanceRequiredError,
    SessionCatalogNode,
    _validate_workspace_id,
)
from app.core.session_catalog_store.validators import (
    validate_path_budget,
    validate_session_storage_identity,
    validate_storage_relative_locator,
    validate_thread_id,
)

# 根级 active session 直接可见；其它候选只有完整 active 祖先链到根才可见。
# UNION 在损坏的环上收敛；孤儿/环与旧从根遍历行为一致，不成为列表成员。
_SESSION_LIST_VISIBLE = """
    kind = 'session' AND state = 'active'
    AND (candidate.parent_node_id IS NULL OR (
        WITH RECURSIVE ancestors(node_id, parent_node_id, state) AS (
            SELECT node_id, parent_node_id, state FROM nodes
            WHERE node_id = candidate.parent_node_id
            UNION
            SELECT parent.node_id, parent.parent_node_id, parent.state
            FROM nodes AS parent JOIN ancestors AS child
                ON parent.node_id = child.parent_node_id
            WHERE child.state = 'active'
        )
        SELECT MIN(state = 'active') AND MAX(parent_node_id IS NULL)
        FROM ancestors
    ))
"""


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

    def read_node_with_session_parent(
        self, node_id: str
    ) -> tuple[SessionCatalogNode, str | None]:
        """同一读事务取得节点及最近会话祖先，不拼接第二套父子关系。"""
        with self.read_transaction() as connection:
            node = self._node_from_row(self._require_node(connection, node_id))
            current_id = node.parent_node_id
            visited = {node_id}
            while current_id is not None:
                if current_id in visited:
                    raise RuntimeError(f"会话目录包含循环: {current_id}")
                visited.add(current_id)
                row = connection.execute(
                    "SELECT kind, parent_node_id FROM nodes WHERE node_id = ?",
                    (current_id,),
                ).fetchone()
                if row is None:
                    raise RuntimeError(f"物理会话节点父节点不存在: {current_id}")
                if row["kind"] == "session":
                    return node, current_id
                current_id = row["parent_node_id"]
            return node, None

    def read_session_list_page(
        self, *, skip: int, limit: int, cursor: str | None
    ) -> tuple[list[SessionCatalogNode], dict[str, str | None], int, str | None, bool]:
        """SQLite 索引确定可见成员与排序；Python 只读取本页及其最近会话祖先。"""
        if limit < 1:
            raise ValueError(f"会话列表 limit 必须大于 0: limit={limit}")
        with self.read_transaction() as connection:
            # 分量已由同一 nodes 表的表达式索引在写入时算好；COUNT/SUM
            # 只返回固定大小标量，不把全表成员或逐成员 UDF 搬回 Python。
            sums = ", ".join(
                f"COALESCE(SUM(session_list_member_part(node_id, created_at, {part})), 0)"
                for part in range(_SESSION_LIST_FINGERPRINT_PARTS)
            )
            summary = connection.execute(
                f"SELECT COUNT(*), {sums} FROM nodes AS candidate "
                f"INDEXED BY {_SESSION_LIST_INDEX_NAME} WHERE {_SESSION_LIST_VISIBLE}"
            ).fetchone()
            total = int(summary[0])
            revision = hashlib.sha256(json.dumps(
                tuple(summary), separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            offset = _decode_session_list_cursor(cursor, revision=revision) if cursor is not None else skip
            if offset < 0 or offset > total:
                raise ValueError(f"会话列表 cursor offset 越界: offset={offset}, total={total}")
            page_rows = connection.execute(
                f"SELECT {_NODE_COLUMNS} FROM nodes AS candidate "
                f"INDEXED BY {_SESSION_LIST_INDEX_NAME} WHERE {_SESSION_LIST_VISIBLE} "
                "ORDER BY session_list_created_us(created_at) DESC, node_id ASC "
                "LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            nodes = [self._node_from_row(row) for row in page_rows]
            ancestors: dict[str, sqlite3.Row] = {}
            parents: dict[str, str | None] = {}
            for node in nodes:
                current_id = node.parent_node_id
                visited = {node.node_id}
                while current_id is not None:
                    if current_id in visited:
                        raise RuntimeError(f"会话目录包含循环: {current_id}")
                    visited.add(current_id)
                    if current_id not in ancestors:
                        parent = connection.execute(
                            "SELECT kind, parent_node_id FROM nodes WHERE node_id = ?",
                            (current_id,),
                        ).fetchone()
                        if parent is None:
                            raise RuntimeError(f"物理会话节点父节点不存在: {current_id}")
                        ancestors[current_id] = parent
                    parent = ancestors[current_id]
                    if parent["kind"] == "session":
                        break
                    current_id = parent["parent_node_id"]
                parents[node.node_id] = current_id
            next_offset = offset + len(nodes)
            has_more = next_offset < total
            return (
                nodes, parents, total,
                _encode_session_list_cursor(next_offset, revision=revision) if has_more else None,
                has_more,
            )

    def read_nodes_snapshot(self) -> tuple[int, list[SessionCatalogNode]]:
        """在同一只读事务中返回 navigation revision 与全部节点行。"""
        with self.read_transaction() as connection:
            generation_row = connection.execute(
                "SELECT navigation_revision FROM catalog_metadata "
                "WHERE singleton_id = 1"
            ).fetchone()
            if generation_row is None:
                raise CatalogMaintenanceRequiredError(
                    "session catalog 缺少 navigation revision 记录"
                )
            rows = connection.execute(
                f"SELECT {_NODE_COLUMNS} FROM nodes ORDER BY node_id"
            ).fetchall()
            return int(generation_row[0]), [self._node_from_row(row) for row in rows]

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

    def verify_workspace_consistency(self, *, workspace_id: str) -> None:
        """同一快照核本工作区全部保留记录、父链与冻结存储身份。"""
        _validate_workspace_id(workspace_id)
        with self.read_transaction() as connection:
            mismatch = connection.execute(
                "SELECT 'nodes' AS owner, node_id AS record_id, workspace_id FROM nodes WHERE workspace_id != ? "
                "UNION ALL SELECT 'session_creation_records', session_creation_idempotency_key, workspace_id "
                "FROM session_creation_records WHERE workspace_id != ? "
                "UNION ALL SELECT 'subtree_delete_records', subtree_delete_idempotency_key, workspace_id "
                "FROM subtree_delete_records WHERE workspace_id != ? "
                "UNION ALL SELECT 'fork_retention_claims', fork_retention_claim_id, workspace_id "
                "FROM fork_retention_claims WHERE workspace_id != ? LIMIT 1",
                (workspace_id,) * 4,
            ).fetchone()
            if mismatch is not None:
                raise CatalogMaintenanceRequiredError(
                    "session catalog workspace 不一致，须进入维护模式核对并保留原数据: "
                    f"owner={mismatch['owner']}, record_id={mismatch['record_id']}, "
                    f"record_workspace={mismatch['workspace_id']!r}, backend_workspace={workspace_id!r}"
                )
            self.verify_subtree_delete_records(connection=connection)
            self.verify_creation_record_envelopes(connection=connection)
            rows = connection.execute(
                "SELECT node_id, parent_node_id, workspace_id FROM nodes"
            ).fetchall()
            identities = connection.execute(
                "SELECT node_id AS session_id, main_thread_id, created_at, "
                "storage_relative_locator, 'nodes' AS owner FROM nodes WHERE kind = 'session' "
                "UNION ALL SELECT session_id, main_thread_id, created_at, "
                "storage_relative_locator, 'session_creation_records' AS owner "
                "FROM session_creation_records WHERE state = 'preparing'"
            )
            for identity in identities:
                try:
                    locator = identity["storage_relative_locator"]
                    validate_session_storage_identity(
                        identity["session_id"], datetime.fromisoformat(identity["created_at"]), locator,
                    )
                    validate_thread_id(identity["main_thread_id"])
                    validate_path_budget(self.sessions_root, locator[len(_LOCATOR_PREFIX):])
                except (TypeError, ValueError) as error:
                    raise CatalogMaintenanceRequiredError(
                        "session catalog 存储身份损坏，须进入维护模式核对并保留原数据: "
                        f"owner={identity['owner']}, session_id={identity['session_id']}"
                    ) from error
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

def _session_list_created_us(created_at: str) -> int:
    """排序精度与 Python aware datetime 一致，不使用 SQLite 毫秒日期函数。"""
    moment = datetime.fromisoformat(created_at).astimezone(UTC)
    delta = moment - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _session_list_member_part(node_id: str, created_at: str, part: int) -> int:
    """表达式索引的成员指纹；保留旧成员定义中的 datetime 文本规范化。"""
    encoded = json.dumps(
        (node_id, str(datetime.fromisoformat(created_at))),
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    fingerprint = hashlib.sha256(encoded).digest()
    return int.from_bytes(fingerprint[part * 4:(part + 1) * 4], "big")


def _register_session_list_functions(connection: sqlite3.Connection) -> None:
    """每个 catalog 连接都注册同一派生索引函数，包含只读重开。"""
    connection.create_function("session_list_created_us", 1, _session_list_created_us, deterministic=True)
    connection.create_function("session_list_member_part", 3, _session_list_member_part, deterministic=True)


def _encode_session_list_cursor(offset: int, *, revision: str) -> str:
    payload = json.dumps(
        {"offset": offset, "revision": revision},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_session_list_cursor(cursor: str, *, revision: str) -> int:
    """解析会话列表分页 cursor；revision 变化时显式报错，不静默降级。"""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        binascii.Error,
    ) as error:
        raise ValueError("会话列表 cursor 格式无效") from error
    if not isinstance(payload, dict):
        raise TypeError("会话列表 cursor 格式无效")
    if payload.get("revision") != revision:
        raise ValueError("会话列表已更新，请从第一页重新加载")
    offset = payload.get("offset")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("会话列表 cursor offset 无效")
    return offset
