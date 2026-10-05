"""CSM 控制状态的持久化 owner 与 Saver 端口。

CSM 不执行 I/O：它只持有 :class:`ContextSourceControlStatePort`。本模块提供
两个实现层：

- ``ContextSourceControlStorageMixin``：混入唯一 ``RolloutStorage``，负责
  SQLite 表、owner 锁、事务和严格行校验。
- ``ContextSourceControlOwnerMixin``：混入唯一 ``RolloutCheckpointSaver``，
  把 typed 控制状态端口暴露给 CSM，业务层不得旁路 RolloutStorage。

owner 粒度是精确 ``(session_id, thread_id)``：main 与 child thread 都使用
catalog 中的 canonical Thread ID，并落在对应 Thread 节点的 rollout 库；状态
不共享、不互相覆盖。

TODO: OpenSpec 2.4/3.6 要求 rewind/compaction 事务提交 checkpoint-versioned
CSM 控制状态，并按目标 checkpoint 恢复 tracking state。当前实现只提供每个
registration 的单调 ``state_revision`` 作为将来的绑定锚点，没有实现
checkpoint → 控制状态版本的绑定或回放：现有 checkpoint 恢复入口
（``ContextReconciliationMixin.reconcile_context(operation="checkpoint_restore")``
与 fork/remap 路径）不携带 CSM 控制状态版本，CSM 的
``restore_active_revision`` 也没有生产调用方。因此 rewind 后按 checkpoint
回退 tracked/untracked 状态仍是不完整能力，本任务不猜测其事务语义。
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from typing import cast

from app.core.sqlite_state import utc_now_text as _now
from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.enums import CommitKind
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import canonical_json_bytes
from app.domain.itemized.mutation_intents import ApplySourceLifecycleDecision
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.assembly.validation import json_text
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceControlState,
    ContextSourceOwnerKey,
    ContextSourceTrackingStatus,
)
from app.services.infrastructure.rollout_context.storage.schema import (
    CONTEXT_SOURCE_CONTROL_STATE_SCHEMA_STATEMENTS,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_optional_text,
    strict_text,
)

_TABLE_NAME = "context_source_control_states"
_COLUMNS = (
    "session_id",
    "thread_id",
    "source_id",
    "source_kind",
    "name",
    "binding_revision",
    "tracking_status",
    "latest_visible_committed_revision",
    "latest_revision",
    "state_revision",
    "created_at",
    "updated_at",
)
_SELECT_COLUMNS = ", ".join(_COLUMNS)


def _tracking_status(value: object) -> ContextSourceTrackingStatus:
    if value not in {"tracked", "untracked"}:
        raise RuntimeError(f"{_TABLE_NAME}.tracking_status 非法: {value!r}")
    return cast(ContextSourceTrackingStatus, value)


def _state_from_row(row: tuple[object, ...]) -> ContextSourceControlState:
    """把 SQLite 行还原为 typed 控制状态；任何字段异常都显式失败。"""
    (
        session_id,
        thread_id,
        source_id,
        source_kind,
        name,
        binding_revision,
        tracking_status,
        latest_visible_committed_revision,
        latest_revision,
        state_revision,
        _created_at,
        updated_at,
    ) = row
    state = ContextSourceControlState(
        owner=ContextSourceOwnerKey(
            session_id=strict_text(session_id, field=f"{_TABLE_NAME}.session_id"),
            thread_id=strict_text(thread_id, field=f"{_TABLE_NAME}.thread_id"),
        ),
        source_id=strict_text(source_id, field=f"{_TABLE_NAME}.source_id"),
        source_kind=strict_text(source_kind, field=f"{_TABLE_NAME}.source_kind"),
        name=strict_text(name, field=f"{_TABLE_NAME}.name"),
        binding_revision=strict_optional_text(
            binding_revision, field=f"{_TABLE_NAME}.binding_revision"
        ),
        tracking_status=_tracking_status(tracking_status),
        latest_visible_committed_revision=strict_optional_text(
            latest_visible_committed_revision,
            field=f"{_TABLE_NAME}.latest_visible_committed_revision",
        ),
        latest_revision=strict_optional_text(
            latest_revision, field=f"{_TABLE_NAME}.latest_revision"
        ),
        state_revision=strict_non_negative_int(
            state_revision, field=f"{_TABLE_NAME}.state_revision"
        ),
        updated_at=strict_text(updated_at, field=f"{_TABLE_NAME}.updated_at"),
    )
    if state.state_revision < 1:
        raise RuntimeError(
            f"{_TABLE_NAME}.state_revision 必须从 1 开始: source_id={state.source_id}"
        )
    return state


class ContextSourceControlStorageMixin:
    """唯一 ContextStore owner 侧的 CSM 控制状态持久化实现。

    控制状态会在工具调用与 ``before_model`` 边界同步读写，所以访问方式必须
    与 canonical writer 隔离：

    - **不得**对既有 rollout 库调用 ``initialize()``：它会对未提交的 JSONL
      尾部执行 ``reconcile_jsonl_tail``，把进行中的 Turn 尚未提交的记录截断。
      只有库尚不存在（或为空文件）时才由标准 ``initialize`` 建立完整 v2 库。
    - **不得**取 rollout 文件锁 ``_lock``：它与 canonical 提交共用，在 Turn
      进行中取锁会与在途提交交错并改变 item 提交顺序（详见
      ``write_context_source_control_state`` 的说明）。
    """

    def _context_source_rollout_owner(
        self,
        session_id: str,
        thread_id: str,
    ) -> str:
        """校验精确 ``(session_id, thread_id)`` 归属并返回 rollout owner 节点 ID。"""
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("CSM 控制状态 session_id 必须是非空字符串")
        if not isinstance(thread_id, str) or not thread_id:
            raise ValueError("CSM 控制状态 thread_id 必须是非空字符串")
        resolved_thread_id = self._resolve_thread_identity(session_id, thread_id)
        self._resolve_thread_node(session_id, resolved_thread_id)
        return resolved_thread_id

    def _apply_sealed_context_sources(
        self,
        connection: sqlite3.Connection,
        snapshot: ContextAssemblySnapshot,
    ) -> None:
        """由唯一 assembly_sealed 事务确认 source-applied 与 prefix predecessor。

        ToolSet/PrefixEpoch 的 applied 真值就是不可变 assembly binding；这里
        不另建可改写的状态表。source 控制行只在同一事务内按 from revision
        推进可见基准，失败由调用方回滚整个 assembly。
        """
        binding = snapshot.seal_binding
        if binding is None:
            return
        previous_row = connection.execute(
            "SELECT snapshot_json FROM context_assemblies "
            "WHERE session_id = ? AND thread_id = ? "
            "AND status IN ('sealed','terminal') AND assembly_id <> ? "
            "ORDER BY rowid DESC LIMIT 1",
            (snapshot.session_id, snapshot.thread_id, snapshot.assembly_id),
        ).fetchone()
        previous = None
        if previous_row is not None:
            previous = ContextAssemblySnapshot.from_dict(
                json_text(previous_row[0], field="snapshot_json")
            )
        previous_id = previous.assembly_id if previous is not None else None
        if binding.previous_assembly_id != previous_id:
            raise RuntimeError(
                "context-seal-predecessor-conflict: applied assembly 已变化，拒绝半提交"
            )
        if previous is not None and previous.seal_binding is None:
            raise ItemSchemaError(
                "context-seal-binding-required: 既有 assembly 缺少 applied epoch binding"
            )
        if previous is not None:
            previous_binding = previous.seal_binding
            previous_epoch = previous_binding.prefix.state.epoch
            current_epoch = binding.prefix.state.epoch
            if current_epoch.ordinal == previous_epoch.ordinal:
                if (
                    current_epoch != previous_epoch
                    or binding.prefix.parent != previous_binding.prefix.state.parent
                    or binding.root != previous_binding.root
                    or binding.prefix.state.provider_profile != previous_binding.prefix.state.provider_profile
                    or binding.prefix.state.tool_compatibility_key != previous_binding.prefix.state.tool_compatibility_key
                ):
                    raise ItemSchemaError("context-seal-epoch-conflict: 同 epoch applied binding 被重写")
            elif current_epoch.ordinal != previous_epoch.ordinal + 1:
                raise ItemSchemaError("context-seal-epoch-conflict: rebuild ordinal 必须连续")
        elif binding.prefix.state.epoch.ordinal != 1:
            raise ItemSchemaError("context-seal-epoch-conflict: 首个 assembly 必须是 initial epoch")
        self._ensure_context_source_control_schema(connection)
        for source in binding.source_revisions:
            row = connection.execute(
                f"SELECT {_SELECT_COLUMNS} FROM {_TABLE_NAME} "
                "WHERE session_id = ? AND thread_id = ? AND source_id = ?",
                (snapshot.session_id, snapshot.thread_id, source.source_id),
            ).fetchone()
            if row is None:
                raise RuntimeError(
                    "context-source-seal-conflict: source 未由 owner 登记: " + source.source_id
                )
            stored = _state_from_row(tuple(row))
            if (
                stored.latest_revision != source.revision
                or stored.latest_visible_committed_revision != source.previous_revision
            ):
                raise RuntimeError(
                    "context-source-seal-conflict: observed/applied revision 已变化: "
                    + source.source_id
                )
            selected_source = any(
                provenance.source_identity == source.source_id
                and provenance.revision == source.revision
                for provenance in (() if binding.root is None else binding.root.sources)
            ) or any(
                frame.item_ref.source_identity == source.source_id
                and frame.item_ref.source_revision == source.revision
                for frame in binding.prefix.state.frames
            )
            if not selected_source:
                raise ItemSchemaError(
                    "context-source-seal-conflict: applied source 没有真实 selected frame: "
                    + source.source_id
                )
            cursor = connection.execute(
                f"UPDATE {_TABLE_NAME} SET latest_visible_committed_revision = ?, "
                "state_revision = state_revision + 1, updated_at = ? "
                "WHERE session_id = ? AND thread_id = ? AND source_id = ? "
                "AND state_revision = ?",
                (
                    source.revision, _now(), snapshot.session_id, snapshot.thread_id,
                    source.source_id, stored.state_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("context-source-seal-conflict: source applied CAS 未命中")

    def _context_source_rollout_owner_or_none(
        self,
        session_id: str,
        thread_id: str,
    ) -> str | None:
        """返回 durable Thread ID；没有 catalog Session 的合成 owner 返回 None。

        只有「会话不在权威目录索引中」这一种情况会被识别为没有 durable owner
        （例如工具清单检查用的 ``tools_inspection_session``：它从不落盘，也不是
        目录损坏）。已登记 Session 下缺失的 Thread 必须显式失败，不能静默短路
        为「该 Thread 没有控制状态」。物理节点/manifest 损坏时严格解析仍抛
        RuntimeError，本方法不吞掉这类错误。
        """
        try:
            self._resolve_session_node(session_id)
        except KeyError:
            return None
        try:
            return self._context_source_rollout_owner(session_id, thread_id)
        except KeyError as error:
            raise RuntimeError(
                "catalog 中不存在精确 SessionThread owner，拒绝把未登记 Thread "
                "当作无控制状态短路（fail closed）: "
                f"session_id={session_id}, thread_id={thread_id}"
            ) from error

    def context_source_control_owner_available(
        self,
        session_id: str,
        *,
        thread_id: str,
    ) -> bool:
        """该 SessionThread 是否有可用的 durable rollout owner。"""
        return (
            self._context_source_rollout_owner_or_none(session_id, thread_id)
            is not None
        )

    @staticmethod
    def _context_source_control_table_exists(connection: sqlite3.Connection) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (_TABLE_NAME,),
            ).fetchone()
            is not None
        )

    @classmethod
    def _require_context_source_control_runtime(
        cls,
        connection: sqlite3.Connection,
    ) -> None:
        """控制状态只允许访问 active 的 v2 rollout，且不触发恢复副作用。"""
        cls._require_v2_readable(connection)
        row = connection.execute(
            "SELECT database_state FROM database_meta WHERE singleton_id = 1"
        ).fetchone()
        if row is None:
            raise RuntimeError("rollout database_meta 缺失，无法访问 CSM 控制状态")
        database_state = strict_text(row[0], field="database_meta.database_state")
        if database_state != "active":
            raise RuntimeError(
                f"CSM 控制状态要求 active rollout: database_state={database_state}"
            )

    def _context_source_control_index_ready(
        self,
        session_id: str,
        thread_id: str,
    ) -> bool:
        index_path = self.index_path(session_id, "", thread_id=thread_id)
        return index_path.is_file() and index_path.stat().st_size > 0

    @staticmethod
    def _ensure_context_source_control_schema(connection: sqlite3.Connection) -> None:
        """为已存在的 rollout 库补齐控制状态表并执行显式列迁移。

        该表是 add-context-injection-lifecycle 期间新增的对象：新库直接使用
        schema owner 的当前 DDL；开发期建立的旧表按一次性显式
        ALTER TABLE RENAME COLUMN 迁移到 latest_visible_committed_revision
        命名，不保留双列读取。
        """
        table_exists = (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (_TABLE_NAME,),
            ).fetchone()
            is not None
        )
        if table_exists:
            columns = {
                row[0]
                for row in connection.execute(
                    f"PRAGMA table_info({_TABLE_NAME})"
                ).fetchall()
            }
            if (
                "applied_revision" in columns
                and "latest_visible_committed_revision" not in columns
            ):
                connection.execute(
                    f"ALTER TABLE {_TABLE_NAME} RENAME COLUMN applied_revision "
                    "TO latest_visible_committed_revision"
                )
        for statement in CONTEXT_SOURCE_CONTROL_STATE_SCHEMA_STATEMENTS:
            connection.execute(statement)

    def read_context_source_control_states(
        self,
        session_id: str,
        *,
        thread_id: str,
    ) -> tuple[ContextSourceControlState, ...]:
        """读取该 SessionThread 的全部控制状态，不建库、不执行恢复副作用。"""
        resolved_thread_id = self._context_source_rollout_owner_or_none(
            session_id, thread_id
        )
        if resolved_thread_id is None:
            # 合成 SessionThread 没有 ContextStore，也就没有可恢复的持久化
            # 控制状态；这是短路而不是吞错，索引损坏仍会抛 RuntimeError。
            return ()
        if not self._context_source_control_index_ready(session_id, resolved_thread_id):
            # 既有库尚未建立（或为空文件）：没有任何可恢复的控制状态。
            return ()
        with self._connect(
            resolved_thread_id,
            "",
            session_id=session_id,
            read_only=True,
        ) as connection:
            self._require_context_source_control_runtime(connection)
            self._validate_schema_state(connection)
            if not self._context_source_control_table_exists(connection):
                # 既有 v4 rollout 库可能还没有该表；没有表就等于没有控制状态。
                return ()
            rows = connection.execute(
                f"SELECT {_SELECT_COLUMNS} FROM {_TABLE_NAME} "
                "WHERE session_id = ? AND thread_id = ? "
                "ORDER BY created_at, source_id",
                (session_id, thread_id),
            ).fetchall()
        return tuple(_state_from_row(tuple(row)) for row in rows)

    def write_context_source_control_state(
        self,
        state: ContextSourceControlState,
    ) -> ContextSourceControlState:
        """在单个 owner 事务中 upsert 一条控制状态，并做乐观版本校验。

        这里**不取 rollout 文件锁**：``_lock`` 同时保护 canonical JSONL/SQLite
        的提交顺序，而在工具调用与 ``before_model`` 边界取锁会与在途提交交错，
        使 tool-call/tool-result item 的提交顺序倒置，itemized 投影随之失去
        tool protocol closure（实测会让刚注入的 source item 从下一次模型请求中
        消失）。控制状态独立成表，一致性由 SQLite 事务、``state_revision`` CAS
        和 owner 单写者约束保证，不需要 canonical writer 的文件锁。
        """
        if not isinstance(state, ContextSourceControlState):
            raise TypeError(
                "write_context_source_control_state 需要 ContextSourceControlState"
            )
        owner = state.owner
        thread_id = self._context_source_rollout_owner(
            owner.session_id,
            owner.thread_id,
        )
        if not self._context_source_control_index_ready(owner.session_id, thread_id):
            # 只有库尚不存在时建立完整 v2 库（initialize 自己负责取锁）；
            # 既有库绝不走 initialize()，避免截断在途 Turn 的未提交 JSONL 尾部。
            self.initialize(
                owner.session_id,
                "",
                thread_id=thread_id,
                validate_jsonl_items=False,
            )
        with self._connect(thread_id, "", session_id=owner.session_id) as connection:
            self._require_context_source_control_runtime(connection)
            self._validate_schema_state(connection)
            self._ensure_context_source_control_schema(connection)
            existing_row = connection.execute(
                f"SELECT {_SELECT_COLUMNS} FROM {_TABLE_NAME} "
                "WHERE session_id = ? AND thread_id = ? AND source_id = ?",
                (owner.session_id, owner.thread_id, state.source_id),
            ).fetchone()
            timestamp = _now()
            if existing_row is not None:
                stored = _state_from_row(tuple(existing_row))
                if stored.durable_fields() == state.durable_fields():
                    # 幂等写入：并发/重复 register 不能推进 state_revision。
                    return stored
                if state.state_revision != stored.state_revision:
                    raise RuntimeError(
                        "context-source-control-state-conflict: "
                        "控制状态已被其它写入者推进: "
                        f"session_id={owner.session_id}, "
                        f"thread_id={owner.thread_id}, "
                        f"source_id={state.source_id}, "
                        f"expected={state.state_revision}, "
                        f"stored={stored.state_revision}"
                    )
                next_revision = stored.state_revision + 1
                cursor = connection.execute(
                    f"UPDATE {_TABLE_NAME} SET source_kind = ?, name = ?, "
                    "binding_revision = ?, tracking_status = ?, "
                    "latest_visible_committed_revision = ?, latest_revision = ?, "
                    "state_revision = ?, updated_at = ? "
                    "WHERE session_id = ? AND thread_id = ? AND source_id = ? "
                    "AND state_revision = ?",
                    (
                        state.source_kind,
                        state.name,
                        state.binding_revision,
                        state.tracking_status,
                        state.latest_visible_committed_revision,
                        state.latest_revision,
                        next_revision,
                        timestamp,
                        owner.session_id,
                        owner.thread_id,
                        state.source_id,
                        stored.state_revision,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(
                        "context-source-control-state-conflict: "
                        "控制状态更新未命中唯一 owner 行: "
                        f"source_id={state.source_id}"
                    )
            else:
                if state.state_revision != 0:
                    raise RuntimeError(
                        "context-source-control-state-conflict: "
                        "控制状态尚不存在，不能携带既有 state_revision: "
                        f"source_id={state.source_id}, "
                        f"state_revision={state.state_revision}"
                    )
                next_revision = 1
                cursor = connection.execute(
                    f"INSERT INTO {_TABLE_NAME} "
                    f"({_SELECT_COLUMNS}) VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        owner.session_id,
                        owner.thread_id,
                        state.source_id,
                        state.source_kind,
                        state.name,
                        state.binding_revision,
                        state.tracking_status,
                        state.latest_visible_committed_revision,
                        state.latest_revision,
                        next_revision,
                        timestamp,
                        timestamp,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(
                        "context-source-control-state-conflict: "
                        f"控制状态写入失败: source_id={state.source_id}"
                    )
            connection.commit()
        return replace(
            state,
            state_revision=next_revision,
            updated_at=timestamp,
        )

    def apply_source_lifecycle_decision(
        self,
        decision: ApplySourceLifecycleDecision,
        item: CanonicalItemRecord | None,
    ) -> tuple[int, ...]:
        """在同一 owner transaction 提交 source item 与 control state。

        ``ApplySourceLifecycleDecision`` 是唯一 source mutation 入口：正文已经
        在 typed intent 中封存，storage 不从文件或当前资源快照补读。``item``
        为 ``None`` 只允许 track/untrack 这类没有 source item 的控制决策；
        ``observe_pending`` 只提交 pending ambient item，不推进 applied revision。
        """
        if not isinstance(decision, ApplySourceLifecycleDecision):
            raise TypeError(
                "apply_source_lifecycle_decision 需要 ApplySourceLifecycleDecision"
            )
        if item is not None and not isinstance(item, CanonicalItemRecord):
            raise TypeError(
                "apply_source_lifecycle_decision.item 必须是 CanonicalItemRecord 或 None"
            )
        owner = decision.owner
        thread_id = self._context_source_rollout_owner(
            owner.session_id,
            owner.thread_id,
        )
        if not self._context_source_control_index_ready(owner.session_id, thread_id):
            self.initialize(
                owner.session_id,
                "",
                thread_id=thread_id,
                validate_jsonl_items=False,
            )
        with (
            self._lock(thread_id, "", session_id=owner.session_id),
            self._connect(thread_id, "", session_id=owner.session_id) as connection,
        ):
            self._require_context_source_control_runtime(connection)
            self._validate_schema_state(connection)
            self._ensure_context_source_control_schema(connection)
            existing_row = connection.execute(
                f"SELECT {_SELECT_COLUMNS} FROM {_TABLE_NAME} "
                "WHERE session_id = ? AND thread_id = ? AND source_id = ?",
                (owner.session_id, owner.thread_id, decision.source_id),
            ).fetchone()
            stored = (
                _state_from_row(tuple(existing_row))
                if existing_row is not None
                else None
            )
            if item is not None:
                if decision.content is None:
                    raise ValueError(
                        "source item 存在时 ApplySourceLifecycleDecision.content 不能为 None"
                    )
                existing_item = connection.execute(
                    "SELECT content_hash, semantic_kind, payload_kind, status, "
                    "turn_id, turn_scope, message_group_id, wire_role, "
                    "producer_ref_json, projection_manifest_json, commit_id "
                    "FROM item_catalog WHERE item_id = ?",
                    (item.item_id,),
                ).fetchone()
                if existing_item is not None:
                    # 仅允许完全相同的 immutable source item 重放；其它冲突
                    # 必须失败，不能以 revision 或当前正文猜测替代。
                    stored_identity = (
                        existing_item[0],
                        existing_item[1],
                        existing_item[2],
                        existing_item[3],
                        existing_item[4],
                        existing_item[5],
                        existing_item[6],
                        existing_item[7],
                        existing_item[8],
                        existing_item[9],
                    )
                    incoming_identity = (
                        item.content_hash,
                        item.semantic_kind,
                        item.payload_kind,
                        item.status,
                        item.turn_id,
                        item.turn_scope,
                        item.message_group_id,
                        item.wire_role,
                        canonical_json_bytes(dict(item.producer_ref)).decode("utf-8"),
                        canonical_json_bytes(item.projection_manifest()).decode("utf-8"),
                    )
                    if stored_identity != incoming_identity:
                        raise RuntimeError(
                            "source item identity/content 冲突，拒绝重放: "
                            f"item_id={item.item_id}"
                        )
                    commit_id = strict_non_negative_int(
                        existing_item[10],
                        field="item_catalog.commit_id",
                    )
                    commit_ids = (commit_id,)
                else:
                    last_sequence_row = connection.execute(
                        "SELECT last_item_sequence FROM database_meta "
                        "WHERE singleton_id = 1"
                    ).fetchone()
                    if last_sequence_row is None:
                        raise RuntimeError(
                            "rollout database_meta 缺少 last_item_sequence"
                        )
                    next_sequence = (
                        strict_non_negative_int(
                            last_sequence_row[0],
                            field="database_meta.last_item_sequence",
                        )
                        + 1
                    )
                    prepared_item = replace(item, item_sequence=next_sequence)
                    connection.execute("BEGIN IMMEDIATE")
                    commit_id, _offset = self._append_v2_records_transaction(
                        connection,
                        thread_id,
                        "",
                        (prepared_item,),
                        session_id=owner.session_id,
                        commit_kind=CommitKind.ITEM_CONVERGENCE.value,
                        subject_id=prepared_item.item_id,
                        idempotency_key=decision.idempotency_key,
                        begin_transaction=False,
                    )
                    self._append_context_view_items(
                        connection,
                        checkpoint_ns="",
                        item_ids=(prepared_item.item_id,),
                    )
                    commit_ids = (commit_id,)
            else:
                connection.execute("BEGIN IMMEDIATE")
                commit_ids = ()

            # observe_pending 只保留 immutable pending item；提醒等一次性
            # runtime source 不建立 tracking row，避免把事件伪装成 CSM
            # registration。其它 source decision 才更新控制状态。
            if decision.decision_kind != "observe_pending":
                tracking_status = decision.tracking_status or (
                    stored.tracking_status if stored is not None else "untracked"
                )
                latest_revision = (
                    decision.revision
                    if decision.revision is not None
                    else (stored.latest_revision if stored is not None else None)
                )
                latest_visible = (
                    decision.revision
                    if decision.content is not None and not decision.pending_only
                    else (
                        stored.latest_visible_committed_revision
                        if stored is not None
                        else None
                    )
                )
                state = ContextSourceControlState(
                    owner=ContextSourceOwnerKey(
                        session_id=owner.session_id,
                        thread_id=owner.thread_id,
                    ),
                    source_id=decision.source_id,
                    source_kind=decision.source_kind,
                    name=decision.name,
                    binding_revision=(
                        stored.binding_revision if stored is not None else None
                    ),
                    tracking_status=cast(
                        ContextSourceTrackingStatus,
                        tracking_status,
                    ),
                    latest_visible_committed_revision=latest_visible,
                    latest_revision=latest_revision,
                    state_revision=stored.state_revision if stored is not None else 0,
                )
                timestamp = _now()
                if stored is None:
                    connection.execute(
                        f"INSERT INTO {_TABLE_NAME} "
                        f"({_SELECT_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            owner.session_id,
                            owner.thread_id,
                            state.source_id,
                            state.source_kind,
                            state.name,
                            state.binding_revision,
                            state.tracking_status,
                            state.latest_visible_committed_revision,
                            state.latest_revision,
                            1,
                            timestamp,
                            timestamp,
                        ),
                    )
                elif stored.durable_fields() != state.durable_fields():
                    next_state_revision = stored.state_revision + 1
                    cursor = connection.execute(
                        f"UPDATE {_TABLE_NAME} SET source_kind = ?, name = ?, "
                        "binding_revision = ?, tracking_status = ?, "
                        "latest_visible_committed_revision = ?, latest_revision = ?, "
                        "state_revision = ?, updated_at = ? "
                        "WHERE session_id = ? AND thread_id = ? AND source_id = ? "
                        "AND state_revision = ?",
                        (
                            state.source_kind,
                            state.name,
                            state.binding_revision,
                            state.tracking_status,
                            state.latest_visible_committed_revision,
                            state.latest_revision,
                            next_state_revision,
                            timestamp,
                            owner.session_id,
                            owner.thread_id,
                            state.source_id,
                            stored.state_revision,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError(
                            "context-source-control-state-conflict: source decision "
                            f"更新未命中 owner 行: source_id={state.source_id}"
                        )
            connection.commit()
            return commit_ids


class ContextSourceControlOwnerMixin:
    """暴露给 CSM 的唯一 owner 端口；实现委托给 RolloutStorage。"""

    def context_source_control_owner_available(
        self,
        owner: ContextSourceOwnerKey,
    ) -> bool:
        """该 SessionThread 是否有 durable ContextStore owner。

        装配层用它在合成 session（例如工具清单检查）上把 CSM 保持为纯内存
        对象，而不是为不存在的 ContextStore 伪造持久化成功。
        """
        if not isinstance(owner, ContextSourceOwnerKey):
            raise TypeError(
                "context_source_control_owner_available 需要 ContextSourceOwnerKey"
            )
        return self._storage.context_source_control_owner_available(
            owner.session_id,
            thread_id=owner.thread_id,
        )

    def load_context_source_control_states(
        self,
        owner: ContextSourceOwnerKey,
    ) -> tuple[ContextSourceControlState, ...]:
        if not isinstance(owner, ContextSourceOwnerKey):
            raise TypeError(
                "load_context_source_control_states 需要 ContextSourceOwnerKey"
            )
        return self._storage.read_context_source_control_states(
            owner.session_id,
            thread_id=owner.thread_id,
        )

    def save_context_source_control_state(
        self,
        state: ContextSourceControlState,
    ) -> ContextSourceControlState:
        if not isinstance(state, ContextSourceControlState):
            raise TypeError(
                "save_context_source_control_state 需要 ContextSourceControlState"
            )
        return self._storage.write_context_source_control_state(state)

    def apply_source_lifecycle_decision(
        self,
        decision: ApplySourceLifecycleDecision,
        item: CanonicalItemRecord | None,
    ) -> tuple[int, ...]:
        """通过唯一 Saver/ContextStore owner 提交 source intent。"""
        if not isinstance(decision, ApplySourceLifecycleDecision):
            raise TypeError(
                "apply_source_lifecycle_decision 需要 ApplySourceLifecycleDecision"
            )
        return self._storage.apply_source_lifecycle_decision(decision, item)


__all__ = [
    "ContextSourceControlOwnerMixin",
    "ContextSourceControlStorageMixin",
]
