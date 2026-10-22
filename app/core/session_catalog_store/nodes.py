"""nodes 表读写一条垂直链路（行投影、校验 helper 与写操作）。

承载 ``nodes`` 行的取用/投影、父子与 locator/身份唯一性校验、递归 CTE
后代查询，以及 create_folder/create_session_node/rename_node/move_node/
apply_navigation_mutation/set_node_state 等节点写操作。

错误分类沿用宿主约定：``TypeError`` 输入类型错误、``ValueError`` 输入形态
非法、``KeyError`` 目标行不存在、``RuntimeError`` 语义冲突。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import nullcontext
from datetime import datetime

from app.core.session_catalog_store._schema import (
    _LOCATOR_PREFIX,
    _NODE_COLUMNS,
)
from app.core.session_catalog_store.contracts import (
    _UNSET,
    SessionCatalogNode,
    SessionGenerationOriginDTO,
)
from app.core.session_catalog_store.validators import (
    validate_path_budget,
    validate_session_id,
    validate_session_storage_identity,
    validate_thread_id,
)


class CatalogNodesMixin:
    """nodes 表读写方法族（唯一实现点）。"""

    @staticmethod
    def _fetch_node(
        connection: sqlite3.Connection,
        node_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            f"SELECT {_NODE_COLUMNS} FROM nodes WHERE node_id = ?",
            (node_id,),
        ).fetchone()

    def _require_node(
        self,
        connection: sqlite3.Connection,
        node_id: str,
    ) -> sqlite3.Row:
        row = self._fetch_node(connection, node_id)
        if row is None:
            raise KeyError(f"会话目录节点不存在: {node_id}")
        return row

    @staticmethod
    def _node_from_row(row: sqlite3.Row) -> SessionCatalogNode:
        return SessionCatalogNode(
            node_id=str(row["node_id"]),
            kind=str(row["kind"]),
            parent_node_id=(
                str(row["parent_node_id"])
                if row["parent_node_id"] is not None
                else None
            ),
            display_name=str(row["display_name"]),
            title_source=(
                str(row["title_source"])
                if row["title_source"] is not None
                else None
            ),
            state=str(row["state"]),
            revision=int(row["revision"]),
            workspace_id=str(row["workspace_id"]),
            created_at=(
                str(row["created_at"]) if row["created_at"] is not None else None
            ),
            storage_relative_locator=(
                str(row["storage_relative_locator"])
                if row["storage_relative_locator"] is not None
                else None
            ),
            main_thread_id=(
                str(row["main_thread_id"])
                if row["main_thread_id"] is not None
                else None
            ),
            session_kind=row["session_kind"],
            current_agent_id=row["current_agent_id"],
            current_provider_id=row["current_provider_id"],
            context_source_session_id=row["context_source_session_id"],
            generation_origin_json=row["generation_origin_json"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _validate_common_fields(workspace_id: str, display_name: str) -> None:
        if not isinstance(workspace_id, str):
            raise TypeError(f"workspace_id 必须是字符串: {workspace_id!r}")
        if not workspace_id:
            raise ValueError(f"workspace_id 不能为空: {workspace_id!r}")
        if not isinstance(display_name, str):
            raise TypeError(f"显示名必须是字符串: {display_name!r}")
        if not display_name:
            raise ValueError(f"显示名不能为空: {display_name!r}")

    def _require_node_id_available(
        self,
        connection: sqlite3.Connection,
        node_id: str,
    ) -> None:
        if self._fetch_node(connection, node_id) is not None:
            raise RuntimeError(f"节点 ID 已存在: {node_id}")

    def _require_mutable_parent(
        self,
        connection: sqlite3.Connection,
        parent_node_id: str | None,
        workspace_id: str,
    ) -> None:
        """验证父节点存在、非 deleting 且与子节点同 workspace。"""
        if parent_node_id is None:
            return
        parent = self._require_node(connection, parent_node_id)
        if parent["state"] == "deleting":
            raise RuntimeError(f"父节点正在删除，拒绝挂载: {parent_node_id}")
        if parent["workspace_id"] != workspace_id:
            raise RuntimeError(
                "父节点属于其他 workspace，拒绝跨 workspace 挂载: "
                f"parent={parent_node_id}, "
                f"parent_workspace={parent['workspace_id']}, "
                f"child_workspace={workspace_id}"
            )

    def _validate_locator_budget(self, storage_relative_locator: str) -> None:
        """locator 去 ``sessions/`` 前缀后对 sessions_root 做路径预算校验。"""
        relative = storage_relative_locator[len(_LOCATOR_PREFIX):]
        validate_path_budget(self.sessions_root, relative)

    def _require_unique_session_fields(
        self,
        connection: sqlite3.Connection,
        workspace_id: str,
        storage_relative_locator: str,
        main_thread_id: str,
    ) -> None:
        duplicate_thread = connection.execute(
            "SELECT node_id FROM nodes "
            "WHERE workspace_id = ? AND main_thread_id = ?",
            (workspace_id, main_thread_id),
        ).fetchone()
        if duplicate_thread is not None:
            raise RuntimeError(
                "main_thread_id 已被同 workspace 的 session 占用: "
                f"workspace_id={workspace_id}, main_thread_id={main_thread_id}, "
                f"existing_node={duplicate_thread[0]}"
            )
        duplicate_locator = connection.execute(
            "SELECT node_id FROM nodes "
            "WHERE workspace_id = ? AND storage_relative_locator = ?",
            (workspace_id, storage_relative_locator),
        ).fetchone()
        if duplicate_locator is not None:
            raise RuntimeError(
                "storage_relative_locator 已被同 workspace 的 session 占用: "
                f"workspace_id={workspace_id}, "
                f"locator={storage_relative_locator}, "
                f"existing_node={duplicate_locator[0]}"
            )

    @staticmethod
    def _descendant_node_ids(
        connection: sqlite3.Connection,
        node_id: str,
    ) -> set[str]:
        """递归 CTE 求全部后代节点 ID（含 folder 与 session，不含自身）。"""
        rows = connection.execute(
            """
            WITH RECURSIVE descendants(node_id) AS (
                SELECT node_id FROM nodes WHERE parent_node_id = ?
                UNION
                SELECT n.node_id FROM nodes n
                JOIN descendants d ON n.parent_node_id = d.node_id
            )
            SELECT node_id FROM descendants
            """,
            (node_id,),
        ).fetchall()
        return {str(row[0]) for row in rows}

    # ------------------------------------------------------------------
    # 写操作（每个一事务，事务内先验证后写入）
    # ------------------------------------------------------------------

    def create_folder(
        self,
        node_id: str,
        workspace_id: str,
        parent_node_id: str | None,
        display_name: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> SessionCatalogNode:
        """创建 folder 节点；folder 无物理 locator/manifest。

        folder 节点 ID 使用与 session 相同的 canonical ``ses_`` 前缀形态，
        但 folder 本身不分配物理 locator。

        ``connection`` 非 None 时在调用方已开的写事务连接上执行同一段校验
        + INSERT，不 BEGIN/COMMIT/ROLLBACK（供调用方把 nodes 变更与同库旁挂
        journal/事件放进同一事务）；为 None 时自行开写事务。
        """
        validate_session_id(node_id)
        self._validate_common_fields(workspace_id, display_name)
        transaction = (
            self.write_transaction()
            if connection is None
            else nullcontext(connection)
        )
        with transaction as active:
            self._require_node_id_available(active, node_id)
            self._require_mutable_parent(active, parent_node_id, workspace_id)
            active.execute(
                "INSERT INTO nodes (node_id, kind, parent_node_id, display_name, "
                "state, revision, workspace_id) "
                "VALUES (?, 'folder', ?, ?, 'active', 1, ?)",
                (node_id, parent_node_id, display_name, workspace_id),
            )
            return self._node_from_row(self._require_node(active, node_id))

    def create_session_node(
        self,
        node_id: str,
        workspace_id: str,
        parent_node_id: str | None,
        display_name: str,
        created_at: datetime,
        storage_relative_locator: str,
        main_thread_id: str,
        title_source: str = "user",
        *,
        current_agent_id: str,
        current_provider_id: str | None = None,
        session_kind: str = "normal",
        context_source_session_id: str | None = None,
        generation_origin_json: str | None = None,
        updated_at: datetime | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> SessionCatalogNode:
        """创建 session 节点；locator 日期必须等于 created_at 的 UTC 日期。"""
        validate_thread_id(main_thread_id)
        validate_session_storage_identity(node_id, created_at, storage_relative_locator)
        self._validate_common_fields(workspace_id, display_name)
        self._validate_title_source(title_source)
        self._validate_locator_budget(storage_relative_locator)
        updated_at = updated_at or created_at
        self._validate_session_fields(
            session_kind, current_agent_id, current_provider_id,
            context_source_session_id, generation_origin_json, updated_at,
        )
        transaction = self.write_transaction() if connection is None else nullcontext(connection)
        with transaction as active:
            self._require_node_id_available(active, node_id)
            self._require_unique_session_fields(
                active,
                workspace_id,
                storage_relative_locator,
                main_thread_id,
            )
            self._require_mutable_parent(active, parent_node_id, workspace_id)
            active.execute(
                "INSERT INTO nodes (node_id, kind, parent_node_id, display_name, "
                "title_source, state, revision, workspace_id, created_at, "
                "storage_relative_locator, main_thread_id, session_kind, current_agent_id, "
                "current_provider_id, context_source_session_id, generation_origin_json, updated_at) "
                "VALUES (?, 'session', ?, ?, ?, 'active', 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    node_id,
                    parent_node_id,
                    display_name,
                    title_source,
                    workspace_id,
                    created_at.isoformat(),
                    storage_relative_locator,
                    main_thread_id,
                    session_kind,
                    current_agent_id,
                    current_provider_id,
                    context_source_session_id,
                    generation_origin_json,
                    updated_at.isoformat(),
                ),
            )
            return self._node_from_row(self._require_node(active, node_id))

    @staticmethod
    def _validate_session_fields(
        session_kind: str,
        current_agent_id: str,
        current_provider_id: str | None,
        context_source_session_id: str | None,
        generation_origin_json: str | None,
        updated_at: datetime,
    ) -> None:
        if not isinstance(current_agent_id, str) or not current_agent_id:
            raise ValueError(f"current_agent_id 不能为空: {current_agent_id!r}")
        if current_provider_id is not None and (
            not isinstance(current_provider_id, str) or not current_provider_id
        ):
            raise ValueError(f"current_provider_id 非法: {current_provider_id!r}")
        if session_kind not in ("normal", "context_fork") or (
            session_kind == "normal" and context_source_session_id is not None
        ) or (
            session_kind == "context_fork" and not context_source_session_id
        ):
            raise ValueError(
                "session kind/source 不一致: "
                f"kind={session_kind!r}, source={context_source_session_id!r}"
            )
        if context_source_session_id is not None:
            validate_session_id(context_source_session_id)
        if generation_origin_json is not None:
            origin = SessionGenerationOriginDTO.model_validate_json(generation_origin_json)
            canonical = json.dumps(origin.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if canonical != generation_origin_json:
                raise ValueError("generation_origin_json 必须是 typed canonical JSON")
        if not isinstance(updated_at, datetime) or updated_at.tzinfo is None:
            raise ValueError(f"updated_at 必须是带时区 datetime: {updated_at!r}")

    def update_session_configuration(
        self,
        node_id: str,
        *,
        current_agent_id: str,
        current_provider_id: str | None,
        updated_at: datetime,
    ) -> SessionCatalogNode:
        """持有 Session 写准入时更新业务配置，允许删除 drain 等待中的已准入写完成。"""
        with self.write_transaction() as connection:
            node = self._require_node(connection, node_id)
            if node["kind"] != "session":
                raise RuntimeError(f"会话配置更新要求 session 节点: {node_id}")
            self._validate_session_fields(
                node["session_kind"], current_agent_id, current_provider_id,
                node["context_source_session_id"], node["generation_origin_json"], updated_at,
            )
            connection.execute(
                "UPDATE nodes SET current_agent_id = ?, current_provider_id = ?, "
                "updated_at = ? WHERE node_id = ?",
                (current_agent_id, current_provider_id, updated_at.isoformat(), node_id),
            )
            return self._node_from_row(self._require_node(connection, node_id))

    def rename_node(self, node_id: str, display_name: str) -> SessionCatalogNode:
        """重命名节点；显示名只存 catalog，不参与物理路径。"""
        if not isinstance(display_name, str):
            raise TypeError(f"显示名必须是字符串: {display_name!r}")
        if not display_name:
            raise ValueError(f"显示名不能为空: {display_name!r}")
        with self.write_transaction() as connection:
            node = self._require_node(connection, node_id)
            if str(node["kind"]) == "session":
                connection.execute(
                    "UPDATE nodes SET display_name = ?, title_source = 'user', "
                    "revision = revision + 1 WHERE node_id = ?",
                    (display_name, node_id),
                )
            else:
                connection.execute(
                    "UPDATE nodes SET display_name = ?, revision = revision + 1 "
                    "WHERE node_id = ?",
                    (display_name, node_id),
                )
            return self._node_from_row(self._require_node(connection, node_id))

    @staticmethod
    def _validate_title_source(title_source: str) -> None:
        if title_source not in ("default", "user", "auto"):
            raise ValueError(f"title_source 非法: {title_source!r}")

    def apply_auto_title_mutation(
        self,
        node_id: str,
        *,
        expected_revision: int,
        expected_title: str,
        new_title: str,
        connection: sqlite3.Connection,
    ) -> SessionCatalogNode | None:
        """仅在默认来源、原标题和节点 revision 同时匹配时自动改名。"""
        if not isinstance(expected_revision, int) or isinstance(
            expected_revision, bool
        ):
            raise TypeError(f"expected_revision 必须是整数: {expected_revision!r}")
        if not isinstance(expected_title, str) or not expected_title:
            raise ValueError(f"expected_title 不能为空: {expected_title!r}")
        if not isinstance(new_title, str) or not new_title:
            raise ValueError(f"new_title 不能为空: {new_title!r}")
        node = self._require_node(connection, node_id)
        if (
            str(node["kind"]) != "session"
            or str(node["state"]) != "active"
            or str(node["title_source"]) != "default"
            or str(node["display_name"]) != expected_title
            or int(node["revision"]) != expected_revision
        ):
            return None
        self._require_display_name_available(connection, node_id, new_title)
        updated = connection.execute(
            "UPDATE nodes SET display_name = ?, title_source = 'auto', "
            "revision = revision + 1 WHERE node_id = ? AND kind = 'session' "
            "AND state = 'active' AND title_source = 'default' "
            "AND display_name = ? AND revision = ?",
            (new_title, node_id, expected_title, expected_revision),
        )
        if updated.rowcount != 1:
            return None
        return self._node_from_row(self._require_node(connection, node_id))

    def move_node(
        self,
        node_id: str,
        new_parent_node_id: str | None,
    ) -> SessionCatalogNode:
        """调整父节点；只改导航关系，不搬移物理目录。"""
        with self.write_transaction() as connection:
            self._validate_move_target(connection, node_id, new_parent_node_id)
            connection.execute(
                "UPDATE nodes SET parent_node_id = ?, revision = revision + 1 "
                "WHERE node_id = ?",
                (new_parent_node_id, node_id),
            )
            return self._node_from_row(self._require_node(connection, node_id))

    def _validate_move_target(
        self,
        connection: sqlite3.Connection,
        node_id: str,
        new_parent_node_id: str | None,
    ) -> sqlite3.Row:
        """校验移动目标：自环、父存在/active/同 workspace、无祖先环。

        返回被移动节点行，供调用方复用。move_node 与 apply_navigation_mutation
        共用本实现，不是第二套校验。
        """
        node = self._require_node(connection, node_id)
        # 被移动节点自身必须 active：deleting 子树内的 node 不得被移出并把
        # 已逻辑删除的节点重新写进正常拓扑（否则随后 finish 仍按冻结集合
        # tombstone，形成「搬进去又自己消失」的伪成功）。
        if node["state"] == "deleting":
            raise RuntimeError(f"被移动节点正在删除: {node_id}")
        if new_parent_node_id == node_id:
            raise RuntimeError(f"移动目标不能是节点自身: {node_id}")
        if new_parent_node_id is not None:
            parent = self._require_node(connection, new_parent_node_id)
            if parent["state"] == "deleting":
                raise RuntimeError(f"目标父节点正在删除: {new_parent_node_id}")
            if parent["workspace_id"] != node["workspace_id"]:
                raise RuntimeError(
                    "目标父节点属于其他 workspace: "
                    f"node={node_id}, node_workspace={node['workspace_id']}, "
                    f"parent={new_parent_node_id}, "
                    f"parent_workspace={parent['workspace_id']}"
                )
            if new_parent_node_id in self._descendant_node_ids(connection, node_id):
                raise RuntimeError(
                    "移动会形成循环: "
                    f"node={node_id}, new_parent={new_parent_node_id}"
                )
        return node

    def apply_navigation_mutation(
        self,
        node_id: str,
        *,
        expected_revision: int,
        new_parent_node_id: object = _UNSET,
        new_display_name: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> SessionCatalogNode:
        """带 expected-revision CAS 的组合导航写（8.1-G 复用面，单事务）。

        单事务内复用现有校验（父存在/active/同 workspace/无祖先环、同名兄弟
        唯一）后校验 ``revision == expected_revision``（漂移 → ``RuntimeError``）
        再 ``UPDATE nodes ... revision = revision + 1``。``new_parent_node_id``
        用哨兵 ``_UNSET`` 区分「不改父」与「显式移到根(None)」；
        ``new_display_name=None`` 表示不改名。本方法是 create_folder/move_node/
        rename_node 已有校验的**外部组合入口**，不是第二套实现。

        ``connection`` 非 None 时在调用方写事务连接上执行，不自行 BEGIN/COMMIT，
        供调用方把 node CAS、terminal record 与事件 outbox 放进同一事务。
        """
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
            raise TypeError(f"expected_revision 必须是整数: {expected_revision!r}")
        if new_display_name is not None:
            if not isinstance(new_display_name, str):
                raise TypeError(
                    f"new_display_name 必须是字符串或 None: {new_display_name!r}"
                )
            if not new_display_name:
                raise ValueError("new_display_name 不能为空字符串")
        transaction = (
            self.write_transaction()
            if connection is None
            else nullcontext(connection)
        )
        with transaction as active:
            if new_parent_node_id is not _UNSET:
                self._validate_move_target(active, node_id, new_parent_node_id)  # type: ignore[arg-type]
            else:
                self._require_node(active, node_id)
            row = self._require_node(active, node_id)
            actual_revision = int(row["revision"])
            if actual_revision != expected_revision:
                raise RuntimeError(
                    "导航 mutation CAS 失败：节点 revision 已漂移: "
                    f"node_id={node_id}, expected_revision={expected_revision}, "
                    f"actual_revision={actual_revision}"
                )
            updates: list[str] = ["revision = revision + 1"]
            params: list[object] = []
            if new_parent_node_id is not _UNSET:
                updates.append("parent_node_id = ?")
                params.append(new_parent_node_id)
            if new_display_name is not None:
                self._require_display_name_available(
                    active, node_id, new_display_name
                )
                updates.append("display_name = ?")
                params.append(new_display_name)
                if str(row["kind"]) == "session":
                    updates.append("title_source = 'user'")
            params.append(node_id)
            active.execute(
                f"UPDATE nodes SET {', '.join(updates)} WHERE node_id = ?",
                tuple(params),
            )
            return self._node_from_row(self._require_node(active, node_id))

    def _require_display_name_available(
        self,
        connection: sqlite3.Connection,
        node_id: str,
        display_name: str,
    ) -> None:
        """拒绝同一父下与其它节点同名的兄弟（8.1-E 同名兄弟冲突）。"""
        row = self._require_node(connection, node_id)
        duplicate = connection.execute(
            "SELECT node_id FROM nodes WHERE parent_node_id IS ? AND "
            "workspace_id = ? AND display_name = ? AND node_id != ? LIMIT 1",
            (
                row["parent_node_id"],
                row["workspace_id"],
                display_name,
                node_id,
            ),
        ).fetchone()
        if duplicate is not None:
            raise RuntimeError(
                "同名兄弟冲突，拒绝改名: "
                f"node_id={node_id}, display_name={display_name!r}, "
                f"existing_node={duplicate[0]}"
            )

    def set_node_state(self, node_id: str, state: str) -> SessionCatalogNode:
        """设置节点状态；active→deleting 允许，deleting→active 拒绝（不可复活）。"""
        if state not in ("active", "deleting"):
            raise ValueError(f"节点状态非法: {state!r}")
        with self.write_transaction() as connection:
            row = self._require_node(connection, node_id)
            current = str(row["state"])
            if current == state:
                raise RuntimeError(
                    f"节点状态已是 {state}，拒绝无变化写入: {node_id}"
                )
            if current == "deleting":
                raise RuntimeError(f"节点正在删除，不可复活: {node_id}")
            connection.execute(
                "UPDATE nodes SET state = ?, revision = revision + 1 "
                "WHERE node_id = ?",
                (state, node_id),
            )
            return self._node_from_row(self._require_node(connection, node_id))
