"""v2 checkpoint/context query owner。

本模块只提供已提交 SQLite view/catalog 的查询，不扫描 v1、不生成 LangChain
message，也不拥有 checkpoint 写入事实。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from typing import TYPE_CHECKING

from langgraph.checkpoint.base import (
    Checkpoint,
    CheckpointMetadata,
    DeltaChannelHistory,
)
from langgraph.checkpoint.serde.types import _DeltaSnapshot

from app.core.execution_admission_context import current_execution_admission
from app.core.session_control_primitives import CONTROL_DATABASE_NAME
from app.core.session_control_store import SessionControlStore
from app.domain.itemized.enums import ControlOutcome
from app.domain.itemized.identity.refs import ContextRef
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.storage.catalog.history_scope import (
    require_historical_scope,
)
from app.services.infrastructure.rollout_context.storage.checkpoint_lineage import (
    delta_history_path,
)
from app.services.infrastructure.rollout_context.storage.checkpoint_state_values import (
    read_state_value,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutNotMaterializedError,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_optional_non_negative_int,
    strict_optional_text,
    strict_text,
)
from app.services.mapping.itemized.carrier_dedup import (
    superseded_canonical_message_item_ids,
    superseded_item_ids,
)

if TYPE_CHECKING:
    from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
    from app.services.infrastructure.rollout_context.storage.primitives import (
        RolloutCheckpointIndex,
        RolloutReadSnapshot,
    )
    from app.services.infrastructure.rollout_context.storage.service import (
        RolloutStorage,
    )


class RolloutCheckpointQueriesMixin:
    """checkpoint header、active view 和已提交 context query owner。"""

    if TYPE_CHECKING:
        _storage: RolloutStorage

        @staticmethod
        def _pending_context_epoch_row(connection: sqlite3.Connection, checkpoint_ns: str) -> tuple[object, ...] | None: ...

    def list_checkpoint_owners(self, *, session_id: str | None = None) -> tuple[MutationIntentOwner, ...]:
        """从 catalog 枚举 main/child；精确 Session 读取也覆盖删除排空期。"""
        resolver = self._storage._path_resolver
        owners: list[MutationIntentOwner] = []
        session_ids = ([session_id] if session_id is not None else
                       [node.node_id for node in resolver.list_nodes() if node.kind == "session"])
        for owner_session_id in session_ids:
            main_thread_id = resolver.main_thread_id(owner_session_id)
            session_node = resolver.resolve_session_node(owner_session_id)
            control = SessionControlStore(session_node / CONTROL_DATABASE_NAME, read_only=True)
            try:
                control.verify_matches_catalog_main_thread(main_thread_id)
                children = control.list_child_thread_rows()
            finally:
                control.close()
            owners.append(MutationIntentOwner(owner_session_id, main_thread_id))
            owners.extend(MutationIntentOwner(owner_session_id, child.thread_id) for child in children)
        return tuple(owners)

    def execution_context_excluded_item_ids(
        self, connection: sqlite3.Connection, *, session_id: str, thread_id: str,
        checkpoint_ns: str = "",
    ) -> frozenset[str]:
        """用户与内部执行共用唯一 admission owner 的持久准入游标。"""
        admission = current_execution_admission(langgraph_thread_id=thread_id)
        if admission is None:
            return frozenset()
        if (admission.session_id, admission.thread_id) != (session_id, thread_id):
            raise ValueError("execution context fence 与当前 owner 不一致")
        return self._context.executions.execution_context_excluded_item_ids(
            admission, connection=connection, checkpoint_ns=checkpoint_ns,
        )

    @staticmethod
    def _preceding_terminal_tool_sequences(
        connection: sqlite3.Connection, *, session_id: str, thread_id: str,
        checkpoint_ns: str,
    ) -> list[int]:
        """新执行恢复前序终态工具组；同 Turn 的显式 resume 仍使用冻结 channel。"""
        admission = current_execution_admission(langgraph_thread_id=thread_id)
        if admission is None:
            return []
        outcomes = tuple(outcome.value for outcome in ControlOutcome)
        placeholders = ",".join("?" for _ in outcomes)
        rows = connection.execute(
            "SELECT call.assistant_message_sequence, message.message_sequence, "
            "execution.session_id, execution.thread_id, emitting_execution.session_id, emitting_execution.thread_id "
            "FROM checkpoint_namespace_state AS namespace "
            "JOIN branches AS branch ON branch.branch_id=namespace.active_branch_id "
            "JOIN context_view_items AS member ON member.view_id=branch.head_view_id AND member.visible=1 AND member.context_selected=1 "
            "JOIN item_catalog AS item ON item.item_id=member.item_id "
            "JOIN messages AS message ON message.jsonl_offset=item.jsonl_offset AND message.jsonl_length=item.jsonl_length "
            "JOIN executions AS execution ON execution.execution_id=item.execution_id "
            "JOIN tool_calls AS call ON call.result_message_sequence=message.message_sequence "
            "JOIN messages AS emitting ON emitting.message_sequence=call.assistant_message_sequence "
            "JOIN item_catalog AS emitting_item ON emitting_item.jsonl_offset=emitting.jsonl_offset AND emitting_item.jsonl_length=emitting.jsonl_length "
            "LEFT JOIN executions AS emitting_execution ON emitting_execution.execution_id=emitting_item.execution_id "
            "JOIN context_view_items AS emitting_member ON emitting_member.view_id=branch.head_view_id AND emitting_member.item_id=emitting_item.item_id AND emitting_member.visible=1 AND emitting_member.context_selected=1 "
            "WHERE namespace.checkpoint_ns=? AND item.semantic_kind='tool_result' AND item.status='completed' "
            f"AND execution.outcome IN ({placeholders}) AND execution.execution_id<>? "
            "AND (? IS NULL OR item.turn_id IS NOT ?) "
            "ORDER BY member.context_ordinal, call.assistant_message_sequence",
            (checkpoint_ns, *outcomes, admission.execution_id,
             admission.turn_id, admission.turn_id),
        ).fetchall()
        sequences: list[int] = []
        for row in rows:
            if row[2:4] != row[4:6]:
                raise RuntimeError("source-mismatch: 前序工具组与原执行 owner 不一致")
            require_historical_scope(
                connection, row[2], row[3], current_owner=(session_id, thread_id),
            )
            sequences.extend(
                strict_non_negative_int(sequence, field="preceding terminal tool message_sequence")
                for sequence in row[:2]
            )
        return list(dict.fromkeys(sequences))

    @staticmethod
    def _require_checkpoint_messages_view(
        connection: sqlite3.Connection, *, checkpoint_id: str,
        history_view_id: str, branch_id: str, messages_view_id: str,
    ) -> str:
        """读取与继承都必须证明 messages selection 属于精确冻结 checkpoint。"""
        owner = connection.execute(
            "SELECT messages.branch_id, messages.view_kind, messages.control_sequence, "
            "history.branch_id, history.control_sequence, event.checkpoint_id, "
            "event.view_id, event.branch_id, event.control_kind "
            "FROM context_views AS messages "
            "JOIN context_views AS history ON history.view_id = ? "
            "JOIN control_events AS event ON event.control_sequence = history.control_sequence "
            "WHERE messages.view_id = ?",
            (history_view_id, messages_view_id),
        ).fetchone()
        if (
            owner is None
            or owner[0] != branch_id
            or owner[1] != "checkpoint_messages"
            or owner[2] != owner[4]
            or owner[3] != branch_id
            or owner[5:] != (checkpoint_id, history_view_id, branch_id, "checkpoint_created")
        ):
            raise RuntimeError(f"checkpoint messages selection 与冻结 checkpoint 不一致: checkpoint_id={checkpoint_id}, messages_view_id={messages_view_id}, owner={owner!r}")
        strict_non_negative_int(owner[2], field="checkpoint messages context_views.control_sequence")
        return messages_view_id

    def latest_checkpoint(
        self,
        session_id: str,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str | None,
        *,
        snapshot: RolloutReadSnapshot | None = None,
    ) -> RolloutCheckpointIndex | None:
        session_id = strict_text(session_id, field="latest_checkpoint.session_id")
        thread_id = strict_text(thread_id, field="latest_checkpoint.thread_id")
        strict_text(
            checkpoint_ns, field="latest_checkpoint.checkpoint_ns", allow_empty=True
        )
        checkpoint_id = strict_optional_text(
            checkpoint_id, field="latest_checkpoint.checkpoint_id"
        )
        if snapshot is not None and (
            snapshot.session_id != session_id
            or snapshot.thread_id != thread_id
            or snapshot.checkpoint_ns != checkpoint_ns
        ):
            raise ValueError("latest_checkpoint snapshot 与目标 rollout 不一致")

        try:
            with self._storage.read_connection(
                session_id, thread_id, checkpoint_ns, snapshot=snapshot,
            ) as connection:
                row = self._checkpoint_row(
                    connection, checkpoint_ns, checkpoint_id,
                )
                return self._storage._checkpoint_index(row) if row else None
        except RolloutNotMaterializedError:
            return None

    def require_history_view(
        self, snapshot: RolloutReadSnapshot, history_view_id: object,
    ) -> str:
        if not isinstance(history_view_id, str) or not history_view_id:
            raise ValueError("history-projection-unavailable: history_view_id 必须是非空 selector")
        connection = self._storage._snapshot_connection(snapshot)
        history = connection.execute(
            "SELECT v.view_kind, e.control_kind, e.view_id, e.branch_id, v.branch_id, c.checkpoint_ns "
            "FROM context_views AS v JOIN control_events AS e ON e.control_sequence=v.control_sequence "
            "JOIN checkpoints AS c ON c.checkpoint_id=e.checkpoint_id WHERE v.view_id=?",
            (history_view_id,),
        ).fetchone()
        if history is None or history[:3] != ("history_replay", "history_replay", history_view_id) or (
            history[3] != history[4] or history[5] != snapshot.checkpoint_ns
        ):
            raise ValueError("history-projection-unavailable: selector 不属于同一 owner/namespace 的历史 view")
        return history_view_id

    def delta_channel_history(
        self, snapshot: RolloutReadSnapshot, checkpoint_id: str | None,
        *, channels: Sequence[str],
    ) -> dict[str, DeltaChannelHistory]:
        """在一个冻结快照中沿真实父链恢复各通道，不物化无关 messages。"""
        remaining = {strict_text(channel, field="delta_history.channel") for channel in channels}
        result: dict[str, DeltaChannelHistory] = {channel: {"writes": []} for channel in channels}
        if not remaining:
            return result
        index = self.latest_checkpoint(
            snapshot.session_id, snapshot.thread_id, snapshot.checkpoint_ns,
            checkpoint_id, snapshot=snapshot,
        )
        if index is None:
            return result
        # 目标自己的 pending writes 由 Pregel 单独恢复；这里只验证其冻结指针。
        self.checkpoint_values(
            snapshot.session_id, snapshot.thread_id, snapshot.checkpoint_ns,
            index, snapshot=snapshot, requested_channels=frozenset(),
        )
        connection = self._storage._snapshot_connection(snapshot)
        for cursor in delta_history_path(
            connection, index.checkpoint_id, checkpoint_ns=snapshot.checkpoint_ns,
            channels=channels,
        ):
            index = self.latest_checkpoint(
                snapshot.session_id, snapshot.thread_id, snapshot.checkpoint_ns,
                cursor, snapshot=snapshot,
            )
            if index is None:
                raise RuntimeError(f"checkpoint parent lineage 缺少已引用的祖先: {cursor}")
            requested = frozenset(remaining)
            writes = self.pending_writes(
                snapshot.session_id, snapshot.thread_id, snapshot.checkpoint_ns,
                index.checkpoint_id, snapshot=snapshot, requested_channels=requested,
            )
            for write in reversed(writes):
                result[write[1]]["writes"].append(write)
            values = self.checkpoint_values(
                snapshot.session_id, snapshot.thread_id, snapshot.checkpoint_ns,
                index, snapshot=snapshot, requested_channels=requested, message_protocol=True,
            )
            for channel, seed in values.items():
                result[channel]["seed"] = seed
                remaining.remove(channel)
        for history in result.values():
            history["writes"].reverse()
        return result

    def active_view_id(
        self,
        snapshot: RolloutReadSnapshot,
    ) -> str | None:
        connection = self._storage._snapshot_connection(snapshot)
        self._storage._require_v2_runtime(connection)
        row = connection.execute(
            "SELECT head_view_id FROM branches WHERE branch_id = ?",
            (snapshot.manifest.active_branch_id,),
        ).fetchone()
        return (
            strict_optional_text(row[0], field="branches.head_view_id")
            if row is not None
            else None
        )

    def committed_context_items(
        self,
        snapshot: RolloutReadSnapshot,
        *,
        predecessor: ContextAssemblySnapshot | None,
        include_pending_notices: bool = True,
    ) -> tuple[CanonicalItemRecord, ...]:
        """canonical 历史加当前准入来源；已 applied 前缀只精确继承。"""
        view_id = self.active_view_id(snapshot)
        if view_id is None:
            # v2 初始化会先建立 active view，acceptance/stream sink 只会补充
            # 该 view 的 membership。没有 active view 就无法知道当前 branch 的
            # selection；回退到 item_catalog 的物理顺序会把其它 branch/ambient
            # item 冒充当前上下文，属于第二套事实源，必须 fail-closed。
            raise RuntimeError(
                "rollout active view 缺失，拒绝从 item_catalog 物理顺序推导 context"
            )
        selected_ids = self.execution_context_item_ids(
            snapshot.connection, session_id=snapshot.session_id, thread_id=snapshot.thread_id,
            checkpoint_ns=snapshot.checkpoint_ns, view_id=view_id,
            predecessor=predecessor,
            include_pending_notices=include_pending_notices,
        )
        by_id = {item.item_id: item for item in self._storage.read_items(
            snapshot.session_id, snapshot.thread_id, checkpoint_ns=snapshot.checkpoint_ns,
            item_ids=selected_ids, snapshot=snapshot,
        )}
        return tuple(by_id[item_id] for item_id in selected_ids)

    def precompaction_context_items(
        self, snapshot: RolloutReadSnapshot,
    ) -> tuple[CanonicalItemRecord, ...]:
        """恢复刚被压缩的实际选集，包括尚未进入上一 assembly 的工具结果。"""
        pending = self._pending_context_epoch_row(snapshot.connection, snapshot.checkpoint_ns)
        if pending is None or pending[1] != "compaction":
            return ()
        row = snapshot.connection.execute(
            "SELECT parent_view_id FROM context_views WHERE view_id=? AND view_kind='compaction'",
            (pending[2],),
        ).fetchone()
        if row is None:
            raise RuntimeError("definition-restore-conflict: compaction 缺少真实 view")
        # parent 的有效成员加本次新提交成员，正是压缩前的选集；不能从完整
        # messages channel 或展示历史恢复早已压缩掉的发现。
        ids = tuple(row[0] for row in snapshot.connection.execute(
            "SELECT member.item_id FROM context_view_items member LEFT JOIN context_view_items parent "
            "ON parent.view_id=? AND parent.item_id=member.item_id "
            "WHERE member.view_id=? AND member.visible=1 AND member.source_kind='canonical' "
            "AND (parent.context_selected=1 AND parent.visible=1 OR parent.item_id IS NULL) "
            "ORDER BY member.logical_item_ordinal", (row[0], pending[2]),
        ).fetchall())
        return tuple(self._storage.read_items(
            snapshot.session_id, snapshot.thread_id, checkpoint_ns=snapshot.checkpoint_ns,
            item_ids=ids, snapshot=snapshot,
        ))

    def execution_context_item_ids(
        self, connection: sqlite3.Connection, *, session_id: str, thread_id: str,
        checkpoint_ns: str, view_id: str, include_pending_notices: bool = True,
        predecessor: ContextAssemblySnapshot | None,
    ) -> tuple[str, ...]:
        """Provider 与 LangGraph 执行读取共用 canonical/current admission selection。"""
        selected = connection.execute(
            "SELECT item_id, context_ordinal FROM context_view_items WHERE view_id=? "
            "AND visible=1 AND context_selected=1 AND source_kind='canonical' "
            "ORDER BY context_ordinal", (view_id,),
        ).fetchall()
        if any(type(ordinal) is not int or ordinal < 0 for _item_id, ordinal in selected):
            raise RuntimeError("active context selection 缺少合法顺序")
        excluded = self.execution_context_excluded_item_ids(
            connection, session_id=session_id, thread_id=thread_id, checkpoint_ns=checkpoint_ns,
        )
        item_ids = [item_id for item_id, _ordinal in selected if item_id not in excluded]
        admission = current_execution_admission(langgraph_thread_id=thread_id)
        if not include_pending_notices or admission is None:
            return tuple(item_ids)
        self._context.executions.execution_admission_cursor(
            admission, connection=connection, checkpoint_ns=checkpoint_ns,
        )
        pending_ids = []
        # 从唯一 applied assembly 携带精确前缀，再追加当前准入自己的
        # 输入、输出和 retry 来源；用户执行同样可能产生 Turn 外来源。
        if predecessor is not None and self._pending_context_epoch_row(connection, checkpoint_ns) is None:
            pending_ids.extend(entry.ref.ref_id for entry in predecessor.selection
                               if entry.included and entry.ref.ref_type == "canonical_item")
        pending_ids.extend(row[0] for row in connection.execute(
            "SELECT item.item_id FROM context_view_items member "
            "JOIN item_catalog item ON item.item_id=member.item_id "
            "WHERE member.view_id=? AND member.visible=1 AND member.context_selected=1 "
            "AND item.execution_id=? ORDER BY member.context_ordinal",
            (view_id, admission.execution_id),
        ).fetchall())
        known = set(item_ids)
        for raw_id in pending_ids:
            item_id = strict_text(raw_id, field="item_catalog.item_id")
            if item_id not in known and item_id not in excluded:
                item_ids.append(item_id)
                known.add(item_id)
        return tuple(item_ids)

    def selected_context_message_sequences(
        self,
        snapshot: RolloutReadSnapshot,
        *,
        items: tuple[CanonicalItemRecord, ...],
    ) -> list[int]:
        """执行状态、闭合校验与计划选择共用同一 typed carrier selection。"""
        view_id = self.active_view_id(snapshot)
        selected = {item.item_id for item in items} - superseded_item_ids(items)
        rows = snapshot.connection.execute(
            "SELECT member.item_id, message.message_sequence "
            "FROM context_view_items AS member "
            "JOIN item_catalog AS item ON item.item_id = member.item_id "
            "JOIN messages AS message ON message.jsonl_offset = item.jsonl_offset "
            "AND message.jsonl_length = item.jsonl_length "
            "WHERE member.view_id = ? AND member.visible = 1 AND member.context_selected=1 "
            "ORDER BY member.context_ordinal",
            (view_id,),
        ).fetchall()
        # backing locator 可以保留镜像，但不能把不属于可见 membership 的声明补入执行 selection。
        return list(dict.fromkeys(sequence for item_id, sequence in rows if item_id in selected))

    def committed_context_refs(
        self,
        snapshot: RolloutReadSnapshot,
        *,
        items: tuple[CanonicalItemRecord, ...],
    ) -> tuple[ContextRef, ...]:
        """从已提交 item 生成 Saver-owned canonical refs。"""
        replaced = superseded_canonical_message_item_ids(items)
        selected = tuple(item for item in items if item.item_id not in replaced)
        by_message_id: dict[str, CanonicalItemRecord] = {}
        for item in selected:
            identity = item.projection_identity
            message_id = identity.projection_message_id if identity is not None else None
            if message_id is None:
                continue
            if message_id in by_message_id:
                raise RuntimeError(f"canonical message selection 版本歧义: {message_id}")
            by_message_id[message_id] = item
        superseded_message_ids: set[str] = set()
        for item in selected:
            superseded = next(
                (
                    relation.target_id
                    for relation in item.provenance_relations
                    if relation.relation == "replaces"
                    and relation.target_kind == "projection_message"
                ),
                None,
            )
            if superseded is None:
                continue
            if not isinstance(superseded, str) or not superseded:
                raise RuntimeError(
                    f"canonical item typed replaces relation 非法: {item.item_id}"
                )
            previous = by_message_id.get(superseded)
            if previous is None:
                continue
            if previous.turn_id != item.turn_id:
                raise RuntimeError(
                    "canonical assistant supersedes 跨 Turn: "
                    f"{previous.item_id}->{item.item_id}"
                )
            if previous.item_sequence >= item.item_sequence:
                raise RuntimeError(
                    "canonical assistant supersedes 顺序非法: "
                    f"{previous.item_id}->{item.item_id}"
                )
            if superseded in superseded_message_ids:
                raise RuntimeError(f"canonical assistant 被多个消息替代: {superseded}")
            superseded_message_ids.add(superseded)
        session_id = snapshot.session_id
        thread_id = snapshot.thread_id
        return tuple(
            ContextRef.canonical_item(
                item,
                session_id=session_id,
                thread_id=thread_id,
            )
            for item in selected
            if (
                item.projection_identity.projection_message_id
                if item.projection_identity is not None
                else None
            )
            not in superseded_message_ids
        )

    def list_checkpoints(
        self,
        session_id: str,
        thread_id: str,
        checkpoint_ns: str | None,
        *,
        before_checkpoint_id: str | None,
        limit: int | None,
    ) -> list[RolloutCheckpointIndex]:
        session_id = strict_text(session_id, field="list_checkpoints.session_id")
        thread_id = strict_text(thread_id, field="list_checkpoints.thread_id")
        if checkpoint_ns is not None and not isinstance(checkpoint_ns, str):
            raise TypeError("list_checkpoints.checkpoint_ns 必须是字符串或 null")
        before_checkpoint_id = strict_optional_text(
            before_checkpoint_id, field="list_checkpoints.before_checkpoint_id"
        )
        if limit is not None and (
            not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0
        ):
            raise ValueError("checkpoint limit 必须是正整数或 null")
        try:
            with self._storage.open_read_snapshot(session_id, thread_id, checkpoint_ns) as snapshot:
                connection = snapshot.connection
                self._storage._require_v2_runtime(connection)
                before = (
                    connection.execute(
                        "SELECT cv.control_sequence FROM checkpoints AS c "
                        "LEFT JOIN context_views AS cv ON cv.view_id = c.view_id "
                        "WHERE c.checkpoint_id = ? AND (? IS NULL OR c.checkpoint_ns = ?)",
                        (before_checkpoint_id, checkpoint_ns, checkpoint_ns),
                    ).fetchone()
                    if before_checkpoint_id
                    else None
                )
                query = (
                    "SELECT c.checkpoint_id, c.checkpoint_ns, c.commit_id, c.message_sequence, "
                    "c.message_count, c.parent_checkpoint_id, c.view_id, c.branch_id, "
                    "c.checkpoint_version, c.checkpoint_timestamp, c.checkpoint_json, "
                    "c.metadata_json, c.versions_seen_type, c.versions_seen_blob, "
                    "c.pending_sends_type, c.pending_sends_blob, cv.control_sequence "
                    "FROM checkpoints AS c LEFT JOIN context_views AS cv ON cv.view_id = c.view_id "
                    "WHERE (? IS NULL OR c.checkpoint_ns = ?) AND c.status = 'active' "
                    "AND (? IS NULL OR cv.control_sequence < ?) ORDER BY cv.control_sequence DESC"
                    + (" LIMIT ?" if limit is not None else "")
                )
                before_control_sequence = (
                    strict_non_negative_int(before[0], field="checkpoint context_views.control_sequence")
                    if before is not None
                    else None
                )
                params: tuple[object, ...] = (
                    checkpoint_ns,
                    checkpoint_ns,
                    before_control_sequence,
                    before_control_sequence,
                ) + ((limit,) if limit is not None else ())
                rows = connection.execute(query, params).fetchall()
        except RolloutNotMaterializedError:
            return []
        # 仅 channel/metadata 变化的 checkpoint 可以共用 item commit，
        # 已有 checkpoint_created 的控制序才是其唯一创建顺序。
        for row in rows:
            strict_non_negative_int(
                row[-1], field="checkpoint context_views.control_sequence"
            )
        return [self._storage._checkpoint_index(row[:-1]) for row in rows]

    def checkpoint_values(
        self,
        session_id: str,
        thread_id: str,
        checkpoint_ns: str,
        index: RolloutCheckpointIndex,
        *,
        snapshot: RolloutReadSnapshot | None = None,
        context_view_id_override: str | None = None,
        include_execution_context: bool = False,
        requested_channels: frozenset[str] | None = None,
        message_protocol: bool = False,
    ) -> dict[str, object]:
        session_id = strict_text(session_id, field="checkpoint_values.session_id")
        strict_text(thread_id, field="checkpoint_values.thread_id")
        strict_text(
            checkpoint_ns, field="checkpoint_values.checkpoint_ns", allow_empty=True
        )
        if snapshot is not None and (
            snapshot.session_id != session_id
            or snapshot.thread_id != thread_id
            or snapshot.checkpoint_ns != checkpoint_ns
        ):
            raise ValueError("checkpoint_values snapshot 与目标 rollout 不一致")
        context_view_id_override = strict_optional_text(
            context_view_id_override, field="checkpoint_values.context_view_id_override"
        )

        def read(connection: sqlite3.Connection) -> dict[str, object]:
            self._storage._require_v2_runtime(connection)
            channel_filter = ""
            parameters: tuple[object, ...] = (index.checkpoint_id,)
            if requested_channels is not None:
                selected = sorted(requested_channels | {"messages"})
                channel_filter = f" AND channel_name IN ({','.join('?' for _ in selected)})"
                parameters += tuple(selected)
            rows = connection.execute(
                "SELECT channel_name, storage_kind, value_state, value_id, value_session_id, value_thread_id, channel_version, context_view_id FROM checkpoint_channels WHERE checkpoint_id = ?" + channel_filter,
                parameters,
            ).fetchall()
            values: dict[str, object] = {}
            has_messages = False
            for raw_row in rows:
                if len(raw_row) != 8:
                    raise RuntimeError("checkpoint_channels 行字段数非法")
                channel, storage_kind, state, value_id, value_session_id, value_thread_id, version, view_id = (
                    raw_row
                )
                channel = strict_text(channel, field="checkpoint_channels.channel_name")
                storage_kind = strict_text(
                    storage_kind, field="checkpoint_channels.storage_kind"
                )
                state = strict_text(state, field="checkpoint_channels.value_state")
                view_id = strict_optional_text(
                    view_id, field="checkpoint_channels.context_view_id"
                )
                if channel == "messages":
                    context_view_id = context_view_id_override or view_id
                    if (
                        storage_kind != "rollout_view"
                        or state not in {"view", "delta", "snapshot"}
                        or view_id is None
                        or any(value is not None for value in (value_id, value_session_id, value_thread_id))
                    ):
                        raise RuntimeError(
                            f"checkpoint messages view 指针非法: {index.checkpoint_id}"
                        )
                    self._require_checkpoint_messages_view(
                        connection, checkpoint_id=index.checkpoint_id,
                        history_view_id=index.view_id, branch_id=index.branch_id,
                        messages_view_id=view_id,
                    )
                    has_messages = True
                    if requested_channels is not None and channel not in requested_channels:
                        continue
                    if message_protocol and state == "delta" and not include_execution_context and context_view_id_override is None:
                        continue
                    selection_view_id = None
                    execution_view_id = None
                    if include_execution_context:
                        selection = connection.execute(
                            "SELECT branch.branch_id, branch.head_view_id "
                            "FROM checkpoint_namespace_state AS namespace "
                            "JOIN branches AS branch ON branch.branch_id = namespace.active_branch_id "
                            "WHERE namespace.checkpoint_ns = ?",
                            (checkpoint_ns,),
                        ).fetchone()
                        if selection is None:
                            raise RuntimeError("最新 checkpoint 缺少权威 active branch selection")
                        execution_view_id = strict_text(selection[1], field="branches.head_view_id")
                        if selection[0] != index.branch_id:
                            # rewind 等显式分支选择可继续引用旧 checkpoint；只截断
                            # 其冻结 channel，不把 active view 的 stream 消息重新注入。
                            selection_view_id = execution_view_id
                    additional_sequences: set[int] = set()
                    if include_execution_context:
                        # 根输入由 acceptance 证明；只有前序已终态执行的真实
                        # 工具组可补入新执行，不混读当前执行尚未 checkpoint 的输出。
                        roots = connection.execute(
                            "SELECT message.message_sequence, turn.session_id, turn.thread_id, "
                            "acceptance.turn_id, item.turn_id, acceptance.session_id, acceptance.thread_id, turn.turn_id "
                            "FROM checkpoint_namespace_state AS namespace "
                            "JOIN branches AS branch ON branch.branch_id = namespace.active_branch_id "
                            "JOIN context_view_items AS member ON member.view_id = branch.head_view_id AND member.visible = 1 AND member.context_selected=1 "
                            "JOIN item_catalog AS item ON item.item_id = member.item_id "
                            "LEFT JOIN turn_records AS turn ON turn.root_input_item_id = item.item_id "
                            "LEFT JOIN turn_acceptances AS acceptance ON acceptance.accepted_ingress_id = turn.accepted_ingress_id "
                            "LEFT JOIN messages AS message ON message.jsonl_offset = item.jsonl_offset AND message.jsonl_length = item.jsonl_length "
                            "WHERE namespace.checkpoint_ns = ? AND item.semantic_kind = 'user_input' AND item.turn_scope = 'turn_root' "
                            "ORDER BY member.context_ordinal",
                            (checkpoint_ns,),
                        ).fetchall()
                        for root in roots:
                            if (root[4] is None or root[3] != root[4] or root[7] != root[4]
                                or root[1:3] != root[5:7]):
                                raise RuntimeError(f"最新 checkpoint 根输入缺少同 owner 的权威 acceptance: {root!r}")
                            require_historical_scope(
                                connection, root[1], root[2], current_owner=(session_id, thread_id),
                            )
                            additional_sequences.add(strict_non_negative_int(root[0], field="accepted input messages.message_sequence"))
                        additional_sequences.update(self._preceding_terminal_tool_sequences(
                            connection, session_id=session_id, thread_id=thread_id,
                            checkpoint_ns=checkpoint_ns,
                        ))
                    values[channel] = self._messages_for_view(
                        session_id,
                        thread_id,
                        checkpoint_ns,
                        context_view_id,
                        connection=connection,
                        selection_view_id=selection_view_id,
                        execution_view_id=execution_view_id,
                        additional_message_sequences=additional_sequences,
                        # 冷查询读取冻结历史；只有真实执行准入才能按其私有
                        # notice selection 过滤，不能把无准入当成空选集。
                        execution_fenced=(include_execution_context and
                            current_execution_admission(langgraph_thread_id=thread_id) is not None),
                    )
                    if message_protocol and state in {"delta", "snapshot"}:
                        values[channel] = _DeltaSnapshot(values[channel])
                elif state == "present":
                    if storage_kind != "sqlite_value" or view_id is not None:
                        raise RuntimeError(
                            f"checkpoint channel BLOB 校验失败: {index.checkpoint_id}/{channel}"
                        )
                    frozen = read_state_value(
                        connection, value_id, channel=channel,
                        version=strict_text(version, field="checkpoint_channels.channel_version"),
                        checkpoint_ns=checkpoint_ns,
                        session_id=strict_text(value_session_id, field="checkpoint_channels.value_session_id"),
                        thread_id=strict_text(value_thread_id, field="checkpoint_channels.value_thread_id"),
                    )
                    values[channel] = self._storage.decode_value(frozen)
                elif state == "absent":
                    if storage_kind != "sqlite_value" or any(
                        value is not None for value in (value_id, value_session_id, value_thread_id, view_id)
                    ):
                        raise RuntimeError(
                            f"checkpoint absent channel 存储状态非法: {index.checkpoint_id}/{channel}"
                        )
                else:
                    raise RuntimeError(
                        f"checkpoint channel value_state 未知: {index.checkpoint_id}/{channel}"
                    )
            if not has_messages:
                raise RuntimeError(
                    f"checkpoint 缺少 messages channel: {index.checkpoint_id}"
                )
            return values

        with self._storage.read_connection(
            session_id, thread_id, checkpoint_ns, snapshot=snapshot,
        ) as connection:
            return read(connection)

    def load_checkpoint(
        self,
        session_id: str,
        thread_id: str,
        checkpoint_ns: str,
        index: RolloutCheckpointIndex,
        *,
        snapshot: RolloutReadSnapshot | None = None,
        context_view_id_override: str | None = None,
        include_execution_context: bool = False,
    ) -> Checkpoint:
        session_id = strict_text(session_id, field="load_checkpoint.session_id")
        strict_text(thread_id, field="load_checkpoint.thread_id")
        if snapshot is not None and (
            snapshot.session_id != session_id
            or snapshot.thread_id != thread_id
            or snapshot.checkpoint_ns != checkpoint_ns
        ):
            raise ValueError("load_checkpoint snapshot 与目标 rollout 不一致")
        if snapshot is None:
            with self._storage.open_read_snapshot(session_id, thread_id, checkpoint_ns) as owned:
                return self.load_checkpoint(
                    session_id, thread_id, checkpoint_ns, index, snapshot=owned,
                    context_view_id_override=context_view_id_override,
                    include_execution_context=include_execution_context,
                )
        checkpoint = json.loads(index.checkpoint_json)
        if not isinstance(checkpoint, dict):
            raise TypeError("checkpoint core JSON 必须是对象")
        checkpoint["channel_values"] = self.checkpoint_values(
            session_id,
            thread_id,
            checkpoint_ns,
            index,
            snapshot=snapshot,
            context_view_id_override=context_view_id_override,
            include_execution_context=include_execution_context,
            message_protocol=True,
        )
        checkpoint["channel_versions"] = {}
        checkpoint["updated_channels"] = []
        connection = self._storage._snapshot_connection(snapshot)
        message_state = connection.execute(
            "SELECT value_state FROM checkpoint_channels WHERE checkpoint_id=? AND channel_name='messages'",
            (index.checkpoint_id,),
        ).fetchone()
        if message_state == ("delta",):
            # latest overlay 虽返回完整 selection，也不能掩盖其已登记增量依赖损坏。
            delta_history_path(connection, index.checkpoint_id, checkpoint_ns=checkpoint_ns, channels=("messages",))
        rows = connection.execute(
            "SELECT channel_name, channel_version, updated_index FROM checkpoint_channels WHERE checkpoint_id = ? ORDER BY updated_index",
            (index.checkpoint_id,),
        ).fetchall()
        for raw_row in rows:
            if len(raw_row) != 3:
                raise RuntimeError("checkpoint_channels version 行字段数非法")
            name, version, updated_index = raw_row
            name = strict_text(name, field="checkpoint_channels.channel_name")
            version = strict_optional_text(
                version, field="checkpoint_channels.channel_version"
            )
            updated_index = strict_optional_non_negative_int(
                updated_index, field="checkpoint_channels.updated_index"
            )
            if version is not None:
                checkpoint["channel_versions"][name] = version
            if updated_index is not None:
                checkpoint["updated_channels"].append(name)
        checkpoint["versions_seen"] = self._storage.decode_value(
            (index.versions_seen_type, index.versions_seen_blob)
        )
        checkpoint["pending_sends"] = self._storage.decode_value(
            (index.pending_sends_type, index.pending_sends_blob)
        )
        return checkpoint

    def metadata(self, index: RolloutCheckpointIndex) -> CheckpointMetadata:
        value = json.loads(index.metadata_json)
        if not isinstance(value, dict):
            raise TypeError("checkpoint metadata JSON 必须是对象")
        return value
