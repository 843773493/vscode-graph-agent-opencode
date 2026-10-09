"""精确 SessionThread 的 CSM 控制状态持久化；与 checkpoint durable owner 共用事务。"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from typing import cast

from app.core.sqlite_state import utc_now_text as _now
from app.domain.itemized.enums import CommitKind
from app.domain.itemized.hashing import canonical_json_bytes
from app.domain.itemized.mutation_intents import ApplySourceLifecycleDecision
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceControlState,
    ContextSourceOwnerKey,
    ContextSourceTrackingStatus,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutNotMaterializedError,
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
    """唯一 checkpoint durable owner 侧的 CSM 控制状态持久化实现。

    控制状态会在工具调用与 ``before_model`` 边界同步读写，所以访问方式必须
    与 canonical writer 隔离：

    - **不得**对既有 rollout 库调用 ``initialize()``：它会对未提交的 JSONL
      尾部执行 ``reconcile_jsonl_tail``，把进行中的 Turn 尚未提交的记录截断。
      只有库尚不存在（或为空文件）时才由标准 ``initialize`` 建立完整 v2 库。
    - 写入**不得**取 rollout 文件锁 ``_lock``：它与 canonical 提交共用，在
      Turn 进行中取锁会与在途提交交错并改变 item 提交顺序（详见
      ``write_context_source_control_state`` 的说明）。读取复用统一只读快照。
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
        resolved_thread_id = self._storage._resolve_thread_identity(session_id, thread_id)
        self._storage._resolve_thread_node(session_id, resolved_thread_id)
        return resolved_thread_id





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
            self._storage._resolve_session_node(session_id)
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

    def _require_context_source_control_runtime(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        """控制状态只允许访问 active 的 v2 rollout，且不触发恢复副作用。"""
        self._storage._require_v2_readable(connection)
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
        index_path = self._storage.index_path(session_id, "", thread_id=thread_id)
        return index_path.is_file() and index_path.stat().st_size > 0

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
        try:
            with self._storage.read_connection(session_id, resolved_thread_id) as connection:
                self._require_context_source_control_runtime(connection)
                rows = connection.execute(
                    f"SELECT {_SELECT_COLUMNS} FROM {_TABLE_NAME} "
                    "WHERE session_id = ? AND thread_id = ? "
                    "ORDER BY created_at, source_id",
                    (session_id, resolved_thread_id),
                ).fetchall()
        except RolloutNotMaterializedError:
            return ()
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
            self._storage.initialize(
                owner.session_id,
                "",
                thread_id=thread_id,
                validate_jsonl_items=False,
            )
        with self._storage._connect(thread_id, "", session_id=owner.session_id) as connection:
            self._require_context_source_control_runtime(connection)
            self._storage._validate_schema_state(connection)
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
            self._storage.initialize(
                owner.session_id,
                "",
                thread_id=thread_id,
                validate_jsonl_items=False,
            )
        with self._context.executions._execution_input_transaction(owner.session_id, thread_id) as connection:
            return self._apply_source_lifecycle_decision_transaction(connection, decision, item)

    def _apply_source_lifecycle_decision_transaction(
        self, connection: sqlite3.Connection,
        decision: ApplySourceLifecycleDecision, item: CanonicalItemRecord | None,
    ) -> tuple[int, ...]:
        """source owner 的唯一事务内实现；不会自行提交或推进 pending applied。"""
        if not connection.in_transaction:
            raise RuntimeError("source decision 需要活动 ContextStore 事务")
        owner = decision.owner
        thread_id = self._context_source_rollout_owner(owner.session_id, owner.thread_id)
        self._require_context_source_control_runtime(connection)
        self._storage._validate_schema_state(connection)
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
                commit_id, _offset = self._storage._append_v2_records_transaction(
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
                self._storage._append_context_view_items(
                    connection,
                    checkpoint_ns="",
                    item_ids=(prepared_item.item_id,),
                )
                commit_ids = (commit_id,)
        else:
            commit_ids = ()

        if item is not None and decision.execution_id is not None:
            self._storage._bind_canonical_item_execution(
                connection, item_id=item.item_id, execution_id=decision.execution_id,
            )

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
        return commit_ids




__all__ = [
    "ContextSourceControlStorageMixin",
]
