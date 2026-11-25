"""原 rollout 库内的冻结候选与 ready 结果；不调度任务，不修改 active view。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from app.core.sqlite_state import utc_now_text
from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.compaction import (
    ApplyCompactionCandidateIntent,
    CanonicalTextRange,
    CompactionPredecessor,
    FrozenCompactionRange,
    FrozenCompactionTaskInput,
    FrozenSummaryResult,
    ReadyCompactionCandidate,
    compaction_selection_ids,
    summary_output_text,
)
from app.domain.itemized.dispatch_ref import SealedContextDispatchRef
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.identity.detail_ref import DetailRef
from app.domain.itemized.identity.refs import ContextRef
from app.domain.itemized.serde.registry import parse_context_ref
from app.services.infrastructure.rollout_context.assembly.detail_identity import (
    detail_ref_from_key,
    detail_ref_key,
)
from app.services.infrastructure.rollout_context.checkpoint.boundary.tool_protocol_boundary import (
    ToolProtocolBoundary,
)
from app.services.infrastructure.rollout_context.runtime.detail_store import (
    ContextPlanDetailStore,
)
from app.services.infrastructure.rollout_context.storage.catalog.history_scope import (
    require_historical_scope,
)
from app.services.infrastructure.rollout_context.storage.catalog.history_text_ranges import (
    history_line_locators,
    read_history_page,
)
from app.services.infrastructure.rollout_context.storage.catalog.view_ancestry import (
    is_view_ancestor,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutNotMaterializedError,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_line,
    canonical_json_text,
)
from app.services.mapping.itemized.carrier_dedup import select_item_carriers
from app.services.mapping.itemized.history import compaction_history_fields

if TYPE_CHECKING:
    from app.domain.itemized.plans.contributions import ContextContribution
    from app.domain.itemized.records import CanonicalItemRecord
    from app.services.infrastructure.rollout_context.store import RolloutContextStore


def _normalize_coverage(
    parts: Sequence[tuple[ContextRef, CanonicalTextRange | None]],
) -> tuple[tuple[ContextRef, ...], tuple[CanonicalTextRange, ...]]:
    """相邻分片合为同一原件；间隙、重叠和回头覆盖都不能冒充完整输入。"""
    refs: list[ContextRef] = []
    seen: set[ContextRef] = set()
    ranges: dict[ContextRef, CanonicalTextRange] = {}
    for ref, part in parts:
        if part is not None and part.ref != ref:
            raise ValueError("compaction-coverage-mismatch: 分片与实际来源引用不一致")
        if ref in seen:
            previous = ranges.get(ref)
            if (refs[-1] != ref or previous is None or part is None
                    or previous.text_length != part.text_length or previous.end != part.start):
                raise ValueError("compaction-coverage-mismatch: 原文分片必须有序、无缝且不重叠")
            part = CanonicalTextRange(ref, previous.start, part.end, part.text_length)
        else:
            refs.append(ref)
            seen.add(ref)
        if part is not None and (part.start != 0 or part.end != part.text_length):
            ranges[ref] = part
        else:
            ranges.pop(ref, None)
    return tuple(refs), tuple(ranges[ref] for ref in refs if ref in ranges)


def _source_version_error(connection: sqlite3.Connection, frozen: FrozenCompactionRange,
                          versions: Sequence[tuple[str, str]]) -> str | None:
    for source_id, revision in versions:
        if connection.execute(
            "SELECT latest_revision FROM context_source_control_states WHERE session_id=? AND thread_id=? AND source_id=?",
            (frozen.session_id, frozen.thread_id, source_id),
        ).fetchone() != (revision,):
            return "source-version-changed"
    return None


class RolloutCompactionCandidates:
    """候选只拥有范围与结果事实；后台任务继续独占运行状态、期限和取消。"""

    def __init__(self, context: RolloutContextStore) -> None:
        self._context = context
        self._storage = context.storage
        self._details = ContextPlanDetailStore(self._storage.sessions_dir)

    def selected_item_ids(
        self, connection: sqlite3.Connection, frozen: FrozenCompactionRange, *,
        view_id: str, context_range: tuple[int, int] | None = None,
    ) -> tuple[str, ...]:
        """范围只读实际成员及同 ModelCall 的载体，识别首尾外晚到的 winner。"""
        bounds = " AND context_ordinal BETWEEN ? AND ?" if context_range is not None else ""
        members = tuple(connection.execute(
            "SELECT item_id,context_ordinal FROM context_view_items WHERE view_id=? AND visible=1 "
            "AND context_selected=1 AND source_kind='canonical'" + bounds + " ORDER BY context_ordinal",
            (view_id, *(() if context_range is None else context_range)),
        ))
        ids = tuple(row[0] for row in members)
        by_id = {item.item_id: item for item in self._storage.read_items(
            frozen.session_id, frozen.thread_id, item_ids=ids,
            checkpoint_ns=frozen.checkpoint_ns, connection=connection,
        )}
        carrier_ids = ids
        if context_range is not None:
            scopes = tuple(dict.fromkeys(item.projection_identity.model_call_id for item in by_id.values()
                                        if item.projection_identity is not None and item.projection_identity.model_call_id is not None))
            if scopes:
                # 先沿现有 ModelCall 索引定位，再核当前 view；不能为一个候选扫全部活动历史。
                scoped = tuple(connection.execute(
                    "SELECT item.item_id,member.context_ordinal FROM json_each(?) scope CROSS JOIN item_catalog item "
                    "ON json_extract(item.projection_manifest_json,'$.projection_identity.model_call_id')=scope.value "
                    "CROSS JOIN context_view_items member ON member.item_id=item.item_id "
                    "WHERE member.view_id=? AND member.visible=1 AND member.context_selected=1 "
                    "AND member.source_kind='canonical'",
                    (canonical_json_text(scopes), view_id),
                ))
                missing = tuple(item_id for item_id, _ordinal in scoped if item_id not in by_id)
                if missing:
                    by_id.update((item.item_id, item) for item in self._storage.read_items(
                        frozen.session_id, frozen.thread_id, item_ids=missing,
                        checkpoint_ns=frozen.checkpoint_ns, connection=connection,
                    ))
                positions = dict((*members, *scoped))
                carrier_ids = tuple(sorted(positions, key=positions.__getitem__))
        effective = {item.item_id for item in select_item_carriers(tuple(by_id[item_id] for item_id in carrier_ids))}
        return tuple(item_id for item_id in ids if item_id in effective)

    def _range_error(self, connection: sqlite3.Connection, frozen: FrozenCompactionRange) -> str | None:
        """只查冻结范围与实际读取来源；整体 revision 和无关来源不参与失效。"""
        branch = connection.execute(
            "SELECT branch.branch_id,branch.head_view_id FROM checkpoint_namespace_state namespace "
            "JOIN branches branch ON branch.branch_id=namespace.active_branch_id "
            "WHERE namespace.checkpoint_ns=? AND branch.status='active'", (frozen.checkpoint_ns,),
        ).fetchone()
        if branch is None or branch[0] != frozen.source_branch_id or branch[1] is None:
            return "source-branch-changed"
        if not is_view_ancestor(connection, frozen.source_view_id, branch[1]):
            return "source-lineage-changed"
        original_ids = [ref.ref_id for ref in frozen.refs]
        relevant_ids = [*original_ids, *(() if frozen.predecessor is None else (frozen.predecessor.summary_item_id,))]
        selected = connection.execute(
            "SELECT member.item_id,member.context_ordinal FROM context_view_items member "
            "WHERE member.view_id=? AND member.visible=1 AND member.context_selected=1 AND member.source_kind='canonical' "
            "AND member.item_id IN (SELECT value FROM json_each(?)) ORDER BY member.context_ordinal",
            (branch[1], canonical_json_text(relevant_ids)),
        ).fetchall()
        actual = compaction_selection_ids(frozen, [row[0] for row in selected])
        if actual is None:
            return "source-selection-changed"
        positions = {row[0]: row[1] for row in selected}
        selected_range = self.selected_item_ids(
            connection, frozen, view_id=branch[1],
            context_range=(positions[actual[0]], positions[actual[-1]]),
        )
        if selected_range != actual:
            return "source-selection-changed"
        rows = {row[0]: row[1:] for row in connection.execute(
            "SELECT item_id,content_hash,source_revision FROM item_catalog WHERE item_id IN (SELECT value FROM json_each(?))",
            (canonical_json_text(original_ids),),
        )}
        for ref in frozen.refs:
            require_historical_scope(connection, ref.session_id, ref.thread_id,
                                     current_owner=(frozen.session_id, frozen.thread_id))
            if rows.get(ref.ref_id) != (ref.content_hash, ref.source_revision):
                return "source-content-changed"
        if frozen.predecessor is not None:
            previous = connection.execute(
                "SELECT result_detail_ref,discarded_reason,applied_summary_item_id FROM context_compaction_candidates WHERE candidate_id=?",
                (frozen.predecessor.candidate_id,),
            ).fetchone()
            if previous is None or previous[0] != detail_ref_key(frozen.predecessor.result_ref):
                return "predecessor-result-changed"
            if previous[1] is not None and previous[1] != "superseded-by-successor":
                return "predecessor-discarded"
            if frozen.predecessor.summary_item_id in actual and previous[2] != frozen.predecessor.summary_item_id:
                return "predecessor-summary-changed"
        return _source_version_error(connection, frozen, frozen.source_versions)

    def _candidate_error(self, connection: sqlite3.Connection, frozen: FrozenCompactionRange, result: str | None) -> str | None:
        error = self._range_error(connection, frozen)
        if error is not None or result is None:
            return error
        body = self._read_result_manifest(frozen, detail_ref_from_key(result))
        return _source_version_error(connection, frozen, tuple(tuple(pair) for pair in body["source_versions"]))

    def freeze(
        self, session_id: str, thread_id: str, *, candidate_id: str, task_id: str,
        item_ids: Sequence[str], model_config_hash: str, checkpoint_ns: str = "",
        source_versions: tuple[tuple[str, str], ...] = (),
        initial_sources: Sequence[ContextContribution] = (),
        predecessor_candidate_id: str | None = None,
        target_constraint_hash: str | None = None,
    ) -> FrozenCompactionRange:
        """从当前真实 selection 读取有序来源，并在网络调用前保护其原 view。"""
        ids = tuple(item_ids)
        if not ids or len(set(ids)) != len(ids):
            raise ValueError("候选范围必须是非空且不重复的 Item 身份序列")
        with self._storage.write_connection(session_id, thread_id, checkpoint_ns) as connection:
            connection.execute("BEGIN IMMEDIATE")
            versions = dict(source_versions)
            for source in initial_sources:
                binding = source.source_binding
                if binding is None or binding.source_ref is None or connection.execute(
                    "SELECT 1 FROM context_source_control_states WHERE session_id=? AND thread_id=? AND source_id=?",
                    (session_id, thread_id, binding.source_ref),
                ).fetchone() is None:
                    continue
                if binding.source_ref in versions and versions[binding.source_ref] != source.source_revision:
                    raise ValueError("source-version-changed: 当前来源快照与候选版本冲突")
                versions[binding.source_ref] = source.source_revision
            source_versions = tuple(versions.items())
            existing = connection.execute(
                "SELECT manifest_json FROM context_compaction_candidates WHERE candidate_id=?", (candidate_id,),
            ).fetchone()
            if existing is not None:
                frozen = FrozenCompactionRange.from_dict(json.loads(existing[0]))
                if (frozen.session_id, frozen.thread_id, frozen.checkpoint_ns, frozen.task_id,
                    compaction_selection_ids(frozen, ids), frozen.model_config_hash, frozen.target_constraint_hash,
                    None if frozen.predecessor is None else frozen.predecessor.candidate_id) != (
                    session_id, thread_id, checkpoint_ns, task_id, ids, model_config_hash, target_constraint_hash, predecessor_candidate_id,
                ):
                    raise ValueError("compaction-candidate-conflict: candidate identity 已绑定其它输入")
                if (frozen.predecessor is None and frozen.source_versions != source_versions
                        or any(dict(frozen.source_versions).get(key) != revision for key, revision in source_versions)):
                    raise ValueError("compaction-candidate-conflict: candidate identity 已绑定其它来源版本")
                return frozen
            branch = connection.execute(
                "SELECT branch.branch_id,branch.head_view_id FROM checkpoint_namespace_state namespace "
                "JOIN branches branch ON branch.branch_id=namespace.active_branch_id "
                "WHERE namespace.checkpoint_ns=? AND branch.status='active'", (checkpoint_ns,),
            ).fetchone()
            if branch is None or branch[1] is None:
                raise ValueError("compaction-source-unavailable: 没有活动 view")
            items = self._storage.read_items(session_id, thread_id, item_ids=ids,
                                             checkpoint_ns=checkpoint_ns, connection=connection)
            by_id = {item.item_id: item for item in items}
            refs = []
            for item_id in ids:
                item = by_id[item_id]
                owner = connection.execute(
                    "SELECT execution.session_id,execution.thread_id FROM item_catalog item "
                    "LEFT JOIN executions execution USING(execution_id) WHERE item.item_id=?", (item_id,),
                ).fetchone()
                historical_owner = owner if owner[0] is not None else (session_id, thread_id)
                refs.append(ContextRef.canonical_item(item, session_id=historical_owner[0], thread_id=historical_owner[1]))
            predecessor = None
            if predecessor_candidate_id is not None:
                previous, previous_result, discarded = self._read(connection, session_id, thread_id, predecessor_candidate_id, checkpoint_ns)
                if previous_result is None or discarded is not None:
                    raise ValueError("compaction-predecessor-unavailable: 前序没有完整可复用结果")
                summary_id = f"item-summary-{previous.candidate_id}"
                if ids[0] == summary_id:
                    previous_applied = connection.execute(
                        "SELECT applied_summary_item_id FROM context_compaction_candidates WHERE candidate_id=?", (previous.candidate_id,),
                    ).fetchone()[0]
                    if previous_applied != summary_id:
                        raise ValueError("source-mismatch: 前序摘要尚未实际采用")
                    refs = [*previous.refs, *refs[1:]]
                elif tuple(refs[:len(previous.refs)]) != previous.refs:
                    raise ValueError("compaction-coverage-mismatch: 前序必须覆盖真实输入的有序前缀")
                versions = dict(previous.source_versions)
                if any(key in versions and versions[key] != revision for key, revision in source_versions):
                    raise ValueError("source-version-changed: 前序与新增范围读取了不同来源版本")
                source_versions = tuple({**versions, **dict(source_versions)}.items())
                predecessor = CompactionPredecessor(previous.candidate_id, summary_id, detail_ref_from_key(previous_result), len(previous.refs))
            frozen = FrozenCompactionRange(candidate_id, task_id, session_id, thread_id, checkpoint_ns,
                                            branch[1], branch[0], model_config_hash, tuple(refs), source_versions,
                                            self._context.assemblies._latest_context_assembly_id_connection(
                                                connection, session_id, thread_id, checkpoint_ns=checkpoint_ns,
                                            ), predecessor, target_constraint_hash)
            error = self._range_error(connection, frozen)
            if error is not None:
                raise ValueError(f"compaction-source-unavailable: {error}")
            # 真实配对 owner 核验该范围；不能以完整原历史闭合来掩盖单块孤立结果。
            sequences = [row[0] for row in connection.execute(
                "SELECT DISTINCT message.message_sequence,member.context_ordinal FROM context_view_items member "
                "JOIN item_catalog item USING(item_id) JOIN messages message "
                "ON message.jsonl_offset=item.jsonl_offset AND message.jsonl_length=item.jsonl_length "
                "WHERE member.view_id=? AND member.item_id IN (SELECT value FROM json_each(?)) "
                "ORDER BY member.context_ordinal", (branch[1], canonical_json_text(ids)),
            )]
            ToolProtocolBoundary(connection, list(dict.fromkeys(sequences))).validate(len(set(sequences)))
            timestamp = utc_now_text()
            connection.execute(
                "INSERT INTO context_compaction_candidates(candidate_id,task_id,checkpoint_ns,source_view_id,manifest_json,created_at,predecessor_result_detail_ref) "
                "VALUES (?,?,?,?,?,?,?)", (candidate_id, task_id, checkpoint_ns, branch[1], canonical_json_text(frozen.to_dict()), timestamp,
                    None if predecessor is None else detail_ref_key(predecessor.result_ref)),
            )
            connection.execute(
                "INSERT INTO retention_refs(retention_id,reference_kind,reference_id,target_view_id,owner_session_id,status,created_at) "
                "VALUES (?,'compaction_candidate',?,?,?,'active',?)",
                (f"compaction:{candidate_id}", candidate_id, branch[1], session_id, timestamp),
            )
            self._storage._commit_connection(connection)
            return frozen

    def read(self, session_id: str, thread_id: str, candidate_id: str, *, checkpoint_ns: str = "") -> FrozenCompactionRange:
        with self._storage.read_connection(session_id, thread_id, checkpoint_ns) as connection:
            return self._read(connection, session_id, thread_id, candidate_id, checkpoint_ns)[0]

    def current_status(self, session_id: str, thread_id: str, *, checkpoint_ns: str = "") -> dict[str, object] | None:
        """只读最近候选的登记事实；运行状态和错误仍由原后台任务提供。"""
        try:
            with self._storage.read_connection(session_id, thread_id, checkpoint_ns) as connection:
                row = connection.execute(
                    "SELECT candidate_id,task_id,applied_view_id,discarded_reason,result_detail_ref,created_at "
                    "FROM context_compaction_candidates WHERE checkpoint_ns=? ORDER BY created_at DESC,rowid DESC LIMIT 1",
                    (checkpoint_ns,),
                ).fetchone()
        except RolloutNotMaterializedError:
            return None
        if row is None:
            return None
        return {"candidate_id": row[0], "task_id": row[1],
                "status": "applied" if row[2] is not None else "discarded" if row[3] is not None else "ready" if row[4] is not None else "preparing",
                "reason": row[3], "created_at": row[5]}

    def reusable_prefix(self, session_id: str, thread_id: str, item_ids: Sequence[str], *, checkpoint_ns: str = "") -> FrozenCompactionRange | None:
        """只复用当前真实范围的完整前缀；不产生候选或读取全部历史。"""
        ids = tuple(item_ids)
        with self._storage.read_connection(session_id, thread_id, checkpoint_ns) as connection:
            rows = connection.execute(
                "SELECT candidate_id,applied_summary_item_id FROM context_compaction_candidates WHERE checkpoint_ns=? "
                "AND result_detail_ref IS NOT NULL AND discarded_reason IS NULL "
                "ORDER BY created_at DESC,rowid DESC LIMIT 2", (checkpoint_ns,),
            ).fetchall()
            for candidate_id, summary_id in rows:
                frozen, _result, _discarded = self._read(connection, session_id, thread_id, candidate_id, checkpoint_ns)
                selected = compaction_selection_ids(frozen, ids)
                if summary_id is not None and ids and ids[0] == summary_id:
                    body = self._read_result_manifest(frozen, detail_ref_from_key(_result))
                    versions = (*frozen.source_versions, *(tuple(pair) for pair in body["source_versions"]))
                    if _source_version_error(connection, frozen, versions) is None:
                        return frozen
                if selected is not None and ids[:len(selected)] == selected and self._candidate_error(connection, frozen, _result) is None:
                    return frozen
        return None

    def existing_proposal(self, session_id: str, thread_id: str, item_ids: Sequence[str], *, model_config_hash: str,
                          target_constraint_hash: str | None, checkpoint_ns: str = "") -> FrozenCompactionRange | None:
        """同范围同目标只观察原任务，失败或重复准备不能重置累计调用预算。"""
        ids = tuple(item_ids)
        with self._storage.read_connection(session_id, thread_id, checkpoint_ns) as connection:
            rows = connection.execute(
                "SELECT candidate_id FROM context_compaction_candidates WHERE checkpoint_ns=? "
                "AND (discarded_reason IS NULL OR discarded_reason='task-failed') "
                "AND json_extract(manifest_json,'$.model_config_hash')=? AND json_extract(manifest_json,'$.target_constraint_hash') IS ? "
                "ORDER BY created_at DESC,rowid DESC LIMIT 2", (checkpoint_ns, model_config_hash, target_constraint_hash),
            ).fetchall()
            for (candidate_id,) in rows:
                frozen, _result, _discarded = self._read(connection, session_id, thread_id, candidate_id, checkpoint_ns)
                if compaction_selection_ids(frozen, ids) == ids and self._candidate_error(connection, frozen, _result) is None:
                    return frozen
        return None

    def request_application(self, session_id: str, thread_id: str, task_id: str, *, checkpoint_ns: str = "") -> None:
        """显式手动请求可提升同一个在途／ready 候选，不修改其冻结输入。"""
        with self._storage.write_connection(session_id, thread_id, checkpoint_ns) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT candidate_id FROM context_compaction_candidates WHERE task_id=? AND checkpoint_ns=?",
                (task_id, checkpoint_ns),
            ).fetchone()
            if row is None:
                raise KeyError(f"compaction-candidate-unavailable: task={task_id}")
            candidate_id = row[0]
            _frozen, _result, discarded = self._read(connection, session_id, thread_id, candidate_id, checkpoint_ns)
            if discarded is not None:
                raise ValueError(f"compaction-candidate-unavailable: task={task_id}, reason={discarded}")
            connection.execute(
                "UPDATE context_compaction_candidates SET apply_on_next_request=1 "
                "WHERE candidate_id=? AND applied_view_id IS NULL AND discarded_reason IS NULL",
                (candidate_id,),
            )
            self._storage._commit_connection(connection)

    def latest_ready(self, session_id: str, thread_id: str, *, checkpoint_ns: str = "",
                     include_automatic: bool = True) -> ReadyCompactionCandidate | None:
        """一次快照只读未采用且仍适用的至多两份候选。"""
        with self._storage.read_connection(session_id, thread_id, checkpoint_ns) as connection:
            rows = connection.execute(
                "SELECT candidate_id,apply_on_next_request FROM context_compaction_candidates "
                "WHERE checkpoint_ns=? AND result_detail_ref IS NOT NULL AND (apply_on_next_request=1 OR ?) "
                "AND applied_view_id IS NULL AND discarded_reason IS NULL "
                "ORDER BY apply_on_next_request DESC,created_at DESC,rowid DESC LIMIT 2",
                (checkpoint_ns, include_automatic),
            ).fetchall()
            for candidate_id, requested in rows:
                frozen, result, _discarded = self._read(connection, session_id, thread_id, candidate_id, checkpoint_ns)
                if self._candidate_error(connection, frozen, result) is None:
                    ref = detail_ref_from_key(result)
                    return ReadyCompactionCandidate(
                        frozen, self._read_summary_result(connection, frozen, ref, self._read_result_manifest(frozen, ref)), bool(requested),
                    )
        return None

    def predecessor_result(self, frozen: FrozenCompactionRange) -> FrozenSummaryResult | None:
        """按首次冻结的直接依赖冷读，不以当前 ready 顺序替换前序。"""
        dependency = frozen.predecessor
        if dependency is None:
            return None
        with self._storage.read_connection(frozen.session_id, frozen.thread_id, frozen.checkpoint_ns) as connection:
            previous, result, discarded = self._read(connection, frozen.session_id, frozen.thread_id,
                                                       dependency.candidate_id, frozen.checkpoint_ns)
            if (result != detail_ref_key(dependency.result_ref)
                    or discarded is not None and discarded != "superseded-by-successor"
                    or previous.refs != frozen.refs[:dependency.coverage_count]):
                raise ValueError("source-mismatch: 前序结果与冻结扁平覆盖不一致")
            body = self._read_result_manifest(previous, dependency.result_ref)
            if _source_version_error(connection, frozen, tuple(tuple(pair) for pair in body["source_versions"])) is not None:
                raise ValueError("source-version-changed: 前序摘要实际读取的来源已改变")
            return self._read_summary_result(connection, previous, dependency.result_ref, body)

    def call_usage(self, frozen: FrozenCompactionRange) -> tuple[int, int, bool]:
        """累计预算只读原 admission、ModelCall 与 terminal failure，不另存进度。"""
        with self._storage.read_connection(frozen.session_id, frozen.thread_id, frozen.checkpoint_ns) as connection:
            rows = connection.execute(
                "SELECT execution.outcome,(SELECT COUNT(*) FROM model_calls call WHERE call.execution_id=execution.execution_id),"
                "(SELECT json_extract(terminal.metadata_json,'$.failure.code') FROM storage_commits terminal "
                "WHERE terminal.subject_id=execution.execution_id AND terminal.commit_kind='terminal_convergence' "
                "AND terminal.status='committed' ORDER BY terminal.commit_id DESC LIMIT 1) "
                "FROM storage_commits admission JOIN executions execution ON execution.execution_id=admission.subject_id "
                "WHERE admission.commit_kind='execution_admission' AND admission.status='committed' "
                "AND json_extract(admission.metadata_json,'$.request_input.reference.task_id')=? ORDER BY admission.commit_id",
                (frozen.task_id,),
            ).fetchall()
        calls = sum(row[1] for row in rows)
        failures = sum(row[2] == "provider_failure" for row in rows)
        retryable = bool(rows and rows[-1][0] == "failed" and rows[-1][2] == "provider_failure"
                         and failures == 1 and all(row[0] == "completed" for row in rows[:-1]))
        return calls, failures, retryable

    def prior_request(self, frozen: FrozenCompactionRange, plan_id: str) -> tuple[str, FrozenSummaryResult | None] | None:
        """显式补尝试复用已完成块，只给原失败块分配第二次请求身份。"""
        with self._storage.read_connection(frozen.session_id, frozen.thread_id, frozen.checkpoint_ns) as connection:
            row = connection.execute(
                "SELECT execution.outcome,call.assembly_id,call.model_call_id,execution.execution_id "
                "FROM storage_commits admission JOIN executions execution ON execution.execution_id=admission.subject_id "
                "LEFT JOIN model_calls call ON call.execution_id=execution.execution_id "
                "WHERE admission.commit_kind='execution_admission' AND admission.status='committed' "
                "AND json_extract(admission.metadata_json,'$.request_input.reference.task_id')=? "
                "AND json_extract(admission.metadata_json,'$.request_input.reference.plan_id')=?",
                (frozen.task_id, plan_id),
            ).fetchall()
            if not row:
                return None
            if len(row) != 1:
                raise ValueError("source-mismatch: 单次摘要请求对应多个模型调用")
            outcome, assembly, model_call, execution = row[0]
            if outcome != "completed":
                return outcome, None
            snapshot = self._context.assemblies.get_context_assembly(
                frozen.session_id, frozen.thread_id, assembly_id=assembly, checkpoint_ns=frozen.checkpoint_ns,
            )
            dispatch = SealedContextDispatchRef.from_snapshot(snapshot, checkpoint_ns=frozen.checkpoint_ns,
                                                             attempt_key=plan_id, callback_model_call_id=model_call)
            ids = tuple(value[0] for value in connection.execute(
                "SELECT item_id FROM item_catalog WHERE execution_id=? AND semantic_kind='assistant_output' AND status='completed' "
                "ORDER BY item_sequence", (execution,),
            ))
            items = self._storage.read_items(frozen.session_id, frozen.thread_id, item_ids=ids,
                                             checkpoint_ns=frozen.checkpoint_ns, connection=connection)
            refs = tuple(ContextRef.canonical_item(item, session_id=frozen.session_id, thread_id=frozen.thread_id) for item in items)
        return outcome, self.save_summary_result(frozen, output_refs=refs, dispatch_ref=dispatch)

    def remaining_calls(self, frozen: FrozenCompactionRange, plan_ids: Sequence[str]) -> int:
        """已完成块不占本轮剩余次数；未知、失败和新请求都须预留一次调用。"""
        with self._storage.read_connection(frozen.session_id, frozen.thread_id, frozen.checkpoint_ns) as connection:
            completed = connection.execute(
                "SELECT COUNT(*) FROM storage_commits admission JOIN executions execution ON execution.execution_id=admission.subject_id "
                "WHERE admission.commit_kind='execution_admission' AND admission.status='committed' AND execution.outcome='completed' "
                "AND json_extract(admission.metadata_json,'$.request_input.reference.task_id')=? "
                "AND json_extract(admission.metadata_json,'$.request_input.reference.plan_id') IN (SELECT value FROM json_each(?))",
                (frozen.task_id, canonical_json_text(plan_ids)),
            ).fetchone()[0]
        return len(plan_ids) - completed

    def save_task_input(self, value: FrozenCompactionTaskInput) -> DetailRef:
        """recipe 只存原受保护 detail，保留与候选共用现有 retention。"""
        frozen = value.frozen
        body = value.to_dict()
        prepared = self._details.prepare(
            session_id=frozen.session_id, thread_id=frozen.thread_id,
            assembly_id=f"compaction-{frozen.candidate_id}", detail_kind="compaction_input",
            retention_class="compaction_candidate", visibility="internal", detail=body,
            required=True, sensitive=True, checkpoint_ns=frozen.checkpoint_ns,
        )
        with self._storage.write_connection(frozen.session_id, frozen.thread_id, frozen.checkpoint_ns) as connection:
            connection.execute("BEGIN IMMEDIATE")
            stored, _result, discarded = self._read(connection, frozen.session_id, frozen.thread_id, frozen.candidate_id, frozen.checkpoint_ns)
            if stored != frozen or discarded is not None:
                raise ValueError("compaction-input-conflict: 候选已经失效或输入不一致")
            existing = connection.execute(
                "SELECT detail_ref FROM context_plan_details WHERE assembly_id=? AND detail_kind='compaction_input' AND status='available'",
                (f"compaction-{frozen.candidate_id}",),
            ).fetchone()
            if existing is not None:
                ref = detail_ref_from_key(existing[0])
                if self.read_task_input(frozen.session_id, frozen.thread_id, ref, checkpoint_ns=frozen.checkpoint_ns) != value:
                    raise ValueError("compaction-input-conflict: 同候选任务 recipe 或期限已冻结")
                return ref
            paths = self._details.publish(prepared)
            try:
                self._context.assemblies._register_context_plan_detail(
                    connection, prepared.record, session_id=frozen.session_id,
                    thread_id=frozen.thread_id, checkpoint_ns=frozen.checkpoint_ns,
                )
                self._storage._commit_connection(connection)
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                    self._details.discard_published(paths)
                raise
        return prepared.record.detail_ref

    def read_task_input(self, session_id: str, thread_id: str, ref: DetailRef, *, checkpoint_ns: str = "") -> FrozenCompactionTaskInput:
        ref.require_owner(session_id, thread_id)
        record = self._context.assemblies.get_context_plan_detail(session_id, thread_id, detail_ref=ref, checkpoint_ns=checkpoint_ns)
        if (record.detail_kind, record.retention_class) != ("compaction_input", "compaction_candidate"):
            raise ValueError("source-mismatch: 任务输入不是压缩计划")
        value = FrozenCompactionTaskInput.from_dict(self._details.read(
            session_id=session_id, thread_id=thread_id, record=record, include_sensitive=True,
        )["detail"])
        if (value.frozen.session_id, value.frozen.thread_id, value.frozen.checkpoint_ns) != (session_id, thread_id, checkpoint_ns):
            raise ValueError("source-mismatch: 压缩任务输入归属不一致")
        return value

    def prepare_application(
        self, connection: sqlite3.Connection, intent: ApplyCompactionCandidateIntent,
    ) -> FrozenSummaryResult:
        """只在原 checkpoint writer 持有的同一连接核对采用前提。"""
        frozen = intent.frozen
        stored, result, discarded = self._read(
            connection, frozen.session_id, frozen.thread_id, frozen.candidate_id, frozen.checkpoint_ns,
        )
        if stored != frozen:
            raise ValueError("compaction-candidate-conflict: 采用输入与冻结候选不一致")
        row = connection.execute(
            "SELECT branch.head_view_id,candidate.applied_view_id FROM checkpoint_namespace_state namespace "
            "JOIN branches branch ON branch.branch_id=namespace.active_branch_id "
            "JOIN context_compaction_candidates candidate ON candidate.candidate_id=? "
            "WHERE namespace.checkpoint_ns=? AND branch.status='active'", (frozen.candidate_id, frozen.checkpoint_ns),
        ).fetchone()
        if row is None or row[0] != intent.expected_view_id:
            raise ValueError("compaction-view-conflict: 预算模拟后的 active view 已改变")
        if row[1] is not None:
            raise ValueError("compaction-candidate-consumed: 候选已经采用")
        error = discarded or self._candidate_error(connection, frozen, result)
        if error is not None:
            raise ValueError(f"compaction-candidate-discarded: {error}")
        if result is None:
            raise ValueError("compaction-result-unavailable: 候选尚无完整 ready 结果")
        ref = detail_ref_from_key(result)
        return self._read_summary_result(connection, frozen, ref, self._read_result_manifest(frozen, ref))

    def consume_application(
        self, connection: sqlite3.Connection, intent: ApplyCompactionCandidateIntent, *, view_id: str, summary_item_id: str,
    ) -> None:
        """采用事实与新 view、checkpoint 和 epoch transition 在同一事务发布。"""
        if not connection.in_transaction:
            raise RuntimeError("compaction-transaction-required: 采用必须参加 checkpoint 原事务")
        cursor = connection.execute(
            "UPDATE context_compaction_candidates SET applied_view_id=?,applied_summary_item_id=? "
            "WHERE candidate_id=? AND applied_view_id IS NULL AND discarded_reason IS NULL AND result_detail_ref IS NOT NULL",
            (view_id, summary_item_id, intent.frozen.candidate_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("compaction-candidate-conflict: 候选无法在本事务消费")
        connection.execute("UPDATE retention_refs SET status='released' WHERE retention_id=?",
                           (f"compaction:{intent.frozen.candidate_id}",))
        self._append_history_ranges(connection, intent.frozen, view_id=view_id)
        if intent.frozen.predecessor is not None:
            previous_id = intent.frozen.predecessor.candidate_id
            connection.execute(
                "UPDATE context_compaction_candidates SET discarded_reason='superseded-by-successor' "
                "WHERE candidate_id=? AND applied_view_id IS NULL AND discarded_reason IS NULL", (previous_id,),
            )
            connection.execute("UPDATE retention_refs SET status='released' WHERE retention_id=?", (f"compaction:{previous_id}",))

    def _append_history_ranges(self, connection: sqlite3.Connection, frozen: FrozenCompactionRange, *, view_id: str) -> None:
        """只按本次实际采用顺序追加未登记原件，原位置不受 rewind／裁剪影响。"""
        control = connection.execute("SELECT control_sequence FROM context_views WHERE view_id=?", (view_id,)).fetchone()[0]
        next_line = connection.execute(
            "SELECT COALESCE(MAX(line_start+line_count),1) FROM compaction_history_ranges WHERE checkpoint_ns=?", (frozen.checkpoint_ns,),
        ).fetchone()[0]
        for ref in frozen.refs:
            if connection.execute(
                "SELECT 1 FROM compaction_history_ranges WHERE checkpoint_ns=? AND item_id=? AND source_revision=?",
                (frozen.checkpoint_ns, ref.ref_id, ref.source_revision),
            ).fetchone() is not None:
                continue
            item = self._storage.read_items(frozen.session_id, frozen.thread_id, item_ids=(ref.ref_id,),
                                            checkpoint_ns=frozen.checkpoint_ns, connection=connection)[0]
            if ContextRef.canonical_item(item, session_id=ref.session_id, thread_id=ref.thread_id) != ref:
                raise ValueError("source-mismatch: 原文索引与候选冻结版本不一致")
            if item.semantic_kind == "compaction_summary":
                # 摘要不是原文；fork 已复制原采用事件的行范围，即使正文缺失也保留其位置。
                origins = [relation.to_dict() for relation in item.provenance_relations
                           if relation.relation == "derived_from" and relation.target_kind == "canonical_item"]
                if connection.execute(
                    "SELECT 1 FROM json_each(?) origin JOIN compaction_history_ranges history "
                    "ON history.item_id=json_extract(origin.value,'$.target_id') "
                    "AND json_extract(history.ref_json,'$.session_id')=json_extract(origin.value,'$.target_session_id') "
                    "AND json_extract(history.ref_json,'$.thread_id')=json_extract(origin.value,'$.target_thread_id') "
                    "WHERE history.checkpoint_ns=? LIMIT 1",
                    (canonical_json_text(origins), frozen.checkpoint_ns),
                ).fetchone() is None:
                    raise ValueError("detail-unavailable: 来源摘要缺少原采用事件的回读范围")
                continue
            lines = history_line_locators(canonical_json_line(item.to_dict()), compaction_history_fields(item),
                                          header=f"[{item.semantic_kind} {item.item_id}]")
            count = len(lines)
            connection.execute(
                "INSERT INTO compaction_history_ranges(checkpoint_ns,item_id,source_revision,ref_json,render_version,line_start,line_count,adopted_control_sequence) "
                "VALUES (?,?,?,?,2,?,?,?)", (frozen.checkpoint_ns, ref.ref_id, ref.source_revision, canonical_json_text(ref.to_dict()), next_line, count, control),
            )
            connection.executemany(
                "INSERT INTO compaction_history_lines(checkpoint_ns,item_id,source_revision,line_ordinal,prefix,source_offset,source_length,source_hash,quoted,literal) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)", [(frozen.checkpoint_ns, ref.ref_id, ref.source_revision, index, *line)
                                           for index, line in enumerate(lines)],
            )
            next_line += count

    def read_history(self, session_id: str, thread_id: str, *, line_offset: int = 1, limit: int = 200,
                     checkpoint_ns: str = "", max_bytes: int = 65536) -> str:
        """只定位本页相交的原件；缺失区间明确报错，不改读父 Thread 或旧文件。"""
        if type(line_offset) is not int or line_offset < 1 or type(limit) is not int or not 1 <= limit <= 2000:
            raise ValueError("压缩原文要求 line_offset>=1 且 1<=limit<=2000")
        with self._storage.read_connection(session_id, thread_id, checkpoint_ns) as connection:
            ranges = connection.execute(
                "SELECT ref_json,line_start,line_count,render_version FROM compaction_history_ranges WHERE checkpoint_ns=? "
                "AND line_start<? AND line_start+line_count>? ORDER BY line_start LIMIT ?",
                (checkpoint_ns, line_offset + limit, line_offset, limit),
            ).fetchall()
            if not ranges:
                if connection.execute(
                    "SELECT 1 FROM item_catalog WHERE semantic_kind='compaction_summary' LIMIT 1",
                ).fetchone() is not None and connection.execute("SELECT 1 FROM compaction_history_ranges LIMIT 1").fetchone() is None:
                    raise ValueError("detail-unavailable: 当前 Thread 未物化该摘要的原文回读范围")
                return ""
            lines: list[str] = []
            byte_count = 0
            with self._storage.jsonl_path(session_id, checkpoint_ns, thread_id=thread_id).open("rb") as stream:
                for raw, start, count, version in ranges:
                    if version != 2:
                        raise ValueError("compaction-history-format-unsupported: 原文渲染版本未知")
                    first, end = max(0, line_offset-start), min(count, line_offset+limit-start)
                    page = read_history_page(connection, stream, checkpoint_ns=checkpoint_ns,
                                             ref=parse_context_ref(json.loads(raw)), line_start=start,
                                             first=first, end=end, max_bytes=max_bytes-byte_count)
                    if not page and not lines:
                        raise ValueError(f"detail-unavailable: 原文单行超过工具字节上限，line_offset={start+first}")
                    lines.extend(page)
                    byte_count += sum(len(line.encode("utf-8")) + 1 for line in page)
                    if len(page) < end-first:
                        break
            return "\n".join(lines)

    @staticmethod
    def _read(connection: sqlite3.Connection, session_id: str, thread_id: str, candidate_id: str,
              checkpoint_ns: str) -> tuple[FrozenCompactionRange, str | None, str | None]:
        row = connection.execute(
            "SELECT manifest_json,result_detail_ref,discarded_reason FROM context_compaction_candidates "
            "WHERE candidate_id=? AND checkpoint_ns=?", (candidate_id, checkpoint_ns),
        ).fetchone()
        if row is None:
            raise KeyError(f"compaction-candidate-unavailable: {candidate_id}")
        frozen = FrozenCompactionRange.from_dict(json.loads(row[0]))
        if (frozen.session_id, frozen.thread_id, frozen.checkpoint_ns, frozen.candidate_id) != (
            session_id, thread_id, checkpoint_ns, candidate_id,
        ):
            raise ValueError("source-mismatch: 候选 owner 不一致")
        return frozen, row[1], row[2]

    def applicability(self, session_id: str, thread_id: str, candidate_id: str, *, checkpoint_ns: str = "") -> str | None:
        with self._storage.read_connection(session_id, thread_id, checkpoint_ns) as connection:
            frozen, result, discarded = self._read(connection, session_id, thread_id, candidate_id, checkpoint_ns)
            return discarded or self._candidate_error(connection, frozen, result)

    def discard(self, session_id: str, thread_id: str, candidate_id: str, *, reason: str, checkpoint_ns: str = "") -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("候选失效必须保留真实原因")
        with self._storage.write_connection(session_id, thread_id, checkpoint_ns) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._read(connection, session_id, thread_id, candidate_id, checkpoint_ns)
            applied = connection.execute(
                "SELECT applied_view_id FROM context_compaction_candidates WHERE candidate_id=?", (candidate_id,),
            ).fetchone()[0]
            if applied is not None:
                raise ValueError("compaction-candidate-conflict: 已采用的历史事实不得改写为 discarded")
            connection.execute(
                "UPDATE context_compaction_candidates SET discarded_reason=COALESCE(discarded_reason,?) WHERE candidate_id=?",
                (reason, candidate_id),
            )
            connection.execute("UPDATE retention_refs SET status='released' WHERE retention_id=?", (f"compaction:{candidate_id}",))
            self._storage._commit_connection(connection)

    def publish_result(
        self, frozen: FrozenCompactionRange, *, output_refs: tuple[ContextRef, ...], dispatch_ref: SealedContextDispatchRef,
        completion_deadline: datetime | None = None,
    ) -> DetailRef:
        """原任务发布 gate 内调用：提交完整 ready，再由调用方确认原 task。"""
        return self._save_result(frozen, output_refs=output_refs, dispatch_ref=dispatch_ref,
                                 ready=True, completion_deadline=completion_deadline).result_ref

    def save_summary_result(
        self, frozen: FrozenCompactionRange, *, output_refs: tuple[ContextRef, ...], dispatch_ref: SealedContextDispatchRef,
    ) -> FrozenSummaryResult:
        """保存真实块／合并结果，不把局部覆盖标记为 ready，也不占用候选名额。"""
        return self._save_result(frozen, output_refs=output_refs, dispatch_ref=dispatch_ref, ready=False)

    def _save_result(
        self, frozen: FrozenCompactionRange, *, output_refs: tuple[ContextRef, ...], dispatch_ref: SealedContextDispatchRef,
        ready: bool,
        completion_deadline: datetime | None = None,
    ) -> FrozenSummaryResult:
        if not output_refs or any(not isinstance(ref, ContextRef) for ref in output_refs):
            raise ValueError("compaction-result-unconfirmed: 必须引用完整有序的 canonical 摘要输出")
        if (dispatch_ref.session_id, dispatch_ref.thread_id, dispatch_ref.checkpoint_ns, dispatch_ref.turn_id) != (
            frozen.session_id, frozen.thread_id, frozen.checkpoint_ns, None,
        ):
            raise ValueError("source-mismatch: 候选结果必须来自本 Thread 的独立派生请求")
        with self._storage.write_connection(frozen.session_id, frozen.thread_id, frozen.checkpoint_ns) as connection:
            connection.execute("BEGIN IMMEDIATE")
            if completion_deadline is not None and datetime.now(UTC) >= completion_deadline:
                raise TimeoutError("compaction-deadline-exceeded: 结果提交已超过绝对期限")
            stored, existing, discarded = self._read(connection, frozen.session_id, frozen.thread_id,
                                                      frozen.candidate_id, frozen.checkpoint_ns)
            if stored != frozen:
                raise ValueError("compaction-candidate-conflict: 发布范围与冻结计划不一致")
            error = discarded or self._range_error(connection, frozen)
            if error is not None:
                raise ValueError(f"compaction-candidate-discarded: {error}")
            snapshot = self._context.assemblies.get_context_assembly(
                frozen.session_id, frozen.thread_id, assembly_id=dispatch_ref.assembly_id, checkpoint_ns=frozen.checkpoint_ns,
            )
            dispatch_ref.validate_snapshot(snapshot)
            if snapshot.selection_policy != "derived_request":
                raise ValueError("source-mismatch: 普通 Agent 请求不能发布候选摘要")
            coverage, text_ranges = self._request_coverage(frozen, snapshot)
            if not coverage or coverage[0] not in frozen.refs:
                raise ValueError("compaction-coverage-mismatch: 实际摘要输入不属于冻结范围")
            offset = frozen.refs.index(coverage[0])
            if (frozen.refs[offset:offset + len(coverage)] != coverage
                    or ready and (coverage != frozen.refs or text_ranges)):
                raise ValueError("compaction-coverage-mismatch: 实际摘要输入未按顺序完整覆盖冻结范围")
            body = {"output_refs": [ref.to_dict() for ref in output_refs], "dispatch_ref": dispatch_ref.to_dict(),
                    "coverage": [ref.to_dict() for ref in coverage],
                    "text_ranges": [part.to_dict() for part in text_ranges],
                    "source_versions": [list(pair) for pair in self._request_source_versions(connection, frozen, snapshot)]}
            if _source_version_error(connection, frozen, tuple(tuple(pair) for pair in body["source_versions"])) is not None:
                raise ValueError("compaction-candidate-discarded: source-version-changed")
            if ready and existing is not None:
                ref = detail_ref_from_key(existing)
                if self._read_result_manifest(frozen, ref) != body:
                    raise ValueError("compaction-candidate-conflict: 同一候选已有不同结果")
                return self._read_summary_result(connection, frozen, ref, body)
            completed = connection.execute(
                "SELECT execution.execution_origin,execution.turn_id,execution.outcome,call.outcome "
                "FROM executions execution JOIN model_calls call USING(execution_id) "
                "WHERE execution.session_id=? AND execution.thread_id=? AND execution.execution_id=? "
                "AND call.model_call_id=? AND call.assembly_id=?",
                (frozen.session_id, frozen.thread_id, dispatch_ref.execution_id,
                 dispatch_ref.sealed_model_call_id, dispatch_ref.assembly_id),
            ).fetchone()
            if completed != ("internal", None, "completed", "completed"):
                raise ValueError("compaction-result-unconfirmed: 派生执行尚未确认完成")
            admission = connection.execute(
                "SELECT json_extract(metadata_json,'$.request_input.reference.plan_id'),"
                "json_extract(metadata_json,'$.request_input.reference.task_id') FROM storage_commits "
                "WHERE subject_id=? AND commit_kind='execution_admission' AND status='committed'",
                (dispatch_ref.execution_id,),
            ).fetchone()
            if admission != (snapshot.plan_id, frozen.task_id):
                raise ValueError("source-mismatch: 派生请求不属于当前候选和任务")
            outputs = connection.execute(
                "SELECT item_id FROM item_catalog WHERE execution_id=? AND semantic_kind='assistant_output' "
                "AND status='completed' AND json_extract(projection_manifest_json,'$.projection_identity.model_call_id')=? "
                "ORDER BY item_sequence", (dispatch_ref.execution_id, dispatch_ref.sealed_model_call_id),
            ).fetchall()
            if tuple(row[0] for row in outputs) != tuple(ref.ref_id for ref in output_refs):
                raise ValueError("source-mismatch: 摘要必须引用该派生请求的完整有序输出")
            self._summary_text(connection, frozen, output_refs)
            if ready:
                if frozen.predecessor is not None:
                    # 发布时才交接尚未消费的手动意图，覆盖继任生成期间对前序的手动请求。
                    connection.execute(
                        "UPDATE context_compaction_candidates SET apply_on_next_request=MAX(apply_on_next_request,"
                        "COALESCE((SELECT apply_on_next_request FROM context_compaction_candidates "
                        "WHERE candidate_id=? AND applied_view_id IS NULL),0)) WHERE candidate_id=?",
                        (frozen.predecessor.candidate_id, frozen.candidate_id),
                    )
                ready_count = connection.execute(
                    "SELECT COUNT(*) FROM context_compaction_candidates WHERE result_detail_ref IS NOT NULL "
                    "AND applied_view_id IS NULL AND discarded_reason IS NULL",
                ).fetchone()[0]
                if ready_count >= 2:
                    retained_predecessor = None if frozen.predecessor is None else frozen.predecessor.candidate_id
                    obsolete = connection.execute(
                        "SELECT candidate_id FROM context_compaction_candidates WHERE result_detail_ref IS NOT NULL "
                        "AND applied_view_id IS NULL AND discarded_reason IS NULL AND candidate_id IS NOT ? "
                        "AND result_detail_ref NOT IN (SELECT predecessor_result_detail_ref FROM context_compaction_candidates "
                        "WHERE predecessor_result_detail_ref IS NOT NULL AND result_detail_ref IS NULL AND discarded_reason IS NULL) "
                        "ORDER BY created_at,rowid LIMIT 1", (retained_predecessor,),
                    ).fetchone()
                    if obsolete is None:
                        raise ValueError("compaction-candidate-capacity: 两份结果都仍受直接依赖保护")
                    connection.execute("UPDATE context_compaction_candidates SET discarded_reason='superseded-by-successor' WHERE candidate_id=?", obsolete)
                    connection.execute("UPDATE retention_refs SET status='released' WHERE retention_id=?", (f"compaction:{obsolete[0]}",))
            result_scope = f"compaction-{frozen.candidate_id}"
            revision = sha256_jcs(body)
            saved = connection.execute(
                "SELECT detail_ref FROM context_plan_details WHERE assembly_id=? AND source_revision=? "
                "AND detail_kind='compaction_result' AND status='available'", (result_scope, revision),
            ).fetchone()
            if saved is not None:
                result_ref = detail_ref_from_key(saved[0])
                if ready:
                    connection.execute(
                        "UPDATE context_compaction_candidates SET result_detail_ref=? WHERE candidate_id=?",
                        (saved[0], frozen.candidate_id),
                    )
                    if completion_deadline is not None and datetime.now(UTC) >= completion_deadline:
                        raise TimeoutError("compaction-deadline-exceeded: 结果提交已超过绝对期限")
                    self._storage._commit_connection(connection)
                return self._read_summary_result(connection, frozen, result_ref, body)
            prepared = self._details.prepare(
                session_id=frozen.session_id, thread_id=frozen.thread_id, assembly_id=result_scope,
                detail_kind="compaction_result", retention_class="compaction_candidate", visibility="internal",
                detail=body, required=True, checkpoint_ns=frozen.checkpoint_ns, source_revision=revision,
                sensitive=any(ref.protection != "public" for ref in coverage),
            )
            paths = self._details.publish(prepared)
            try:
                self._context.assemblies._register_context_plan_detail(
                    connection, prepared.record, session_id=frozen.session_id,
                    thread_id=frozen.thread_id, checkpoint_ns=frozen.checkpoint_ns,
                )
                if ready:
                    connection.execute(
                        "UPDATE context_compaction_candidates SET result_detail_ref=? WHERE candidate_id=?",
                        (detail_ref_key(prepared.record.detail_ref), frozen.candidate_id),
                    )
                if completion_deadline is not None and datetime.now(UTC) >= completion_deadline:
                    raise TimeoutError("compaction-deadline-exceeded: 结果提交已超过绝对期限")
                self._storage._commit_connection(connection)
            except BaseException:
                # 只有仍处于未提交事务时才能删除本次独占文件；提交结果不明时保留原件。
                if connection.in_transaction:
                    connection.rollback()
                    self._details.discard_published(paths)
                raise
            return self._read_summary_result(connection, frozen, prepared.record.detail_ref, body)

    def _request_coverage(
        self, frozen: FrozenCompactionRange, snapshot: ContextAssemblySnapshot,
    ) -> tuple[tuple[ContextRef, ...], tuple[CanonicalTextRange, ...]]:
        """以真正封存的输入展开覆盖；不能以调用方声称的候选范围代替实际输入。"""
        sources = {source.contribution_id: source for source in snapshot.contributions}
        parts: list[tuple[ContextRef, CanonicalTextRange | None]] = []
        slices: dict[ContextRef, CanonicalTextRange] = {}
        if snapshot.seal_binding is None:
            raise ValueError("compaction-coverage-mismatch: 实际摘要请求缺少冻结前缀")
        for frame in snapshot.seal_binding.prefix.state.frames:
            if not isinstance(frame.content, Mapping) or "text_range" not in frame.content:
                continue
            part = CanonicalTextRange.from_dict(frame.content["text_range"])
            if (part.ref in slices or frame.content.get("item_ids") != [part.ref.ref_id]
                    or tuple(member.ref_id for member in frame.item_refs if member.ref_type == "canonical_item") != (part.ref.ref_id,)):
                raise ValueError("compaction-coverage-mismatch: 冻结分片必须唯一绑定实际原件 frame")
            slices[part.ref] = part
        for entry in snapshot.selection:
            if not entry.included or not isinstance(entry.ref, ContextRef):
                continue
            if entry.ref.ref_type == "canonical_item":
                parts.append((entry.ref, slices.pop(entry.ref, None)))
                continue
            source = sources.get(entry.contribution_id)
            binding = source.source_binding if source is not None else None
            if binding is None or binding.summary_result_ref is None:
                continue
            record = self._context.assemblies.get_context_plan_detail(
                frozen.session_id, frozen.thread_id, detail_ref=binding.summary_result_ref,
                checkpoint_ns=frozen.checkpoint_ns,
            )
            if record.detail_kind != "compaction_result":
                raise ValueError("source-mismatch: 合并来源不是候选结果")
            manifest = self._details.read(
                session_id=frozen.session_id, thread_id=frozen.thread_id,
                record=record, include_sensitive=True,
            )["detail"]
            if not isinstance(manifest, dict) or set(manifest) != {"output_refs", "dispatch_ref", "coverage", "source_versions", "text_ranges"}:
                raise ValueError("compaction-coverage-mismatch: 摘要来源 manifest 字段非法")
            inherited = tuple(parse_context_ref(value) for value in manifest["coverage"])
            inherited_parts = tuple(CanonicalTextRange.from_dict(value) for value in manifest["text_ranges"])
            by_ref = {part.ref: part for part in inherited_parts}
            if len(by_ref) != len(inherited_parts) or any(ref not in inherited for ref in by_ref):
                raise ValueError("compaction-coverage-mismatch: 摘要来源分片必须唯一且属于覆盖范围")
            parts.extend((ref, by_ref.get(ref)) for ref in inherited)
        if slices:
            raise ValueError("compaction-coverage-mismatch: 分片 frame 未被实际选入")
        return _normalize_coverage(parts)

    def _request_source_versions(self, connection: sqlite3.Connection, frozen: FrozenCompactionRange,
                                 snapshot: ContextAssemblySnapshot) -> tuple[tuple[str, str], ...]:
        """只沿真正封存且选入的来源与前序结果解析依赖，不读取正文或全部 live sources。"""
        included = {entry.contribution_id for entry in snapshot.selection if entry.included}
        versions = dict(frozen.source_versions)
        # 同一封存请求可含旧 root 和后续更新；依赖其最终有效版本，不把合法替代当跨块冲突。
        binding = snapshot.seal_binding
        effective = {} if binding is None or binding.root is None else {
            source.source_identity: source.revision for source in binding.root.sources
        }
        if binding is not None:
            for frame in binding.prefix.state.frames:
                if frame.role != "system":
                    effective.update((ref.source_identity, ref.source_revision) for ref in frame.item_refs
                                     if ref.ref_type == "source_item")
        for source in snapshot.contributions:
            binding = source.source_binding
            if source.contribution_id not in included or binding is None:
                continue
            direct = ()
            if binding.summary_result_ref is not None:
                record = self._context.assemblies.get_context_plan_detail(
                    frozen.session_id, frozen.thread_id, detail_ref=binding.summary_result_ref, checkpoint_ns=frozen.checkpoint_ns,
                )
                if record.detail_kind != "compaction_result":
                    raise ValueError("source-mismatch: 摘要实际依赖的结果用途错误")
                manifest = self._details.read(session_id=frozen.session_id, thread_id=frozen.thread_id,
                                              record=record, include_sensitive=True)["detail"]
                direct = tuple(tuple(pair) for pair in manifest["source_versions"])
            elif binding.source_ref is not None and connection.execute(
                "SELECT 1 FROM context_source_control_states WHERE session_id=? AND thread_id=? AND source_id=?",
                (frozen.session_id, frozen.thread_id, binding.source_ref),
            ).fetchone() is not None:
                if effective.get(binding.source_ref, source.source_revision) != source.source_revision:
                    continue
                direct = ((binding.source_ref, source.source_revision),)
            for source_id, revision in direct:
                if source_id in versions and versions[source_id] != revision:
                    raise ValueError("source-version-changed: 摘要块读取了同一来源的不同版本")
                versions[source_id] = revision
        return tuple(versions.items())

    def _read_result_manifest(self, frozen: FrozenCompactionRange, ref: DetailRef) -> dict[str, object]:
        record = self._context.assemblies.get_context_plan_detail(
            frozen.session_id, frozen.thread_id, detail_ref=ref, checkpoint_ns=frozen.checkpoint_ns,
        )
        if (record.detail_kind, record.retention_class) != ("compaction_result", "compaction_candidate"):
            raise ValueError("source-mismatch: 候选结果 detail 用途错误")
        body = self._details.read(session_id=frozen.session_id, thread_id=frozen.thread_id,
                                  record=record, include_sensitive=True)["detail"]
        if not isinstance(body, dict) or set(body) != {"output_refs", "dispatch_ref", "coverage", "source_versions", "text_ranges"} or (
            body["coverage"] != [ref.to_dict() for ref in frozen.refs] or body["text_ranges"] != []
        ):
            raise ValueError("source-mismatch: 候选结果与冻结来源不一致")
        return body

    def read_result(self, session_id: str, thread_id: str, candidate_id: str, *, checkpoint_ns: str = "") -> FrozenSummaryResult | None:
        with self._storage.read_connection(session_id, thread_id, checkpoint_ns) as connection:
            frozen, result, discarded = self._read(connection, session_id, thread_id, candidate_id, checkpoint_ns)
            applied = connection.execute("SELECT applied_view_id FROM context_compaction_candidates WHERE candidate_id=?", (candidate_id,)).fetchone()[0]
            if discarded is not None or applied is None and self._candidate_error(connection, frozen, result) is not None:
                return None
            if result is None:
                return None
            body = self._read_result_manifest(frozen, detail_ref_from_key(result))
            return self._read_summary_result(connection, frozen, detail_ref_from_key(result), body)

    def select_ready_for_refs(
        self, connection: sqlite3.Connection, session_id: str, thread_id: str,
        refs: Sequence[ContextRef], *, checkpoint_ns: str = "",
    ) -> FrozenSummaryResult | None:
        """只读复用已就绪的完整连续子段；不追新候选、不改变任何采用事实。"""
        selected = tuple(refs)
        rows = connection.execute(
            "SELECT candidate_id FROM context_compaction_candidates WHERE checkpoint_ns=? "
            "AND result_detail_ref IS NOT NULL AND applied_view_id IS NULL AND discarded_reason IS NULL "
            "ORDER BY created_at DESC,candidate_id DESC LIMIT 2", (checkpoint_ns,),
        ).fetchall()
        for (candidate_id,) in rows:
            frozen, result, _discarded = self._read(connection, session_id, thread_id, candidate_id, checkpoint_ns)
            actual = compaction_selection_ids(frozen, [ref.ref_id for ref in selected])
            if actual is None or self._candidate_error(connection, frozen, result) is not None:
                continue
            actual_refs = {ref.ref_id: ref for ref in selected}
            if any(actual_refs.get(ref.ref_id) != ref for ref in frozen.refs if ref.ref_id in actual):
                continue
            ref = detail_ref_from_key(result)
            return self._read_summary_result(connection, frozen, ref, self._read_result_manifest(frozen, ref))
        return None

    def _read_summary_result(
        self, connection: sqlite3.Connection, frozen: FrozenCompactionRange,
        result_ref: DetailRef, body: dict[str, object],
    ) -> FrozenSummaryResult:
        output_refs = tuple(parse_context_ref(value) for value in body["output_refs"])
        return FrozenSummaryResult(
            text=self._summary_text(connection, frozen, output_refs),
            dispatch_ref=SealedContextDispatchRef.from_dict(body["dispatch_ref"]),
            coverage=tuple(parse_context_ref(ref) for ref in body["coverage"]), result_ref=result_ref, output_refs=output_refs,
            source_versions=tuple(tuple(pair) for pair in body["source_versions"]),
            text_ranges=tuple(CanonicalTextRange.from_dict(value) for value in body["text_ranges"]),
        )

    def _summary_text(
        self, connection: sqlite3.Connection, frozen: FrozenCompactionRange, output_refs: tuple[ContextRef, ...],
    ) -> str:
        items = self._storage.read_items(
            frozen.session_id, frozen.thread_id, checkpoint_ns=frozen.checkpoint_ns,
            item_ids=tuple(ref.ref_id for ref in output_refs), connection=connection,
        )
        if tuple(ContextRef.canonical_item(item, session_id=frozen.session_id, thread_id=frozen.thread_id)
                 for item in items) != output_refs:
            raise ValueError("source-mismatch: 摘要输出引用与真实 canonical Item 不一致")
        text = "".join(summary_output_text(item) for item in items)
        if not text.strip():
            raise ValueError("compaction-result-empty: 摘要正文必须是非空文本")
        return text

    def observation_increment(
        self, session_id: str, thread_id: str, *, after_sequence: int, limit: int = 200,
    ) -> tuple[int, tuple[CanonicalItemRecord, ...]]:
        """只读上次完整预算后的索引小页；未入当前主 context 的行仅推进水位。"""
        if after_sequence < 0 or not 1 <= limit <= 200:
            raise ValueError("稳定上下文观察的序号或分页上限非法")
        with self._storage.read_connection(session_id, thread_id, "") as connection:
            view = connection.execute(
                "SELECT branch.head_view_id FROM checkpoint_namespace_state namespace "
                "JOIN branches branch ON branch.branch_id=namespace.active_branch_id "
                "WHERE namespace.checkpoint_ns='' AND branch.status='active'",
            ).fetchone()
            if view is None or view[0] is None:
                raise RuntimeError("稳定上下文观察缺少原 active view")
            rows = connection.execute(
                "SELECT item.item_sequence,item.item_id,item.status,item.semantic_kind,"
                "EXISTS(SELECT 1 FROM context_view_items member WHERE member.view_id=? "
                "AND member.item_id=item.item_id AND member.visible=1 AND member.context_selected=1 "
                "AND member.source_kind='canonical') "
                "FROM item_catalog item WHERE item.item_sequence>? ORDER BY item.item_sequence LIMIT ?",
                (view[0], after_sequence, limit),
            ).fetchall()
            ids = tuple(row[1] for row in rows if row[2] == "completed" and row[4] and row[3] in {
                "user_input", "assistant_output", "reasoning", "tool_call", "tool_result", "attachment",
            })
            items = self._storage.read_items(session_id, thread_id, item_ids=ids, checkpoint_ns="", connection=connection) if ids else ()
            return (after_sequence if not rows else rows[-1][0]), tuple(items)
