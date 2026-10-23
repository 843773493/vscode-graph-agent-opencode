"""session_creation_records 一条垂直链路（8.1-A 创建流 journal）。

承载创建 record 的 create-or-get（同 key 同 preimage 幂等、不同
preimage 冲突）、冻结父 revision、CAS 发布（唯一可见性提交点：同事务插入
可见 node）与 preparing→aborted 终结。

错误分类沿用宿主约定：``TypeError`` 输入类型错误、``ValueError`` 输入形态
非法、``KeyError`` 目标行不存在、``RuntimeError`` 语义冲突。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.session_control_store.contracts import ThreadGraphBindingRecord

from app.core.identifier import (
    create_prefixed_id,
    to_epoch_ms,
    uuid7_datetime_from_hex,
    validate_session_id,
    validate_thread_id,
)
from app.core.session_catalog_store._schema import _CREATION_RECORD_COLUMNS
from app.core.session_catalog_store.contracts import (
    CatalogMaintenanceRequiredError,
    SessionCatalogNode,
    SessionCreationPurpose,
    SessionCreationRecord,
)
from app.core.session_catalog_store.creation_envelope import (
    validate_session_creation_envelope,
)
from app.core.session_catalog_store.creation_record_rows import (
    fetch_session_creation_record_row,
    session_creation_record_from_row,
)
from app.core.session_catalog_store.validators import (
    validate_operation_idempotency_key,
    validate_session_storage_identity,
)


class CreationJournalMixin:
    """creation record journal 方法族（唯一实现点）。"""

    def verify_creation_record_envelopes(self, *, connection: sqlite3.Connection) -> None:
        """启动核验全部冻结 intent；不将历史 envelope 与当前 node/配置重绑定。"""
        for row in connection.execute(f"SELECT {_CREATION_RECORD_COLUMNS} FROM session_creation_records"):
            try:
                validate_session_creation_envelope(session_creation_record_from_row(row))
            except (TypeError, ValueError, RuntimeError) as error:
                raise CatalogMaintenanceRequiredError(
                    "session creation record envelope 损坏，须进入维护模式核对并保留原数据: "
                    f"operation_id={row['session_creation_idempotency_key']}"
                ) from error

    def create_or_get_creation_record(
        self,
        *,
        idempotency_key: str,
        workspace_id: str,
        parent_node_id: str | None,
        display_name: str,
        title_source: str = "user",
        purpose: SessionCreationPurpose,
        graph_binding: ThreadGraphBindingRecord,
        session_metadata_json: str,
        preimage_hash: str,
        request_intent_json: str | None = None,
        fork_delivery_request_id: str | None = None,
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
        from app.core.session_control_store.contracts import ThreadGraphBindingRecord

        if not isinstance(graph_binding, ThreadGraphBindingRecord):
            raise TypeError("graph_binding 必须是 ThreadGraphBindingRecord")
        validate_operation_idempotency_key(idempotency_key)
        self._validate_common_fields(workspace_id, display_name)
        if title_source not in ("default", "user", "auto"):
            raise ValueError(f"title_source 非法: {title_source!r}")
        if purpose not in ("normal", "context_fork"):
            raise ValueError(f"session creation purpose 非法: {purpose!r}")
        if fork_delivery_request_id is not None:
            if not isinstance(fork_delivery_request_id, str):
                raise TypeError("fork delivery request_id 必须是字符串")
            if purpose != "context_fork" or not fork_delivery_request_id.strip():
                raise ValueError("只有 context fork 可以冻结非空 delivery request_id")
        if not isinstance(session_metadata_json, str) or not session_metadata_json:
            raise ValueError("session_metadata_json 不能为空")
        try:
            session_metadata = json.loads(session_metadata_json)
        except ValueError as error:
            raise ValueError("session_metadata_json 必须是 JSON 对象") from error
        if not isinstance(session_metadata, dict):
            raise ValueError(  # noqa: TRY004 - JSON 根类型属于序列化形态错误。
                "session_metadata_json 必须是 JSON 对象"
            )
        canonical_metadata = json.dumps(
            session_metadata,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if canonical_metadata != session_metadata_json:
            raise ValueError("session_metadata_json 必须使用 canonical JSON")
        if request_intent_json is not None:
            request_intent = json.loads(request_intent_json)
            if not isinstance(request_intent, dict):
                raise ValueError("request_intent_json 必须是 JSON 对象")
            if json.dumps(request_intent, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")) != request_intent_json:
                raise ValueError("request_intent_json 必须使用 canonical JSON")
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
            existing = fetch_session_creation_record_row(connection, idempotency_key)
            if existing is not None:
                if tuple(existing[field] for field in (
                    "graph_id", "graph_revision", "graph_schema_hash", "capability_profile_hash",
                )) != graph_binding.as_tuple():
                    raise RuntimeError(
                        "session creation record GraphBinding preimage 冲突（同 key 不同 selector，拒绝复用）: "
                        f"key={idempotency_key!r}"
                    )
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
                if str(existing["title_source"]) != title_source:
                    raise RuntimeError(
                        "session creation record title_source 冲突（同 key 幂等"
                        "复用时拒绝改绑）: "
                        f"key={idempotency_key!r}, "
                        f"existing_title_source={existing['title_source']!r}, "
                        f"requested_title_source={title_source!r}"
                    )
                if str(existing["purpose"]) != purpose:
                    raise RuntimeError(
                        "session creation record purpose 冲突（同 key 幂等"
                        "复用时拒绝改绑）: "
                        f"key={idempotency_key!r}, "
                        f"existing_purpose={existing['purpose']!r}, "
                        f"requested_purpose={purpose!r}"
                    )
                if str(existing["session_metadata_json"]) != session_metadata_json:
                    raise RuntimeError(
                        "session creation record metadata 冲突（同 key 幂等"
                        "复用时拒绝改绑）: "
                        f"key={idempotency_key!r}"
                    )
                if existing["request_intent_json"] != request_intent_json:
                    raise RuntimeError(
                        "session creation record 请求意图冲突（同 key 不同意图，拒绝复用）: "
                        f"key={idempotency_key!r}"
                    )
                return session_creation_record_from_row(existing)
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
            validate_session_storage_identity(allocated_session_id, allocated_created_at, locator)
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
                "workspace_id, parent_node_id, display_name, title_source, purpose, "
                "graph_id, graph_revision, graph_schema_hash, capability_profile_hash, "
                "session_metadata_json, request_intent_json, created_at, storage_relative_locator, "
                "preimage_hash, parent_revision, state, abort_reason, "
                "record_created_at, record_updated_at, fork_delivery_request_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'preparing', "
                "NULL, ?, ?, ?)",
                (
                    idempotency_key,
                    allocated_session_id,
                    allocated_main_thread_id,
                    workspace_id,
                    parent_node_id,
                    display_name,
                    title_source,
                    purpose,
                    *graph_binding.as_tuple(),
                    session_metadata_json,
                    request_intent_json,
                    allocated_created_at.isoformat(),
                    locator,
                    preimage_hash,
                    parent_revision,
                    record_created_at,
                    record_created_at,
                    fork_delivery_request_id,
                ),
            )
            row = fetch_session_creation_record_row(connection, idempotency_key)
            if row is None:
                # 防御性兜底：同事务内刚插入必然可见。
                raise RuntimeError(
                    "creation record 插入后不可见（事务异常）: "
                    f"key={idempotency_key!r}"
                )
            return session_creation_record_from_row(row)

    def get_creation_record(self, idempotency_key: str) -> SessionCreationRecord:
        """按幂等键返回 creation record 投影；不存在抛 KeyError。"""
        validate_operation_idempotency_key(idempotency_key)
        with self.read_transaction() as connection:
            row = fetch_session_creation_record_row(connection, idempotency_key)
            if row is None:
                raise KeyError(
                    "session creation record 不存在: "
                    f"key={idempotency_key!r}"
                )
            return session_creation_record_from_row(row)

    def list_preparing_creation_records(
        self,
        *,
        workspace_id: str,
        purpose: SessionCreationPurpose,
    ) -> list[SessionCreationRecord]:
        """按冻结 workspace 与 purpose 列出未发布创建记录，供启动恢复使用。"""
        self._validate_common_fields(workspace_id, "session")
        if purpose not in ("normal", "context_fork"):
            raise ValueError(f"session creation purpose 非法: {purpose!r}")
        with self.read_transaction() as connection:
            rows = connection.execute(
                f"SELECT {_CREATION_RECORD_COLUMNS} FROM session_creation_records "
                "WHERE workspace_id = ? AND purpose = ? AND state = 'preparing' "
                "ORDER BY record_created_at, session_creation_idempotency_key",
                (workspace_id, purpose),
            ).fetchall()
            return [session_creation_record_from_row(row) for row in rows]

    def list_published_fork_creation_records(self) -> list[SessionCreationRecord]:
        """只由权威 catalog 枚举公开 active fork；删除中的 target 归删除 owner。"""
        with self.read_transaction() as connection:
            rows = connection.execute(
                "SELECT c.* FROM session_creation_records c JOIN nodes n ON n.node_id = c.session_id "
                "JOIN fork_source_captures f ON f.creation_key = c.session_creation_idempotency_key "
                "WHERE c.purpose = 'context_fork' AND c.state = 'published' AND n.state = 'active' "
                "AND f.state = 'materialized' ORDER BY c.session_creation_idempotency_key"
            ).fetchall()
        return [session_creation_record_from_row(row) for row in rows]

    def publish_creation_record(
        self,
        idempotency_key: str,
        *,
        expected_purpose: SessionCreationPurpose,
    ) -> SessionCatalogNode:
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
        validate_operation_idempotency_key(idempotency_key)
        if expected_purpose not in ("normal", "context_fork"):
            raise ValueError(
                f"session creation purpose 非法: {expected_purpose!r}"
            )
        with self.write_transaction() as connection:
            row = fetch_session_creation_record_row(connection, idempotency_key)
            if row is None:
                raise KeyError(
                    "session creation record 不存在: "
                    f"key={idempotency_key!r}"
                )
            state = str(row["state"])
            if str(row["purpose"]) != expected_purpose:
                raise RuntimeError(
                    "session creation publish purpose 与 durable record 不一致: "
                    f"key={idempotency_key!r}, expected={expected_purpose!r}, "
                    f"actual={row['purpose']!r}"
                )
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
            title_source = str(row["title_source"])
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
            if title_source not in ("default", "user", "auto"):
                raise RuntimeError(
                    "creation record title_source 非法（record 被外部改动，"
                    f"fail closed）: {title_source!r}"
                )
            validate_thread_id(main_thread_id)
            validate_session_storage_identity(session_id, created_at, locator)
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
            # 冻结 intent 只在首次发布时初始化 nodes；后续读取与业务更新不回读 journal。
            metadata = json.loads(row["session_metadata_json"])
            origin = metadata["generation_origin"]
            node = self.create_session_node(
                session_id, workspace_id, parent_node_id, display_name,
                created_at, locator, main_thread_id, title_source,
                current_agent_id=metadata["current_agent_id"],
                current_provider_id=metadata["current_provider_id"],
                session_kind=metadata["kind"],
                context_source_session_id=metadata["context_source_session_id"],
                generation_origin_json=(
                    json.dumps(origin, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    if origin is not None else None
                ),
                connection=connection,
            )
            connection.execute(
                "UPDATE session_creation_records "
                "SET state = 'published', record_updated_at = ? "
                "WHERE session_creation_idempotency_key = ?",
                (datetime.now(UTC).isoformat(), idempotency_key),
            )
            return node

    def abort_creation_record(
        self,
        idempotency_key: str,
        reason: str,
    ) -> SessionCreationRecord:
        """终结 creation record：preparing → aborted（记 reason）。

        ``published`` 不可撤销（RuntimeError）；已 aborted 幂等返回既有
        record（不覆盖原 abort_reason）。
        """
        validate_operation_idempotency_key(idempotency_key)
        if not isinstance(reason, str):
            raise TypeError(f"abort reason 必须是字符串: {reason!r}")
        if not reason:
            raise ValueError("abort reason 不能为空")
        with self.write_transaction() as connection:
            row = fetch_session_creation_record_row(connection, idempotency_key)
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
                return session_creation_record_from_row(row)
            connection.execute(
                "UPDATE session_creation_records "
                "SET state = 'aborted', abort_reason = ?, record_updated_at = ? "
                "WHERE session_creation_idempotency_key = ?",
                (reason, datetime.now(UTC).isoformat(), idempotency_key),
            )
            updated = fetch_session_creation_record_row(connection, idempotency_key)
            if updated is None:
                # 防御性兜底：同事务内更新后必然可见。
                raise RuntimeError(
                    "creation record abort 后不可见（事务异常）: "
                    f"key={idempotency_key!r}"
                )
            return session_creation_record_from_row(updated)
