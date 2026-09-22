"""Gateway 全局 workspace registry 及其 registry apply journal 的唯一实现。

本模块承载单一垂直链路的唯一实现：

- ``gateway_workspace_registry`` 权威注册行（``registry`` 与
  ``workspace_registry_meta`` 元数据一起构成 Gateway 工作区注册表）；
- ``registry_meta`` 中 ``workspace`` 注册表 revision 的读写与 CAS；
- ``registry_apply_journal`` 批处理恢复账本（start / finish / list /
  recovery）；
- 受控启动把自身 registry 提交纳入配置 apply 基线的
  :meth:`GatewayRegistryMixin.rebase_config_apply_registry_revision`。

``GatewayRegistryMixin`` 由 :class:`app.gateway.control.gateway_state.
GatewayStateStore` 继承装配；宿主负责 ``_GATEWAY_MIGRATIONS`` 中本族三张表
的 DDL 与迁移序号，本模块只承载读写方法族，宿主同时提供 ``_database`` 与
``get_config``。错误分类沿用 gateway_state 约定：``ValueError`` 输入形态
非法、``PermissionError`` 跨 target owner 越权、``ConfigConflictError``
CAS/并发冲突。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from app.core.sqlite_state import utc_now_text
from app.services.infrastructure.config.state import (
    ConfigConflictError,
    dump_json,
    new_config_id,
)

__all__ = [
    "GatewayRegistryMixin",
]


class GatewayRegistryMixin:
    @staticmethod
    def _registry_meta_record_exists(connection: sqlite3.Connection) -> bool:
        """判断是否已存在证明 revision 曾被写入的 ``workspace_registry_meta`` 记录。"""

        return (
            connection.execute(
                "SELECT 1 FROM gateway_config WHERE config_key = 'workspace_registry_meta'"
            ).fetchone()
            is not None
        )

    def _read_registry_revision(self, connection: sqlite3.Connection) -> int:
        """读取调用方同一连接内 ``registry_meta`` 的 workspace revision。

        ``registry_meta`` 行由 :meth:`replace_workspace_registry` 与
        ``workspace_registry_meta`` 在同一事务写入。真正的初始库（以及 ``registry_meta``
        迁移之前的历史库）没有该记录，缺失即合法的 revision 0；但若 ``workspace_
        registry_meta`` 已证明 revision 曾被写入，``registry_meta`` 却读不到，说明有人
        绕过软件直接清空了索引，必须响亮报错，绝不能把外部清空误判成合法的
        revision 0 而让过期 CAS 静默通过。
        """

        row = connection.execute(
            "SELECT revision FROM registry_meta WHERE registry_key = 'workspace'"
        ).fetchone()
        if row is not None:
            return int(row[0])
        if self._registry_meta_record_exists(connection):
            raise RuntimeError(
                "Gateway registry_meta 缺少 workspace revision，但 workspace_registry_meta "
                "已存在；检测到绕过软件直接修改 Gateway 注册状态"
            )
        return 0

    def _assert_registry_revision(
        self, *, current: int, expected: int
    ) -> None:
        """revision CAS 唯一实现：不一致即 fail closed。"""

        if current != expected:
            raise ConfigConflictError(
                "Gateway registry revision CAS 冲突: "
                f"current={current}, expected={expected}"
            )

    def load_workspace_registry(self) -> dict[str, object] | None:
        meta = self.get_config("workspace_registry_meta")
        connection = self._database.connection()
        try:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM gateway_workspace_registry
                ORDER BY position ASC, workspace_id ASC
                """
            ).fetchall()
            if meta is None and not rows:
                return None
            targets: list[object] = []
            for row in rows:
                payload = json.loads(str(row[0]))
                if not isinstance(payload, dict):
                    raise ValueError("Gateway SQLite 工作区注册记录必须是对象")
                targets.append(payload)
            metadata = meta.payload if meta is not None else {}
            return {
                "schema_version": int(metadata.get("schema_version", 10)),
                "registry_revision": int(metadata.get("registry_revision", 0)),
                "active_workspace_id": metadata.get("active_workspace_id"),
                "order_customized": bool(metadata.get("order_customized", False)),
                "runtime_generation": metadata.get("runtime_generation"),
                "remote_gateway_connections": metadata.get(
                    "remote_gateway_connections", []
                ),
                "targets": targets,
            }
        finally:
            connection.close()

    def get_registry_revision(self) -> int:
        """只读返回 Gateway workspace registry 的当前 revision。"""

        connection = self._database.connection()
        try:
            return self._read_registry_revision(connection)
        finally:
            connection.close()

    def rebase_config_apply_registry_revision(
        self,
        *,
        apply_id: str,
        expected_registry_revision: int,
        allowed_owners: tuple[str, ...] = ("system", "config_batch"),
    ) -> int:
        """把受控启动自身产生的 registry 提交纳入配置 apply 基线。

        Gateway pending generation 在启动时会重建默认工作区、恢复托管运行时，
        以及按 pending 配置重建 remote projection。这些操作会正常推进 registry
        revision，但不能因此被最终 promotion 当成外部并发修改。只有当基线之后
        的每一条已提交 registry journal 都属于明确允许的启动 owner，才允许原子
        更新配置 apply journal；人工 CRUD 或缺失 journal 会保留 CAS 冲突。
        """

        if not apply_id:
            raise ValueError("Gateway 配置 apply_id 不能为空")
        if expected_registry_revision < 0:
            raise ValueError("Gateway registry revision 不能为负数")
        if not allowed_owners:
            raise ValueError("Gateway registry 启动 owner 白名单不能为空")
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            journal = connection.execute(
                """
                SELECT registry_revision, state
                FROM config_apply_journal
                WHERE apply_id = ?
                """,
                (apply_id,),
            ).fetchone()
            if journal is None:
                raise ConfigConflictError(
                    f"Gateway 配置 apply journal 不存在: apply_id={apply_id}"
                )
            if str(journal[1]) != "applying":
                raise ConfigConflictError(
                    "Gateway 配置 apply journal 当前不可重设 registry 基线: "
                    f"state={journal[1]}"
                )
            journal_revision = (
                int(journal[0]) if journal[0] is not None else None
            )
            if journal_revision != expected_registry_revision:
                raise ConfigConflictError(
                    "Gateway 配置 apply journal registry 基线已变化: "
                    f"current={journal_revision}, expected={expected_registry_revision}"
                )
            current_revision = self._read_registry_revision(connection)
            if current_revision < expected_registry_revision:
                raise ConfigConflictError(
                    "Gateway registry revision 不能回退: "
                    f"current={current_revision}, expected={expected_registry_revision}"
                )
            if current_revision == expected_registry_revision:
                connection.execute("COMMIT")
                return current_revision

            rows = connection.execute(
                """
                SELECT base_revision, target_revision, owner
                FROM registry_apply_journal
                WHERE state = 'committed'
                  AND target_revision > ?
                  AND target_revision <= ?
                ORDER BY target_revision ASC
                """,
                (expected_registry_revision, current_revision),
            ).fetchall()
            next_revision = expected_registry_revision
            for row in rows:
                base_revision = int(row[0])
                target_revision = row[1]
                owner = str(row[2])
                if owner not in allowed_owners:
                    raise ConfigConflictError(
                        "Gateway pending 启动期间发现非启动 registry 修改: "
                        f"owner={owner}, base={base_revision}, target={target_revision}"
                    )
                if (
                    base_revision != next_revision
                    or target_revision is None
                    or int(target_revision) != next_revision + 1
                ):
                    raise ConfigConflictError(
                        "Gateway pending 启动期间 registry journal 不连续: "
                        f"expected_base={next_revision}, base={base_revision}, "
                        f"target={target_revision}"
                    )
                next_revision = int(target_revision)
            if next_revision != current_revision:
                raise ConfigConflictError(
                    "Gateway pending 启动期间 registry revision 缺少可审计 journal: "
                    f"current={current_revision}, covered={next_revision}"
                )
            updated = connection.execute(
                """
                UPDATE config_apply_journal
                SET registry_revision = ?, updated_at = ?
                WHERE apply_id = ? AND state = 'applying'
                  AND registry_revision = ?
                """,
                (
                    current_revision,
                    utc_now_text(),
                    apply_id,
                    expected_registry_revision,
                ),
            )
            if updated.rowcount != 1:
                raise ConfigConflictError(
                    "Gateway 配置 apply journal registry 基线更新 CAS 失败"
                )
            connection.execute("COMMIT")
            return current_revision
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list_registry_apply_journal(
        self,
        *,
        states: tuple[str, ...] = (),
    ) -> tuple[dict[str, object], ...]:
        """只读返回 registry 批处理的持久恢复记录。"""

        connection = self._database.connection()
        try:
            query = """
                SELECT apply_id, owner, base_revision, target_revision, state,
                       payload_digest, last_error, created_at, updated_at
                FROM registry_apply_journal
            """
            params: tuple[object, ...] = ()
            if states:
                placeholders = ",".join("?" for _ in states)
                query += f" WHERE state IN ({placeholders})"
                params = states
            query += " ORDER BY updated_at ASC, apply_id ASC"
            rows = connection.execute(query, params).fetchall()
        finally:
            connection.close()
        return tuple(
            {
                "apply_id": str(row[0]),
                "owner": str(row[1]),
                "base_revision": int(row[2]),
                "target_revision": int(row[3]) if row[3] is not None else None,
                "state": str(row[4]),
                "payload_digest": str(row[5]),
                "last_error": str(row[6]) if row[6] is not None else None,
                "created_at": str(row[7]),
                "updated_at": str(row[8]),
            }
            for row in rows
        )

    def recover_registry_apply_journal(self) -> tuple[dict[str, object], ...]:
        """将遗留 applying 标记为 recovery_required，禁止静默重放整批。"""

        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE registry_apply_journal
                SET state = 'recovery_required',
                    last_error = 'Gateway 进程在 registry apply journal 提交前退出',
                    updated_at = ?
                WHERE state = 'applying'
                """,
                (utc_now_text(),),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.list_registry_apply_journal(states=("recovery_required",))

    def replace_workspace_registry(
        self,
        payload: dict[str, object],
        *,
        expected_revision: int | None = None,
        owner: str = "registry",
    ) -> int:
        targets = payload.get("targets", [])
        remote_connections = payload.get("remote_gateway_connections", [])
        if not isinstance(targets, list) or not isinstance(remote_connections, list):
            raise ValueError("Gateway SQLite 注册表 payload 结构无效")
        metadata = {
            "schema_version": int(payload.get("schema_version", 10)),
            "registry_revision": int(payload.get("registry_revision", 0)),
            "active_workspace_id": payload.get("active_workspace_id"),
            "order_customized": bool(payload.get("order_customized", False)),
            "runtime_generation": payload.get("runtime_generation"),
            "remote_gateway_connections": remote_connections,
        }
        apply_id = new_config_id("registry_apply")
        payload_digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        self._start_registry_apply_journal(
            apply_id=apply_id,
            owner=owner,
            expected_revision=expected_revision,
            payload_digest=payload_digest,
        )
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current_revision = self._read_registry_revision(connection)
                if expected_revision is not None:
                    self._assert_registry_revision(
                        current=current_revision, expected=expected_revision
                    )
                existing_rows = connection.execute(
                    """
                    SELECT workspace_id, payload_json
                    FROM gateway_workspace_registry
                    ORDER BY position ASC, workspace_id ASC
                    """
                ).fetchall()
                existing_targets: dict[str, dict[str, object]] = {}
                for row in existing_rows:
                    existing_payload = json.loads(str(row[1]))
                    if not isinstance(existing_payload, dict):
                        raise ValueError("Gateway SQLite 已有工作区注册记录必须是对象")
                    existing_targets[str(row[0])] = existing_payload

                scope_owner = {
                    "config": "config",
                    "config_batch": "config",
                    "manual": "manual",
                    "manual_crud": "manual",
                    "system": "system",
                    "remote_projection": "remote_projection",
                }.get(owner)
                scope_target_owners = (
                    {
                        "config",
                        "remote_projection",
                    }
                    if scope_owner == "config"
                    else {scope_owner}
                )
                if scope_owner is not None:
                    current_meta_row = connection.execute(
                        """
                        SELECT payload_json
                        FROM gateway_config
                        WHERE config_key = 'workspace_registry_meta'
                        """
                    ).fetchone()
                    if current_meta_row is not None:
                        current_metadata = json.loads(str(current_meta_row[0]))
                        if isinstance(current_metadata, dict) and isinstance(
                            current_metadata.get("remote_gateway_connections"), list
                        ):
                            metadata["remote_gateway_connections"] = current_metadata[
                                "remote_gateway_connections"
                            ]
                if scope_owner is not None:
                    for target in targets:
                        if not isinstance(target, dict):
                            raise ValueError("Gateway SQLite 工作区注册记录必须是对象")
                        target_workspace_id = target.get("workspace_id")
                        target_owner = target.get("owner", "manual")
                        existing_target = (
                            existing_targets.get(target_workspace_id)
                            if isinstance(target_workspace_id, str)
                            else None
                        )
                        existing_owner = (
                            existing_target.get("owner", "manual")
                            if existing_target is not None
                            else None
                        )
                        system_update_existing_target = (
                            scope_owner == "system"
                            and existing_target is not None
                            and existing_owner == target_owner
                            and all(
                                existing_target.get(field) == target.get(field)
                                for field in (
                                    "owner",
                                    "target_namespace",
                                    "connection_id",
                                    "connection_kind",
                                    "root_path",
                                    "managed",
                                    "removable",
                                    "system_default",
                                    "remote_gateway_connection_id",
                                    "remote_workspace_id",
                                )
                            )
                        )
                        manual_update_system_default = (
                            scope_owner == "manual"
                            and existing_target is not None
                            and existing_owner == target_owner == "system"
                            and bool(existing_target.get("system_default"))
                            and bool(target.get("system_default"))
                            and all(
                                existing_target.get(field) == target.get(field)
                                for field in (
                                    "owner",
                                    "target_namespace",
                                    "connection_id",
                                    "connection_kind",
                                    "root_path",
                                    "managed",
                                    "removable",
                                    "system_default",
                                    "remote_gateway_connection_id",
                                    "remote_workspace_id",
                                )
                            )
                        )
                        if target_owner not in scope_target_owners and not (
                            system_update_existing_target
                            or manual_update_system_default
                            or (
                                existing_target is not None
                                and dump_json(existing_target) == dump_json(target)
                            )
                        ):
                            raise PermissionError(
                                "Gateway registry 批处理不能修改其他 target owner: "
                                f"batch={scope_owner}, workspace_id={target_workspace_id}, "
                                f"target={target_owner}"
                            )

                incoming_targets: dict[str, dict[str, object]] = {}
                for target in targets:
                    if not isinstance(target, dict):
                        raise ValueError("Gateway SQLite 工作区注册记录必须是对象")
                    workspace_id = target.get("workspace_id")
                    if not isinstance(workspace_id, str) or not workspace_id:
                        raise ValueError(
                            "Gateway SQLite 工作区注册记录缺少 workspace_id"
                        )
                    if workspace_id in incoming_targets:
                        raise ValueError(
                            f"Gateway SQLite 工作区注册记录 workspace_id 重复: {workspace_id}"
                        )
                    target_owner = target.get("owner", "manual")
                    if target_owner not in {
                        "config",
                        "manual",
                        "system",
                        "remote_projection",
                    }:
                        raise ValueError(
                            "Gateway SQLite 工作区注册记录 owner 非法: "
                            f"workspace_id={workspace_id}, owner={target_owner}"
                        )
                    previous_target = existing_targets.get(workspace_id)
                    if (
                        previous_target is not None
                        and previous_target.get("owner") is not None
                        and previous_target.get("owner", "manual") != target_owner
                    ):
                        raise PermissionError(
                            "Gateway registry 不允许批处理改变 target owner: "
                            f"workspace_id={workspace_id}, "
                            f"current={previous_target.get('owner')}, "
                            f"requested={target_owner}"
                        )
                    incoming_targets[workspace_id] = target

                if scope_owner is None:
                    final_targets = [
                        target for target in targets if isinstance(target, dict)
                    ]
                else:
                    preserved_targets = [
                        existing_targets[workspace_id]
                        for workspace_id in existing_targets
                        if existing_targets[workspace_id].get("owner", "manual")
                        not in scope_target_owners
                        and workspace_id not in incoming_targets
                    ]
                    final_targets = [
                        *[target for target in targets if isinstance(target, dict)],
                        *preserved_targets,
                    ]

                final_target_ids: set[str] = set()
                target_identity_keys: set[tuple[str, str, str]] = set()
                for position, target in enumerate(final_targets):
                    workspace_id = target.get("workspace_id")
                    if not isinstance(workspace_id, str) or not workspace_id:
                        raise ValueError(
                            "Gateway SQLite 工作区注册记录缺少 workspace_id"
                        )
                    if workspace_id in final_target_ids:
                        raise ValueError(
                            f"Gateway SQLite 最终注册记录 workspace_id 重复: {workspace_id}"
                        )
                    final_target_ids.add(workspace_id)
                    target_owner = target.get("owner", "manual")
                    if target_owner not in {
                        "config",
                        "manual",
                        "system",
                        "remote_projection",
                    }:
                        raise ValueError(
                            "Gateway SQLite 最终注册记录 owner 非法: "
                            f"workspace_id={workspace_id}, owner={target_owner}"
                        )
                    namespace = target.get("target_namespace", "gateway")
                    if not isinstance(namespace, str) or not namespace.strip():
                        raise ValueError(
                            "Gateway SQLite 工作区注册记录 target_namespace 无效: "
                            f"workspace_id={workspace_id}"
                        )
                    connection_id = target.get("connection_id")
                    if connection_id is not None and (
                        not isinstance(connection_id, str) or not connection_id
                    ):
                        raise ValueError(
                            "Gateway SQLite 工作区注册记录 connection_id 无效: "
                            f"workspace_id={workspace_id}"
                        )
                    identity_value = (
                        target.get("remote_workspace_id")
                        if target_owner == "remote_projection"
                        else connection_id or workspace_id
                    )
                    if not isinstance(identity_value, str) or not identity_value:
                        raise ValueError(
                            "Gateway SQLite 工作区注册记录缺少稳定 identity: "
                            f"workspace_id={workspace_id}"
                        )
                    identity_key = (
                        str(target_owner),
                        namespace,
                        identity_value,
                    )
                    if identity_key in target_identity_keys:
                        raise ValueError(
                            "Gateway SQLite 工作区注册记录 owner/namespace/identity 重复: "
                            f"{identity_key}"
                        )
                    target_identity_keys.add(identity_key)
                    connection.execute(
                        """
                        INSERT INTO gateway_workspace_registry(
                            workspace_id, position, active, payload_json, updated_at
                        ) VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(workspace_id) DO UPDATE SET
                            position=excluded.position,
                            active=excluded.active,
                            payload_json=excluded.payload_json,
                            updated_at=excluded.updated_at
                        """,
                        (
                            workspace_id,
                            position,
                            int(workspace_id == metadata["active_workspace_id"]),
                            json.dumps(target, ensure_ascii=False, sort_keys=True),
                            utc_now_text(),
                        ),
                    )
                stale_ids = set(existing_targets) - final_target_ids
                if stale_ids:
                    placeholders = ",".join("?" for _ in stale_ids)
                    connection.execute(
                        "DELETE FROM gateway_workspace_registry WHERE workspace_id IN ("
                        + placeholders
                        + ")",
                        tuple(stale_ids),
                    )
                next_revision = current_revision + 1
                metadata["registry_revision"] = next_revision
                connection.execute(
                    """
                    INSERT INTO gateway_config(config_key, config_version, payload_json, updated_at)
                    VALUES ('workspace_registry_meta', 1, ?, ?)
                    ON CONFLICT(config_key) DO UPDATE SET
                        config_version=excluded.config_version,
                        payload_json=excluded.payload_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                        utc_now_text(),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO registry_meta(registry_key, revision)
                    VALUES ('workspace', ?)
                    ON CONFLICT(registry_key) DO UPDATE SET revision=excluded.revision
                    """,
                    (next_revision,),
                )
                connection.execute(
                    """
                    UPDATE registry_apply_journal
                    SET state = 'committed', target_revision = ?, updated_at = ?
                    WHERE apply_id = ? AND state = 'applying'
                    """,
                    (next_revision, utc_now_text(), apply_id),
                )
                connection.execute("COMMIT")
                return next_revision
            except Exception:
                connection.rollback()
                self._finish_registry_apply_journal(
                    apply_id=apply_id,
                    state="failed",
                    error="registry batch transaction 失败",
                )
                raise
        finally:
            connection.close()

    def _start_registry_apply_journal(
        self,
        *,
        apply_id: str,
        owner: str,
        expected_revision: int | None,
        payload_digest: str,
    ) -> None:
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current_revision = self._read_registry_revision(connection)
            if expected_revision is not None:
                self._assert_registry_revision(
                    current=current_revision, expected=expected_revision
                )
            connection.execute(
                """
                INSERT INTO registry_apply_journal(
                    apply_id, owner, base_revision, target_revision, state,
                    payload_digest, last_error, created_at, updated_at
                ) VALUES (?, ?, ?, NULL, 'applying', ?, NULL, ?, ?)
                """,
                (
                    apply_id,
                    owner,
                    current_revision,
                    payload_digest,
                    utc_now_text(),
                    utc_now_text(),
                ),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _finish_registry_apply_journal(
        self,
        *,
        apply_id: str,
        state: str,
        error: str,
    ) -> None:
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE registry_apply_journal
                SET state = ?, last_error = ?, updated_at = ?
                WHERE apply_id = ? AND state = 'applying'
                """,
                (state, error, utc_now_text(), apply_id),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
