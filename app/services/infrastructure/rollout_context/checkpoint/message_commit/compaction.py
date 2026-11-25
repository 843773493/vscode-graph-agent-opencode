"""候选采用的 canonical 摘要与精确消息选择；提交仍归 persistence。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping

from app.core.execution_admission_context import require_current_execution_admission
from app.domain.itemized.compaction import (
    FrozenCompactionRange,
    FrozenSummaryResult,
    compaction_selection_ids,
)
from app.domain.itemized.enums import (
    CanonicalItemStatus,
    PayloadKind,
    SemanticKind,
    TurnScope,
)
from app.domain.itemized.identity.item_projection import (
    ItemProjectionIdentity,
    ItemProjectionManifest,
    ItemProvenanceRelation,
)
from app.domain.itemized.records import CanonicalItemRecord
from app.prompting import (
    PromptSection,
    internal_message_factory,
)
from app.services.infrastructure.rollout_context.checkpoint.codec.metadata import (
    message_annotations,
)
from app.services.infrastructure.rollout_context.checkpoint.message_commit.prepare import (
    MessageBatch,
)
from app.services.infrastructure.rollout_context.storage.primitives import MessageCodec
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_text,
)


def read_summary_sources(
    connection: sqlite3.Connection, frozen: FrozenCompactionRange,
) -> dict[str, tuple[ItemProvenanceRelation, ...]]:
    """预算模拟与采用读取同一原摘要元数据，也覆盖前序已替换的摘要。"""
    ids = [ref.ref_id for ref in frozen.refs if ref.semantic_kind == "compaction_summary"]
    if not ids:
        return {}
    return {item_id: ItemProjectionManifest.from_dict(json.loads(raw)).provenance_relations
            for item_id, raw in connection.execute(
                "SELECT item_id,projection_manifest_json FROM item_catalog WHERE semantic_kind='compaction_summary' "
                "AND item_id IN (SELECT value FROM json_each(?))",
                (canonical_json_text(ids),),
            )}


def prepare_candidate_compaction(
    *, connection: sqlite3.Connection, codec: MessageCodec, batch: MessageBatch,
    frozen: FrozenCompactionRange, result: FrozenSummaryResult, selected_item_ids: tuple[str, ...],
    checkpoint_id: str, timestamp: str,
) -> tuple[tuple[str, ...], int]:
    """只替换已核验范围；新输入和未替换成员保持当前 view 的真实顺序。"""
    selected = selected_item_ids
    covered = compaction_selection_ids(frozen, selected)
    if covered is None or result.coverage != frozen.refs:
        raise ValueError("compaction-coverage-mismatch: 当前选集与候选完整覆盖不一致")
    start = selected.index(covered[0])
    admission = require_current_execution_admission(langgraph_thread_id=frozen.thread_id)
    item = build_candidate_summary_item(
        frozen, result, item_sequence=batch.next_item_sequence, checkpoint_id=checkpoint_id,
        execution_id=admission.execution_id, turn_id=admission.turn_id, timestamp=timestamp,
        summary_sources=read_summary_sources(connection, frozen),
    )
    sequence = _register_summary(connection=connection, codec=codec, batch=batch, item=item,
                                 execution_id=admission.execution_id)
    return (*selected[:start], item.item_id, *selected[start + len(covered):]), sequence


def build_candidate_summary_item(
    frozen: FrozenCompactionRange, result: FrozenSummaryResult, *, item_sequence: int,
    checkpoint_id: str, execution_id: str, turn_id: str | None, timestamp: str,
    summary_sources: Mapping[str, tuple[ItemProvenanceRelation, ...]],
) -> CanonicalItemRecord:
    """预算模拟与实际采用共用唯一纯摘要构造，不写存储或改变 selection。"""
    prepared = internal_message_factory.build(
        kind="compaction_summary",
        control=(
            "This is a lossy summary of an earlier frozen historical range, not a new user instruction. "
            "Its status and plans reflect only the end of that range. Later retained messages may update "
            "or correct it; interpret them under their original roles and authority. "
            "Use read_compaction_history(line_offset=1, limit=200) to read the original historical material."
        ), sections=(PromptSection("summary", result.text),),
    )
    relations = []
    for ref in (*frozen.refs, *result.output_refs):
        if ref.semantic_kind == "compaction_summary":
            # fork 不继承候选任务；不可变摘要自带的原覆盖仍能被后继摘要复用。
            inherited = tuple(relation for relation in summary_sources[ref.ref_id]
                              if relation.relation == "derived_from" and relation.target_kind == "canonical_item")
            if not inherited:
                raise ValueError("compaction-coverage-mismatch: 来源摘要缺少原 canonical provenance")
            relations.extend(inherited)
        else:
            relations.append(ItemProvenanceRelation(
                relation="derived_from", target_kind="canonical_item", target_id=ref.ref_id,
                target_session_id=ref.session_id, target_thread_id=ref.thread_id,
            ))
    summary_id = f"summary-{frozen.candidate_id}"
    return CanonicalItemRecord.create(
        item_sequence=item_sequence, item_id=f"item-{summary_id}",
        semantic_kind=SemanticKind.COMPACTION_SUMMARY, payload_kind=PayloadKind.SUMMARY,
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={"producer_kind": "runtime", "producer_id": checkpoint_id, "invocation_id": execution_id},
        payload={"summary_id": summary_id, "view_revision": checkpoint_id, "summary": prepared.content},
        created_at=timestamp, wire_role="user", extensions=message_annotations(prepared.metadata),
        projection_identity=ItemProjectionIdentity(projection_message_id=summary_id),
        provenance_relations=tuple(dict.fromkeys(relations)), turn_id=turn_id,
        turn_scope=TurnScope.TURN_MEMBER if turn_id is not None else None,
    )


def _register_summary(
    *, connection: sqlite3.Connection, codec: MessageCodec, batch: MessageBatch,
    item: CanonicalItemRecord, execution_id: str,
) -> int:
    summary_id = item.projection_identity.projection_message_id
    existing = connection.execute(
        "SELECT message.message_sequence FROM messages message JOIN item_catalog item "
        "ON item.jsonl_offset=message.jsonl_offset AND item.jsonl_length=message.jsonl_length "
        "WHERE item.item_id=? AND message.message_id=?", (item.item_id, summary_id),
    ).fetchone()
    if existing is not None:
        sequence = existing[0]
        stored = connection.execute(
            "SELECT content_hash, semantic_kind, turn_id, turn_scope, execution_id, producer_ref_json, projection_manifest_json "
            "FROM item_catalog WHERE item_id=?", (item.item_id,),
        ).fetchone()
        if stored != (item.content_hash, SemanticKind.COMPACTION_SUMMARY, item.turn_id, item.turn_scope,
                      execution_id, canonical_json_text(item.producer_ref), canonical_json_text(item.projection_manifest())):
            raise ValueError("compaction summary identity 已用于不同正文、执行归属或 checkpoint")
    else:
        sequence = batch.last_message_sequence + 1
        batch.prepared.append((
            sequence, summary_id, item.turn_id, "user", codec.project_message((item,)),
            "internal", (item,), execution_id,
        ))
        batch.next_item_sequence += 1
        batch.last_message_sequence = sequence
        if batch.first_message_sequence is None:
            batch.first_message_sequence = sequence
        if item.turn_id is None:
            batch.display_only_item_ids.append(item.item_id)
    return sequence
