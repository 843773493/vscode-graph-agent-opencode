"""view 前缀与 typed 正文闭包；可见成员和冷读支撑事实分别选择。"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from app.core.sqlite_state import load_sql
from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.identity.detail_ref import DetailRef
from app.domain.itemized.identity.refs import ContextRef
from app.services.infrastructure.rollout_context.assembly.detail_identity import (
    detail_ref_from_key,
    detail_ref_key,
)
from app.services.infrastructure.rollout_context.checkpoint.codec.pending_messages import (
    MESSAGE_REFERENCE_SERIALIZER,
    restore_message_write,
)
from app.services.infrastructure.rollout_context.checkpoint.durable.message_writes import (
    PendingMessageReferences,
    decode_message_reference,
)
from app.services.infrastructure.rollout_context.checkpoint.durable.writes import (
    _pending_write_row,
)
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    detail_relative_path,
)
from app.services.infrastructure.rollout_context.runtime.detail_payload import (
    parse_detail_payload,
)
from app.services.infrastructure.rollout_context.storage.catalog.history_scope import (
    current_scope,
    require_historical_scope,
)
from app.services.infrastructure.rollout_context.storage.catalog.message_revisions import (
    validate_message_revision_source,
)
from app.services.infrastructure.rollout_context.storage.checkpoint_lineage import (
    delta_history_path,
)

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.storage.service import (
        RolloutStorage,
    )

_BATCH_SIZE = 256
_BUFFER_SIZE = 1024 * 1024

# 有效前缀按模型 selection 截止；历史 logical 坐标只负责展示相对序。
EFFECTIVE_PREFIX_SQL = (
    "SELECT member.item_id,member.logical_item_ordinal,member.context_ordinal "
    "FROM context_view_items member JOIN context_view_items anchor "
    "ON anchor.view_id=member.view_id AND anchor.item_id=? "
    "AND anchor.visible=1 AND anchor.context_selected=1 "
    "WHERE member.view_id=? AND member.visible=1 AND member.context_selected=1 "
    "AND member.context_ordinal<=anchor.context_ordinal-?"
)


def _include_detail(connection: sqlite3.Connection, ref: DetailRef) -> None:
    """只消费领域 owner 已解析的 DetailRef，不解释普通 JSON 正文。"""
    require_historical_scope(connection, ref.session_id, ref.thread_id)
    key = detail_ref_key(ref)
    row = connection.execute(
        "SELECT session_id,thread_id,assembly_id FROM context_plan_details WHERE detail_ref=?", (key,),
    ).fetchone()
    if row != (ref.session_id, ref.thread_id, ref.assembly_id):
        raise ValueError("detail-unavailable: typed 正文依赖缺少精确冻结 manifest")
    connection.execute(
        "INSERT OR IGNORE INTO fork_selected_details(detail_ref) VALUES (?)", (key,),
    )
    connection.execute(
        "INSERT OR IGNORE INTO fork_selected_assemblies(assembly_id) "
        "SELECT assembly_id FROM context_assemblies WHERE assembly_id=?", (ref.assembly_id,),
    )


def _include_context_ref(connection: sqlite3.Connection, ref: ContextRef) -> None:
    require_historical_scope(connection, ref.session_id, ref.thread_id)
    if ref.ref_type == "canonical_item":
        row = connection.execute(
            "SELECT content_hash,payload_length,source_revision,item_sequence "
            "FROM item_catalog WHERE item_id=?", (ref.ref_id,),
        ).fetchone()
        if row != (ref.content_hash, ref.content_length, ref.source_revision, ref.item_sequence):
            raise ValueError("source-mismatch: typed canonical 正文依赖缺失或 manifest 不一致")
        connection.execute(
            "INSERT OR IGNORE INTO fork_selected_items(item_id) VALUES (?)", (ref.ref_id,),
        )
    elif isinstance(ref.source_ref, DetailRef):
        _include_detail(connection, ref.source_ref)


def select_view_prefix(
    storage: RolloutStorage, connection: sqlite3.Connection, *, stage: Path,
    source_view_id: str, anchor_item_id: str | None, anchor_mode: str,
    source_view_revision: int | None, source_checkpoint_id: str | None,
) -> None:
    """SQLite 去重、防环；只有可见根展开 canonical 正文，封存详情留作 backing。"""
    revision = connection.execute(
        "SELECT history_view_revision FROM database_meta WHERE singleton_id=1",
    ).fetchone()[0]
    if source_view_revision is not None and source_view_revision != revision:
        raise ValueError("source-mismatch: fork source view revision 已变化")
    if anchor_mode not in {"inclusive", "before"}:
        raise ValueError("fork anchor_mode 必须是 inclusive 或 before")
    anchor = connection.execute(
        "SELECT context_ordinal FROM context_view_items "
        "WHERE view_id=? AND item_id=? AND visible=1 AND context_selected=1", (source_view_id, anchor_item_id),
    ).fetchone()
    if anchor_item_id is not None and (anchor is None or anchor[0] is None):
        raise ValueError("source-mismatch: 点击 item 不属于冻结 view 的有效上下文")
    if anchor is None:
        empty = connection.execute(
            "SELECT 1 FROM context_view_items WHERE view_id=? AND visible=1 LIMIT 1", (source_view_id,),
        ).fetchone()
        if empty is not None:
            raise ValueError("fork_source_not_completed: source view 没有可复制的安全锚点")
    connection.execute(
        load_sql(__package__, "sql/fork_selected_items.sql"),
    )
    connection.execute(
        load_sql(__package__, "sql/fork_selected_assemblies.sql"),
    )
    connection.execute(
        load_sql(__package__, "sql/fork_selected_details.sql"),
    )
    connection.execute(
        load_sql(__package__, "sql/fork_selected_model_calls.sql"),
    )
    connection.execute(
        load_sql(__package__, "sql/fork_selected_source_checkpoints.sql"),
    )
    connection.execute(
        load_sql(__package__, "sql/fork_selected_activations.sql"),
    )
    connection.execute(
        "INSERT INTO fork_selected_items(item_id,visible_ordinal) "
        f"SELECT item_id,logical_item_ordinal FROM ({EFFECTIVE_PREFIX_SQL})",
        (anchor_item_id, source_view_id, int(anchor_mode == "before")),
    )
    if anchor_item_id is None and source_checkpoint_id is not None:
        binding = connection.execute(
            "SELECT json_extract(metadata_json,'$.boxteam_execution_binding') "
            "FROM checkpoints WHERE checkpoint_id=?", (source_checkpoint_id,),
        ).fetchone()
        if binding is None or binding[0] is None:
            raise ValueError("source-mismatch: 空上下文缺少精确 internal checkpoint binding")
        frozen = json.loads(binding[0])
        owner = connection.execute(
            "SELECT session_id,thread_id,job_id,admission_idempotency_key,admission_payload_hash "
            "FROM executions WHERE execution_id=? AND execution_origin='internal' "
            "AND turn_id IS NULL AND outcome IN ('completed','completed_empty')",
            (frozen["execution_id"],),
        ).fetchone()
        if owner is None or frozen != {"execution_id": frozen["execution_id"], "job_id": owner[2],
                                      "admission_idempotency_key": owner[3], "admission_payload_hash": owner[4]}:
            raise ValueError("source-mismatch: 空上下文 internal admission 与 checkpoint 不一致")
        require_historical_scope(connection, owner[0], owner[1])
        # metadata-only seed 没有 canonical producer；只展开该冻结 execution 的真实封存正文。
        assemblies = connection.execute(
            "INSERT INTO fork_selected_assemblies(assembly_id,expand_body) "
            "SELECT assembly_id,1 FROM context_assemblies WHERE session_id=? AND thread_id=? "
            "AND execution_id=? AND status='terminal'", (*owner[:2], frozen["execution_id"]),
        )
        if assemblies.rowcount < 1:
            raise ValueError("source-mismatch: 空 internal checkpoint 缺少真实完成的封存 assembly")
    checkpoints_frozen = False
    with (stage / "rollout.jsonl").open("rb") as stream:
        while True:
            items = connection.execute(
                "SELECT item.item_id,item.item_sequence,item.jsonl_offset,item.jsonl_length,"
                "selected.visible_ordinal FROM fork_selected_items selected "
                "JOIN item_catalog item ON item.item_id=selected.item_id "
                "WHERE selected.visited=0 ORDER BY item.item_sequence LIMIT ?", (_BATCH_SIZE,),
            ).fetchall()
            for item_id, sequence, offset, length, visible in items:
                item = storage._read_v2_item_at(
                    connection, stream, sequence=sequence, offset=offset, length=length,
                )
                connection.execute("UPDATE fork_selected_items SET visited=1 WHERE item_id=?", (item_id,))
                source = validate_message_revision_source(connection, item)
                if source is not None:
                    connection.execute(
                        "INSERT OR IGNORE INTO fork_selected_items(item_id) VALUES (?)", (source.item_id,),
                    )
                if item.message_group_id is not None:
                    connection.execute(
                        "INSERT OR IGNORE INTO fork_selected_items(item_id) "
                        "SELECT item_id FROM item_catalog WHERE message_group_id=?", (item.message_group_id,),
                    )
                if item.turn_id is not None:
                    connection.execute(
                        "INSERT OR IGNORE INTO fork_selected_items(item_id) "
                        "SELECT root_input_item_id FROM turn_records WHERE turn_id=?", (item.turn_id,),
                    )
                    connection.execute(
                        "INSERT OR IGNORE INTO fork_selected_items(item_id) "
                        "SELECT final_item_id FROM turn_records WHERE turn_id=? AND final_item_id IS NOT NULL",
                        (item.turn_id,),
                    )
                producer_call = item.producer_ref.get("model_call_id")
                projection_call = item.projection_identity.model_call_id if item.projection_identity is not None else None
                if producer_call is not None and projection_call is not None and producer_call != projection_call:
                    raise ValueError(f"source-mismatch: canonical item 的 ModelCall 身份冲突: {item_id}")
                call_id = producer_call or projection_call
                if call_id is not None:
                    assembly_id = storage._require_canonical_model_call(
                        connection, item_id=item_id, model_call_id=call_id,
                    )
                    connection.execute(
                        "INSERT OR IGNORE INTO fork_selected_model_calls(model_call_id) VALUES (?)", (call_id,),
                    )
                    connection.execute(
                        "INSERT INTO fork_selected_assemblies(assembly_id,expand_body) "
                        "VALUES (?,?) "
                        "ON CONFLICT(assembly_id) DO UPDATE SET expand_body=MAX(expand_body,excluded.expand_body),"
                        "visited=CASE WHEN excluded.expand_body>expand_body THEN 0 ELSE visited END",
                        (assembly_id, int(visible is not None)),
                    )
            assemblies = connection.execute(
                "SELECT assembly_id,expand_body FROM fork_selected_assemblies WHERE visited=0 LIMIT ?", (_BATCH_SIZE,),
            ).fetchall()
            details = connection.execute(
                "SELECT detail_ref FROM fork_selected_details WHERE visited=0 LIMIT ?", (_BATCH_SIZE,),
            ).fetchall()
            activations = connection.execute(
                "SELECT session_id,thread_id,activation_snapshot_id FROM fork_selected_activations "
                "WHERE visited=0 LIMIT ?", (_BATCH_SIZE,),
            ).fetchall()
            if not items and not assemblies and not details and not activations:
                if checkpoints_frozen:
                    break
                # 先冻结原有前缀允许保留的 checkpoint，再展开 pending 正文依赖。
                # backing item 不能反过来扩大 checkpoint 集合而递归带入锚点之后的历史。
                for table in ("turns", "executions", "checkpoints"):
                    connection.execute(load_sql(__package__, f"sql/fork_selected_{table}.sql"))
                connection.execute("DROP TABLE fork_selected_turns")
                connection.execute("DROP TABLE fork_selected_executions")
                _include_delta_history(connection)
                _include_pending_message_references(storage, connection, stage=stage)
                checkpoints_frozen = True
                continue
            for assembly_id, expand_body in assemblies:
                row = connection.execute(
                    "SELECT snapshot_json,detail_ref FROM context_assemblies WHERE assembly_id=?", (assembly_id,),
                ).fetchone()
                if row is None:
                    raise ValueError("source-mismatch: 所需 ModelCall 的封存 assembly 缺失")
                assembly = ContextAssemblySnapshot.from_dict(json.loads(row[0]))
                assembly.validate_hashes()
                require_historical_scope(connection, assembly.session_id, assembly.thread_id)
                if assembly.model_call_id is not None:
                    connection.execute(
                        "INSERT OR IGNORE INTO fork_selected_model_calls(model_call_id) VALUES (?)",
                        (assembly.model_call_id,),
                    )
                predecessor_ids = {assembly.seal_binding.previous_assembly_id} if assembly.seal_binding is not None else set()
                transition = assembly.seal_binding.prefix.consumed_transition if assembly.seal_binding is not None else None
                if transition is not None and transition.reason.value in {"rewind", "compaction"}:
                    inherited = connection.execute(
                        "SELECT header.checkpoint_id,header.source_assembly_id "
                        "FROM context_epoch_transitions transition "
                        "JOIN checkpoint_context_source_snapshots header "
                        "ON header.checkpoint_id=transition.source_checkpoint_id "
                        "AND header.checkpoint_ns=transition.checkpoint_ns "
                        "WHERE transition.transition_id=? AND header.session_id=? AND header.thread_id=?",
                        (transition.transition_id, assembly.session_id, assembly.thread_id),
                    ).fetchone()
                    if inherited is None:
                        raise ValueError("source-mismatch: 封存 rebuild 缺少精确 checkpoint source selection")
                    connection.execute(
                        "INSERT OR IGNORE INTO fork_selected_source_checkpoints(checkpoint_id) VALUES (?)", (inherited[0],),
                    )
                    predecessor_ids.add(inherited[1])
                for predecessor_id in predecessor_ids - {None}:
                    # 精确前任只作封存 header 证据；-1 不展开正文，真实 producer 可升级到 0/1。
                    connection.execute(
                        "INSERT OR IGNORE INTO fork_selected_assemblies(assembly_id,visited,expand_body) VALUES (?,1,-1)",
                        (predecessor_id,),
                    )
                    connection.execute(
                        "INSERT OR IGNORE INTO fork_selected_model_calls(model_call_id) "
                        "SELECT model_call_id FROM model_calls WHERE assembly_id=?",
                        (predecessor_id,),
                    )
                activation = connection.execute(
                    "SELECT session_id,thread_id,activation_snapshot_id "
                    "FROM resource_activation_assembly_bindings WHERE assembly_id=?", (assembly_id,),
                ).fetchone()
                if activation is not None:
                    if activation[:2] != (assembly.session_id, assembly.thread_id):
                        raise ValueError("source-mismatch: assembly activation 的历史 owner 不一致")
                    connection.execute(
                        "INSERT OR IGNORE INTO fork_selected_activations "
                        "(session_id,thread_id,activation_snapshot_id) VALUES (?,?,?)", activation,
                    )
                for entry in assembly.selection:
                    if not entry.included:
                        continue
                    if expand_body and isinstance(entry.ref, ContextRef):
                        _include_context_ref(connection, entry.ref)
                    if entry.detail_ref is not None:
                        _include_detail(connection, entry.detail_ref)
                if row[1] is not None:
                    _include_detail(connection, detail_ref_from_key(row[1]))
                connection.execute(
                    "UPDATE fork_selected_assemblies SET visited=1 WHERE assembly_id=?", (assembly_id,),
                )
            for session_id, thread_id, snapshot_id in activations:
                require_historical_scope(connection, session_id, thread_id)
                activation = connection.execute(
                    "SELECT lineage_detail_ref,parent_execution_snapshot_id FROM resource_activation_snapshots "
                    "WHERE session_id=? AND thread_id=? AND activation_snapshot_id=?",
                    (session_id, thread_id, snapshot_id),
                ).fetchone()
                if activation is None:
                    raise ValueError("resource-activation-unavailable: 所需冻结 snapshot 缺失")
                lineage = detail_ref_from_key(activation[0])
                lineage.require_owner(session_id, thread_id, snapshot_id)
                _include_detail(connection, lineage)
                if activation[1] is not None:
                    connection.execute(
                        "INSERT OR IGNORE INTO fork_selected_activations "
                        "(session_id,thread_id,activation_snapshot_id) VALUES (?,?,?)",
                        (session_id, thread_id, activation[1]),
                    )
                for (key,) in connection.execute(
                    "SELECT detail_ref FROM resource_activation_bindings "
                    "WHERE session_id=? AND thread_id=? AND activation_snapshot_id=? AND detail_ref IS NOT NULL",
                    (session_id, thread_id, snapshot_id),
                ):
                    detail = detail_ref_from_key(key)
                    detail.require_owner(session_id, thread_id)
                    _include_detail(connection, detail)
                    # retry 的原正文 snapshot 是 backing；保留 lineage，不增加任何可见 item。
                    connection.execute(
                        "INSERT OR IGNORE INTO fork_selected_activations "
                        "(session_id,thread_id,activation_snapshot_id) VALUES (?,?,?)",
                        (session_id, thread_id, detail.assembly_id),
                    )
                connection.execute(
                    "UPDATE fork_selected_activations SET visited=1 "
                    "WHERE session_id=? AND thread_id=? AND activation_snapshot_id=?",
                    (session_id, thread_id, snapshot_id),
                )
            for (key,) in details:
                ref = detail_ref_from_key(key)
                row = connection.execute(
                    "SELECT availability,required,content_hash FROM context_plan_details WHERE detail_ref=?", (key,),
                ).fetchone()
                if row is None:
                    raise ValueError("detail-unavailable: 所需正文缺少冻结 manifest")
                if row[0] == "available":
                    path = stage / detail_relative_path(ref).relative_to("rollout")
                    if path.is_symlink() or not path.is_file():
                        raise ValueError("detail-unavailable: 所需正文缺少捕获文件")
                    _value, record = parse_detail_payload(path.read_bytes(), detail_ref=ref)
                    if record.content_hash != row[2]:
                        raise ValueError("source-mismatch: captured detail 与权威 manifest hash 不一致")
                elif row[1]:
                    raise ValueError("detail-unavailable: required 正文依赖不可用")
                connection.execute("UPDATE fork_selected_details SET visited=1 WHERE detail_ref=?", (key,))
    if connection.execute(
        "SELECT 1 FROM fork_selected_items selected LEFT JOIN item_catalog item "
        "ON item.item_id=selected.item_id WHERE item.item_id IS NULL LIMIT 1",
    ).fetchone():
        raise ValueError("source-mismatch: 前缀正文闭包存在缺失 canonical item")
    connection.execute(
        load_sql(__package__, "sql/fork_visible_messages.sql"),
    )


def _include_delta_history(connection: sqlite3.Connection) -> None:
    """只展开已冻结 checkpoint 的 on-path 基准和中间写入，不扩大可见前缀。"""
    pending = {row[0] for row in connection.execute("SELECT checkpoint_id FROM fork_selected_checkpoints")}
    expanded: set[str] = set()
    while pending:
        checkpoint_id = pending.pop()
        expanded.add(checkpoint_id)
        for checkpoint_ns, channel in connection.execute(
            "SELECT checkpoint_ns,channel_name FROM checkpoints JOIN checkpoint_channels USING(checkpoint_id) "
            "WHERE checkpoint_id=? AND (value_state='delta' "
            "OR (value_state='absent' AND channel_version IS NOT NULL))", (checkpoint_id,),
        ).fetchall():
            for ancestor in delta_history_path(
                connection, checkpoint_id, checkpoint_ns=checkpoint_ns, channels=(channel,),
            ):
                connection.execute(
                    "INSERT INTO fork_selected_checkpoints(checkpoint_id) "
                    "SELECT ? WHERE NOT EXISTS(SELECT 1 FROM fork_selected_checkpoints WHERE checkpoint_id=?)",
                    (ancestor, ancestor),
                )
                if ancestor not in expanded:
                    pending.add(ancestor)
    # 冻结 view 的全部成员是恢复支撑，原 selection 坐标保持不变。
    connection.execute(
        "INSERT OR IGNORE INTO fork_selected_items(item_id) "
        "SELECT member.item_id FROM context_view_items member JOIN checkpoints checkpoint "
        "ON member.view_id=checkpoint.view_id JOIN fork_selected_checkpoints USING(checkpoint_id) "
        "UNION SELECT member.item_id FROM context_view_items member JOIN checkpoint_channels channel "
        "ON member.view_id=channel.context_view_id JOIN fork_selected_checkpoints USING(checkpoint_id)",
    )


def _include_pending_message_references(storage: RolloutStorage, connection: sqlite3.Connection, *, stage: Path) -> None:
    session_id, thread_id = current_scope(connection)
    for row in connection.execute(
        "SELECT pending.task_id,pending.task_path,pending.write_index,pending.channel,pending.serializer_name,"
        "pending.value_blob,pending.value_length,pending.value_hash,pending.status,pending.created_at,checkpoint.checkpoint_ns "
        "FROM pending_writes pending JOIN fork_selected_checkpoints selected USING(checkpoint_id) "
        "JOIN checkpoints checkpoint USING(checkpoint_id) WHERE substr(pending.serializer_name,1,?)=?",
        (len(MESSAGE_REFERENCE_SERIALIZER), MESSAGE_REFERENCE_SERIALIZER),
    ).fetchall():
        _pending_write_row(tuple(row[:10]))
        references = PendingMessageReferences(
            storage, connection, session_id=session_id, thread_id=thread_id,
            checkpoint_ns=row[10], jsonl_path=stage / "rollout.jsonl",
        )

        def include(reference, *, references=references):
            message = references.read(reference)
            connection.executemany(
                "INSERT OR IGNORE INTO fork_selected_items(item_id) VALUES (?)",
                ((item_id,) for item_id, _digest in reference["group"]),
            )
            return message

        descriptor = decode_message_reference(storage, row[4], row[5])
        restore_message_write(descriptor["write"], include)


def prune_view_prefix(
    connection: sqlite3.Connection, *, stage: Path,
) -> None:
    """裁去必要集合之外的事实；selection 已由同一事务的 capture 内核固定。"""
    _prune_prefix_facts(connection)
    _copy_selected_jsonl(connection, stage)
    details_root = stage / "context-plan-details"
    if details_root.exists():
        for directory, _subdirs, files in os.walk(details_root):
            for name in files:
                path = Path(directory) / name
                key = detail_ref_key(DetailRef(
                    storage_scope[0], storage_scope[1], path.parent.name, path.name,
                )) if (storage_scope := _detail_file_owner(connection, path.parent.name, path.name)) else None
                if key is None or connection.execute(
                    "SELECT 1 FROM fork_selected_details WHERE detail_ref=?", (key,),
                ).fetchone() is None:
                    path.unlink()


def _detail_file_owner(connection: sqlite3.Connection, assembly_id: str, detail_id: str) -> tuple[str, str] | None:
    row = connection.execute(
        "SELECT session_id,thread_id FROM context_plan_details WHERE assembly_id=? AND detail_id=?",
        (assembly_id, detail_id),
    ).fetchone()
    return row


def _prune_prefix_facts(connection: sqlite3.Connection) -> None:
    """保留关系由各事实 owner 明确列出，不凭同名列推断业务依赖。"""
    connection.execute(
        load_sql(__package__, "sql/fork_selected_turns.sql"),
    )
    connection.execute(
        load_sql(__package__, "sql/fork_selected_executions.sql"),
    )
    connection.execute(
        load_sql(__package__, "sql/fork_selected_plans.sql"),
    )
    connection.execute(
        "DELETE FROM messages WHERE NOT EXISTS(SELECT 1 FROM item_catalog item "
        "JOIN fork_selected_items selected ON selected.item_id=item.item_id "
        "WHERE item.jsonl_offset=messages.jsonl_offset AND item.jsonl_length=messages.jsonl_length)",
    )
    for table in ("message_projections", "reasoning_blocks"):
        connection.execute(
            f"DELETE FROM {table} WHERE message_sequence NOT IN (SELECT message_sequence FROM messages)",
        )
    connection.execute(
        "DELETE FROM tool_calls WHERE assistant_message_sequence NOT IN (SELECT message_sequence FROM messages)",
    )
    for table in ("item_resource_references", "item_catalog", "item_projections", "item_parts", "operation_anchors", "context_view_items"):
        connection.execute(
            f"DELETE FROM {table} WHERE item_id NOT IN (SELECT item_id FROM fork_selected_items)",
        )
    for table in ("turns", "turn_records", "turn_acceptances", "context_view_turns"):
        connection.execute(
            f"DELETE FROM {table} WHERE turn_id NOT IN (SELECT turn_id FROM fork_selected_turns)",
        )
    for table in ("executions", "turn_execution_links"):
        connection.execute(
            f"DELETE FROM {table} WHERE execution_id NOT IN (SELECT execution_id FROM fork_selected_executions)",
        )
    connection.execute(
        "DELETE FROM model_calls WHERE model_call_id NOT IN (SELECT model_call_id FROM fork_selected_model_calls)",
    )
    for table in ("context_assemblies", "assembly_item_refs", "context_assembly_contributions", "context_assembly_selections"):
        connection.execute(
            f"DELETE FROM {table} WHERE assembly_id NOT IN (SELECT assembly_id FROM fork_selected_assemblies)",
        )
    connection.execute(
        "DELETE FROM context_plans WHERE plan_id NOT IN (SELECT plan_id FROM fork_selected_plans)",
    )
    connection.execute(
        "DELETE FROM resource_activation_assembly_bindings "
        "WHERE assembly_id NOT IN (SELECT assembly_id FROM fork_selected_assemblies)",
    )
    for table in ("resource_activation_bindings", "resource_activation_snapshots"):
        connection.execute(
            f"DELETE FROM {table} WHERE NOT EXISTS(SELECT 1 FROM fork_selected_activations selected "
            f"WHERE selected.session_id={table}.session_id AND selected.thread_id={table}.thread_id "
            f"AND selected.activation_snapshot_id={table}.activation_snapshot_id)",
        )
    for table in ("checkpoints", "checkpoint_channels", "pending_writes"):
        connection.execute(
            f"DELETE FROM {table} WHERE checkpoint_id NOT IN (SELECT checkpoint_id FROM fork_selected_checkpoints)",
        )
    connection.execute(
        "DELETE FROM checkpoint_state_values WHERE value_id NOT IN "
        "(SELECT value_id FROM checkpoint_channels WHERE value_id IS NOT NULL)",
    )
    for table in ("checkpoint_context_source_snapshots", "checkpoint_context_source_states"):
        connection.execute(
            f"DELETE FROM {table} WHERE checkpoint_id NOT IN "
            "(SELECT checkpoint_id FROM fork_selected_checkpoints UNION "
            "SELECT checkpoint_id FROM fork_selected_source_checkpoints)",
        )
    connection.execute(
        "DELETE FROM context_plan_details WHERE detail_ref NOT IN (SELECT detail_ref FROM fork_selected_details)",
    )
    connection.execute(
        "DELETE FROM storage_commits WHERE commit_mode='item_bearing' AND commit_id NOT IN "
        "(SELECT commit_id FROM item_catalog WHERE commit_id IS NOT NULL)",
    )


def _copy_selected_jsonl(connection: sqlite3.Connection, stage: Path) -> None:
    source = stage / "rollout.jsonl"
    destination = stage / ".prefix.jsonl"
    connection.execute(
        load_sql(__package__, "sql/fork_item_offsets.sql"),
    )
    cursor = connection.execute(
        "SELECT jsonl_offset,jsonl_length FROM item_catalog ORDER BY item_sequence",
    )
    with source.open("rb") as reader, destination.open("xb") as writer:
        while rows := cursor.fetchmany(_BATCH_SIZE):
            for offset, length in rows:
                new_offset = writer.tell()
                reader.seek(offset)
                remaining = length
                while remaining:
                    chunk = reader.read(min(remaining, _BUFFER_SIZE))
                    if not chunk:
                        raise ValueError("source-mismatch: captured canonical locator 短读")
                    writer.write(chunk)
                    remaining -= len(chunk)
                connection.execute(
                    "INSERT INTO fork_item_offsets VALUES (?,?,?,?)",
                    (offset, offset + length, new_offset, writer.tell()),
                )
        size = writer.tell()
        writer.flush()
        os.fsync(writer.fileno())
    for table in ("messages", "item_catalog"):
        connection.execute(
            f"UPDATE {table} SET jsonl_offset=(SELECT new_offset FROM fork_item_offsets "
            f"WHERE old_offset={table}.jsonl_offset AND old_end={table}.jsonl_offset+{table}.jsonl_length)",
        )
    for column in ("jsonl_start_offset", "jsonl_end_offset", "jsonl_offset_before", "jsonl_offset_after"):
        connection.execute(
            f"UPDATE storage_commits SET {column}=COALESCE("
            f"(SELECT MAX(new_end) FROM fork_item_offsets WHERE old_end<=storage_commits.{column}),0)",
        )
    connection.execute(
        "UPDATE storage_commits SET jsonl_record_count=(SELECT COUNT(*) FROM item_catalog "
        "WHERE item_catalog.commit_id=storage_commits.commit_id)",
    )
    connection.execute(
        "UPDATE database_meta SET committed_jsonl_offset=?,"
        "last_item_sequence=COALESCE((SELECT MAX(item_sequence) FROM item_catalog),0),"
        "last_message_sequence=COALESCE((SELECT MAX(message_sequence) FROM messages),0),"
        "last_commit_id=(SELECT MAX(commit_id) FROM storage_commits) WHERE singleton_id=1", (size,),
    )
    destination.replace(source)


__all__ = ["EFFECTIVE_PREFIX_SQL", "prune_view_prefix", "select_view_prefix"]
