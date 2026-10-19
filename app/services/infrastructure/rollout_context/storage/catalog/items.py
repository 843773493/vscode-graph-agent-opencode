"""v2 canonical catalog 写入与按 SQLite locator 读取。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING

from app.core.hashing import sha256_hex as _hash_bytes
from app.domain.itemized.enums import (
    CommitKind,
    CommitMode,
    ControlOutcome,
    SemanticKind,
)
from app.domain.itemized.hashing import (
    payload_content_length,
)
from app.domain.itemized.identity.refs import ContextRef, ref_identity
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.resource_platform.virtual_resources.resolver import (
    check_resource_target,
)
from app.services.infrastructure.resource_platform.virtual_resources.values import (
    ResourceTargetAction,
    ResourceTargetContext,
    ResourceTargetFacts,
)
from app.services.infrastructure.rollout_context.storage.catalog.history_scope import (
    current_scope,
    read_local_scopes,
)
from app.services.infrastructure.rollout_context.storage.catalog.integrity import (
    read_catalog_body,
    validate_catalog_projection,
)
from app.services.infrastructure.rollout_context.storage.catalog.message_groups import (
    read_message_group,
)
from app.services.infrastructure.rollout_context.storage.catalog.tool_result_identity import (
    _strict_db_optional_text,
    _strict_db_text,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    RequestMessageGroup,
    RolloutNotMaterializedError,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_line as _v2_json_line,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_text as _json,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_optional_text,
    strict_text,
    validate_item_storage_projection,
)

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.storage.primitives import (
        RolloutReadSnapshot,
    )


class RolloutItemsMixin:
    """只负责 canonical item 的 JSONL/catalog transaction 与定位读取。"""

    @staticmethod
    def _require_canonical_model_call(
        connection: sqlite3.Connection, *, item_id: str, model_call_id: str,
    ) -> str:
        """正文 producer 必须绑定同一历史 execution 的真实 ModelCall 与 assembly。"""
        row = connection.execute(
            "SELECT call.assembly_id,execution.session_id,execution.thread_id,execution.execution_id,"
            "call.session_id,call.thread_id,call.execution_id,"
            "assembly.session_id,assembly.thread_id,assembly.execution_id "
            "FROM item_catalog item LEFT JOIN executions execution ON execution.execution_id=item.execution_id "
            "LEFT JOIN model_calls call ON call.model_call_id=? "
            "LEFT JOIN context_assemblies assembly ON assembly.assembly_id=call.assembly_id "
            "WHERE item.item_id=?", (model_call_id, item_id),
        ).fetchone()
        if row is None or row[4] is None:
            raise ValueError(f"source-mismatch: canonical item 的 ModelCall 缺失: {item_id}/{model_call_id}")
        if not isinstance(row[0], str) or not row[0] or row[7] is None:
            raise ValueError(f"source-mismatch: canonical item 的 ModelCall assembly 缺失: {item_id}/{model_call_id}")
        if row[1] is None or row[1:4] != row[4:7] or row[4:7] != row[7:10]:
            raise ValueError(f"source-mismatch: canonical item 的 ModelCall/assembly 历史 execution 不一致: {item_id}/{model_call_id}")
        return row[0]

    @staticmethod
    def check_fork_target_resources(
        connection: sqlite3.Connection, source: ResourceTargetFacts, target: ResourceTargetContext,
        *, choices: Mapping[str, ResourceTargetAction], selected_only: bool,
    ) -> None:
        """canonical 和工具正文由原 catalog 提供内容身份，不解码或重算正文。"""
        selection = " JOIN fork_selected_items selected ON selected.item_id=item.item_id" if selected_only else ""
        for revision, digest, length, item_id, producer_call, projection_call in connection.execute(
            "SELECT item.source_revision,item.content_hash,item.payload_length,item.item_id,"
            "json_extract(item.producer_ref_json,'$.model_call_id'),"
            "json_extract(item.projection_manifest_json,'$.projection_identity.model_call_id') "
            "FROM item_catalog item" + selection,
        ):
            if producer_call is not None and projection_call is not None and producer_call != projection_call:
                raise ValueError(f"source-mismatch: canonical item 的 ModelCall 身份冲突: {item_id}")
            call_id = producer_call or projection_call
            if call_id is not None:
                RolloutItemsMixin._require_canonical_model_call(connection, item_id=item_id, model_call_id=call_id)
            decision = check_resource_target(
                replace(source, revision=revision, content_hash=digest, content_length=length),
                target, choice=choices.get(source.resource_uri),
            )
            if decision.action != "copy_snapshot":
                raise ValueError("canonical 正文 owner 只支持冻结快照导入")

    def has_committed_tool_result(self, session_id: str, thread_id: str, model_call_id: str, tool_call_id: str) -> bool:
        """只读 canonical SQL locator，给邮箱 ack 恢复提供真实提交证明。"""
        with self._lock(thread_id, "", session_id=session_id), self._connect(
            thread_id, session_id=session_id, read_only=True,
        ) as connection:
            connection.execute("BEGIN")
            self._validate_schema_state(connection)
            self._require_v2_runtime(connection)
            owner = connection.execute(
                "SELECT session_id,thread_id,database_state,committed_jsonl_offset "
                "FROM database_meta WHERE singleton_id=1"
            ).fetchone()
            if owner is None or owner[:2] != (session_id, thread_id) or owner[2] != "active":
                raise RuntimeError("邮箱恢复的 canonical rollout owner/state 不一致")
            committed_offset = strict_non_negative_int(owner[3], field="database_meta.committed_jsonl_offset")
            if self.jsonl_path(session_id, thread_id=thread_id).stat().st_size < committed_offset:
                raise RuntimeError("邮箱恢复的 canonical JSONL 小于已提交边界")
            rows = connection.execute(
                "SELECT item_id,jsonl_offset,jsonl_length,commit_id FROM item_catalog "
                "INDEXED BY item_catalog_background_result_identity WHERE semantic_kind='tool_result' "
                "AND status='completed' AND tool_call_id IN (?,?) "
                "AND json_extract(projection_manifest_json,'$.projection_identity.model_call_id')=? LIMIT 2",
                (tool_call_id, f"{model_call_id}:tool-call:{tool_call_id}", model_call_id),
            ).fetchall()
            if len(rows) > 1:
                raise RuntimeError("邮箱消费的 canonical ToolResult identity 歧义")
            if rows:
                _, offset, length, commit_id = rows[0]
                commit = connection.execute(
                    "SELECT status,jsonl_start_offset,jsonl_end_offset FROM storage_commits WHERE commit_id=?",
                    (commit_id,),
                ).fetchone()
                if commit is None or commit[0] != "committed" or not (
                    0 <= commit[1] <= offset < offset + length <= commit[2] <= committed_offset
                ):
                    raise RuntimeError("邮箱恢复的 canonical ToolResult 缺少真实提交边界")
            return bool(rows)

    def _insert_canonical_item(
        self,
        connection: sqlite3.Connection,
        item: CanonicalItemRecord,
        *,
        commit_id: int,
        jsonl_offset: int,
        jsonl_length: int,
        commit_recorded_at: str,
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
        self._insert_item_projection(connection, item, commit_recorded_at)
        self._insert_canonical_relations(connection, item)
        if item.semantic_kind == SemanticKind.USER_INPUT and isinstance(item.payload, Mapping):
            # user_input_payload 的 typed 附件槽在首次 canonical 提交时登记。
            # 同一附件的多次使用分别归各 item，不借用 blob owner 的单 item_ref。
            attachments = item.payload.get("attachments", [])
            if not isinstance(attachments, list):
                raise TypeError("UserInput attachments 必须是 typed list")
            for ordinal, attachment in enumerate(attachments):
                if not isinstance(attachment, Mapping):
                    raise TypeError("UserInput attachment 必须是 typed mapping")
                file_id = strict_text(attachment.get("file_id"), field="UserInput.attachment.file_id")
                connection.execute(
                    "INSERT INTO item_resource_references VALUES (?, ?, 'attachment', ?)",
                    (item.item_id, ordinal, file_id),
                )
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



    def read_request_groups(
        self, session_id: str, thread_id: str, *, turn_id: str | None,
        execution_id: str, message_ids: Sequence[str], checkpoint_ns: str = "",
        writer_connection: sqlite3.Connection | None = None,
    ) -> tuple[RequestMessageGroup, ...]:
        """读取请求 group 的 canonical locator 和 admission；不解析消息协议。"""
        session_id = strict_text(session_id, field="session_id")
        execution_id = strict_text(execution_id, field="execution_id")
        if turn_id is not None:
            turn_id = strict_text(turn_id, field="turn_id")
        groups: list[RequestMessageGroup] = []
        with self.read_connection(session_id, thread_id, checkpoint_ns, connection=writer_connection) as connection:
            self._require_v2_runtime(connection)
            admission = connection.execute(
                "SELECT turn_id,execution_origin FROM executions "
                "WHERE session_id=? AND thread_id=? AND execution_id=?",
                (session_id, thread_id, execution_id),
            ).fetchone()
            if admission is None:
                raise RuntimeError(
                    "request item canonicalization 缺少当前 SessionThread 的 durable execution admission: "
                    f"session_id={session_id}, thread_id={thread_id}, execution_id={execution_id}"
                )
            admitted_turn_id = _strict_db_optional_text(admission[0], field="executions.turn_id")
            origin = _strict_db_text(admission[1], field="executions.execution_origin")
            if turn_id != admitted_turn_id:
                raise RuntimeError(f"request item Turn 与 durable execution admission 不一致: execution_id={execution_id}")
            if origin == "user_turn" and turn_id is None:
                raise RuntimeError("user_turn request item admission 缺少真实 Turn")
            if origin == "internal" and turn_id is not None:
                raise RuntimeError("internal request item admission 必须 Turn-less")
            with self.jsonl_path(session_id, checkpoint_ns, thread_id=thread_id).open("rb") as stream:
                for message_id in message_ids:
                    indexed = connection.execute(
                        "SELECT turn_id,execution_id,jsonl_offset,jsonl_length FROM messages WHERE message_id=?", (message_id,),
                    ).fetchone()
                    catalog = connection.execute(
                        "SELECT turn_id,execution_id,jsonl_offset,jsonl_length FROM item_catalog "
                        "WHERE jsonl_offset=? AND jsonl_length=?",
                        indexed[2:4],
                    ).fetchone() if indexed is not None else None
                    if indexed is not None and catalog is None:
                        raise RuntimeError(f"message projection 缺少 canonical item: {message_id}")
                    if indexed is not None and indexed[:2] != catalog[:2]:
                        raise RuntimeError(f"message projection 与 canonical item attribution 不一致: {message_id}")
                    attribution = catalog[:2] if catalog is not None else (turn_id, execution_id)
                    if not isinstance(attribution[1], str) or not attribution[1]:
                        raise RuntimeError(f"request message 缺少 canonical execution attribution: {message_id}")
                    items = tuple(read_message_group(
                        connection, stream, offset=catalog[2], length=catalog[3], read_item=self._read_v2_item_at,
                    )) if catalog is not None else ()
                    groups.append(RequestMessageGroup(message_id, attribution[0], attribution[1], items))
        return tuple(groups)


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

    def read_ref_items(
        self,
        session_id: str,
        thread_id: str,
        *,
        refs: Iterable[ContextRef],
        checkpoint_ns: str = "",
        snapshot: RolloutReadSnapshot | None = None,
    ) -> dict[tuple[str, str, str], CanonicalItemRecord]:
        """按 ref 的精确线程读取；已捕获历史只消费目标本地冻结正文。

        read_items 仍是单物理 rollout 的 locator 原语；ContextRef 消费者
        只能从这里取得带身份的 registry，不能先压成一组裸 item ID。
        """
        references = tuple(refs)
        if any(not isinstance(ref, ContextRef) or ref.ref_type != "canonical_item" for ref in references):
            raise TypeError("canonical ref reader 只接受 canonical ContextRef")
        with self.read_connection(session_id, thread_id, checkpoint_ns, snapshot=snapshot) as connection:
            owner = current_scope(connection)
            if owner != (session_id, thread_id):
                raise ValueError("source-mismatch: canonical ref reader 物理 owner 不一致")
            local_scopes = read_local_scopes(connection)
            groups: dict[tuple[str, str], list[ContextRef]] = {}
            for ref in references:
                groups.setdefault((ref.session_id, ref.thread_id), []).append(ref)
            result: dict[tuple[str, str, str], CanonicalItemRecord] = {}
            for scope, group in groups.items():
                # binding 是已捕获历史的权威；本地缺失必须报错，不能在线回源。
                local = scope in local_scopes
                items = self.read_items(
                    *(owner if local else scope), checkpoint_ns=checkpoint_ns,
                    item_ids=(ref.ref_id for ref in group),
                    connection=connection if local else None,
                )
                by_id = {item.item_id: item for item in items}
                for ref in group:
                    result[ref_identity(ref)] = by_id[ref.ref_id]
            return result

    def read_items(
        self,
        session_id: str,
        thread_id: str,
        *,
        checkpoint_ns: str = "",
        item_ids: Iterable[str] | None = None,
        snapshot: RolloutReadSnapshot | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> list[CanonicalItemRecord]:
        """在一致的 rollout 读边界内读取已提交 v2 item。

        普通读取持有 rollout 文件锁，writer 读取复用已持锁的事务连接，直到
        JSONL 正文完成校验；否则 compaction/backup 可能在 SQLite locator
        已读出后改写 JSONL，形成跨文件的半个快照。
        """
        values = tuple(dict.fromkeys(item_ids if item_ids is not None else ()))
        if any(not isinstance(item_id, str) or not item_id for item_id in values):
            raise TypeError("canonical item_ids 必须是非空字符串")
        try:
            with self.read_connection(
                session_id, thread_id, checkpoint_ns, snapshot=snapshot,
                connection=connection,
            ) as read_connection:
                self._require_v2_readable(read_connection)
                # None 请求全部；显式空选集仍须过滤，不得读回全部历史。
                if item_ids is not None:
                    placeholders = ",".join("?" for _ in values)
                    rows = read_connection.execute(
                        f"SELECT item_sequence, item_id, content_hash, payload_length, source_revision, jsonl_offset, jsonl_length, projection_manifest_json, tool_call_id FROM item_catalog WHERE item_id IN ({placeholders}) ORDER BY item_sequence",
                        values,
                    ).fetchall()
                else:
                    rows = read_connection.execute(
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
                relation_rows = read_connection.execute(
                    "SELECT canonical_item_id, canonical_relation_ordinal, relation, source_ref, target_ref "
                    "FROM item_relations WHERE canonical_item_id IS NOT NULL "
                    "ORDER BY canonical_item_id, canonical_relation_ordinal"
                ).fetchall()
                relations_by_item: dict[str, list[tuple[object, ...]]] = {}
                for owner, ordinal, relation, source, target in relation_rows:
                    relations_by_item.setdefault(owner, []).append(
                        (ordinal, relation, source, target)
                    )
                committed_row = read_connection.execute(
                    "SELECT committed_jsonl_offset FROM database_meta WHERE singleton_id = 1"
                ).fetchone()
                if committed_row is None:
                    raise RuntimeError("database_meta 缺少 committed_jsonl_offset")
                committed = strict_non_negative_int(
                    committed_row[0], field="database_meta.committed_jsonl_offset"
                )
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
                        item = read_catalog_body(
                            raw, sequence=sequence, item_id=item_id,
                            catalog_hash=expected_hash, payload_length=payload_length_value,
                            source_revision=expected_source_revision,
                        )
                        validate_catalog_projection(
                            item,
                            projection_manifest_json=expected_projection_manifest,
                            tool_call_id=expected_tool_call_id,
                            relation_rows=relations_by_item.get(item.item_id, ()),
                        )
                        result.append(item)
                return result
        except RolloutNotMaterializedError:
            if values:
                raise
            return []

    @staticmethod
    def _bind_canonical_item_execution(
        connection: sqlite3.Connection,
        *,
        item_id: str,
        execution_id: str,
    ) -> None:
        """在同一 writer 事务内把 canonical item 绑定到确定的 execution。"""
        row = connection.execute(
            "SELECT execution_id FROM item_catalog WHERE item_id = ?",
            (item_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"canonical item 缺少 item_catalog: {item_id}")
        current_execution_id = strict_optional_text(
            row[0], field=f"item_catalog.execution_id:{item_id}"
        )
        if current_execution_id is not None:
            if current_execution_id != execution_id:
                raise RuntimeError(
                    "canonical item 已绑定不同 execution: "
                    f"item={item_id}, execution={current_execution_id}"
                )
            return
        cursor = connection.execute(
            "UPDATE item_catalog SET execution_id = ? "
            "WHERE item_id = ? AND execution_id IS NULL",
            (execution_id, item_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(f"canonical item execution 绑定失败: {item_id}")


__all__ = ["RolloutItemsMixin"]
