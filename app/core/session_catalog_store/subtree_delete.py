"""subtree_delete_records 一条垂直链路（8.1-B 子树删除 journal）。

承载删除 record 的行投影、递归 CTE 冻结 (node_id, revision) 与不可变
locator、create-or-get、整树 active→deleting 的唯一逻辑可见性关闭点 CAS、
物理隔离进度追加、drain 完整性校验 + 深度序 tombstone 终结、aborted 终结、
未终结 record 恢复入口，以及空 folder 的非递归简单删除。

错误分类沿用宿主约定：``TypeError`` 输入类型错误、``ValueError`` 输入形态
非法、``KeyError`` 目标行不存在、``RuntimeError`` 语义冲突。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from app.core.session_catalog_store._schema import _SUBTREE_DELETE_RECORD_COLUMNS
from app.core.session_catalog_store.contracts import (
    CatalogTransactionHook,
    SubtreeDeleteMarkRejectedError,
    SubtreeDeleteRecord,
    SubtreeFrozenNode,
    _parse_drained_session_ids,
    _parse_frozen_node_ids,
    _parse_frozen_session_locators,
    _validate_workspace_id,
)
from app.core.session_catalog_store.validators import (
    validate_session_id,
    validate_storage_relative_locator,
)


class SubtreeDeleteMixin:
    """subtree delete journal 与空 folder 删除方法族（唯一实现点）。"""

    @staticmethod
    def _fetch_subtree_delete_record(
        connection: sqlite3.Connection,
        idempotency_key: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            f"SELECT {_SUBTREE_DELETE_RECORD_COLUMNS} FROM subtree_delete_records "
            "WHERE subtree_delete_idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()

    @staticmethod
    def _subtree_delete_record_from_row(
        row: sqlite3.Row,
    ) -> SubtreeDeleteRecord:
        return SubtreeDeleteRecord(
            subtree_delete_idempotency_key=str(
                row["subtree_delete_idempotency_key"]
            ),
            workspace_id=str(row["workspace_id"]),
            root_node_id=str(row["root_node_id"]),
            frozen_node_ids=_parse_frozen_node_ids(str(row["frozen_node_ids"])),
            frozen_session_locators=_parse_frozen_session_locators(
                str(row["frozen_session_locators"])
            ),
            state=str(row["state"]),
            abort_reason=(
                str(row["abort_reason"]) if row["abort_reason"] is not None else None
            ),
            record_created_at=str(row["record_created_at"]),
            record_updated_at=str(row["record_updated_at"]),
            drained_session_ids=_parse_drained_session_ids(
                str(row["drained_session_ids"])
            ),
        )

    @staticmethod
    def _subtree_rows(
        connection: sqlite3.Connection,
        root_node_id: str,
    ) -> list[sqlite3.Row]:
        """递归 CTE 返回子树全部节点行（含 root 自身，按 node_id 排序）。"""
        return connection.execute(
            """
            WITH RECURSIVE subtree(node_id) AS (
                SELECT node_id FROM nodes WHERE node_id = ?
                UNION
                SELECT n.node_id FROM nodes n
                JOIN subtree s ON n.parent_node_id = s.node_id
            )
            SELECT node_id, kind, parent_node_id, state, revision,
                   workspace_id, storage_relative_locator
            FROM nodes
            WHERE node_id IN (SELECT node_id FROM subtree)
            ORDER BY node_id
            """,
            (root_node_id,),
        ).fetchall()

    def create_or_get_subtree_delete_record(
        self,
        *,
        idempotency_key: str,
        workspace_id: str,
        root_node_id: str,
    ) -> SubtreeDeleteRecord:
        """create-or-get 子树删除流 journal record（gate 内短事务，8.1-B）。

        - 同 key 已存在：preimage（``workspace_id`` + ``root_node_id``）
          一致 → 幂等返回既有 record——冻结集合以既有 record 为准，**不
          重查当前树**（对齐 design.md「按 record 定点继续，不重新查询
          当前树」；completed/aborted/deleting 状态下节点行可能已删除或
          已 deleting，重验会破坏恢复重入）；不一致 → ``RuntimeError``
          （同 key 不同 preimage 冲突）。
        - 不存在 → root 必须存在、active 且属于本 workspace；递归 CTE
          冻结子树全部 node（含 root）的 (node_id, revision) 与每个
          session 的不可变 locator；**子树内任一节点非 active →
          ``RuntimeError``**（子树已有 deleting 节点，拒绝新删除）；插入
          ``state='preparing'``。本方法**不做任何节点状态变更**——整树
          deleting 由 :meth:`mark_subtree_deleting` 的单事务 CAS 完成。
        """
        self._validate_idempotency_key(idempotency_key)
        _validate_workspace_id(workspace_id)
        validate_session_id(root_node_id)
        with self.write_transaction() as connection:
            existing = self._fetch_subtree_delete_record(
                connection, idempotency_key
            )
            if existing is not None:
                record = self._subtree_delete_record_from_row(existing)
                if (
                    record.workspace_id != workspace_id
                    or record.root_node_id != root_node_id
                ):
                    raise RuntimeError(
                        "subtree delete record preimage 冲突（同 key 不同 "
                        "workspace/root，拒绝复用）: "
                        f"key={idempotency_key!r}, "
                        f"existing_workspace={record.workspace_id!r}, "
                        f"existing_root={record.root_node_id!r}, "
                        f"requested_workspace={workspace_id!r}, "
                        f"requested_root={root_node_id!r}"
                    )
                return record
            # 插入路径：事务内先验证后写入。
            root = self._require_node(connection, root_node_id)
            if str(root["workspace_id"]) != workspace_id:
                raise RuntimeError(
                    "root 节点属于其他 workspace，拒绝跨 workspace 删除: "
                    f"root={root_node_id}, root_workspace={root['workspace_id']!r}, "
                    f"requested_workspace={workspace_id!r}"
                )
            if str(root["state"]) != "active":
                raise RuntimeError(
                    "root 节点非 active，拒绝新删除: "
                    f"root={root_node_id}, state={root['state']!r}"
                )
            rows = self._subtree_rows(connection, root_node_id)
            frozen_nodes: list[SubtreeFrozenNode] = []
            locators: dict[str, str] = {}
            for subtree_row in rows:
                if str(subtree_row["workspace_id"]) != workspace_id:
                    raise RuntimeError(
                        "子树内存在跨 workspace 节点（目录不一致，fail closed）: "
                        f"node_id={subtree_row['node_id']}, "
                        f"node_workspace={subtree_row['workspace_id']!r}, "
                        f"requested_workspace={workspace_id!r}"
                    )
                if str(subtree_row["state"]) != "active":
                    raise RuntimeError(
                        "子树内存在非 active 节点，拒绝新删除: "
                        f"node_id={subtree_row['node_id']}, "
                        f"state={subtree_row['state']!r}"
                    )
                frozen_nodes.append(
                    SubtreeFrozenNode(
                        node_id=str(subtree_row["node_id"]),
                        revision=int(subtree_row["revision"]),
                    )
                )
                if str(subtree_row["kind"]) == "session":
                    locator = subtree_row["storage_relative_locator"]
                    if locator is None:
                        # DDL CHECK 已保证 session 行必有 locator；防御性 fail closed。
                        raise RuntimeError(
                            "session 节点缺少 storage_relative_locator"
                            f"（目录被外部改动，fail closed）: "
                            f"node_id={subtree_row['node_id']}"
                        )
                    validate_storage_relative_locator(str(locator))
                    locators[str(subtree_row["node_id"])] = str(locator)
            frozen_nodes.sort(key=lambda item: item.node_id)
            record_created_at = datetime.now(UTC).isoformat()
            connection.execute(
                "INSERT INTO subtree_delete_records ("
                "subtree_delete_idempotency_key, workspace_id, root_node_id, "
                "frozen_node_ids, frozen_session_locators, state, abort_reason, "
                "record_created_at, record_updated_at, drained_session_ids) "
                "VALUES (?, ?, ?, ?, ?, 'preparing', NULL, ?, ?, '[]')",
                (
                    idempotency_key,
                    workspace_id,
                    root_node_id,
                    json.dumps(
                        [
                            {
                                "node_id": item.node_id,
                                "revision": item.revision,
                            }
                            for item in frozen_nodes
                        ],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    json.dumps(
                        [
                            {
                                "session_id": session_id,
                                "storage_relative_locator": locator,
                            }
                            for session_id, locator in sorted(locators.items())
                        ],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    record_created_at,
                    record_created_at,
                ),
            )
            inserted = self._fetch_subtree_delete_record(
                connection, idempotency_key
            )
            if inserted is None:
                # 防御性兜底：同事务内刚插入必然可见。
                raise RuntimeError(
                    "subtree delete record 插入后不可见（事务异常）: "
                    f"key={idempotency_key!r}"
                )
            return self._subtree_delete_record_from_row(inserted)

    def mark_subtree_deleting(
        self,
        idempotency_key: str,
        *,
        transaction_hook: CatalogTransactionHook | None = None,
    ) -> None:
        """单事务 CAS 把整棵冻结子树 active→deleting（**唯一逻辑可见性关闭点**）。

        record 必须 ``preparing``（已 ``deleting`` → 幂等 no-op；其余状态
        → ``RuntimeError``）。事务内先 CAS 验证：冻结集合内全部节点仍
        存在、active 且 revision 等于冻结值（漂移 → ``RuntimeError`` 含
        期望/实际）；随后同一事务内全部节点置 ``deleting`` 且
        revision+1、record 推进为 ``deleting``。任何失败整体回滚：子树
        保持 active、record 保持 preparing。
        """
        self._validate_idempotency_key(idempotency_key)
        with self.write_transaction() as connection:
            row = self._fetch_subtree_delete_record(connection, idempotency_key)
            if row is None:
                raise KeyError(
                    f"subtree delete record 不存在: key={idempotency_key!r}"
                )
            state = str(row["state"])
            if state in ("deleting", "draining"):
                # 幂等重入：整树已 deleting，不重验子树（以首次 mark 提交为准）；
                # transaction_hook 可与既有逻辑提交事实原子对账。
                if transaction_hook is not None:
                    transaction_hook(
                        connection, self._subtree_delete_record_from_row(row)
                    )
                return
            if state != "preparing":
                raise RuntimeError(
                    "subtree delete record 状态不允许 mark: "
                    f"key={idempotency_key!r}, state={state!r}"
                )
            record = self._subtree_delete_record_from_row(row)
            # 8.1-D：整树 catalog deleting 提交前的 pinned retention 预检。
            # 冻结集合内任一 session 存在未释放（preparing/active）claim 即
            # fail closed，且本事务不写任何节点状态——整棵子树保持 active。
            # claim 准入（create_or_get_fork_retention_claim 要求 source
            # active）与本预检在同一 DB 的 BEGIN IMMEDIATE 事务序列上竞争，
            # 因此无需任何本地 fence 窗口。
            for session_id in sorted(record.frozen_session_locators):
                claims = self.list_pinned_claims_for_source(
                    session_id, connection=connection
                )
                if claims:
                    self._raise_for_source_claims(claims)
            for item in record.frozen_node_ids:
                node = self._fetch_node(connection, item.node_id)
                if node is None:
                    raise SubtreeDeleteMarkRejectedError(
                        "mark CAS 失败：冻结节点已不存在: "
                        f"key={idempotency_key!r}, node_id={item.node_id}, "
                        f"expected_revision={item.revision}, actual=缺失"
                    )
                if str(node["state"]) != "active":
                    raise SubtreeDeleteMarkRejectedError(
                        "mark CAS 失败：冻结节点非 active: "
                        f"key={idempotency_key!r}, node_id={item.node_id}, "
                        f"expected_state='active', actual_state={node['state']!r}"
                    )
                actual_revision = int(node["revision"])
                if actual_revision != item.revision:
                    raise SubtreeDeleteMarkRejectedError(
                        "mark CAS 失败：冻结节点 revision 已漂移: "
                        f"key={idempotency_key!r}, node_id={item.node_id}, "
                        f"expected_revision={item.revision}, "
                        f"actual_revision={actual_revision}"
                    )
            for item in record.frozen_node_ids:
                connection.execute(
                    "UPDATE nodes SET state = 'deleting', revision = revision + 1 "
                    "WHERE node_id = ?",
                    (item.node_id,),
                )
            connection.execute(
                "UPDATE subtree_delete_records SET state = 'deleting', "
                "record_updated_at = ? WHERE subtree_delete_idempotency_key = ?",
                (datetime.now(UTC).isoformat(), idempotency_key),
            )
            if transaction_hook is not None:
                updated = self._fetch_subtree_delete_record(
                    connection, idempotency_key
                )
                if updated is None:
                    raise RuntimeError(
                        "mark 后 subtree delete record 不可见: "
                        f"key={idempotency_key!r}"
                    )
                transaction_hook(
                    connection, self._subtree_delete_record_from_row(updated)
                )

    def record_drain_progress(self, idempotency_key: str, session_id: str) -> None:
        """记录单个 session 的物理隔离进度；首次调用把 record → draining。

        record 必须 ``deleting``/``draining``；session 必须在冻结集合内；
        已记录过 → 幂等 no-op。进度存 ``drained_session_ids``（JSON 数组
        追加），保证 drain 中途崩溃重入按 record 定点继续、不重查当前树。
        """
        self._validate_idempotency_key(idempotency_key)
        validate_session_id(session_id)
        with self.write_transaction() as connection:
            row = self._fetch_subtree_delete_record(connection, idempotency_key)
            if row is None:
                raise KeyError(
                    f"subtree delete record 不存在: key={idempotency_key!r}"
                )
            state = str(row["state"])
            if state not in ("deleting", "draining"):
                raise RuntimeError(
                    "subtree delete record 状态不允许记录 drain 进度: "
                    f"key={idempotency_key!r}, state={state!r}"
                )
            record = self._subtree_delete_record_from_row(row)
            if session_id not in record.frozen_session_locators:
                raise RuntimeError(
                    "drain 进度的 session 不在冻结集合内（拒绝记录）: "
                    f"key={idempotency_key!r}, session_id={session_id!r}"
                )
            if session_id in record.drained_session_ids:
                return
            connection.execute(
                "UPDATE subtree_delete_records SET state = 'draining', "
                "drained_session_ids = ?, record_updated_at = ? "
                "WHERE subtree_delete_idempotency_key = ?",
                (
                    json.dumps(
                        [*record.drained_session_ids, session_id],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    datetime.now(UTC).isoformat(),
                    idempotency_key,
                ),
            )

    def finish_subtree_delete(
        self,
        idempotency_key: str,
        *,
        transaction_hook: CatalogTransactionHook | None = None,
    ) -> None:
        """单事务终结删除：drain 完整性校验 + 全树 tombstone（行删除）。

        record 必须 ``draining``；无 session 的空子树允许 ``deleting``
        直接 finish（drained 集合已等于冻结 session 集合=∅，无 drain 需
        要）。``drained_session_ids`` 与冻结 session 集合不一致 →
        ``RuntimeError``（未完成）。校验通过后同一事务内按深度降序删除
        nodes 表全部冻结行（tombstone=行删除；深度序满足 ``parent_node_id``
        自引用外键）并把 record 推进为 ``completed``。
        """
        self._validate_idempotency_key(idempotency_key)
        with self.write_transaction() as connection:
            row = self._fetch_subtree_delete_record(connection, idempotency_key)
            if row is None:
                raise KeyError(
                    f"subtree delete record 不存在: key={idempotency_key!r}"
                )
            state = str(row["state"])
            if state == "completed" and transaction_hook is not None:
                transaction_hook(
                    connection, self._subtree_delete_record_from_row(row)
                )
                return
            if state not in ("deleting", "draining"):
                raise RuntimeError(
                    "subtree delete record 状态不允许 finish: "
                    f"key={idempotency_key!r}, state={state!r}"
                )
            record = self._subtree_delete_record_from_row(row)
            frozen_sessions = set(record.frozen_session_locators)
            drained = set(record.drained_session_ids)
            if drained != frozen_sessions:
                raise RuntimeError(
                    "subtree delete drain 未完成，拒绝 tombstone: "
                    f"key={idempotency_key!r}, "
                    f"missing={sorted(frozen_sessions - drained)}, "
                    f"unexpected={sorted(drained - frozen_sessions)}"
                )
            frozen_ids = [item.node_id for item in record.frozen_node_ids]
            frozen_set = set(frozen_ids)
            placeholders = ",".join("?" for _ in frozen_ids)
            parent_rows = connection.execute(
                f"SELECT node_id, parent_node_id FROM nodes "
                f"WHERE node_id IN ({placeholders})",
                tuple(frozen_ids),
            ).fetchall()
            parent_of: dict[str, str | None] = {
                str(item["node_id"]): (
                    str(item["parent_node_id"])
                    if item["parent_node_id"] is not None
                    else None
                )
                for item in parent_rows
            }
            depth_by_id: dict[str, int] = {}
            for node_id in frozen_ids:
                chain: list[str] = []
                current: str | None = node_id
                while (
                    current is not None
                    and current in frozen_set
                    and current not in depth_by_id
                ):
                    chain.append(current)
                    current = parent_of.get(current)
                if current is not None and current in depth_by_id:
                    base = depth_by_id[current]
                else:
                    base = -1
                for member in reversed(chain):
                    base += 1
                    depth_by_id[member] = base
            for node_id in sorted(frozen_ids, key=lambda n: (-depth_by_id[n], n)):
                cursor = connection.execute(
                    "DELETE FROM nodes WHERE node_id = ?", (node_id,)
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(
                        "finish 失败：冻结节点行已缺失（外部改动，fail closed）: "
                        f"key={idempotency_key!r}, node_id={node_id}"
                    )
            connection.execute(
                "UPDATE subtree_delete_records SET state = 'completed', "
                "record_updated_at = ? WHERE subtree_delete_idempotency_key = ?",
                (datetime.now(UTC).isoformat(), idempotency_key),
            )
            if transaction_hook is not None:
                updated = self._fetch_subtree_delete_record(
                    connection, idempotency_key
                )
                if updated is None:
                    raise RuntimeError(
                        "finish 后 subtree delete record 不可见: "
                        f"key={idempotency_key!r}"
                    )
                transaction_hook(
                    connection, self._subtree_delete_record_from_row(updated)
                )

    def abort_subtree_delete(
        self,
        idempotency_key: str,
        reason: str,
    ) -> SubtreeDeleteRecord:
        """终结删除 record：preparing/deleting/draining → aborted（记 reason）。

        ``completed`` 不可撤销（RuntimeError）；已 aborted 幂等返回既有
        record（不覆盖原 abort_reason）。**注意**：``deleting``/
        ``draining`` 状态的 abort 不回滚节点状态——design.md 明确「不回滚
        active」，节点保持 deleting 待人工/新操作处置；本方法只终结
        record，不动 nodes 表。
        """
        self._validate_idempotency_key(idempotency_key)
        if not isinstance(reason, str):
            raise TypeError(f"abort reason 必须是字符串: {reason!r}")
        if not reason:
            raise ValueError("abort reason 不能为空")
        with self.write_transaction() as connection:
            row = self._fetch_subtree_delete_record(connection, idempotency_key)
            if row is None:
                raise KeyError(
                    f"subtree delete record 不存在: key={idempotency_key!r}"
                )
            state = str(row["state"])
            if state == "completed":
                raise RuntimeError(
                    "subtree delete record 已完成，不可撤销: "
                    f"key={idempotency_key!r}"
                )
            if state == "aborted":
                return self._subtree_delete_record_from_row(row)
            connection.execute(
                "UPDATE subtree_delete_records SET state = 'aborted', "
                "abort_reason = ?, record_updated_at = ? "
                "WHERE subtree_delete_idempotency_key = ?",
                (reason, datetime.now(UTC).isoformat(), idempotency_key),
            )
            updated = self._fetch_subtree_delete_record(
                connection, idempotency_key
            )
            if updated is None:
                # 防御性兜底：同事务内更新后必然可见。
                raise RuntimeError(
                    "subtree delete record abort 后不可见（事务异常）: "
                    f"key={idempotency_key!r}"
                )
            return self._subtree_delete_record_from_row(updated)

    def get_subtree_delete_record(self, idempotency_key: str) -> SubtreeDeleteRecord:
        """按幂等键返回 subtree delete record 投影；不存在抛 KeyError。"""
        self._validate_idempotency_key(idempotency_key)
        with self.read_transaction() as connection:
            row = self._fetch_subtree_delete_record(connection, idempotency_key)
            if row is None:
                raise KeyError(
                    f"subtree delete record 不存在: key={idempotency_key!r}"
                )
            return self._subtree_delete_record_from_row(row)

    def list_pending_subtree_delete_records(
        self,
        workspace_id: str,
    ) -> list[SubtreeDeleteRecord]:
        """列出本 workspace 未终结的子树删除 record（唯一恢复入口）。

        返回 ``preparing``/``deleting``/``draining`` 三种中间态 record，按
        ``record_created_at`` + key 稳定排序；``completed``（已 tombstone）与
        ``aborted``（已显式终结）不返回。这是崩溃恢复的**权威依据**：按
        SQLite record 定点继续或 fail-closed 报告，不扫描磁盘、不吸收外部
        改动，也不假回滚 active。
        """
        _validate_workspace_id(workspace_id)
        with self.read_transaction() as connection:
            rows = connection.execute(
                f"SELECT {_SUBTREE_DELETE_RECORD_COLUMNS} "
                "FROM subtree_delete_records "
                "WHERE workspace_id = ? "
                "AND state IN ('preparing', 'deleting', 'draining') "
                "ORDER BY record_created_at, subtree_delete_idempotency_key",
                (workspace_id,),
            ).fetchall()
            return [self._subtree_delete_record_from_row(row) for row in rows]

    def delete_empty_folder(
        self,
        folder_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        """空 folder 的非递归简单删除（design.md §9 约 776 行允许面）。

        folder 必须存在、active 且无任何直接子节点；非 folder 节点、
        非 active 状态、有子节点（含 deleting 子节点）一律
        ``RuntimeError``——「非空 folder 的非递归删除仍明确拒绝，不能因
        folder 无物理目录就悄悄丢弃子节点」；session 节点的删除走子树
        删除协议，不经本方法。
        """
        validate_session_id(folder_id)
        if connection is None:
            with self.write_transaction() as transaction:
                self.delete_empty_folder(folder_id, connection=transaction)
            return
        row = self._require_node(connection, folder_id)
        if str(row["kind"]) != "folder":
            raise RuntimeError(
                "非 folder 节点拒绝非递归删除（session 走子树删除协议）: "
                f"node_id={folder_id}, kind={row['kind']!r}"
            )
        if str(row["state"]) != "active":
            raise RuntimeError(
                f"folder 非 active，拒绝删除: node_id={folder_id}, "
                f"state={row['state']!r}"
            )
        child_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM nodes WHERE parent_node_id = ?",
                (folder_id,),
            ).fetchone()[0]
        )
        if child_count > 0:
            raise RuntimeError(
                "非空 folder 的非递归删除被明确拒绝: "
                f"folder_id={folder_id}, child_count={child_count}"
            )
        connection.execute("DELETE FROM nodes WHERE node_id = ?", (folder_id,))
