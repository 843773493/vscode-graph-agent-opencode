"""v2 checkpoint message materialization and view-chain validation owner。

本模块只从已经提交的 v2 item/catalog/view 读取并校验，不推断 v1 物理
邻接，也不把 LangChain message 作为持久化事实。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING

from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.storage.catalog.message_groups import (
    read_message_group,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_optional_non_negative_int,
    strict_optional_text,
    strict_text,
)
from app.services.mapping.itemized.carrier_dedup import superseded_stream_item_ids

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.storage.primitives import (
        RolloutReadSnapshot,
    )


class RolloutMessageMaterializerMixin:
    """消息 locator、canonical item 读取和 context view chain 校验 owner。"""

    def _read_messages(
        self,
        session_id: str,
        thread_id: str,
        checkpoint_ns: str,
        sequences: Iterable[int],
        *,
        connection: sqlite3.Connection | None = None,
    ) -> list[dict[str, object]]:
        session_id = strict_text(session_id, field="_read_messages.session_id")
        thread_id = strict_text(thread_id, field="_read_messages.thread_id")
        sequence_values = tuple(
            dict.fromkeys(
                strict_non_negative_int(value, field="message_sequence")
                for value in sequences
            )
        )
        if not sequence_values:
            return []
        values: list[tuple[tuple[CanonicalItemRecord, ...], dict[str, object]]] = []
        if connection is None:
            with self._storage.read_connection(session_id, thread_id, checkpoint_ns) as owned_connection:
                return self._read_messages(
                    session_id,
                    thread_id,
                    checkpoint_ns,
                    sequence_values,
                    connection=owned_connection,
                )
        rows = connection.execute(
            "SELECT m.message_sequence, m.message_id, m.turn_id, m.created_at, "
            "m.jsonl_offset, m.jsonl_length, f.source_session_id FROM messages AS m "
            "LEFT JOIN fork_identity_mappings AS f ON f.target_session_id = ? "
            "AND f.entity_type = 'message' AND f.target_local_id = m.message_id "
            "WHERE m.message_sequence IN ("
            + ",".join("?" for _ in sequence_values)
            + ") ORDER BY m.message_sequence",
            (session_id, *sequence_values),
        ).fetchall()
        found_sequences: set[int] = set()
        with self._storage.jsonl_path(
            session_id,
            checkpoint_ns,
            thread_id=thread_id,
        ).open("rb") as stream:
            for (
                sequence_value,
                message_id,
                turn_id,
                created_at,
                offset_value,
                length_value,
                fork_source_session_id,
            ) in rows:
                sequence = strict_non_negative_int(
                    sequence_value,
                    field="SQLite message_sequence",
                )
                if sequence in found_sequences:
                    raise RuntimeError(
                        f"SQLite messages 存在重复 message_sequence: {sequence}"
                    )
                found_sequences.add(sequence)
                message_id = strict_text(message_id, field="messages.message_id")
                turn_id = strict_optional_text(turn_id, field="messages.turn_id")
                created_at = strict_text(
                    created_at, field="messages.created_at"
                )
                offset = strict_non_negative_int(
                    offset_value,
                    field=f"SQLite message offset: {message_id}",
                )
                length = strict_non_negative_int(
                    length_value,
                    field=f"SQLite message length: {message_id}",
                )
                if length == 0:
                    raise RuntimeError(
                        f"SQLite message length 必须大于 0: {message_id}"
                    )
                items = read_message_group(
                    connection,
                    stream,
                    offset=offset,
                    length=length,
                    read_item=self._storage._read_v2_item_at,
                )
                item = items[-1]
                message = self._storage._codec().project_message(items)
                item_message_id = (item.projection_identity.projection_message_id if item.projection_identity is not None else None)
                item_message_id = strict_optional_text(
                    item_message_id,
                    field="canonical item projection_message_id",
                )
                if item_message_id is not None and item_message_id != message_id:
                    raise RuntimeError(
                        "v2 item 与 SQLite message identity 不一致: "
                        f"message={message_id}, item={item.item_id}"
                    )
                values.append(
                    (tuple(items), self._hydrate_message_identity(
                        message,
                        message_id=message_id,
                        turn_id=turn_id,
                        created_at=created_at,
                        fork_source_session_id=strict_optional_text(
                            fork_source_session_id,
                            field="fork_identity_mappings.source_session_id",
                        ),
                    ))
                )
        missing_sequences = tuple(
            sequence for sequence in sequence_values if sequence not in found_sequences
        )
        if missing_sequences:
            raise RuntimeError(
                "SQLite messages 缺少请求的 message_sequence: "
                + ",".join(str(sequence) for sequence in missing_sequences)
            )
        # 私有 checkpoint 恢复也复用 typed carrier 去重；已被完整 checkpoint
        # carrier 替代的 stream 影子不得再次成为 LangGraph 工具调用输入。
        superseded = superseded_stream_item_ids(tuple(
            item for group, _value in values for item in group
        ))
        return [value for group, value in values
                if not all(item.item_id in superseded for item in group)]




    @staticmethod
    def _hydrate_message_identity(
        message: Mapping[str, object],
        *,
        message_id: str,
        turn_id: str | None,
        created_at: str,
        fork_source_session_id: str | None = None,
    ) -> dict[str, object]:
        """把 SQLite 的 canonical identity 回填到 LangChain 消息。

        JSONL 只保存模型消息原文，原文不要求把业务层的 message_id、Turn
        和持久化时间重复写入 response_metadata。SQLite 是这些字段的权威
        来源；读取 checkpoint、Replay 和 Fork 时必须把它们恢复到
        BaseMessage，否则同一条消息在 Web DTO 和 replay 定位中会失去身份。
        fork 来源只取 target message 的唯一 fork_identity_mappings 事实。
        该回填只发生在内存中的反序列化副本，不改变 JSONL canonical 内容。
        """
        hydrated = dict(message)
        raw_data = hydrated.get("data")
        if not isinstance(raw_data, Mapping):
            raise TypeError("rollout 消息 data 必须是对象")
        data = dict(raw_data)
        raw_metadata = data.get("response_metadata")
        metadata = dict(raw_metadata) if isinstance(raw_metadata, Mapping) else {}
        if fork_source_session_id is not None:
            metadata["context_fork_source_session_id"] = fork_source_session_id
        metadata["message_id"] = message_id
        metadata["created_at"] = created_at
        metadata["updated_at"] = created_at
        raw_message_metadata = metadata.get("message_metadata")
        message_metadata = (
            dict(raw_message_metadata)
            if isinstance(raw_message_metadata, Mapping)
            else {}
        )
        if turn_id is None:
            message_metadata.pop("turn_id", None)
        else:
            message_metadata["turn_id"] = turn_id
        message_metadata.pop("job_id", None)
        metadata["message_metadata"] = message_metadata
        data["response_metadata"] = metadata
        data["id"] = message_id
        hydrated["data"] = data
        return hydrated


    def materialize_messages(
        self,
        session_id: str,
        thread_id: str,
        checkpoint_ns: str,
        message_sequence: int,
        *,
        snapshot: RolloutReadSnapshot | None = None,
        context_ranges: Iterable[tuple[str, int, int]] | None = None,
    ) -> list[object]:
        session_id = strict_text(session_id, field="materialize_messages.session_id")
        thread_id = strict_text(thread_id, field="materialize_messages.thread_id")
        with self._storage.read_connection(
            session_id, thread_id, checkpoint_ns, snapshot=snapshot,
        ) as connection:
            ranges = tuple(context_ranges or ())
            if ranges:
                sequences = self._storage._view_message_sequences(
                    thread_id,
                    checkpoint_ns,
                    ranges[-1][0],
                    set(),
                    connection=connection,
                )
            else:
                row = self._checkpoint_row(connection, checkpoint_ns, None)
                sequences = (
                    self._storage._view_message_sequences(
                        thread_id,
                        checkpoint_ns,
                        strict_text(row[6], field="checkpoints.view_id"),
                        set(),
                        connection=connection,
                    )
                    if row
                    else []
                )
            return [
                self._storage._codec().from_dict(value)
                for value in self._read_messages(
                    session_id,
                    thread_id,
                    checkpoint_ns,
                    sequences,
                    connection=connection,
                )
            ]


    def resolve_context_chain_ranges(
        self, snapshot: RolloutReadSnapshot, message_sequence: int
    ) -> tuple[tuple[str, int, int], ...]:
        connection = self._storage._snapshot_connection(snapshot)
        self._storage._require_v2_runtime(connection)
        row = connection.execute(
            "SELECT c.view_id, c.message_sequence, cv.control_sequence "
            "FROM checkpoints AS c LEFT JOIN context_views AS cv ON cv.view_id = c.view_id "
            "WHERE c.checkpoint_ns = ? AND c.message_sequence = ? "
            "ORDER BY cv.control_sequence DESC LIMIT 1",
            (snapshot.checkpoint_ns, message_sequence),
        ).fetchone()
        if row is not None:
            strict_non_negative_int(
                row[2], field="checkpoint context_views.control_sequence"
            )
        if row is None:
            row = connection.execute(
                "SELECT cv.view_id, cv.head_message_sequence "
                "FROM context_views AS cv "
                "JOIN branches AS b ON b.head_view_id = cv.view_id "
                "WHERE b.branch_id = ? AND b.status = 'active' LIMIT 1",
                (snapshot.manifest.active_branch_id,),
            ).fetchone()
        if row is None or row[0] is None:
            return ()
        view_id = strict_text(row[0], field="context_views.view_id")
        head_sequence = strict_non_negative_int(
            row[1], field="context_views.head_message_sequence"
        )
        self._validate_context_view_chain_connection(connection, view_id)
        return ((view_id, 0, head_sequence),)


    def validate_context_view_chain(
        self,
        thread_id: str,
        checkpoint_ns: str,
        view_id: str,
    ) -> None:
        """校验 context view 父链、jump table 和消息范围，发现损坏立即失败。"""
        session_id = self._storage._session_id_from_owner_scope(thread_id)
        with self._storage.read_connection(session_id, thread_id, checkpoint_ns) as connection:
            self._validate_context_view_chain_connection(connection, view_id)


    def _validate_context_view_chain_connection(
        self,
        connection: sqlite3.Connection,
        view_id: str,
    ) -> None:
        rows = connection.execute(
            "SELECT view_id, parent_view_id FROM context_views"
        ).fetchall()
        parents = {
            strict_text(row[0], field="context_views.view_id"): strict_optional_text(
                row[1], field="context_views.parent_view_id"
            )
            for row in rows
        }
        if view_id not in parents:
            raise RuntimeError(f"context view 不存在: {view_id}")
        current = view_id
        visited: set[str] = set()
        while current is not None:
            if current in visited:
                raise RuntimeError(f"context view 父链成环: {view_id}")
            visited.add(current)
            parent = parents[current]
            if parent is not None and parent not in parents:
                raise RuntimeError(
                    f"context view 引用不存在的 parent: {current} -> {parent}"
                )
            current = parent

        jumps = connection.execute(
            "SELECT jump_level, ancestor_view_id, ancestor_depth FROM context_view_jumps WHERE view_id = ? ORDER BY jump_level",
            (view_id,),
        ).fetchall()
        for level, ancestor_view_id, ancestor_depth in jumps:
            level = strict_non_negative_int(
                level, field="context_view_jumps.jump_level"
            )
            ancestor_view_id = strict_text(
                ancestor_view_id,
                field="context_view_jumps.ancestor_view_id",
            )
            ancestor_depth = strict_non_negative_int(
                ancestor_depth,
                field="context_view_jumps.ancestor_depth",
            )
            expected = view_id
            for _ in range(2**level):
                parent = parents.get(expected)
                if parent is None:
                    raise RuntimeError(
                        f"context view jump 越界: view={view_id}, level={level}"
                    )
                expected = parent
            if expected != ancestor_view_id or ancestor_depth != 2**level:
                raise RuntimeError(
                    f"context view jump 非法: view={view_id}, level={level}"
                )

        ranges = connection.execute(
            "SELECT source_kind, start_message_sequence, end_message_sequence FROM context_view_ranges WHERE view_id = ? ORDER BY range_index",
            (view_id,),
        ).fetchall()
        for source_kind, start, end in ranges:
            source_kind = strict_text(
                source_kind, field="context_view_ranges.source_kind"
            )
            start = strict_optional_non_negative_int(
                start,
                field="context_view_ranges.start_message_sequence",
            )
            end = strict_optional_non_negative_int(
                end,
                field="context_view_ranges.end_message_sequence",
            )
            if source_kind != "messages" or start is None or end is None:
                continue
            expected_count = 1 if start == end else 2
            present_count = strict_non_negative_int(
                connection.execute(
                    "SELECT COUNT(*) FROM messages WHERE message_sequence IN (?, ?)",
                    (start, end),
                ).fetchone()[0],
                field="messages.count",
            )
            if present_count != expected_count:
                raise RuntimeError(
                    f"context view range 引用不存在消息: view={view_id}, start={start}, end={end}"
                )

    def decode_indexed_message(
        self, value: object, *, summary_only: bool = False
    ) -> object:
        del summary_only
        if not isinstance(value, dict):
            raise TypeError("rollout message 必须是对象")
        return self._storage._codec().from_dict(value)
