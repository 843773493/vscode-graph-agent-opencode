"""session_creation_records 一条垂直链路（8.1-A 创建流 journal）。

承载创建 record 的行投影、create-or-get（同 key 同 preimage 幂等、不同
preimage 冲突）、冻结父 revision、CAS 发布（唯一可见性提交点：同事务插入
可见 node）与 preparing→aborted 终结。

错误分类沿用宿主约定：``TypeError`` 输入类型错误、``ValueError`` 输入形态
非法、``KeyError`` 目标行不存在、``RuntimeError`` 语义冲突。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from app.core.identifier import (
    create_prefixed_id,
    to_epoch_ms,
    uuid7_datetime_from_hex,
)
from app.core.session_catalog_store._schema import _CREATION_RECORD_COLUMNS
from app.core.session_catalog_store.contracts import (
    SessionCatalogNode,
    SessionCreationRecord,
)
from app.core.session_catalog_store.validators import (
    validate_session_id,
    validate_storage_relative_locator,
    validate_thread_id,
)


class CreationJournalMixin:
    """creation record journal 方法族（唯一实现点）。"""

    @staticmethod
    def _fetch_creation_record(
        connection: sqlite3.Connection,
        idempotency_key: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            f"SELECT {_CREATION_RECORD_COLUMNS} FROM session_creation_records "
            "WHERE session_creation_idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()

    @staticmethod
    def _creation_record_from_row(row: sqlite3.Row) -> SessionCreationRecord:
        return SessionCreationRecord(
            session_creation_idempotency_key=str(
                row["session_creation_idempotency_key"]
            ),
            session_id=str(row["session_id"]),
            main_thread_id=str(row["main_thread_id"]),
            workspace_id=str(row["workspace_id"]),
            parent_node_id=(
                str(row["parent_node_id"])
                if row["parent_node_id"] is not None
                else None
            ),
            display_name=str(row["display_name"]),
            created_at=str(row["created_at"]),
            storage_relative_locator=str(row["storage_relative_locator"]),
            preimage_hash=str(row["preimage_hash"]),
            parent_revision=(
                int(row["parent_revision"])
                if row["parent_revision"] is not None
                else None
            ),
            state=str(row["state"]),
            abort_reason=(
                str(row["abort_reason"]) if row["abort_reason"] is not None else None
            ),
            record_created_at=str(row["record_created_at"]),
            record_updated_at=str(row["record_updated_at"]),
        )

    @staticmethod
    def _validate_idempotency_key(idempotency_key: str) -> None:
        if not isinstance(idempotency_key, str):
            raise TypeError(
                f"idempotency_key 必须是字符串: {idempotency_key!r}"
            )
        if not idempotency_key:
            raise ValueError("idempotency_key 不能为空")

    def create_or_get_creation_record(
        self,
        *,
        idempotency_key: str,
        workspace_id: str,
        parent_node_id: str | None,
        display_name: str,
        preimage_hash: str,
        created_at: datetime | None = None,
        session_id: str | None = None,
        main_thread_id: str | None = None,
    ) -> SessionCreationRecord:
        """create-or-get 创建流 journal record（gate 内短事务，8.1-A）。

        - 同 key 已存在：``preimage_hash`` 一致 → 返回既有 record（幂等，
          created_at 等冻结值以既有 record 为准）；不一致 → ``RuntimeError``
          （同 key 不同 preimage 冲突）。
        - 实时创建省略 ``created_at`` 与固定 ID：session/main-thread ID 均走
          uuid-utils 默认 UUIDv7 分配，``created_at`` 与 session locator 日期
          只从 session ID 内嵌毫秒推导。
        - 固定历史 fixture 必须同时传入 ``session_id``、``main_thread_id`` 与
          ``created_at``，两个 ID 的内嵌毫秒都须与给定时刻相同；journal 不按
          显式时刻生成 ID。
        - 幂等命中直接复用冻结 record，不重新分配 ID 或时间。新 record 插入
          ``state='preparing'``，不发布可见 node。
        - parent 校验：存在、非 deleting、同 workspace（复用
          :meth:`_require_mutable_parent`）；同时预检 session_id /
          main_thread_id / locator 未被既有 nodes 行占用（fail fast，
          UNIQUE 约束兜底）。
        """
        self._validate_idempotency_key(idempotency_key)
        self._validate_common_fields(workspace_id, display_name)
        if created_at is not None and not isinstance(created_at, datetime):
            raise TypeError(f"created_at 必须是 datetime: {created_at!r}")
        if created_at is not None and created_at.tzinfo is None:
            raise ValueError(f"created_at 必须带时区: {created_at!r}")
        if not isinstance(preimage_hash, str) or not preimage_hash:
            raise ValueError(f"preimage_hash 不能为空: {preimage_hash!r}")
        fixture_values = (created_at, session_id, main_thread_id)
        if any(value is not None for value in fixture_values) and any(
            value is None for value in fixture_values
        ):
            raise ValueError(
                "固定 Session fixture 必须同时提供 created_at、session_id 与 "
                "main_thread_id"
            )
        if session_id is not None:
            validate_session_id(session_id)
        if main_thread_id is not None:
            validate_thread_id(main_thread_id)
        if created_at is not None:
            if session_id is None or main_thread_id is None:
                raise ValueError(
                    "固定 Session fixture 必须同时提供 created_at、session_id 与 "
                    "main_thread_id"
                )
            expected_ms = to_epoch_ms(created_at)
            session_ms = to_epoch_ms(uuid7_datetime_from_hex(session_id[4:]))
            main_thread_ms = to_epoch_ms(
                uuid7_datetime_from_hex(main_thread_id[4:])
            )
            if session_ms != expected_ms or main_thread_ms != expected_ms:
                raise ValueError(
                    "固定 Session fixture 的 created_at 必须与 session_id 和 "
                    "main_thread_id 内嵌时间戳一致: "
                    f"created_at_ms={expected_ms}, session_ms={session_ms}, "
                    f"main_thread_ms={main_thread_ms}"
                )
        with self.write_transaction() as connection:
            existing = self._fetch_creation_record(connection, idempotency_key)
            if existing is not None:
                if str(existing["preimage_hash"]) != preimage_hash:
                    raise RuntimeError(
                        "session creation record preimage 冲突（同 key 不同 "
                        "preimage，拒绝复用）: "
                        f"key={idempotency_key!r}, "
                        f"existing_preimage={existing['preimage_hash']!r}, "
                        f"requested_preimage={preimage_hash!r}"
                    )
                if (
                    session_id is not None
                    and str(existing["session_id"]) != session_id
                ):
                    raise RuntimeError(
                        "session creation record session_id 冲突（同 key 幂等"
                        "复用时传入 ID 与既有 record 不一致，拒绝改绑）: "
                        f"key={idempotency_key!r}, "
                        f"existing_session_id={existing['session_id']!r}, "
                        f"requested_session_id={session_id!r}"
                    )
                if (
                    main_thread_id is not None
                    and str(existing["main_thread_id"]) != main_thread_id
                ):
                    raise RuntimeError(
                        "session creation record main_thread_id 冲突（同 key 幂等"
                        "复用时传入 ID 与既有 record 不一致，拒绝改绑）: "
                        f"key={idempotency_key!r}, "
                        f"existing_main_thread_id={existing['main_thread_id']!r}, "
                        f"requested_main_thread_id={main_thread_id!r}"
                    )
                return self._creation_record_from_row(existing)
            # 插入路径：事务内先验证后写入。
            self._require_mutable_parent(connection, parent_node_id, workspace_id)
            parent_revision: int | None = None
            if parent_node_id is not None:
                parent_row = self._fetch_node(connection, parent_node_id)
                if parent_row is None:
                    # 防御性兜底：_require_mutable_parent 刚验证过存在。
                    raise KeyError(f"会话目录节点不存在: {parent_node_id}")
                parent_revision = int(parent_row["revision"])
            if session_id is None:
                allocated_session_id = create_prefixed_id("ses")
                allocated_main_thread_id = create_prefixed_id("thr")
                allocated_created_at = uuid7_datetime_from_hex(
                    allocated_session_id[4:]
                )
            else:
                if created_at is None or main_thread_id is None:
                    raise RuntimeError("Session journal 固定 fixture 字段缺失")
                allocated_session_id = session_id
                allocated_main_thread_id = main_thread_id
                allocated_created_at = created_at
            validate_session_id(allocated_session_id)
            validate_thread_id(allocated_main_thread_id)
            utc_date = allocated_created_at.astimezone(UTC).date()
            locator = f"sessions/{utc_date:%Y/%m/%d}/{allocated_session_id}"
            validate_storage_relative_locator(locator)
            self._validate_locator_budget(locator)
            # fail fast：新分配身份不得与既有可见 node 冲突（UNIQUE 兜底）。
            self._require_node_id_available(connection, allocated_session_id)
            self._require_unique_session_fields(
                connection, workspace_id, locator, allocated_main_thread_id
            )
            record_created_at = datetime.now(UTC).isoformat()
            connection.execute(
                "INSERT INTO session_creation_records ("
                "session_creation_idempotency_key, session_id, main_thread_id, "
                "workspace_id, parent_node_id, display_name, created_at, "
                "storage_relative_locator, preimage_hash, parent_revision, "
                "state, abort_reason, record_created_at, record_updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'preparing', NULL, ?, ?)",
                (
                    idempotency_key,
                    allocated_session_id,
                    allocated_main_thread_id,
                    workspace_id,
                    parent_node_id,
                    display_name,
                    allocated_created_at.isoformat(),
                    locator,
                    preimage_hash,
                    parent_revision,
                    record_created_at,
                    record_created_at,
                ),
            )
            row = self._fetch_creation_record(connection, idempotency_key)
            if row is None:
                # 防御性兜底：同事务内刚插入必然可见。
                raise RuntimeError(
                    "creation record 插入后不可见（事务异常）: "
                    f"key={idempotency_key!r}"
                )
            return self._creation_record_from_row(row)

    def get_creation_record(self, idempotency_key: str) -> SessionCreationRecord:
        """按幂等键返回 creation record 投影；不存在抛 KeyError。"""
        self._validate_idempotency_key(idempotency_key)
        with self.read_transaction() as connection:
            row = self._fetch_creation_record(connection, idempotency_key)
            if row is None:
                raise KeyError(
                    "session creation record 不存在: "
                    f"key={idempotency_key!r}"
                )
            return self._creation_record_from_row(row)

    def publish_creation_record(self, idempotency_key: str) -> SessionCatalogNode:
        """CAS 发布 creation record 并插入可见 node（**唯一可见性提交点**）。

        单事务内依次验证：record 存在且 ``state='preparing'``；record 内部
        一致性（ID/locator 形态、locator 叶名与日期）；父节点仍存在、
        active 且 revision 等于冻结 ``parent_revision``（漂移/缺失/删除 →
        RuntimeError，含期望/实际）；目标 session_id / main_thread_id /
        locator 未被其它 nodes 行占用（显式预检 + UNIQUE 兜底）。随后同一
        事务内插入 nodes 行（等价 create_session_node 语义，身份字段全部
        来自 record 冻结值）并把 record 推进为 ``published``。任何失败回滚
        整个事务：node 不发布、record 保持 preparing。
        """
        self._validate_idempotency_key(idempotency_key)
        with self.write_transaction() as connection:
            row = self._fetch_creation_record(connection, idempotency_key)
            if row is None:
                raise KeyError(
                    "session creation record 不存在: "
                    f"key={idempotency_key!r}"
                )
            state = str(row["state"])
            if state == "published":
                raise RuntimeError(
                    "session creation record 已发布，拒绝重复发布: "
                    f"key={idempotency_key!r}"
                )
            if state == "aborted":
                raise RuntimeError(
                    "session creation record 已中止，拒绝发布: "
                    f"key={idempotency_key!r}, "
                    f"abort_reason={row['abort_reason']!r}"
                )
            session_id = str(row["session_id"])
            main_thread_id = str(row["main_thread_id"])
            workspace_id = str(row["workspace_id"])
            parent_node_id = (
                str(row["parent_node_id"])
                if row["parent_node_id"] is not None
                else None
            )
            display_name = str(row["display_name"])
            created_at_text = str(row["created_at"])
            locator = str(row["storage_relative_locator"])
            try:
                created_at = datetime.fromisoformat(created_at_text)
            except ValueError as error:
                raise RuntimeError(
                    "creation record created_at 无法解析（record 被外部改动，"
                    f"fail closed）: {created_at_text!r}: {error}"
                ) from error
            # record 内部一致性复验（防绕过软件直改 record）。
            validate_session_id(session_id)
            validate_thread_id(main_thread_id)
            validate_storage_relative_locator(locator)
            self._validate_locator_matches_session(session_id, created_at, locator)
            self._validate_locator_budget(locator)
            # 父节点 CAS：仍存在、active、revision 未漂移。
            if parent_node_id is not None:
                parent = self._fetch_node(connection, parent_node_id)
                if parent is None:
                    raise RuntimeError(
                        "session creation publish 失败：父节点已不存在"
                        f"（operation 须 abort 后换新 key 重试）: "
                        f"key={idempotency_key!r}, parent={parent_node_id}"
                    )
                if str(parent["state"]) != "active":
                    raise RuntimeError(
                        "session creation publish 失败：父节点非 active: "
                        f"key={idempotency_key!r}, parent={parent_node_id}, "
                        f"parent_state={parent['state']!r}"
                    )
                frozen_revision = row["parent_revision"]
                if frozen_revision is None:
                    raise RuntimeError(
                        "creation record 冻结 parent_revision 缺失（record 被"
                        f"外部改动，fail closed）: key={idempotency_key!r}"
                    )
                actual_revision = int(parent["revision"])
                if actual_revision != int(frozen_revision):
                    raise RuntimeError(
                        "session creation publish 失败：父节点 revision 已漂移: "
                        f"key={idempotency_key!r}, parent={parent_node_id}, "
                        f"expected_revision={int(frozen_revision)}, "
                        f"actual_revision={actual_revision}"
                    )
            # 目标占用预检（UNIQUE 兜底）。
            self._require_node_id_available(connection, session_id)
            self._require_unique_session_fields(
                connection, workspace_id, locator, main_thread_id
            )
            connection.execute(
                "INSERT INTO nodes (node_id, kind, parent_node_id, display_name, "
                "state, revision, workspace_id, created_at, "
                "storage_relative_locator, main_thread_id) "
                "VALUES (?, 'session', ?, ?, 'active', 1, ?, ?, ?, ?)",
                (
                    session_id,
                    parent_node_id,
                    display_name,
                    workspace_id,
                    created_at.isoformat(),
                    locator,
                    main_thread_id,
                ),
            )
            connection.execute(
                "UPDATE session_creation_records "
                "SET state = 'published', record_updated_at = ? "
                "WHERE session_creation_idempotency_key = ?",
                (datetime.now(UTC).isoformat(), idempotency_key),
            )
            return self._node_from_row(self._require_node(connection, session_id))

    def abort_creation_record(
        self,
        idempotency_key: str,
        reason: str,
    ) -> SessionCreationRecord:
        """终结 creation record：preparing → aborted（记 reason）。

        ``published`` 不可撤销（RuntimeError）；已 aborted 幂等返回既有
        record（不覆盖原 abort_reason）。
        """
        self._validate_idempotency_key(idempotency_key)
        if not isinstance(reason, str):
            raise TypeError(f"abort reason 必须是字符串: {reason!r}")
        if not reason:
            raise ValueError("abort reason 不能为空")
        with self.write_transaction() as connection:
            row = self._fetch_creation_record(connection, idempotency_key)
            if row is None:
                raise KeyError(
                    "session creation record 不存在: "
                    f"key={idempotency_key!r}"
                )
            state = str(row["state"])
            if state == "published":
                raise RuntimeError(
                    "session creation record 已发布，不可撤销: "
                    f"key={idempotency_key!r}"
                )
            if state == "aborted":
                return self._creation_record_from_row(row)
            connection.execute(
                "UPDATE session_creation_records "
                "SET state = 'aborted', abort_reason = ?, record_updated_at = ? "
                "WHERE session_creation_idempotency_key = ?",
                (reason, datetime.now(UTC).isoformat(), idempotency_key),
            )
            updated = self._fetch_creation_record(connection, idempotency_key)
            if updated is None:
                # 防御性兜底：同事务内更新后必然可见。
                raise RuntimeError(
                    "creation record abort 后不可见（事务异常）: "
                    f"key={idempotency_key!r}"
                )
            return self._creation_record_from_row(updated)
