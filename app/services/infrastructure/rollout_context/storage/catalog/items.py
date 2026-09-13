"""v2 canonical catalog 写入与按 SQLite locator 读取。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from app.domain.itemized.enums import (
    CommitKind,
    CommitMode,
    ControlOutcome,
    SemanticKind,
)
from app.domain.itemized.errors import FormatDispatchError, ItemSchemaError
from app.domain.itemized.hashing import (
    canonical_json_bytes,
    payload_content_length,
    sha256_jcs,
)
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.storage.catalog.message_groups import (
    read_message_group,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_line as _v2_json_line,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    validate_item_storage_metadata,
)

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.storage.primitives import (
        RolloutReadSnapshot,
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _strict_non_negative_int(value: object, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RuntimeError(f"{field} 必须是非负整数")
    return value


def _strict_db_text(value: object, *, field: str) -> str:
    """读取 SQLite 文本列，不把损坏值强转成可比较的字符串。"""
    if not isinstance(value, str):
        raise TypeError(f"{field} 必须是字符串")
    return value


def _strict_db_optional_text(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    return _strict_db_text(value, field=field)


def _comparable_tool_result_payload(value: object) -> object:
    """提取工具结果正文，排除每个 producer 独有的生命周期 ID。"""

    if not isinstance(value, Mapping):
        return value
    # on_tool_end 与下一次 model call 的 ToolMessage 可能分别生成 canonical
    # result。result_id、tool invocation/attempt ID 是 producer 生命周期身份，
    # 不是用户可见结果正文；同一 tool_call_id 的这些字段不同不应制造冲突。
    return {
        key: item
        for key, item in value.items()
        if key not in {"result_id", "tool_invocation_id", "tool_attempt_id"}
    }


class RolloutItemsMixin:
    """只负责 canonical item 的 JSONL/catalog transaction 与定位读取。"""

    def _insert_canonical_item(
        self,
        connection: sqlite3.Connection,
        item: CanonicalItemRecord,
        *,
        commit_id: int,
        jsonl_offset: int,
        jsonl_length: int,
        created_at: str,
    ) -> None:
        """写入唯一 canonical catalog，并生成对应 item projection。"""
        item.validate()
        source_revision, part_ordinal = validate_item_storage_metadata(item)
        if (
            not isinstance(commit_id, int)
            or isinstance(commit_id, bool)
            or commit_id < 0
        ):
            raise RuntimeError("item_catalog.commit_id 必须是非负整数")
        if (
            not isinstance(jsonl_offset, int)
            or isinstance(jsonl_offset, bool)
            or jsonl_offset < 0
        ):
            raise RuntimeError("item_catalog.jsonl_offset 必须是非负整数")
        if (
            not isinstance(jsonl_length, int)
            or isinstance(jsonl_length, bool)
            or jsonl_length <= 0
        ):
            raise RuntimeError("item_catalog.jsonl_length 必须是正整数")
        connection.execute(
            "INSERT INTO item_catalog(item_sequence, item_id, semantic_kind, "
            "payload_kind, status, turn_id, turn_scope, message_group_id, "
            "wire_role, producer_ref_json, payload_length, source_revision, "
            "content_hash, jsonl_offset, jsonl_length, commit_id, "
            "operation_anchor_capable, searchable, created_at, metadata_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item.item_sequence,
                item.item_id,
                item.semantic_kind,
                item.payload_kind,
                item.status,
                item.turn_id,
                item.turn_scope,
                item.message_group_id,
                item.wire_role,
                _json(item.producer_ref),
                payload_content_length(item.payload_kind, item.payload),
                source_revision or f"canonical:{item.item_id}:{item.content_hash}",
                item.content_hash,
                jsonl_offset,
                jsonl_length,
                commit_id,
                int(
                    item.semantic_kind
                    in {
                        SemanticKind.TOOL_CALL,
                        SemanticKind.TOOL_RESULT,
                        SemanticKind.COMPACTION_SUMMARY,
                    }
                ),
                int(
                    item.semantic_kind
                    in {SemanticKind.USER_INPUT, SemanticKind.ASSISTANT_OUTPUT}
                ),
                item.created_at,
                _json(item.metadata),
            ),
        )
        self._insert_item_projection(connection, item, created_at)
        block_id = item.metadata.get("block_id")
        if block_id is not None:
            locator = {
                "json_pointer": "/payload",
                "encoding": "jcs:v1",
                "offset": 0,
                "length": payload_content_length(item.payload_kind, item.payload),
            }
            connection.execute(
                "INSERT INTO item_parts(item_id, part_id, part_ordinal, "
                "part_semantic_kind, content_prefix_hash, content_hash, "
                "locator_json, line_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.item_id,
                    block_id,
                    part_ordinal,
                    item.semantic_kind,
                    None,
                    item.content_hash,
                    _json(locator),
                    _hash_bytes(_v2_json_line(item.to_dict())),
                    item.created_at,
                ),
            )

    def append_item(
        self,
        thread_id: str,
        item: CanonicalItemRecord,
        *,
        checkpoint_ns: str = "",
        commit_kind: str = CommitKind.ITEM_CONVERGENCE,
    ) -> int:
        """追加一个 immutable canonical item，并返回 storage commit id。"""
        return self.append_items(
            thread_id,
            (item,),
            checkpoint_ns=checkpoint_ns,
            commit_kind=commit_kind,
        )[0]

    def append_items(
        self,
        thread_id: str,
        items: Sequence[CanonicalItemRecord],
        *,
        checkpoint_ns: str = "",
        commit_kind: str = CommitKind.ITEM_CONVERGENCE,
    ) -> tuple[int, ...]:
        """原子追加一组 canonical item，并为流式 block 分配连续序号。

        provider block 的 item identity 在内存中先确定，物理 ``item_sequence``
        则由 session owner 在提交时分配。这样同一 model call 的多个 block
        能落在一个 item-bearing commit 中，重试同一批 identity 时保持幂等，
        同时不允许调用方伪造或跳过 session 的全局序号。
        """
        if not items:
            raise ValueError("append_items 至少需要一个 canonical item")
        with self._lock(thread_id, checkpoint_ns):
            self.initialize(thread_id, checkpoint_ns)
            with self._connect(thread_id, checkpoint_ns) as connection:
                self._require_v2_runtime(connection)
                existing_commit_ids: list[int] = []
                resolved_item_ids: list[str] = []
                pending: list[CanonicalItemRecord] = []
                for item in items:
                    item.validate()
                    existing = connection.execute(
                        "SELECT semantic_kind, payload_kind, status, turn_id, turn_scope, "
                        "message_group_id, wire_role, producer_ref_json, content_hash, "
                        "created_at, metadata_json, commit_id FROM item_catalog "
                        "WHERE item_id = ?",
                        (item.item_id,),
                    ).fetchone()
                    if existing is None:
                        if item.semantic_kind == SemanticKind.TOOL_RESULT:
                            payload = (
                                item.payload
                                if isinstance(item.payload, Mapping)
                                else {}
                            )
                            tool_call_id = payload.get("tool_call_id")
                            if isinstance(tool_call_id, str) and tool_call_id:
                                existing_result = connection.execute(
                                    "SELECT item_id, content_hash, commit_id, jsonl_offset, jsonl_length FROM item_catalog "
                                    "WHERE semantic_kind = 'tool_result' "
                                    "AND json_extract(metadata_json, '$.tool_call_id') = ? "
                                    "ORDER BY item_sequence LIMIT 1",
                                    (tool_call_id,),
                                ).fetchone()
                                if existing_result is not None:
                                    result_item_id = _strict_db_text(
                                        existing_result[0],
                                        field="item_catalog.item_id",
                                    )
                                    result_offset = _strict_non_negative_int(
                                        existing_result[3],
                                        field="item_catalog.jsonl_offset",
                                    )
                                    result_length = _strict_non_negative_int(
                                        existing_result[4],
                                        field="item_catalog.jsonl_length",
                                    )
                                    result_commit_id = _strict_non_negative_int(
                                        existing_result[2],
                                        field="item_catalog.commit_id",
                                    )
                                    if result_length == 0:
                                        raise RuntimeError(
                                            "既有 tool_result canonical locator 长度非法: "
                                            f"{result_item_id}"
                                        )
                                    existing_payload: object | None = None
                                    result_path = self.jsonl_path(
                                        thread_id,
                                        checkpoint_ns,
                                    )
                                    raw_result = result_path.read_bytes()[
                                        result_offset : result_offset + result_length
                                    ]
                                    try:
                                        existing_envelope = json.loads(
                                            raw_result.decode("utf-8")
                                        )
                                    except (
                                        UnicodeDecodeError,
                                        json.JSONDecodeError,
                                    ) as error:
                                        raise RuntimeError(
                                            "既有 tool_result canonical envelope 无法读取: "
                                            f"{result_item_id}"
                                        ) from error
                                    if isinstance(existing_envelope, Mapping):
                                        existing_payload = existing_envelope.get(
                                            "payload"
                                        )
                                    comparable_existing = _comparable_tool_result_payload(
                                        existing_payload
                                    )
                                    comparable_new = _comparable_tool_result_payload(
                                        item.payload
                                    )
                                    if comparable_existing != comparable_new:
                                        raise ItemSchemaError(
                                            "同一 tool_call_id 的 tool_result 正文发生变化: "
                                            f"{tool_call_id}"
                                        )
                                    existing_commit_ids.append(result_commit_id)
                                    resolved_item_ids.append(result_item_id)
                                    continue
                        pending.append(item)
                        continue
                    stored_identity = (
                        _strict_db_text(
                            existing[0], field="item_catalog.semantic_kind"
                        ),
                        _strict_db_text(existing[1], field="item_catalog.payload_kind"),
                        _strict_db_text(existing[2], field="item_catalog.status"),
                        _strict_db_optional_text(
                            existing[3], field="item_catalog.turn_id"
                        ),
                        _strict_db_optional_text(
                            existing[4], field="item_catalog.turn_scope"
                        ),
                        _strict_db_optional_text(
                            existing[5], field="item_catalog.message_group_id"
                        ),
                        _strict_db_optional_text(
                            existing[6], field="item_catalog.wire_role"
                        ),
                        _strict_db_text(
                            existing[7], field="item_catalog.producer_ref_json"
                        ),
                        _strict_db_text(existing[8], field="item_catalog.content_hash"),
                        _strict_db_text(existing[9], field="item_catalog.created_at"),
                        _strict_db_text(
                            existing[10], field="item_catalog.metadata_json"
                        ),
                    )
                    incoming_identity = (
                        item.semantic_kind,
                        item.payload_kind,
                        item.status,
                        item.turn_id,
                        item.turn_scope,
                        item.message_group_id,
                        item.wire_role,
                        _json(dict(item.producer_ref)),
                        item.content_hash,
                        item.created_at,
                        _json(dict(item.metadata)),
                    )
                    if stored_identity != incoming_identity:
                        raise ItemSchemaError(
                            "canonical item_id immutable identity/status/payload 冲突: "
                            f"{item.item_id}"
                        )
                    if existing[11] is None:
                        raise RuntimeError(
                            f"canonical item 缺少所属 commit: {item.item_id}"
                        )
                    existing_commit_ids.append(
                        _strict_non_negative_int(
                            existing[11], field="item_catalog.commit_id"
                        )
                    )
                    resolved_item_ids.append(item.item_id)
                if not pending:
                    self._append_context_view_items(
                        connection,
                        checkpoint_ns=checkpoint_ns,
                        item_ids=resolved_item_ids,
                    )
                    self._commit_connection(connection)
                    return tuple(dict.fromkeys(existing_commit_ids))
                last_sequence_row = connection.execute(
                    "SELECT last_item_sequence FROM database_meta WHERE singleton_id = 1"
                ).fetchone()
                if last_sequence_row is None:
                    raise RuntimeError("rollout database_meta 缺少 last_item_sequence")
                last_sequence = _strict_non_negative_int(
                    last_sequence_row[0], field="database_meta.last_item_sequence"
                )
                pending = [
                    replace(item, item_sequence=last_sequence + index + 1)
                    for index, item in enumerate(pending)
                ]
                connection.execute("BEGIN IMMEDIATE")
                commit_id, _offset = self._append_v2_records_transaction(
                    connection,
                    thread_id,
                    checkpoint_ns,
                    pending,
                    commit_kind=commit_kind,
                    subject_id=pending[0].item_id,
                    idempotency_key="items:"
                    + sha256_jcs([item.item_id for item in pending]),
                    begin_transaction=False,
                )
                # canonical sink 可能在 LangGraph 创建下一条 checkpoint view
                # 之前提交（尤其是 tool_result）。当前 active view 的 item
                # membership 必须在同一 SQLite 事务内立即可见，否则下一次
                # provider assembly 会看到 tool_call 却看不到对应 result。
                self._append_context_view_items(
                    connection,
                    checkpoint_ns=checkpoint_ns,
                    item_ids=(item.item_id for item in pending),
                )
                self._commit_connection(connection)
                return tuple(dict.fromkeys((*existing_commit_ids, commit_id)))

    def ensure_request_tool_result_items(
        self,
        thread_id: str,
        *,
        turn_id: str,
        messages: Sequence[object],
        checkpoint_ns: str = "",
    ) -> tuple[str, ...]:
        """在下一个 LangGraph checkpoint 产生前固化请求中的工具消息。

        LangGraph 的 ``on_chat_model_start`` 可能先于外层 ``on_tool_end`` 事件
        到达。此时 AIMessage(tool_calls=...) 和 ToolMessage 已经是本次模型请求
        的输入，但 stream/checkpoint sink 可能尚未将其中一个写入 rollout。这里
        由 Saver 调用 storage owner，把这两个请求事实先写成完整 immutable item；
        随后标准 checkpoint 和 stream sink 通过 message/tool-call identity 幂等
        复用它们，避免已 seal 的 assembly 缺少 assistant tool-call 或 ToolMessage。
        """
        candidates: list[CanonicalItemRecord] = []
        with self._lock(thread_id, checkpoint_ns):
            self.initialize(thread_id, checkpoint_ns)
            with self._connect(thread_id, checkpoint_ns, read_only=True) as connection:
                self._require_v2_runtime(connection)
                for index, message in enumerate(messages):
                    is_tool_result = self._codec().message_role(message) == "tool"
                    is_tool_call = bool(self._codec().tool_calls(message))
                    if not is_tool_result and not is_tool_call:
                        continue
                    message_id = self._codec().message_id(message, index)
                    message_turn_id = self._codec().turn_id(
                        message, turn_id, message_id
                    )
                    group = self._codec().items_for_message(
                        message,
                        item_sequence=1,
                        message_id=message_id,
                        turn_id=message_turn_id,
                        timestamp=_now(),
                    )
                    projected = self._codec().project_message(group)
                    anchor = connection.execute(
                        "SELECT jsonl_offset, jsonl_length FROM item_catalog WHERE item_id = ?",
                        (group[-1].item_id,),
                    ).fetchone()
                    if anchor is not None:
                        with self.jsonl_path(thread_id, checkpoint_ns).open("rb") as stream:
                            stored = read_message_group(
                                connection, stream, offset=anchor[0], length=anchor[1],
                                read_item=self._read_v2_item_at,
                            )
                        # 必须比较完整 carrier 与语义 metadata；逐 item hash 无法
                        # 拒绝 companion 被删除或同正文 phase/provenance 改变。
                        if (
                            len(group) != len(stored)
                            or projected != self._codec().project_message(stored)
                            or any(
                                item.item_id != saved.item_id
                                or item.semantic_kind != saved.semantic_kind
                                or item.content_hash != saved.content_hash
                                or item.turn_id != saved.turn_id
                                or item.turn_scope != saved.turn_scope
                                for item, saved in zip(group, stored, strict=True)
                            )
                        ):
                            raise RuntimeError(
                                f"request 与既有 canonical message group 不一致: {message_id}"
                            )
                        continue
                    for item in group:
                        existing = connection.execute(
                            "SELECT item_id FROM item_catalog WHERE item_id = ?",
                            (item.item_id,),
                        ).fetchone()
                        if existing is not None:
                            raise RuntimeError(
                                f"canonical message group 仅有部分已提交成员: {item.item_id}"
                            )
                        candidates.append(item)
        if not candidates:
            return ()
        self.append_items(
            thread_id,
            candidates,
            checkpoint_ns=checkpoint_ns,
            commit_kind=CommitKind.ITEM_CONVERGENCE,
        )
        return tuple(item.item_id for item in candidates)

    def append_control_commit(
        self,
        thread_id: str,
        *,
        outcome: str,
        checkpoint_ns: str = "",
        commit_kind: str = CommitKind.TERMINAL_CONVERGENCE,
        metadata: Mapping[str, object] | None = None,
        subject_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> int:
        """记录没有 JSONL item 的 control/terminal outcome。"""
        if outcome not in {value.value for value in ControlOutcome}:
            raise ValueError(f"未知 control outcome: {outcome}")
        with self._lock(thread_id, checkpoint_ns):
            self.initialize(thread_id, checkpoint_ns)
            with self._connect(thread_id, checkpoint_ns) as connection:
                self._require_v2_runtime(connection)
                commit_id, _offset = self._append_v2_records_transaction(
                    connection,
                    thread_id,
                    checkpoint_ns,
                    (),
                    commit_kind=commit_kind,
                    commit_mode=CommitMode.METADATA_ONLY.value,
                    outcome=outcome,
                    metadata=metadata,
                    subject_id=subject_id,
                    idempotency_key=idempotency_key,
                )
                self._commit_connection(connection)
                return commit_id

    def read_items(
        self,
        thread_id: str,
        *,
        checkpoint_ns: str = "",
        item_ids: Iterable[str] | None = None,
        snapshot: RolloutReadSnapshot | None = None,
        _allow_migrating: bool = False,
    ) -> list[CanonicalItemRecord]:
        """在一致的 rollout 读边界内读取已提交 v2 item。

        没有调用方 snapshot 时也必须持有 rollout 文件锁，直到 JSONL 正文
        完成校验；否则 compaction/backup 可能在 SQLite locator 已读出后改写
        JSONL，形成跨文件的半个快照。显式 migration 仍可通过内部参数读取
        staging 数据，但不会绕过 locator/hash 校验。
        """
        if snapshot is None:
            with self._lock(thread_id, checkpoint_ns):
                return self._read_items_unlocked(
                    thread_id,
                    checkpoint_ns=checkpoint_ns,
                    item_ids=item_ids,
                    snapshot=None,
                    _allow_migrating=_allow_migrating,
                )
        return self._read_items_unlocked(
            thread_id,
            checkpoint_ns=checkpoint_ns,
            item_ids=item_ids,
            snapshot=snapshot,
            _allow_migrating=_allow_migrating,
        )

    def _read_items_unlocked(
        self,
        thread_id: str,
        *,
        checkpoint_ns: str = "",
        item_ids: Iterable[str] | None = None,
        snapshot: RolloutReadSnapshot | None = None,
        _allow_migrating: bool = False,
    ) -> list[CanonicalItemRecord]:
        """按 SQLite catalog 定位并校验已提交 v2 item，避免扫描 JSONL。"""
        if snapshot is not None and (
            snapshot.thread_id != thread_id or snapshot.checkpoint_ns != checkpoint_ns
        ):
            raise ValueError("item read snapshot 与目标 rollout 不一致")
        owned_connection = None
        if snapshot is None:
            self.initialize(thread_id, checkpoint_ns)
            owned_connection = self._connect(thread_id, checkpoint_ns, read_only=True)
        connection = (
            self._snapshot_connection(snapshot)
            if snapshot is not None
            else owned_connection
        )
        assert connection is not None
        try:
            self._require_v2_readable(
                connection,
                allow_migrating=_allow_migrating,
            )
            values = tuple(dict.fromkeys(item_ids or ()))
            if any(not isinstance(item_id, str) or not item_id for item_id in values):
                raise TypeError("canonical item_ids 必须是非空字符串")
            if values:
                placeholders = ",".join("?" for _ in values)
                rows = connection.execute(
                    f"SELECT item_sequence, item_id, content_hash, payload_length, source_revision, jsonl_offset, jsonl_length FROM item_catalog WHERE item_id IN ({placeholders}) ORDER BY item_sequence",
                    values,
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT item_sequence, item_id, content_hash, payload_length, source_revision, jsonl_offset, jsonl_length FROM item_catalog ORDER BY item_sequence"
                ).fetchall()
            if values:
                found_ids = {
                    _strict_db_text(row[1], field="item_catalog.item_id")
                    for row in rows
                }
                missing_ids = tuple(
                    item_id for item_id in values if item_id not in found_ids
                )
                if missing_ids:
                    raise KeyError(
                        "canonical item catalog 缺少请求的 item: "
                        + ",".join(missing_ids)
                    )
            committed_row = connection.execute(
                "SELECT committed_jsonl_offset FROM database_meta WHERE singleton_id = 1"
            ).fetchone()
            if committed_row is None:
                raise RuntimeError("database_meta 缺少 committed_jsonl_offset")
            committed = _strict_non_negative_int(
                committed_row[0], field="database_meta.committed_jsonl_offset"
            )
        finally:
            if owned_connection is not None:
                owned_connection.close()
        result: list[CanonicalItemRecord] = []
        with self.jsonl_path(thread_id, checkpoint_ns).open("rb") as stream:
            for (
                sequence_value,
                item_id,
                expected_hash,
                expected_payload_length,
                expected_source_revision,
                offset_value,
                length_value,
            ) in rows:
                sequence = _strict_non_negative_int(
                    sequence_value,
                    field=f"item_catalog.item_sequence: {item_id}",
                )
                if not isinstance(item_id, str) or not item_id:
                    raise RuntimeError("item_catalog.item_id 必须是非空字符串")
                if not isinstance(expected_hash, str) or not expected_hash:
                    raise RuntimeError(
                        f"item_catalog.content_hash 必须是非空字符串: {item_id}"
                    )
                payload_length_value = _strict_non_negative_int(
                    expected_payload_length,
                    field=f"item_catalog.payload_length: {item_id}",
                )
                if not isinstance(expected_source_revision, str):
                    raise TypeError(
                        f"item_catalog.source_revision 必须是字符串: {item_id}"
                    )
                offset = _strict_non_negative_int(
                    offset_value,
                    field=f"item_catalog.jsonl_offset: {item_id}",
                )
                length = _strict_non_negative_int(
                    length_value,
                    field=f"item_catalog.jsonl_length: {item_id}",
                )
                if length == 0:
                    raise RuntimeError(
                        f"item_catalog.jsonl_length 必须大于 0: {item_id}"
                    )
                if offset + length > committed:
                    raise RuntimeError(f"item 超出已提交 JSONL offset: {item_id}")
                stream.seek(offset)
                raw = stream.read(length)
                if len(raw) != length:
                    raise RuntimeError(f"item JSONL locator 长度不足: {item_id}")
                envelope = json.loads(raw.decode("utf-8"))
                if not isinstance(envelope, Mapping):
                    raise TypeError(f"item envelope 非法: {item_id}")
                if raw != _v2_json_line(envelope):
                    raise FormatDispatchError(
                        f"v2 item JSONL 不是 RFC 8785 JCS canonical line: {item_id}"
                    )
                item = CanonicalItemRecord.from_dict(envelope)
                if item.item_sequence != sequence or item.item_id != item_id:
                    raise RuntimeError(
                        f"item catalog 与 JSONL identity 不一致: {item_id}"
                    )
                if item.content_hash != expected_hash:
                    raise RuntimeError(
                        f"item catalog 与 JSONL content_hash 不一致: {item_id}"
                    )
                payload_length = payload_content_length(item.payload_kind, item.payload)
                if payload_length_value != payload_length:
                    raise RuntimeError(
                        f"item catalog 与 JSONL payload_length 不一致: {item_id}"
                    )
                source_revision = item.metadata.get("source_revision")
                if source_revision is None:
                    source_revision = f"canonical:{item.item_id}:{item.content_hash}"
                elif not isinstance(source_revision, str) or not source_revision:
                    raise RuntimeError(
                        f"canonical item source_revision 非法: {item.item_id}"
                    )
                if expected_source_revision != source_revision:
                    raise RuntimeError(
                        f"item catalog 与 JSONL source_revision 不一致: {item_id}"
                    )
                result.append(item)
        return result


__all__ = ["RolloutItemsMixin"]
