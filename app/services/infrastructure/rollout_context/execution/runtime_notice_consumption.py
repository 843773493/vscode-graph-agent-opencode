"""User acceptance 冻结 notice 排除集；消费事实仅归属 assembly/epoch owner。"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping

from app.domain.itemized.enums import CommitKind
from app.domain.itemized.epoch.prefix_epoch import RuntimeNoticeAcceptanceRef
from app.domain.itemized.execution_admission import ExecutionAdmission
from app.domain.itemized.hashing import sha256_jcs
from app.services.infrastructure.rollout_context.storage.serialization import (
    json_mapping,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    load_committed_idempotency_commit,
    strict_non_negative_int,
    strict_text,
)
from app.services.mapping.itemized.carrier_dedup import superseded_item_ids


def acceptance_metadata(
    connection: sqlite3.Connection, *, turn_id: str, ingress_id: str, acceptance_key: str,
) -> tuple[dict[str, object], int] | None:
    """重放只读首次提交的冻结值，不观察当前 membership 或 notice。"""
    rows = connection.execute(
        "SELECT metadata_json FROM storage_commits WHERE commit_kind=? "
        "AND subject_id=? AND idempotency_key=?",
        (CommitKind.ACCEPTANCE.value, turn_id, acceptance_key),
    ).fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise RuntimeError("runtime-notice-acceptance-conflict: acceptance commit 不唯一")
    metadata = dict(json_mapping(rows[0][0], field="acceptance.metadata"))
    if set(metadata) != {"accepted_ingress_id", "turn_id", "runtime_notice_selection"} or (
        metadata["accepted_ingress_id"], metadata["turn_id"]
    ) != (ingress_id, turn_id):
        raise RuntimeError("runtime-notice-acceptance-conflict: acceptance metadata identity 不一致")
    selection = metadata["runtime_notice_selection"]
    if not isinstance(selection, Mapping) or set(selection) != {
        "owner_execution_id", "owner_admission_key", "source_branch_id", "source_view_id",
        "admission_commit_cut", "item_sequence_cut", "context_ordinal_cut", "notices", "selection_hash",
    }:
        raise RuntimeError("runtime-notice-acceptance-conflict: 冻结 notice selection 字段非法")
    preimage = {key: value for key, value in selection.items() if key != "selection_hash"}
    if selection["selection_hash"] != sha256_jcs(preimage):
        raise RuntimeError("runtime-notice-acceptance-conflict: 冻结 notice selection hash 不一致")
    for key in ("owner_execution_id", "owner_admission_key", "source_branch_id", "source_view_id"):
        strict_text(selection[key], field=f"notice.selection.{key}")
    for key in ("admission_commit_cut", "item_sequence_cut", "context_ordinal_cut"):
        strict_non_negative_int(selection[key], field=f"notice.selection.{key}")
    source_view = connection.execute(
        "SELECT branch_id FROM context_views WHERE view_id=?", (selection["source_view_id"],),
    ).fetchone()
    if source_view != (selection["source_branch_id"],):
        raise RuntimeError("runtime-notice-acceptance-conflict: 冻结 source view/branch 不一致")
    notices = selection["notices"]
    if not isinstance(notices, list):
        raise TypeError("runtime-notice-acceptance-conflict: notices 必须是有序数组")
    ids: set[str] = set()
    previous_ordinal = -1
    for notice in notices:
        if not isinstance(notice, dict) or set(notice) != {
            "notice_item_id", "execution_id", "admission_commit_id", "admission_payload_hash",
            "source_branch_id", "source_view_id", "private_output_item_ids", "notice_context_ordinal",
        }:
            raise RuntimeError("runtime-notice-acceptance-conflict: notice identity 字段非法")
        item_id = strict_text(notice["notice_item_id"], field="notice.item_id")
        commit_id = strict_non_negative_int(notice["admission_commit_id"], field="notice.admission_commit_id")
        ordinal = strict_non_negative_int(notice["notice_context_ordinal"], field="notice.context_ordinal")
        if item_id in ids or not 0 < commit_id <= selection["admission_commit_cut"] or not previous_ordinal < ordinal <= selection["context_ordinal_cut"]:
            raise RuntimeError("runtime-notice-acceptance-conflict: notice 顺序/准入 cut 非法")
        ids.add(item_id)
        previous_ordinal = ordinal
        for key in ("execution_id", "admission_payload_hash", "source_branch_id", "source_view_id"):
            strict_text(notice[key], field=f"notice.{key}")
        outputs = notice["private_output_item_ids"]
        if not isinstance(outputs, list) or len(outputs) != len(set(outputs)):
            raise RuntimeError("runtime-notice-acceptance-conflict: private output IDs 非法")
        for output_id in outputs:
            strict_text(output_id, field="notice.private_output_item_id")
        admission = connection.execute(
            "SELECT item.execution_id,execution.admission_payload_hash,view.branch_id,member.view_id,"
            "commit_record.idempotency_key,member.context_ordinal "
            "FROM item_catalog item JOIN executions execution ON execution.execution_id=item.execution_id "
            "JOIN storage_commits commit_record ON commit_record.commit_id=item.commit_id "
            "JOIN context_view_items member ON member.item_id=item.item_id "
            "JOIN context_views view ON view.view_id=member.view_id "
            "WHERE item.item_id=? AND item.commit_id=? AND item.semantic_kind='runtime_notice' "
            "AND execution.execution_origin='internal' AND commit_record.commit_kind='execution_admission' "
            "AND commit_record.idempotency_key='internal-admission:'||execution.admission_idempotency_key "
            "AND member.view_id=? AND member.visible=1 AND member.source_kind='display_only' AND member.context_selected=1",
            (item_id, commit_id, selection["source_view_id"]),
        ).fetchone()
        if admission is None or admission[:4] != (
            notice["execution_id"], notice["admission_payload_hash"], notice["source_branch_id"], notice["source_view_id"],
        ) or admission[5] != ordinal:
            raise RuntimeError("runtime-notice-acceptance-conflict: notice 脱离冻结 view/admission")
        output_ordinals = []
        for output_id in outputs:
            output = connection.execute(
                "SELECT member.context_ordinal FROM context_view_items member JOIN item_catalog item ON item.item_id=member.item_id "
                "WHERE member.view_id=? AND member.item_id=? AND member.visible=1 AND member.context_selected=1 "
                "AND member.source_kind='display_only' AND item.execution_id=? AND item.semantic_kind='assistant_output' "
                "AND item.commit_id<=? AND item.item_sequence<=? AND member.context_ordinal<=?",
                (selection["source_view_id"], output_id, notice["execution_id"], selection["admission_commit_cut"],
                 selection["item_sequence_cut"], selection["context_ordinal_cut"]),
            ).fetchone()
            if output is None:
                raise RuntimeError("runtime-notice-acceptance-conflict: private output 脱离冻结 execution/view/cut")
            output_ordinals.append(output[0])
        if output_ordinals != sorted(output_ordinals):
            raise RuntimeError("runtime-notice-acceptance-conflict: private output 冻结顺序非法")
    commit = load_committed_idempotency_commit(
        connection, commit_kind=CommitKind.ACCEPTANCE.value, subject_id=turn_id,
        idempotency_key=acceptance_key, commit_mode="item_bearing", outcome=None,
        metadata=metadata, item_count=1,
    )
    if commit is None:
        raise RuntimeError("runtime-notice-acceptance-conflict: acceptance 未提交")
    return metadata, commit.commit_id


def freeze_runtime_notice_selection(
    connection: sqlite3.Connection, *, storage, session_id: str, thread_id: str, checkpoint_ns: str,
    branch_id: str, execution_id: str, admission_key: str,
) -> dict[str, object]:
    """在 acceptance 的 BEGIN IMMEDIATE 内、root append 前冻结 head internal。"""
    if not connection.in_transaction:
        raise RuntimeError("runtime-notice-freeze-transaction-required")
    view = connection.execute("SELECT head_view_id FROM branches WHERE branch_id=?", (branch_id,)).fetchone()
    if view is None:
        raise RuntimeError("runtime-notice-acceptance-conflict: source branch 不存在")
    view_id = strict_text(view[0], field="notice.source_view_id")
    commit_cut, item_cut = connection.execute(
        "SELECT COALESCE(last_commit_id,0),last_item_sequence FROM database_meta WHERE singleton_id=1",
    ).fetchone()
    ordinal_cut = connection.execute(
        "SELECT COALESCE(MAX(context_ordinal),0) FROM context_view_items WHERE view_id=?", (view_id,),
    ).fetchone()[0]
    rows = connection.execute(
        "SELECT item.item_id,execution.execution_id,commit_record.commit_id,"
        "execution.admission_payload_hash,view.branch_id,member.view_id,member.context_ordinal "
        "FROM context_view_items member JOIN item_catalog item ON item.item_id=member.item_id "
        "JOIN context_views view ON view.view_id=member.view_id "
        "JOIN executions execution ON execution.execution_id=item.execution_id "
        "JOIN storage_commits commit_record ON commit_record.commit_id=item.commit_id "
        "WHERE member.view_id=? AND member.visible=1 AND member.context_selected=1 AND member.source_kind='display_only' "
        "AND item.semantic_kind='runtime_notice' AND item.turn_scope='pending_next_turn' "
        "AND item.turn_id IS NULL AND execution.execution_origin='internal' "
        "AND commit_record.commit_kind='execution_admission' AND commit_record.status='committed' "
        "AND commit_record.commit_id<=? ORDER BY member.context_ordinal",
        (view_id, commit_cut),
    ).fetchall()
    fields = ("notice_item_id", "execution_id", "admission_commit_id", "admission_payload_hash",
              "source_branch_id", "source_view_id", "notice_context_ordinal")
    notices = []
    for row in rows:
        notice = dict(zip(fields, row, strict=True))
        output_rows = connection.execute(
            "SELECT item.item_id FROM context_view_items member JOIN item_catalog item ON item.item_id=member.item_id "
            "WHERE member.view_id=? AND member.visible=1 AND member.context_selected=1 AND member.source_kind='display_only' "
            "AND item.execution_id=? AND item.semantic_kind='assistant_output' AND item.commit_id<=? "
            "AND item.item_sequence<=? ORDER BY member.context_ordinal",
            (view_id, row[1], commit_cut, item_cut),
        ).fetchall()
        output_items = [storage._read_canonical_item_from_catalog(
            connection, session_id=session_id, thread_id=thread_id, checkpoint_ns=checkpoint_ns, item_id=item[0],
        ) for item in output_rows]
        shadows = superseded_item_ids(output_items)
        notice["private_output_item_ids"] = [item.item_id for item in output_items if item.item_id not in shadows]
        notices.append(notice)
    selection = {
        "owner_execution_id": execution_id, "owner_admission_key": admission_key,
        "source_branch_id": branch_id, "source_view_id": view_id,
        "admission_commit_cut": commit_cut, "item_sequence_cut": item_cut,
        "context_ordinal_cut": ordinal_cut,
        "notices": notices,
    }
    return {**selection, "selection_hash": sha256_jcs(selection)}


class RuntimeNoticeConsumptionMixin:
    """读回唯一 acceptance 事实；不持有第二份 claim 或 pending ledger。"""

    def runtime_notice_acceptance(
        self, admission: ExecutionAdmission, *, connection: sqlite3.Connection,
    ) -> tuple[RuntimeNoticeAcceptanceRef | None, Mapping[str, object] | None]:
        if admission.execution_origin == "internal":
            return None, None
        row = connection.execute(
            "SELECT acceptance.accepted_ingress_id,acceptance.acceptance_idempotency_key,"
            "turn.initial_execution_id,execution.admission_idempotency_key "
            "FROM turn_acceptances acceptance JOIN turn_records turn ON turn.turn_id=acceptance.turn_id "
            "AND turn.session_id=acceptance.session_id AND turn.thread_id=acceptance.thread_id "
            "JOIN executions execution ON execution.execution_id=turn.initial_execution_id "
            "WHERE turn.session_id=? AND turn.thread_id=? AND turn.turn_id=?",
            (admission.session_id, admission.thread_id, admission.turn_id),
        ).fetchone()
        if row is None:
            raise RuntimeError("runtime-notice-acceptance-conflict: user execution 缺少 acceptance")
        stored = acceptance_metadata(connection, turn_id=admission.turn_id, ingress_id=row[0], acceptance_key=row[1])
        if stored is None:
            raise RuntimeError("runtime-notice-acceptance-conflict: user execution 缺少 acceptance commit")
        metadata, commit_id = stored
        selection = metadata["runtime_notice_selection"]
        if (selection["owner_execution_id"], selection["owner_admission_key"]) != row[2:]:
            raise RuntimeError("runtime-notice-acceptance-conflict: 冻结 owner 与 initial execution 不一致")
        return RuntimeNoticeAcceptanceRef(commit_id, selection["selection_hash"]), selection

    def runtime_notice_excluded_item_ids(
        self, admission: ExecutionAdmission, *, connection: sqlite3.Connection,
    ) -> frozenset[str]:
        _ref, selection = self.runtime_notice_acceptance(admission, connection=connection)
        if selection is None:
            return frozenset()
        excluded: set[str] = set()
        for notice in selection["notices"]:
            excluded.add(notice["notice_item_id"])
            excluded.update(notice["private_output_item_ids"])
        return frozenset(excluded)
