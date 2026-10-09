"""v2 canonical catalog 写入与按 SQLite locator 读取。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace

from app.core.sqlite_state import utc_now_text as _now
from app.domain.itemized.enums import (
    CommitKind,
    SemanticKind,
)
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import (
    sha256_jcs,
)
from app.domain.itemized.records import (
    CanonicalItemRecord,
    parse_item_projection_manifest,
)
from app.services.infrastructure.rollout_context.storage.catalog.tool_result_identity import (
    _comparable_tool_result_payload,
    _select_existing_tool_result,
    _strict_db_optional_text,
    _strict_db_text,
    _tool_result_identity,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_text as _json,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_text,
)


class CanonicalItemCommitMixin:
    def append_item(
        self,
        session_id: str,
        thread_id: str,
        item: CanonicalItemRecord,
        *,
        execution_id: str,
        checkpoint_ns: str = "",
        commit_kind: str = CommitKind.ITEM_CONVERGENCE,
    ) -> int:
        """追加一个 immutable canonical item，并返回 storage commit id。"""
        return self.append_items(
            session_id,
            thread_id,
            (item,),
            execution_id=execution_id,
            checkpoint_ns=checkpoint_ns,
            commit_kind=commit_kind,
        )[0]
    def append_items(
        self,
        session_id: str,
        thread_id: str,
        items: Sequence[CanonicalItemRecord],
        *,
        execution_id: str,
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
        execution_id = strict_text(execution_id, field="execution_id")
        with self._storage._lock(thread_id, checkpoint_ns, session_id=session_id):
            self._storage.initialize(session_id, checkpoint_ns, thread_id=thread_id)
            with self._storage._connect(
                thread_id,
                checkpoint_ns,
                session_id=session_id,
            ) as connection:
                self._storage._require_v2_runtime(connection)
                admission_row = connection.execute(
                    "SELECT turn_id, execution_origin FROM executions "
                    "WHERE session_id = ? AND thread_id = ? AND execution_id = ?",
                    (session_id, thread_id, execution_id),
                ).fetchone()
                if admission_row is None:
                    raise RuntimeError(
                        "canonical append 缺少当前 SessionThread 的 durable execution admission: "
                        f"session_id={session_id}, thread_id={thread_id}, "
                        f"execution_id={execution_id}"
                    )
                admitted_turn_id = _strict_db_optional_text(
                    admission_row[0], field="executions.turn_id"
                )
                execution_origin = _strict_db_text(
                    admission_row[1], field="executions.execution_origin"
                )
                if execution_origin not in {"user_turn", "internal"}:
                    raise RuntimeError(
                        f"canonical append execution_origin 非法: {execution_origin!r}"
                    )
                if execution_origin == "user_turn" and admitted_turn_id is None:
                    raise RuntimeError("user_turn append admission 缺少真实 Turn")
                if execution_origin == "internal" and admitted_turn_id is not None:
                    raise RuntimeError("internal append admission 不得绑定 Turn")
                if any(item.turn_id != admitted_turn_id for item in items):
                    raise RuntimeError(
                        "canonical append item Turn attribution 与 durable execution admission 不一致: "
                        f"execution_id={execution_id}"
                    )
                existing_commit_ids: list[int] = []
                resolved_item_ids: list[str] = []
                pending: list[CanonicalItemRecord] = []
                for item in items:
                    item.validate()
                    existing = connection.execute(
                        "SELECT semantic_kind, payload_kind, status, turn_id, turn_scope, "
                        "message_group_id, wire_role, producer_ref_json, content_hash, "
                        "created_at, projection_manifest_json, commit_id, execution_id, item_sequence, jsonl_offset, jsonl_length FROM item_catalog "
                        "WHERE item_id = ?",
                        (item.item_id,),
                    ).fetchone()
                    if existing is None:
                        if item.semantic_kind == SemanticKind.TOOL_RESULT:
                            existing_result = _select_existing_tool_result(
                                connection, item
                            )
                            if existing_result is not None:
                                result_item_id = _strict_db_text(
                                    existing_result[0],
                                    field="item_catalog.item_id",
                                )
                                result_offset = strict_non_negative_int(
                                    existing_result[3],
                                    field="item_catalog.jsonl_offset",
                                )
                                result_length = strict_non_negative_int(
                                    existing_result[4],
                                    field="item_catalog.jsonl_length",
                                )
                                result_commit_id = strict_non_negative_int(
                                    existing_result[2],
                                    field="item_catalog.commit_id",
                                )
                                if result_length == 0:
                                    raise RuntimeError(
                                        "既有 tool_result canonical locator 长度非法: "
                                        f"{result_item_id}"
                                    )
                                result_path = self._storage.jsonl_path(
                                    session_id,
                                    checkpoint_ns,
                                    thread_id=thread_id,
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
                                if not isinstance(existing_envelope, Mapping):
                                    raise RuntimeError(
                                        "既有 tool_result canonical envelope 非 object: "
                                        f"{result_item_id}"
                                    )
                                existing_item = CanonicalItemRecord.from_dict(
                                    existing_envelope
                                )
                                existing_manifest = parse_item_projection_manifest(
                                    json.loads(existing_result[5])
                                )
                                if (
                                    existing_item.item_id != result_item_id
                                    or existing_manifest
                                    != existing_item.projection_manifest()
                                    or existing_item.content_hash != existing_result[1]
                                ):
                                    raise RuntimeError(
                                        f"既有 tool_result catalog 与 JSONL 不一致: {result_item_id}"
                                    )
                                existing_call = (
                                    existing_item.tool_result_evidence.tool_call_id
                                    if existing_item.tool_result_evidence
                                    else existing_item.payload["tool_call_id"]
                                )
                                if existing_call != existing_result[6]:
                                    raise RuntimeError(
                                        f"既有 tool_result 派生 call identity 与 JSONL 不一致: {result_item_id}"
                                    )
                                comparable_existing = _comparable_tool_result_payload(
                                    existing_item
                                )
                                comparable_new = _comparable_tool_result_payload(item)
                                if comparable_existing != comparable_new:
                                    identity = _tool_result_identity(item)
                                    raise ItemSchemaError(
                                        "同一 tool_call_id 的 tool_result 正文发生变化: "
                                        f"{identity[0] if identity is not None else 'unknown'}"
                                    )
                                # request 侧 tool_result carrier 不得被工具执行侧
                                # live item 等价复用。委派（task 工具）后的下一次
                                # 模型请求装配存在竞态：确认 carrier 可能先于 plan
                                # 创建落入 active view；若把带 projection_message_id
                                # 的 request 侧 carrier“等价复用”成无该身份的 live
                                # item，则不会产生新 commit，投影层的 carrier_dedup
                                # 会把 live tool_result 连同 stream 组一起判为
                                # shadow，且没有任何替代 carrier，最终 provider wire
                                # 缺失尾部 ToolMessage。因此正文等价通过后，incoming
                                # 是 request 侧 carrier 而既有 item 不是时，必须照常
                                # 持久化新 carrier；投影层靠 projection_message_id
                                # 保留它并过滤 live shadow。
                                incoming_is_request_carrier = isinstance(
                                    (
                                        item.projection_identity.projection_message_id
                                        if item.projection_identity is not None
                                        else None
                                    ),
                                    str,
                                )
                                existing_is_request_carrier = isinstance(
                                    (
                                        existing_item.projection_identity.projection_message_id
                                        if existing_item.projection_identity is not None
                                        else None
                                    ),
                                    str,
                                )
                                if (
                                    incoming_is_request_carrier
                                    and not existing_is_request_carrier
                                ):
                                    pending.append(item)
                                    continue
                                existing_commit_ids.append(result_commit_id)
                                resolved_item_ids.append(result_item_id)
                                continue
                        pending.append(item)
                        continue
                    with self._storage.jsonl_path(
                        session_id, checkpoint_ns, thread_id=thread_id
                    ).open("rb") as source_stream:
                        self._storage._read_v2_item_at(
                            connection,
                            source_stream,
                            sequence=strict_non_negative_int(
                                existing[13], field="item_catalog.item_sequence"
                            ),
                            offset=strict_non_negative_int(
                                existing[14], field="item_catalog.jsonl_offset"
                            ),
                            length=strict_non_negative_int(
                                existing[15], field="item_catalog.jsonl_length"
                            ),
                        )
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
                            existing[10], field="item_catalog.projection_manifest_json"
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
                        _json(item.projection_manifest()),
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
                    stored_execution_id = _strict_db_optional_text(
                        existing[12],
                        field=f"item_catalog.execution_id:{item.item_id}",
                    )
                    if stored_execution_id is None:
                        raise RuntimeError(
                            "既有 canonical item 缺少 execution attribution: "
                            f"{item.item_id}"
                        )
                    existing_commit_ids.append(
                        strict_non_negative_int(
                            existing[11], field="item_catalog.commit_id"
                        )
                    )
                    resolved_item_ids.append(item.item_id)
                if not pending:
                    self._storage._append_context_view_items(
                        connection,
                        checkpoint_ns=checkpoint_ns,
                        item_ids=resolved_item_ids,
                        source_kind="display_only" if execution_origin == "internal" else "canonical",
                    )
                    self._storage._commit_connection(connection)
                    return tuple(dict.fromkeys(existing_commit_ids))
                last_sequence_row = connection.execute(
                    "SELECT last_item_sequence FROM database_meta WHERE singleton_id = 1"
                ).fetchone()
                if last_sequence_row is None:
                    raise RuntimeError("rollout database_meta 缺少 last_item_sequence")
                last_sequence = strict_non_negative_int(
                    last_sequence_row[0], field="database_meta.last_item_sequence"
                )
                pending = [
                    replace(item, item_sequence=last_sequence + index + 1)
                    for index, item in enumerate(pending)
                ]
                connection.execute("BEGIN IMMEDIATE")
                commit_id, _offset = self._storage._append_v2_records_transaction(
                    connection,
                    thread_id,
                    checkpoint_ns,
                    pending,
                    session_id=session_id,
                    commit_kind=commit_kind,
                    subject_id=pending[0].item_id,
                    idempotency_key="items:"
                    + sha256_jcs([item.item_id for item in pending]),
                    begin_transaction=False,
                )
                message_meta = connection.execute(
                    "SELECT last_message_sequence FROM database_meta WHERE singleton_id = 1"
                ).fetchone()
                if message_meta is None:
                    raise RuntimeError("canonical append 缺少 message sequence 元数据")
                previous_message_sequence = strict_non_negative_int(
                    message_meta[0], field="database_meta.last_message_sequence"
                )
                next_message_sequence = previous_message_sequence + 1
                new_message_sequences: list[int] = []
                branch_id, _projection_epoch = self._storage._namespace_state(connection, checkpoint_ns)
                timestamp = _now()
                for item in pending:
                    self._storage._bind_canonical_item_execution(
                        connection,
                        item_id=item.item_id,
                        execution_id=execution_id,
                    )
                    # 只协调现有 projection owner；canonical append 与真实配对索引
                    # 同事务发布，避免下一 prepare 只看见旧 checkpoint 的消息范围。
                    materialized = self._context.checkpoints._materialize_canonical_message_projection(
                        connection, item, thread_id=thread_id, session_id=session_id,
                        checkpoint_ns=checkpoint_ns, commit_id=commit_id,
                        message_sequence=next_message_sequence, timestamp=timestamp,
                    )
                    if materialized is None:
                        continue
                    sequence, message_id, role = materialized
                    if sequence > previous_message_sequence:
                        new_message_sequences.append(sequence)
                        next_message_sequence = sequence + 1
                        if item.turn_id is not None:
                            self._storage._upsert_turn(
                                connection, item.turn_id, sequence, message_id, role,
                                branch_id, timestamp,
                            )
                if new_message_sequences:
                    commit_update = connection.execute(
                        "UPDATE storage_commits SET first_message_sequence = ?, "
                        "last_message_sequence = ? WHERE commit_id = ?",
                        (min(new_message_sequences), max(new_message_sequences), commit_id),
                    )
                    if commit_update.rowcount != 1:
                        raise RuntimeError("canonical append commit message sequence 更新失败")
                    meta_update = connection.execute(
                        "UPDATE database_meta SET last_message_sequence = ?, "
                        "history_view_revision = history_view_revision + 1, updated_at = ? "
                        "WHERE singleton_id = 1",
                        (max(new_message_sequences), timestamp),
                    )
                    if meta_update.rowcount != 1:
                        raise RuntimeError("canonical append message sequence 元数据更新失败")
                # canonical sink 可能在 LangGraph 创建下一条 checkpoint view
                # 之前提交（尤其是 tool_result）。当前 active view 的 item
                # membership 必须在同一 SQLite 事务内立即可见，否则下一次
                # provider assembly 会看到 tool_call 却看不到对应 result。
                self._storage._append_context_view_items(
                    connection,
                    checkpoint_ns=checkpoint_ns,
                    item_ids=(item.item_id for item in pending),
                    source_kind="display_only" if execution_origin == "internal" else "canonical",
                )
                self._storage._commit_connection(connection)
                return tuple(dict.fromkeys((*existing_commit_ids, commit_id)))
    def append_request_groups(
        self, session_id: str, thread_id: str, *,
        groups: Sequence[Sequence[CanonicalItemRecord]], execution_id: str,
        checkpoint_ns: str = "",
    ) -> tuple[str, ...]:
        """完整 typed group 统一提交；已提交的部分成员必须显式拒绝。"""
        if not groups:
            return ()
        candidates = tuple(item for group in groups for item in group)
        with self._storage._lock(thread_id, checkpoint_ns, session_id=session_id):
            with self._storage._connect(thread_id, checkpoint_ns, session_id=session_id, read_only=True) as connection:
                for item in candidates:
                    if connection.execute("SELECT item_id FROM item_catalog WHERE item_id=?", (item.item_id,)).fetchone() is not None:
                        raise RuntimeError(f"canonical message group 仅有部分已提交成员: {item.item_id}")
            self.append_items(session_id, thread_id, candidates, execution_id=execution_id,
                              checkpoint_ns=checkpoint_ns, commit_kind=CommitKind.ITEM_CONVERGENCE)
        return tuple(item.item_id for item in candidates)
