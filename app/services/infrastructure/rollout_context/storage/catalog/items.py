"""v2 canonical catalog 写入与按 SQLite locator 读取。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING

from app.core.hashing import sha256_hex as _hash_bytes
from app.core.sqlite_state import utc_now_text as _now
from app.domain.itemized.enums import (
    CommitKind,
    CommitMode,
    ControlOutcome,
    SemanticKind,
)
from app.domain.itemized.errors import FormatDispatchError, ItemSchemaError
from app.domain.itemized.hashing import (
    payload_content_length,
    sha256_jcs,
)
from app.domain.itemized.records import (
    CanonicalItemRecord,
    parse_item_projection_manifest,
)
from app.services.infrastructure.rollout_context.storage.catalog.integrity import (
    validate_catalog_projection,
)
from app.services.infrastructure.rollout_context.storage.catalog.message_groups import (
    read_message_group,
)
from app.services.infrastructure.rollout_context.storage.catalog.tool_result_identity import (
    _comparable_tool_result_payload,
    _select_existing_tool_result,
    _strict_db_optional_text,
    _strict_db_text,
    _tool_result_identity,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_line as _v2_json_line,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_text as _json,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_text,
    validate_item_storage_projection,
)

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.storage.primitives import (
        RolloutReadSnapshot,
    )


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
        source_revision, part_ordinal = validate_item_storage_projection(item)
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
            "operation_anchor_capable, searchable, created_at, projection_manifest_json, tool_call_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                _json(item.projection_manifest()),
                item.tool_call_id,
            ),
        )
        self._insert_item_projection(connection, item, created_at)
        self._insert_canonical_relations(connection, item)
        block_id = (
            item.projection_identity.block_id
            if item.projection_identity is not None
            else None
        )
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

    def _insert_canonical_relations(
        self,
        connection: sqlite3.Connection,
        item: CanonicalItemRecord,
    ) -> None:
        """关系表只是 canonical tuple 的有序派生索引，不能独立发布关系。"""
        for ordinal, relation in enumerate(item.provenance_relations):
            connection.execute(
                "INSERT INTO item_relations(relation_id, relation, canonical_item_id, "
                "canonical_relation_ordinal, source_ref, target_ref, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    f"canonical-relation:{item.item_id}:{ordinal}",
                    relation.relation,
                    item.item_id,
                    ordinal,
                    item.item_id,
                    _json(relation.to_dict()),
                    item.created_at,
                ),
            )

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
        with self._lock(thread_id, checkpoint_ns, session_id=session_id):
            self.initialize(session_id, checkpoint_ns, thread_id=thread_id)
            with self._connect(
                thread_id,
                checkpoint_ns,
                session_id=session_id,
            ) as connection:
                self._require_v2_runtime(connection)
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
                                result_path = self.jsonl_path(
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
                    with self.jsonl_path(
                        session_id, checkpoint_ns, thread_id=thread_id
                    ).open("rb") as source_stream:
                        self._read_v2_item_at(
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
                last_sequence = strict_non_negative_int(
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
                    session_id=session_id,
                    commit_kind=commit_kind,
                    subject_id=pending[0].item_id,
                    idempotency_key="items:"
                    + sha256_jcs([item.item_id for item in pending]),
                    begin_transaction=False,
                )
                for item in pending:
                    self._bind_canonical_item_execution(
                        connection,
                        item_id=item.item_id,
                        execution_id=execution_id,
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

    def ensure_request_items(
        self,
        session_id: str,
        thread_id: str,
        *,
        turn_id: str | None,
        execution_id: str,
        messages: Sequence[object],
        checkpoint_ns: str = "",
    ) -> tuple[str, ...]:
        """在下一个 LangGraph checkpoint 产生前固化请求中的动态消息。

        LangGraph 的 ``on_chat_model_start`` 可能先于外层 ``on_tool_end`` 事件
        到达。此时 AIMessage(tool_calls=...) 和 ToolMessage 已经是本次模型请求
        的输入，但 stream/checkpoint sink 可能尚未将其中一个写入 rollout。这里
        由 Saver 调用 storage owner，把这两个请求事实先写成完整 immutable item；
        上下文来源 middleware 追加的 user item 也在这里固化，避免 sealed
        assembly 只从旧 checkpoint 读取而丢掉本次请求前生成的上下文增量；
        随后标准 checkpoint 和 stream sink 通过 message/tool-call identity 幂等
        复用它们，避免已 seal 的 assembly 缺少 assistant tool-call 或 ToolMessage。
        """
        session_id = strict_text(session_id, field="session_id")
        execution_id = strict_text(execution_id, field="execution_id")
        if turn_id is not None:
            turn_id = strict_text(turn_id, field="turn_id")
        candidates: list[CanonicalItemRecord] = []
        with self._lock(thread_id, checkpoint_ns, session_id=session_id):
            self.initialize(session_id, checkpoint_ns, thread_id=thread_id)
            with self._connect(
                thread_id,
                checkpoint_ns,
                session_id=session_id,
                read_only=True,
            ) as connection:
                self._require_v2_runtime(connection)
                admission_row = connection.execute(
                    "SELECT turn_id, execution_origin FROM executions "
                    "WHERE session_id = ? AND thread_id = ? AND execution_id = ?",
                    (session_id, thread_id, execution_id),
                ).fetchone()
                if admission_row is None:
                    raise RuntimeError(
                        "request item canonicalization 缺少当前 SessionThread 的 durable execution admission: "
                        f"session_id={session_id}, thread_id={thread_id}, "
                        f"execution_id={execution_id}"
                    )
                admitted_turn_id = _strict_db_optional_text(
                    admission_row[0], field="executions.turn_id"
                )
                execution_origin = _strict_db_text(
                    admission_row[1], field="executions.execution_origin"
                )
                if turn_id != admitted_turn_id:
                    raise RuntimeError(
                        "request item Turn 与 durable execution admission 不一致: "
                        f"execution_id={execution_id}"
                    )
                if execution_origin == "user_turn" and turn_id is None:
                    raise RuntimeError("user_turn request item admission 缺少真实 Turn")
                if execution_origin == "internal" and turn_id is not None:
                    raise RuntimeError("internal request item admission 必须 Turn-less")
                for index, message in enumerate(messages):
                    codec = self._codec()
                    message_role = codec.message_role(message)
                    is_tool_result = message_role == "tool"
                    is_tool_call = bool(codec.tool_calls(message))
                    response_metadata = getattr(message, "response_metadata", {})
                    is_context_source = (
                        message_role == "user"
                        and isinstance(response_metadata, Mapping)
                        and isinstance(
                            response_metadata.get("context_source_kind"), str
                        )
                        and isinstance(response_metadata.get("context_revision"), str)
                    )
                    if (
                        not is_tool_result
                        and not is_tool_call
                        and not is_context_source
                    ):
                        continue
                    message_id = self._codec().message_id(message, index)
                    indexed_message = connection.execute(
                        "SELECT turn_id, execution_id FROM messages WHERE message_id = ?",
                        (message_id,),
                    ).fetchone()
                    catalog_message = connection.execute(
                        "SELECT turn_id, execution_id FROM item_catalog WHERE item_id = ?",
                        (f"item-{message_id}",),
                    ).fetchone()
                    if indexed_message is not None and catalog_message is None:
                        raise RuntimeError(
                            f"message projection 缺少 canonical item: {message_id}"
                        )
                    if indexed_message is not None and (
                        indexed_message[0] != catalog_message[0]
                        or indexed_message[1] != catalog_message[1]
                    ):
                        raise RuntimeError(
                            f"message projection 与 canonical item attribution 不一致: {message_id}"
                        )
                    message_turn_id = (
                        indexed_message[0]
                        if indexed_message is not None
                        else catalog_message[0]
                        if catalog_message is not None
                        else turn_id
                    )
                    message_execution_id = (
                        indexed_message[1]
                        if indexed_message is not None
                        else catalog_message[1]
                        if catalog_message is not None
                        else execution_id
                    )
                    if (
                        not isinstance(message_execution_id, str)
                        or not message_execution_id
                    ):
                        raise RuntimeError(
                            f"request message 缺少 canonical execution attribution: {message_id}"
                        )
                    model_call_id = (
                        codec.tool_message_model_call_id(message, messages[:index])
                        if is_tool_result
                        else codec.model_call_id(message)
                        if codec.message_role(message) == "assistant"
                        else None
                    )
                    group = codec.items_for_message(
                        message,
                        item_sequence=1,
                        message_id=message_id,
                        turn_id=message_turn_id,
                        execution_id=message_execution_id,
                        timestamp=_now(),
                        model_call_id=model_call_id,
                    )
                    projected = self._codec().project_message(group)
                    anchor = connection.execute(
                        "SELECT jsonl_offset, jsonl_length FROM item_catalog WHERE item_id = ?",
                        (group[-1].item_id,),
                    ).fetchone()
                    if anchor is not None:
                        with self.jsonl_path(
                            session_id, checkpoint_ns, thread_id=thread_id
                        ).open("rb") as stream:
                            stored = read_message_group(
                                connection,
                                stream,
                                offset=anchor[0],
                                length=anchor[1],
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
            session_id,
            thread_id,
            candidates,
            execution_id=execution_id,
            checkpoint_ns=checkpoint_ns,
            commit_kind=CommitKind.ITEM_CONVERGENCE,
        )
        return tuple(item.item_id for item in candidates)

    def append_control_commit(
        self,
        session_id: str,
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
        with self._lock(thread_id, checkpoint_ns, session_id=session_id):
            self.initialize(session_id, checkpoint_ns, thread_id=thread_id)
            with self._connect(
                thread_id,
                checkpoint_ns,
                session_id=session_id,
            ) as connection:
                self._require_v2_runtime(connection)
                commit_id, _offset = self._append_v2_records_transaction(
                    connection,
                    thread_id,
                    checkpoint_ns,
                    (),
                    session_id=session_id,
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
        session_id: str,
        thread_id: str,
        *,
        checkpoint_ns: str = "",
        item_ids: Iterable[str] | None = None,
        snapshot: RolloutReadSnapshot | None = None,
    ) -> list[CanonicalItemRecord]:
        """在一致的 rollout 读边界内读取已提交 v2 item。

        没有调用方 snapshot 时也必须持有 rollout 文件锁，直到 JSONL 正文
        完成校验；否则 compaction/backup 可能在 SQLite locator 已读出后改写
        JSONL，形成跨文件的半个快照。
        """
        if snapshot is None:
            with self._lock(
                thread_id,
                checkpoint_ns,
                session_id=session_id,
            ):
                return self._read_items_unlocked(
                    session_id,
                    thread_id,
                    checkpoint_ns=checkpoint_ns,
                    item_ids=item_ids,
                    snapshot=None,
                )
        return self._read_items_unlocked(
            session_id,
            thread_id,
            checkpoint_ns=checkpoint_ns,
            item_ids=item_ids,
            snapshot=snapshot,
        )

    def _read_items_unlocked(
        self,
        session_id: str,
        thread_id: str,
        *,
        checkpoint_ns: str = "",
        item_ids: Iterable[str] | None = None,
        snapshot: RolloutReadSnapshot | None = None,
    ) -> list[CanonicalItemRecord]:
        """按 SQLite catalog 定位并校验已提交 v2 item，避免扫描 JSONL。"""
        if snapshot is not None and (
            snapshot.session_id != session_id
            or snapshot.thread_id != thread_id
            or snapshot.checkpoint_ns != checkpoint_ns
        ):
            raise ValueError("item read snapshot 与目标 rollout 不一致")
        owned_connection = None
        if snapshot is None:
            self.initialize(session_id, checkpoint_ns, thread_id=thread_id)
            owned_connection = self._connect(
                thread_id,
                checkpoint_ns,
                session_id=session_id,
                read_only=True,
            )
        connection = (
            self._snapshot_connection(snapshot)
            if snapshot is not None
            else owned_connection
        )
        assert connection is not None
        try:
            self._require_v2_readable(connection)
            # None 请求全部；显式空选集仍须过滤，不得读回全部历史。
            values = tuple(dict.fromkeys(item_ids if item_ids is not None else ()))
            if any(not isinstance(item_id, str) or not item_id for item_id in values):
                raise TypeError("canonical item_ids 必须是非空字符串")
            if item_ids is not None:
                placeholders = ",".join("?" for _ in values)
                rows = connection.execute(
                    f"SELECT item_sequence, item_id, content_hash, payload_length, source_revision, jsonl_offset, jsonl_length, projection_manifest_json, tool_call_id FROM item_catalog WHERE item_id IN ({placeholders}) ORDER BY item_sequence",
                    values,
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT item_sequence, item_id, content_hash, payload_length, source_revision, jsonl_offset, jsonl_length, projection_manifest_json, tool_call_id FROM item_catalog ORDER BY item_sequence"
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
            relation_rows = connection.execute(
                "SELECT canonical_item_id, canonical_relation_ordinal, relation, source_ref, target_ref "
                "FROM item_relations WHERE canonical_item_id IS NOT NULL "
                "ORDER BY canonical_item_id, canonical_relation_ordinal"
            ).fetchall()
            relations_by_item: dict[str, list[tuple[object, ...]]] = {}
            for owner, ordinal, relation, source, target in relation_rows:
                relations_by_item.setdefault(owner, []).append(
                    (ordinal, relation, source, target)
                )
            committed_row = connection.execute(
                "SELECT committed_jsonl_offset FROM database_meta WHERE singleton_id = 1"
            ).fetchone()
            if committed_row is None:
                raise RuntimeError("database_meta 缺少 committed_jsonl_offset")
            committed = strict_non_negative_int(
                committed_row[0], field="database_meta.committed_jsonl_offset"
            )
        finally:
            if owned_connection is not None:
                owned_connection.close()
        result: list[CanonicalItemRecord] = []
        with self.jsonl_path(
            session_id,
            checkpoint_ns,
            thread_id=thread_id,
        ).open("rb") as stream:
            for (
                sequence_value,
                item_id,
                expected_hash,
                expected_payload_length,
                expected_source_revision,
                offset_value,
                length_value,
                expected_projection_manifest,
                expected_tool_call_id,
            ) in rows:
                sequence = strict_non_negative_int(
                    sequence_value,
                    field=f"item_catalog.item_sequence: {item_id}",
                )
                if not isinstance(item_id, str) or not item_id:
                    raise RuntimeError("item_catalog.item_id 必须是非空字符串")
                if not isinstance(expected_hash, str) or not expected_hash:
                    raise RuntimeError(
                        f"item_catalog.content_hash 必须是非空字符串: {item_id}"
                    )
                payload_length_value = strict_non_negative_int(
                    expected_payload_length,
                    field=f"item_catalog.payload_length: {item_id}",
                )
                if not isinstance(expected_source_revision, str):
                    raise TypeError(
                        f"item_catalog.source_revision 必须是字符串: {item_id}"
                    )
                offset = strict_non_negative_int(
                    offset_value,
                    field=f"item_catalog.jsonl_offset: {item_id}",
                )
                length = strict_non_negative_int(
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
                source_revision = (
                    item.source_manifest.source_revision
                    if item.source_manifest is not None
                    else None
                )
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
                validate_catalog_projection(
                    item,
                    projection_manifest_json=expected_projection_manifest,
                    tool_call_id=expected_tool_call_id,
                    relation_rows=relations_by_item.get(item.item_id, ()),
                )
                result.append(item)
        return result


__all__ = ["RolloutItemsMixin"]
