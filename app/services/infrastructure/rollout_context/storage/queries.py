"""v2 checkpoint/context query owner。

本模块只提供已提交 SQLite view/catalog 的查询，不扫描 v1、不生成 LangChain
message，也不拥有 checkpoint 写入事实。
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

from langgraph.checkpoint.base import Checkpoint, CheckpointMetadata

from app.domain.itemized.records import CanonicalItemRecord
from app.domain.itemized.refs import ContextRef
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_optional_non_negative_int,
    strict_optional_text,
    strict_text,
)

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.storage.primitives import (
        RolloutCheckpointIndex,
        RolloutReadSnapshot,
    )


def _hash_bytes(value: bytes) -> str:
    import hashlib

    return hashlib.sha256(value).hexdigest()


class RolloutCheckpointQueriesMixin:
    """checkpoint header、active view 和已提交 context query owner。"""

    def latest_checkpoint(
        self,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str | None,
        *,
        snapshot: RolloutReadSnapshot | None = None,
    ) -> RolloutCheckpointIndex | None:
        thread_id = strict_text(thread_id, field="latest_checkpoint.thread_id")
        strict_text(
            checkpoint_ns, field="latest_checkpoint.checkpoint_ns", allow_empty=True
        )
        checkpoint_id = strict_optional_text(
            checkpoint_id, field="latest_checkpoint.checkpoint_id"
        )
        if snapshot is not None and (
            snapshot.thread_id != thread_id or snapshot.checkpoint_ns != checkpoint_ns
        ):
            raise ValueError("latest_checkpoint snapshot 与目标 rollout 不一致")

        def resolve_target_checkpoint_id(
            connection: sqlite3.Connection,
            requested_id: str | None,
        ) -> str | None:
            if requested_id is None:
                return None
            direct = connection.execute(
                "SELECT 1 FROM checkpoints WHERE checkpoint_id = ? AND checkpoint_ns = ? AND status = 'active'",
                (requested_id, checkpoint_ns),
            ).fetchone()
            if direct is not None:
                return requested_id
            # full_rollout_copy 的物理 checkpoint identity 是 target-local；
            # 这里只提供 source checkpoint 坐标的只读 alias，绝不把 source
            # ID 写回 target 表或作为新的幂等 identity。
            alias = connection.execute(
                "SELECT target_local_id FROM fork_identity_mappings WHERE target_session_id = ? AND entity_type = 'checkpoint' AND source_local_id = ? ORDER BY created_at DESC LIMIT 1",
                (thread_id, requested_id),
            ).fetchone()
            return (
                strict_text(alias[0], field="fork_identity_mappings.target_local_id")
                if alias is not None
                else requested_id
            )

        if snapshot is not None:
            connection = self._snapshot_connection(snapshot)
            self._require_v2_runtime(connection)
            row = self._checkpoint_row(
                connection,
                checkpoint_ns,
                resolve_target_checkpoint_id(connection, checkpoint_id),
            )
            return self._checkpoint_index(row) if row else None
        if snapshot is None:
            self.initialize(thread_id, checkpoint_ns)
        with self._connect(thread_id, checkpoint_ns, read_only=True) as connection:
            self._require_v2_runtime(connection)
            row = self._checkpoint_row(
                connection,
                checkpoint_ns,
                resolve_target_checkpoint_id(connection, checkpoint_id),
            )
        return self._checkpoint_index(row) if row else None

    def active_view_id(
        self,
        snapshot: RolloutReadSnapshot,
    ) -> str | None:
        connection = self._snapshot_connection(snapshot)
        self._require_v2_runtime(connection)
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
        include_pending_notices: bool = True,
    ) -> tuple[CanonicalItemRecord, ...]:
        """返回当前已提交 view 的 item；不从 JSONL 物理顺序推导上下文。

        active view 是历史 selection 的权威来源。pending runtime notice 不属于
        view membership，但可以作为下一次 assembly 的显式 pending reference，
        因此在调用方选择 ``include_pending_notices`` 时按 catalog 顺序追加。
        """
        view_id = self.active_view_id(snapshot)
        if view_id is None:
            # v2 初始化会先建立 active view，acceptance/stream sink 只会补充
            # 该 view 的 membership。没有 active view 就无法知道当前 branch 的
            # selection；回退到 item_catalog 的物理顺序会把其它 branch/ambient
            # item 冒充当前上下文，属于第二套事实源，必须 fail-closed。
            raise RuntimeError(
                "rollout active view 缺失，拒绝从 item_catalog 物理顺序推导 context"
            )
        items = self.read_items_for_view(snapshot, view_id)
        if not include_pending_notices:
            return tuple(items)
        connection = self._snapshot_connection(snapshot)
        pending_rows = connection.execute(
            "SELECT item_id FROM item_catalog WHERE turn_scope = 'pending_next_turn' AND turn_id IS NULL ORDER BY item_sequence"
        ).fetchall()
        known = {item.item_id for item in items}
        pending_ids = []
        for row in pending_rows:
            item_id = strict_text(row[0], field="item_catalog.item_id")
            if item_id not in known:
                pending_ids.append(item_id)
        if pending_ids:
            items.extend(
                self.read_items(
                    snapshot.thread_id,
                    checkpoint_ns=snapshot.checkpoint_ns,
                    item_ids=pending_ids,
                    snapshot=snapshot,
                )
            )
        return tuple(items)

    def committed_context_refs(
        self,
        snapshot: RolloutReadSnapshot,
        *,
        include_pending_notices: bool = True,
    ) -> tuple[ContextRef, ...]:
        """从已提交 item 生成 Saver-owned canonical refs。"""
        return tuple(
            ContextRef.canonical_item(item, session_id=snapshot.thread_id)
            for item in self.committed_context_items(
                snapshot,
                include_pending_notices=include_pending_notices,
            )
        )

    def list_checkpoints(
        self,
        thread_id: str,
        checkpoint_ns: str | None,
        *,
        before_checkpoint_id: str | None,
        limit: int | None,
    ) -> list[RolloutCheckpointIndex]:
        thread_id = strict_text(thread_id, field="list_checkpoints.thread_id")
        if checkpoint_ns is not None and not isinstance(checkpoint_ns, str):
            raise TypeError("list_checkpoints.checkpoint_ns 必须是字符串或 null")
        checkpoint_ns = checkpoint_ns or ""
        before_checkpoint_id = strict_optional_text(
            before_checkpoint_id, field="list_checkpoints.before_checkpoint_id"
        )
        if limit is not None and (
            not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0
        ):
            raise ValueError("checkpoint limit 必须是正整数或 null")
        self.initialize(thread_id, checkpoint_ns)
        with self._connect(thread_id, checkpoint_ns, read_only=True) as connection:
            self._require_v2_runtime(connection)
            before = (
                connection.execute(
                    "SELECT commit_id FROM checkpoints WHERE checkpoint_id = ? AND checkpoint_ns = ?",
                    (before_checkpoint_id, checkpoint_ns),
                ).fetchone()
                if before_checkpoint_id
                else None
            )
            query = (
                "SELECT checkpoint_id, checkpoint_ns, commit_id, message_sequence, message_count, parent_checkpoint_id, view_id, branch_id, checkpoint_version, checkpoint_timestamp, checkpoint_json, metadata_json, versions_seen_type, versions_seen_blob, pending_sends_type, pending_sends_blob FROM checkpoints WHERE checkpoint_ns = ? AND status = 'active' AND (? IS NULL OR commit_id < ?) ORDER BY commit_id DESC"
                + (" LIMIT ?" if limit is not None else "")
            )
            before_commit_id = (
                strict_non_negative_int(before[0], field="checkpoints.commit_id")
                if before is not None
                else None
            )
            params: tuple[object, ...] = (
                checkpoint_ns,
                before_commit_id,
                before_commit_id,
            ) + ((limit,) if limit is not None else ())
            rows = connection.execute(query, params).fetchall()
        return [self._checkpoint_index(row) for row in rows]

    def checkpoint_values(
        self,
        thread_id: str,
        checkpoint_ns: str,
        index: RolloutCheckpointIndex,
        *,
        snapshot: RolloutReadSnapshot | None = None,
        context_view_id_override: str | None = None,
    ) -> dict[str, object]:
        strict_text(thread_id, field="checkpoint_values.thread_id")
        strict_text(
            checkpoint_ns, field="checkpoint_values.checkpoint_ns", allow_empty=True
        )
        context_view_id_override = strict_optional_text(
            context_view_id_override, field="checkpoint_values.context_view_id_override"
        )

        def read(connection: sqlite3.Connection) -> dict[str, object]:
            self._require_v2_runtime(connection)
            rows = connection.execute(
                "SELECT channel_name, storage_kind, value_state, serializer_name, value_blob, value_hash, context_view_id FROM checkpoint_channels WHERE checkpoint_id = ?",
                (index.checkpoint_id,),
            ).fetchall()
            values: dict[str, object] = {}
            has_messages = False
            for raw_row in rows:
                if len(raw_row) != 7:
                    raise RuntimeError("checkpoint_channels 行字段数非法")
                channel, storage_kind, state, serializer, blob, digest, view_id = (
                    raw_row
                )
                channel = strict_text(channel, field="checkpoint_channels.channel_name")
                storage_kind = strict_text(
                    storage_kind, field="checkpoint_channels.storage_kind"
                )
                state = strict_text(state, field="checkpoint_channels.value_state")
                serializer = strict_optional_text(
                    serializer, field="checkpoint_channels.serializer_name"
                )
                digest = strict_optional_text(
                    digest, field="checkpoint_channels.value_hash"
                )
                view_id = strict_optional_text(
                    view_id, field="checkpoint_channels.context_view_id"
                )
                if channel == "messages":
                    context_view_id = context_view_id_override or index.view_id
                    if (
                        storage_kind != "rollout_view"
                        or state != "view"
                        or view_id != index.view_id
                    ):
                        raise RuntimeError(
                            f"checkpoint messages view 指针非法: {index.checkpoint_id}"
                        )
                    values[channel] = self._messages_for_view(
                        thread_id,
                        checkpoint_ns,
                        context_view_id,
                        connection=connection,
                    )
                    has_messages = True
                elif state == "present":
                    if (
                        not isinstance(blob, bytes)
                        or serializer is None
                        or digest is None
                        or _hash_bytes(blob) != digest
                    ):
                        raise RuntimeError(
                            f"checkpoint channel BLOB 校验失败: {index.checkpoint_id}/{channel}"
                        )
                    values[channel] = self.decode_value((serializer, blob))
                elif state != "absent":
                    raise RuntimeError(
                        f"checkpoint channel value_state 未知: {index.checkpoint_id}/{channel}"
                    )
            if not has_messages:
                raise RuntimeError(
                    f"checkpoint 缺少 messages channel: {index.checkpoint_id}"
                )
            return values

        if snapshot is not None:
            return read(self._snapshot_connection(snapshot))
        with self._connect(thread_id, checkpoint_ns) as connection:
            return read(connection)

    def load_checkpoint(
        self,
        thread_id: str,
        checkpoint_ns: str,
        index: RolloutCheckpointIndex,
        *,
        snapshot: RolloutReadSnapshot | None = None,
        context_view_id_override: str | None = None,
    ) -> Checkpoint:
        checkpoint = json.loads(index.checkpoint_json)
        if not isinstance(checkpoint, dict):
            raise TypeError("checkpoint core JSON 必须是对象")
        checkpoint["channel_values"] = self.checkpoint_values(
            thread_id,
            checkpoint_ns,
            index,
            snapshot=snapshot,
            context_view_id_override=context_view_id_override,
        )
        checkpoint["channel_versions"] = {}
        checkpoint["updated_channels"] = []
        if snapshot is None:
            with self._connect(thread_id, checkpoint_ns) as connection:
                self._require_v2_runtime(connection)
                rows = connection.execute(
                    "SELECT channel_name, channel_version, updated_index FROM checkpoint_channels WHERE checkpoint_id = ? ORDER BY updated_index",
                    (index.checkpoint_id,),
                ).fetchall()
        else:
            connection = self._snapshot_connection(snapshot)
            self._require_v2_runtime(connection)
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
        checkpoint["versions_seen"] = self.decode_value(
            (index.versions_seen_type, index.versions_seen_blob)
        )
        checkpoint["pending_sends"] = self.decode_value(
            (index.pending_sends_type, index.pending_sends_blob)
        )
        return checkpoint

    def metadata(self, index: RolloutCheckpointIndex) -> CheckpointMetadata:
        value = json.loads(index.metadata_json)
        if not isinstance(value, dict):
            raise TypeError("checkpoint metadata JSON 必须是对象")
        return value
