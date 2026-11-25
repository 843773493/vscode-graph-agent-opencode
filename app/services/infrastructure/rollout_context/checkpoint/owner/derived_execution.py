"""派生输入及完整结果沿原 rollout／detail／retention owner 保存和读取。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

from app.core.sqlite_state import utc_now_text
from app.domain.itemized.compaction import FrozenSummaryResult, summary_output_text
from app.domain.itemized.derived_execution import FrozenDerivedInput
from app.domain.itemized.dispatch_ref import SealedContextDispatchRef
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.identity.detail_ref import DetailRef
from app.domain.itemized.identity.refs import ContextRef, ref_identity
from app.domain.itemized.plans.contributions import ContextContribution
from app.domain.itemized.serde.registry import parse_context_ref
from app.services.infrastructure.rollout_context.assembly.detail_identity import (
    detail_ref_from_key,
    detail_ref_key,
)
from app.services.infrastructure.rollout_context.runtime.derived_compiler import (
    closed_source_boundaries,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_text,
)


class DerivedExecutionOwnerMixin:
    def freeze_derived_input(
        self, session_id: str, thread_id: str, *, task_id: str, plan_id: str,
        instruction: str, allowed_tool_ids: tuple[str, ...], deadline: datetime,
        max_model_calls: int, submission_hash: str, model_recipe: Mapping[str, object],
        item_ids: Sequence[str] | None = None, checkpoint_ns: str = "",
        initial_sources: tuple[ContextContribution, ...] = (),
        gateway_id: str | None = None,
        initial_tool_set: ToolSetSnapshot | None = None,
    ) -> DetailRef:
        """同键只冻结一次；后续父 append、默认值及当前时间不改写已受理输入。"""
        retention_id = f"derived-input:{plan_id}"
        with self._context_reader.open_snapshot(session_id, thread_id, checkpoint_ns) as read:
            existing = read.connection.execute(
                "SELECT reference_id FROM retention_refs WHERE retention_id=?", (retention_id,),
            ).fetchone()
            if existing is None:
                source_view_id = self._storage.checkpoints.active_view_id(read)
                source_assembly_id = self._storage.assemblies._latest_context_assembly_id_connection(
                    read.connection, session_id, thread_id, checkpoint_ns=checkpoint_ns,
                )
                items = self._storage.checkpoints.committed_context_items(read, predecessor=None, include_pending_notices=False)
                if item_ids is not None:
                    wanted = tuple(item_ids)
                    wanted_ids = set(wanted)
                    selected = tuple(item for item in items if item.item_id in wanted_ids)
                    if tuple(item.item_id for item in selected) != wanted:
                        raise ValueError("derived-source-mismatch: 显式来源不是当前 view 的有序 selection")
                    items = selected
                boundaries = closed_source_boundaries(items)
                if not boundaries or item_ids is not None and boundaries[-1] != len(items):
                    raise ValueError("derived-source-unavailable: 来源没有合法闭合前缀")
                items = items[:boundaries[-1]]
                owners = {row[0]: row[1:] for row in read.connection.execute(
                    "SELECT item.item_id,COALESCE(execution.session_id,turn.session_id,?),"
                    "COALESCE(execution.thread_id,turn.thread_id,?) FROM item_catalog item "
                    "LEFT JOIN executions execution ON execution.execution_id=item.execution_id "
                    "LEFT JOIN turn_records turn ON turn.turn_id=item.turn_id "
                    "WHERE item.item_id IN (SELECT value FROM json_each(?))",
                    (session_id, thread_id, canonical_json_text([item.item_id for item in items])),
                )}
                refs = tuple(ContextRef.canonical_item(item, session_id=owners[item.item_id][0],
                                                      thread_id=owners[item.item_id][1]) for item in items)
                summary = self._storage.compactions.select_ready_for_refs(
                    read.connection, session_id, thread_id, refs, checkpoint_ns=checkpoint_ns,
                )
        if existing is not None:
            ref = detail_ref_from_key(existing[0])
            frozen = self.read_derived_input(session_id, thread_id, ref, checkpoint_ns=checkpoint_ns)
            if (frozen.task_id, frozen.submission_hash) != (task_id, submission_hash):
                raise ValueError("derived-input-conflict: 幂等身份已绑定不同显式输入")
            return ref
        frozen = FrozenDerivedInput(
            task_id, plan_id, session_id, thread_id, checkpoint_ns, source_view_id, source_assembly_id,
            refs, instruction, allowed_tool_ids, deadline, max_model_calls, submission_hash, model_recipe,
            initial_sources=initial_sources if source_assembly_id is None else (),
            summary_result_ref=None if summary is None else summary.result_ref,
            gateway_id=gateway_id,
            initial_tool_set=initial_tool_set if source_assembly_id is None else None,
        )
        prepared = self._detail_store.prepare(
            session_id=session_id, thread_id=thread_id, assembly_id=retention_id,
            detail_kind="derived_input", retention_class="request_replay", visibility="internal",
            detail=frozen.to_dict(), required=True, sensitive=True, checkpoint_ns=checkpoint_ns,
        )
        with self._storage.storage.write_connection(session_id, thread_id, checkpoint_ns) as connection:
            connection.execute("BEGIN IMMEDIATE")
            # 同键并发只发布一个赢家；其它请求不发布临时正文或补造另一份计划。
            existing = connection.execute(
                "SELECT reference_id FROM retention_refs WHERE retention_id=?", (retention_id,),
            ).fetchone()
            if existing is not None:
                ref = detail_ref_from_key(existing[0])
            else:
                published = self._detail_store.publish(prepared)
                try:
                    self._storage.assemblies._register_context_plan_detail(
                        connection, prepared.record, session_id=session_id, thread_id=thread_id, checkpoint_ns=checkpoint_ns,
                    )
                    ref = prepared.record.detail_ref
                    connection.execute(
                        "INSERT INTO retention_refs(retention_id,reference_kind,reference_id,target_view_id,owner_session_id,status,created_at) "
                        "VALUES (?,'derived_input',?,?,?,'active',?)",
                        (retention_id, detail_ref_key(ref), source_view_id, session_id, utc_now_text()),
                    )
                    if frozen.summary_result_ref is not None:
                        connection.execute(
                            "INSERT INTO retention_refs(retention_id,reference_kind,reference_id,target_view_id,owner_session_id,status,created_at) "
                            "VALUES (?,'derived_summary',?,?,?,'active',?)",
                            (f"{retention_id}:summary", detail_ref_key(frozen.summary_result_ref), source_view_id, session_id, utc_now_text()),
                        )
                    self._storage.storage._commit_connection(connection)
                except BaseException:
                    if connection.in_transaction:
                        connection.rollback()
                        self._detail_store.discard_published(published)
                    raise
        stored = self.read_derived_input(session_id, thread_id, ref, checkpoint_ns=checkpoint_ns)
        if (stored.task_id, stored.submission_hash) != (task_id, submission_hash):
            raise ValueError("derived-input-conflict: 幂等身份已绑定不同显式输入")
        return ref

    def read_derived_input(
        self, session_id: str, thread_id: str, detail_ref: DetailRef, *, checkpoint_ns: str = "",
    ) -> FrozenDerivedInput:
        detail_ref.require_owner(session_id, thread_id)
        record = self._storage.assemblies.get_context_plan_detail(
            session_id, thread_id, detail_ref=detail_ref, checkpoint_ns=checkpoint_ns,
        )
        if (record.detail_kind, record.retention_class) != ("derived_input", "request_replay"):
            raise ValueError("source-mismatch: 引用不是派生输入")
        frozen = FrozenDerivedInput.from_dict(self._detail_store.read(
            session_id=session_id, thread_id=thread_id, record=record, include_sensitive=True,
        )["detail"])
        if (frozen.session_id, frozen.thread_id, frozen.checkpoint_ns) != (session_id, thread_id, checkpoint_ns):
            raise ValueError("source-mismatch: 冻结派生输入归属不符")
        return frozen

    def release_derived_input(self, session_id: str, thread_id: str, *, plan_id: str, checkpoint_ns: str = "") -> None:
        """终态解除来源 view 占用；不可变输入引用仍服务原任务查询和对账。"""
        with self._storage.storage.write_connection(session_id, thread_id, checkpoint_ns) as connection:
            connection.execute("UPDATE retention_refs SET status='released' WHERE retention_id IN (?,?) AND reference_kind IN ('derived_input','derived_summary')",
                               (f"derived-input:{plan_id}", f"derived-input:{plan_id}:summary"))
            self._storage.storage._commit_connection(connection)

    def discard_derived_input(self, session_id: str, thread_id: str, *, plan_id: str, checkpoint_ns: str = "") -> None:
        """尚未登记任务时撤销临时引用，详情沿原 GC 回收，同键可重新准入。"""
        with self._storage.storage.write_connection(session_id, thread_id, checkpoint_ns) as connection:
            connection.execute("DELETE FROM retention_refs WHERE retention_id IN (?,?) AND reference_kind IN ('derived_input','derived_summary')",
                               (f"derived-input:{plan_id}", f"derived-input:{plan_id}:summary"))
            self._storage.storage._commit_connection(connection)

    def read_derived_summary(self, frozen: FrozenDerivedInput) -> FrozenSummaryResult | None:
        """读取首次选中的不可变结果，不再询问当前 ready 候选或猜测历史正文。"""
        ref = frozen.summary_result_ref
        if ref is None:
            return None
        record = self._storage.assemblies.get_context_plan_detail(
            frozen.session_id, frozen.thread_id, detail_ref=ref, checkpoint_ns=frozen.checkpoint_ns,
        )
        if (record.detail_kind, record.retention_class) != ("compaction_result", "compaction_candidate"):
            raise ValueError("source-mismatch: 派生冻结摘要不是原候选结果")
        body = self._detail_store.read(session_id=frozen.session_id, thread_id=frozen.thread_id,
                                       record=record, include_sensitive=True)["detail"]
        outputs = tuple(parse_context_ref(value) for value in body["output_refs"])
        items = self._storage.storage.read_ref_items(frozen.session_id, frozen.thread_id, refs=outputs,
                                                     checkpoint_ns=frozen.checkpoint_ns)
        if any(ContextRef.canonical_item(items[ref_identity(ref)], session_id=ref.session_id, thread_id=ref.thread_id) != ref
               for ref in outputs):
            raise ValueError("source-mismatch: 派生冻结摘要的输出内容已改变")
        text = "".join(summary_output_text(items[ref_identity(ref)]) for ref in outputs)
        return FrozenSummaryResult(text=text, dispatch_ref=SealedContextDispatchRef.from_dict(body["dispatch_ref"]),
                                   result_ref=ref, output_refs=outputs,
                                   coverage=tuple(parse_context_ref(value) for value in body["coverage"]))

    def completed_derived_result(
        self, session_id: str, thread_id: str, execution_id: str, *, checkpoint_ns: str = "",
    ) -> dict[str, object] | None:
        """只有 execution 完整收敛才是任务答案；单次工具或模型输出不冒充完整结果。"""
        with self._storage.storage.read_connection(session_id, thread_id, checkpoint_ns) as connection:
            row = connection.execute(
                "SELECT outcome,last_model_call_id FROM executions WHERE session_id=? AND thread_id=? AND execution_id=?",
                (session_id, thread_id, execution_id),
            ).fetchone()
            if row is None or row[0] != "completed":
                return None
            if row[1] is None:
                raise RuntimeError("已完成派生 execution 缺少最终模型调用")
            model_call_id = row[1]
            assembly = connection.execute("SELECT assembly_id FROM model_calls WHERE model_call_id=? AND execution_id=? AND outcome='completed'",
                                          (model_call_id, execution_id)).fetchone()
            if assembly is None:
                raise RuntimeError("派生完整输出缺少原模型调用")
        self.derived_execution_reference(session_id, thread_id=thread_id, execution_id=execution_id)
        snapshot = self._storage.assemblies.get_context_assembly(
            session_id, thread_id, assembly_id=assembly[0], checkpoint_ns=checkpoint_ns,
        )
        with self._storage.storage.read_connection(session_id, thread_id, checkpoint_ns) as connection:
            ids = tuple(row[0] for row in connection.execute(
                "SELECT item_id FROM item_catalog WHERE execution_id=? AND semantic_kind='assistant_output' "
                "AND status='completed' AND json_extract(projection_manifest_json,'$.projection_identity.model_call_id')=? "
                "ORDER BY item_sequence", (execution_id, model_call_id),
            ))
            outputs = self._storage.storage.read_items(session_id, thread_id, item_ids=ids,
                                                       checkpoint_ns=checkpoint_ns, connection=connection)
            if not outputs:
                raise RuntimeError("已完成派生 execution 缺少最终 canonical 文本结果")
        dispatch = self.issue_context_dispatch_ref(snapshot, attempt_key=snapshot.plan_id,
                                                  callback_model_call_id=model_call_id, checkpoint_ns=checkpoint_ns)
        return {"execution_id": execution_id, "dispatch_ref": dispatch.to_dict(),
                "output_refs": [ContextRef.canonical_item(output, session_id=session_id, thread_id=thread_id).to_dict() for output in outputs]}
