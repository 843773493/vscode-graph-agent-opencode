"""CSM 控制状态的持久化 owner 与 Saver 端口。

CSM 不执行 I/O：它只持有 :class:`ContextSourceControlStatePort`。本模块提供
两个实现层：

- ``ContextSourceControlStorageMixin``：混入唯一 ``RolloutStorage``，负责
  SQLite 表、owner 锁、事务和严格行校验。
- ``ContextSourceControlOwnerMixin``：混入唯一 ``RolloutCheckpointSaver``，
  把 typed 控制状态端口暴露给 CSM，业务层不得旁路 RolloutStorage。

owner 粒度是精确 ``(session_id, thread_id)``：main thread 使用
``MAIN_THREAD_ID`` 并落在 Session 节点的 rollout 库；durable child thread
必须解析到它自己的节点，状态不共享、不互相覆盖。

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
from datetime import UTC, datetime
from typing import cast

from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    MAIN_THREAD_ID,
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


def _now() -> str:
    return datetime.now(UTC).isoformat()


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
            session_id=strict_text(
                session_id, field=f"{_TABLE_NAME}.session_id"
            ),
            thread_id=strict_text(
                thread_id, field=f"{_TABLE_NAME}.thread_id"
            ),
        ),
        source_id=strict_text(source_id, field=f"{_TABLE_NAME}.source_id"),
        source_kind=strict_text(source_kind, field=f"{_TABLE_NAME}.source_kind"),
        name=strict_text(name, field=f"{_TABLE_NAME}.name"),
        binding_revision=strict_optional_text(
            binding_revision, field=f"{_TABLE_NAME}.binding_revision"
        ),
        tracking_status=_tracking_status(tracking_status),
        latest_visible_committed_revision=strict_optional_text(
            latest_visible_committed_revision, field=f"{_TABLE_NAME}.latest_visible_committed_revision"
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
            f"{_TABLE_NAME}.state_revision 必须从 1 开始: "
            f"source_id={state.source_id}"
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
        if thread_id == MAIN_THREAD_ID:
            self._path_resolver.resolve_session_node(session_id)
            return session_id
        if thread_id == session_id:
            raise ValueError(
                "CSM 控制状态 main thread 必须使用 MAIN_THREAD_ID，"
                f"不能把裸 session_id 当作 thread_id 持久化: {session_id}"
            )
        node_path = self._path_resolver.resolve_thread_node(session_id, thread_id)
        if node_path.name != thread_id:
            raise RuntimeError(
                "CSM 控制状态 thread 节点物理目录名与稳定 ID 不一致: "
                f"session_id={session_id}, thread_id={thread_id}, path={node_path}"
            )
        return thread_id

    def _context_source_rollout_owner_or_none(
        self,
        session_id: str,
        thread_id: str,
    ) -> str | None:
        """返回 durable rollout owner 节点 ID；合成 SessionThread 返回 None。

        只有「会话不在权威目录索引中」这一种情况会被识别为没有 durable owner
        （例如工具清单检查用的 ``tools_inspection_session``：它从不落盘，也不是
        目录损坏）。catalog 模式下未登记的 non-main thread（thread_catalog
        无对应 child 行）必须显式失败：OpenSpec 8.5 落地前其物理形态不存在，
        静默短路会把「请求了不存在的 thread」伪装成「该 thread 没有控制状态」。
        索引已登记但物理节点/manifest 损坏时严格解析仍抛 RuntimeError，
        本方法不吞掉这类错误。
        """
        try:
            self._path_resolver.resolve_session_node(session_id)
        except KeyError:
            return None
        if thread_id != MAIN_THREAD_ID and thread_id != session_id:
            try:
                self._path_resolver.resolve_thread_node(session_id, thread_id)
            except KeyError as error:
                raise RuntimeError(
                    "非 main thread 物理形态未落地，拒绝把未登记 child thread "
                    "当作无控制状态短路（fail closed）: "
                    f"session_id={session_id}, thread_id={thread_id}"
                ) from error
        return self._context_source_rollout_owner(session_id, thread_id)

    def context_source_control_owner_available(
        self,
        session_id: str,
        *,
        thread_id: str = MAIN_THREAD_ID,
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
                "CSM 控制状态要求 active rollout: "
                f"database_state={database_state}"
            )

    def _context_source_control_index_ready(self, rollout_thread_id: str) -> bool:
        index_path = self.index_path(rollout_thread_id, "")
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
        thread_id: str = MAIN_THREAD_ID,
    ) -> tuple[ContextSourceControlState, ...]:
        """读取该 SessionThread 的全部控制状态，不建库、不执行恢复副作用。"""
        rollout_thread_id = self._context_source_rollout_owner_or_none(
            session_id,
            thread_id,
        )
        if rollout_thread_id is None:
            # 合成 SessionThread 没有 ContextStore，也就没有可恢复的持久化
            # 控制状态；这是短路而不是吞错，索引损坏仍会抛 RuntimeError。
            return ()
        if not self._context_source_control_index_ready(rollout_thread_id):
            # 既有库尚未建立（或为空文件）：没有任何可恢复的控制状态。
            return ()
        with self._connect(rollout_thread_id, "", read_only=True) as connection:
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
        rollout_thread_id = self._context_source_rollout_owner(
            owner.session_id,
            owner.thread_id,
        )
        if not self._context_source_control_index_ready(rollout_thread_id):
            # 只有库尚不存在时建立完整 v2 库（initialize 自己负责取锁）；
            # 既有库绝不走 initialize()，避免截断在途 Turn 的未提交 JSONL 尾部。
            self.initialize(rollout_thread_id, "", validate_jsonl_items=False)
        with self._connect(rollout_thread_id, "") as connection:
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


__all__ = [
    "ContextSourceControlOwnerMixin",
    "ContextSourceControlStorageMixin",
]
