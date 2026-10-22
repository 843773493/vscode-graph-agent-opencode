"""Saver 的执行准入、冻结输入与终态端口；持久事实归 execution owner。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import asdict

from langchain_core.messages import HumanMessage

from app.abstractions.internal_message import PreparedInternalMessage
from app.abstractions.turn_terminal_status import ToolResultCompletion
from app.core.resource_uri import parse_session_artifact_uri
from app.core.session_control_primitives import CONTROL_DATABASE_NAME
from app.core.session_control_store import SessionControlStore
from app.domain.itemized.enums import SemanticKind, TurnScope
from app.domain.itemized.execution_admission import (
    ExecutionAdmission,
    InternalExecutionIntent,
)
from app.domain.itemized.execution_queue import (
    ExecutionQueueInput,
    ExecutionQueueRecord,
    ExecutionQueueSnapshot,
)
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.identity.item_projection import ItemProvenanceRelation
from app.domain.itemized.mutation_intents import (
    ApplySourceLifecycleDecision,
    MutationIntentOwner,
)
from app.domain.itemized.records import CanonicalItemRecord, InternalDisplayProvenance
from app.domain.itemized.runtime import ModelCallRecord
from app.domain.itemized.runtime_wakeup import RuntimeExecutionWakeup
from app.prompting import internal_message_factory
from app.services.infrastructure.rollout_context.runtime.context_sources.models import (
    ContextSourceDescriptor,
    ContextSourceSnapshot,
)
from app.services.infrastructure.rollout_context.storage.catalog.history_scope import (
    require_historical_scope,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutNotMaterializedError,
)
from app.services.mapping.itemized.thread_seed import (
    THREAD_SEED_DISPLAY_CONTENT,
    frozen_thread_seed,
)


class RuntimeExecutionOwnerMixin:
    def import_execution_queue(self, session_id: str, thread_id: str,
                               inputs: tuple[tuple[str, ExecutionQueueInput], ...]) -> None:
        self._storage.executions.import_execution_queue(session_id, thread_id, inputs)

    def execution_queue_before_dispatch(self, record: ExecutionQueueRecord) -> bool:
        return self._storage.executions.execution_queue_before_dispatch(record)

    def execution_queue_owners(self, *, session_id: str | None) -> tuple[MutationIntentOwner, ...]:
        return self._storage.checkpoints.list_checkpoint_owners(session_id=session_id)

    def validate_execution_queue(self, session_id: str, thread_id: str) -> None:
        self._storage.executions.validate_execution_queue(session_id, thread_id)

    def execution_queue_snapshot(self, session_id: str, thread_id: str) -> ExecutionQueueSnapshot:
        return self._storage.executions.execution_queue_snapshot(session_id, thread_id)

    def execution_queue_record(self, session_id: str, thread_id: str, job_id: str) -> ExecutionQueueRecord:
        return self._storage.executions.execution_queue_record(session_id, thread_id, job_id)

    def claim_execution_queue(self, session_id: str, thread_id: str, *, job_id: str,
                              claim_owner: str, previous_generation: int) -> ExecutionQueueRecord:
        return self._storage.executions.claim_execution_queue(
            session_id, thread_id, job_id=job_id, claim_owner=claim_owner,
            previous_generation=previous_generation,
        )

    def start_execution_queue(self, session_id: str, thread_id: str, *, job_id: str,
                              claim_owner: str, claim_generation: int) -> None:
        self._storage.executions.start_execution_queue(
            session_id, thread_id, job_id=job_id, claim_owner=claim_owner,
            claim_generation=claim_generation,
        )

    def update_execution_queue_policy(self, session_id: str, thread_id: str, *, job_id: str,
                                      policy: str, expected_revision: int) -> ExecutionQueueSnapshot:
        return self._storage.executions.update_execution_queue_policy(
            session_id, thread_id, job_id=job_id, policy=policy, expected_revision=expected_revision,
        )

    def attachment_message_references(self, session_id: str, thread_id: str, message_id: str) -> tuple[str, ...] | None:
        """精确读取已接受消息的附件使用；缺消息与损坏提交必须区分。"""
        storage = self._storage.storage
        item_id = f"item-{message_id}"
        try:
            with storage.read_connection(session_id, thread_id) as connection:
                row = connection.execute(
                    "SELECT execution_id FROM item_catalog WHERE item_id=?", (item_id,),
                ).fetchone()
                if row is None:
                    return None
                item = storage._read_canonical_item_from_catalog(
                    connection, session_id=session_id, thread_id=thread_id, checkpoint_ns="", item_id=item_id,
                )
                if item.semantic_kind not in {SemanticKind.USER_INPUT, SemanticKind.RUNTIME_NOTICE} or item.producer_ref["producer_id"] != message_id:
                    raise RuntimeError("附件消息身份与 canonical 输入不一致")
                admission = self._storage.executions._read_execution_admission_connection(
                    connection, session_id=session_id, thread_id=thread_id, execution_id=row[0], checkpoint_ns="",
                )
                cursor = self._storage.executions.execution_admission_cursor(admission, connection=connection)
                if admission.execution_origin == "user_turn":
                    accepted = connection.execute(
                        "SELECT root_input_item_id FROM turn_records WHERE turn_id=?", (admission.turn_id,),
                    ).fetchone()
                else:
                    accepted = connection.execute(
                        "SELECT json_extract(metadata_json, '$.notice_item_id') FROM storage_commits WHERE commit_id=?",
                        (cursor.commit_id,),
                    ).fetchone()
                if accepted != (item_id,):
                    raise RuntimeError("附件消息未绑定原始 canonical 接受提交")
                references = tuple(uri for (uri,) in connection.execute(
                    "SELECT resource_uri FROM item_resource_references WHERE item_id=? "
                    "AND resource_kind='attachment' ORDER BY reference_ordinal", (item_id,),
                ))
                attachments = item.payload.get("attachments", []) if isinstance(item.payload, Mapping) else []
                if references != tuple(entry["file_id"] for entry in attachments):
                    raise RuntimeError("canonical 附件正文与使用索引不一致")
                projected = self._storage.attachment_projection.project_attachment_payload(session_id, thread_id, {"attachments": list(attachments)})
                return tuple(entry["file_id"] for entry in projected["attachments"])
        except RolloutNotMaterializedError:
            return None

    def attachment_reference_in_use(self, session_id: str, thread_id: str, attachment_id: str) -> bool:
        """查询该 Session 各已发布 Thread 的 canonical 使用，含 fork 和隐藏 notice。"""
        storage = self._storage.storage
        resolver = storage._path_resolver
        with closing(SessionControlStore(resolver.resolve_session_node(session_id) / CONTROL_DATABASE_NAME)) as control:
            threads = (resolver.main_thread_id(session_id), *(row.thread_id for row in control.list_child_thread_rows()))
        if thread_id not in threads:
            raise RuntimeError("附件 owner Thread 不属于权威 Session catalog")
        for candidate in threads:
            try:
                with storage.read_connection(session_id, candidate) as connection:
                    references = tuple(uri for (uri,) in connection.execute(
                        "SELECT DISTINCT resource_uri FROM item_resource_references WHERE resource_kind='attachment'",
                    ))
                    projected = self._storage.attachment_projection.project_attachment_payload(session_id, candidate, {
                        "attachments": [{"file_id": uri} for uri in references],
                    })
                    for entry in projected["attachments"]:
                        parsed = parse_session_artifact_uri(entry["file_id"])
                        # 未继承的外部引用可以保留在历史中，不归本 Session owner。
                        if parsed.tail_segments == (session_id, "attachments", attachment_id):
                            return True
            except RolloutNotMaterializedError:
                continue
        return False

    def accepted_user_message(
        self, session_id: str, *, thread_id: str, job_id: str, message_id: str,
        checkpoint_ns: str = "",
    ) -> HumanMessage:
        """Runtime 消费已冻结的用户正文与附件，不重新读取或生成附件预览。"""
        admission = self._storage.executions.execution_admission_for_job(
            session_id, thread_id=thread_id, job_id=job_id, checkpoint_ns=checkpoint_ns,
        )
        item = self._storage.executions.accepted_user_input(admission, checkpoint_ns=checkpoint_ns)
        if item.producer_ref["producer_id"] != message_id:
            raise ValueError("Job 消息身份与已接受的用户根输入不一致")
        codec = self._storage.storage._codec()
        message = codec.from_dict(codec.project_message((item,)))
        if not isinstance(message, HumanMessage) or message.id != message_id:
            raise TypeError("已接受用户根输入未投影为同 ID 的 HumanMessage")
        return message.model_copy(update={"response_metadata": {
            **message.response_metadata,
            "message_id": message_id, "created_at": item.created_at, "updated_at": item.created_at,
            "message_metadata": {**message.response_metadata.get("message_metadata", {}),
                                 "turn_id": admission.turn_id, "job_id": job_id},
        }})

    def admit_internal_execution(
        self, session_id: str, *, thread_id: str, job_id: str,
        intent: InternalExecutionIntent, message: HumanMessage | None,
        checkpoint_ns: str = "",
        queue_input: ExecutionQueueInput | None = None,
    ) -> ExecutionAdmission:
        notice, request_input = self._internal_execution_input(intent, message)
        return self._storage.executions.admit_internal_execution(
            session_id, thread_id=thread_id, execution_id=intent.execution_id,
            job_id=job_id, admission_idempotency_key=intent.admission_idempotency_key,
            runtime_notice_item=notice, request_input=request_input,
            checkpoint_ns=checkpoint_ns, queue_input=queue_input,
        )

    def accepted_internal_message(
        self, session_id: str, *, thread_id: str, job_id: str, message_id: str,
        intent: InternalExecutionIntent,
        checkpoint_ns: str = "",
    ) -> HumanMessage:
        """notice 与 seed/reference 统一从已提交准入投影，不从 pending 正文恢复。"""
        admission = self._storage.executions.execution_admission_for_job(
            session_id, thread_id=thread_id, job_id=job_id, checkpoint_ns=checkpoint_ns,
        )
        if (admission.execution_origin != "internal" or admission.execution_id != intent.execution_id
                or admission.admission_idempotency_key != intent.admission_idempotency_key
                or message_id != intent.message_id):
            raise ValueError("内部输入 reader 必须绑定同一完整 internal identity")
        request_input = self._storage.executions.internal_execution_request_input(
            session_id, thread_id, admission.execution_id,
        )
        supplied_reference = json.loads(intent.input_reference_json) if intent.input_reference_json is not None else None
        if supplied_reference != request_input.get("reference"):
            raise ValueError("内部输入 reader 的 seed/reference 与已接受 preimage 不一致")
        with self._storage.storage.read_connection(session_id, thread_id, checkpoint_ns) as connection:
            created_at = connection.execute(
                "SELECT created_at FROM executions WHERE execution_id=?", (admission.execution_id,),
            ).fetchone()[0]
            if request_input["kind"] == "runtime_notice":
                rows = connection.execute(
                    "SELECT item.item_id FROM item_catalog AS item JOIN storage_commits AS commit_record "
                    "ON commit_record.commit_id=item.commit_id WHERE item.execution_id=? "
                    "AND commit_record.commit_kind='execution_admission' AND commit_record.subject_id=?",
                    (admission.execution_id, admission.execution_id),
                ).fetchall()
                if len(rows) != 1:
                    raise RuntimeError("内部输入缺少唯一 admission notice")
                item = self._storage.storage.read_items(
                    session_id, thread_id, checkpoint_ns=checkpoint_ns,
                    item_ids=(rows[0][0],), connection=connection,
                )[0]
                codec = self._storage.storage._codec()
                human = codec.from_dict(codec.project_message((item,)))
                if not isinstance(human, HumanMessage) or human.id != message_id:
                    raise TypeError("已接受 notice 未投影为同 ID 的 HumanMessage")
                return human.model_copy(update={"response_metadata": {
                    **human.response_metadata, "message_id": message_id,
                    "created_at": item.created_at, "updated_at": item.created_at,
                }})
        source = self.internal_seed_source(session_id, thread_id, admission.execution_id)
        if source is None:
            raise RuntimeError("已接受 seed/reference 缺少冻结输入")
        return HumanMessage(id=message_id, content=source.content, response_metadata={
            "message_id": message_id, "created_at": created_at, "updated_at": created_at,
            "message_metadata": internal_message_factory.metadata_for(
                kind="delegated_task", display_content=THREAD_SEED_DISPLAY_CONTENT,
            ),
        })

    def _internal_execution_input(
        self, intent: InternalExecutionIntent, message: HumanMessage | None,
    ) -> tuple[CanonicalItemRecord | None, dict[str, object]]:
        if not isinstance(intent, InternalExecutionIntent):
            raise TypeError("internal execution admission 必须使用 typed intent")
        if intent.input_reference_json is not None:
            if message is not None:
                raise ValueError("seed/reference admission 不得伪造 runtime notice")
            notice = None
            request_input = {
                "kind": "seed_reference",
                "reference": json.loads(intent.input_reference_json),
            }
        else:
            if not isinstance(message, HumanMessage):
                raise TypeError("internal execution notice 必须是 HumanMessage")
            response_metadata = message.response_metadata or {}
            message_id = response_metadata.get("message_id")
            created_at = response_metadata.get("created_at")
            if message_id != intent.message_id:
                raise ValueError(
                    "internal execution intent 与 message identity 不一致: "
                    f"intent={intent.message_id!r}, message={message_id!r}"
                )
            if not isinstance(created_at, str) or not created_at:
                raise ValueError("internal execution notice 缺少稳定 created_at")
            notice_items = self._storage.storage._codec().items_for_message(
                message,
                item_sequence=1,
                message_id=intent.message_id,
                turn_id=None,
                execution_id=intent.execution_id,
                timestamp=created_at,
            )
            if len(notice_items) != 1:
                raise ValueError("internal execution notice 不得展开成多个 canonical item")
            notice = notice_items[0]
            if (
                notice.item_id != intent.notice_item_id
                or notice.semantic_kind != SemanticKind.RUNTIME_NOTICE
                or notice.turn_id is not None
                or notice.turn_scope != TurnScope.PENDING_NEXT_TURN
            ):
                raise ValueError(
                    "internal execution notice 未生成预期的 pending runtime_notice"
                )
            request_input = {"kind": "runtime_notice", "payload": notice.payload}
        return notice, request_input

    def commit_runtime_source_wakeup(
        self, wakeup: RuntimeExecutionWakeup, *, source_kind: str,
        intent: InternalExecutionIntent, message: HumanMessage,
    ) -> RuntimeExecutionWakeup:
        notice, _request = self._internal_execution_input(intent, message)
        if notice is None:
            raise ValueError("runtime source wakeup 必须绑定真实 notice")
        decision = ApplySourceLifecycleDecision(
            owner=MutationIntentOwner(session_id=wakeup.session_id, thread_id=wakeup.thread_id),
            source_id=wakeup.source_id, source_kind=source_kind, name=source_kind,
            decision_kind="observe_pending", revision=sha256_jcs(notice.payload),
            pending_only=True, content=message.content,
            item_id=notice.item_id, runtime_notice_payload=(notice.payload if isinstance(notice.payload, dict) else None),
            execution_id=intent.execution_id,
        )
        queue_input = ExecutionQueueInput(
            execution_binding=intent, message_id=intent.message_id, agent_id=wakeup.agent_id,
            provider_id=message.response_metadata.get("boxteam_session_provider_id"),
            delivery_policy=wakeup.delivery_policy, gateway_id=wakeup.gateway_id,
            request_id=wakeup.request_id, created_at=wakeup.message_created_at,
        )
        return self._storage.executions.commit_runtime_source_wakeup(decision, notice, wakeup, queue_input=queue_input)

    def runtime_wakeup_for_admission(
        self, session_id: str, thread_id: str, admission_idempotency_key: str,
    ) -> RuntimeExecutionWakeup | None:
        return self._storage.executions.runtime_wakeup_for_admission(session_id, thread_id, admission_idempotency_key)

    def pending_runtime_wakeups(
        self, session_id: str, thread_id: str,
    ) -> tuple[RuntimeExecutionWakeup, ...]:
        return self._storage.executions.pending_runtime_wakeups(session_id, thread_id)

    def runtime_wakeup_message(self, wakeup: RuntimeExecutionWakeup) -> HumanMessage:
        item = self._storage.executions.runtime_wakeup_input(wakeup)
        message = self._storage.storage._codec().from_dict(self._storage.storage._codec().project_message((item,)))
        if not isinstance(message, HumanMessage):
            raise TypeError("runtime wakeup canonical projection 必须是 HumanMessage")
        return message.model_copy(update={"response_metadata": {**message.response_metadata,
            "message_id": message.id, "created_at": wakeup.message_created_at, "updated_at": wakeup.message_created_at}})

    def submit_execution_pending_source(self, decision: ApplySourceLifecycleDecision) -> None:
        """现有 execution 的 pending 来源沿 Session lease 提交，不新建 Job/Turn。"""
        if decision.execution_id is None or decision.decision_kind != "observe_pending":
            raise ValueError("execution pending source 缺少真实 execution 或 pending 决策")
        owner = decision.owner
        admission = self._storage.executions.execution_admission(owner.session_id, thread_id=owner.thread_id, execution_id=decision.execution_id)
        with closing(SessionControlStore(self._storage.storage._path_resolver.resolve_session_node(owner.session_id) / CONTROL_DATABASE_NAME)) as control:
            lease = control.create_or_get_lease(
                operation_kind="context_control", operation_identity=f"source:{decision.source_id}",
                preimage_hash=sha256_jcs({
                    "owner": {"session_id": owner.session_id, "thread_id": owner.thread_id},
                    "execution_id": admission.execution_id, "decision": asdict(decision),
                }).rsplit(":", 1)[1], recovery_ref=decision.item_id,
            )
            if lease.state == "active":
                lease = control.mark_lease_settling(lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token)
            if lease.state not in {"settling", "completed"}:
                raise RuntimeError("execution pending source lease 已失效")
            self.consume_mutation_intent(decision)
            if lease.state == "settling":
                control.settle_lease(lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token, outcome="completed")

    def register_execution_retry_source(
        self, admission: ExecutionAdmission, *, message_id: str, message: PreparedInternalMessage,
    ) -> None:
        """四类 retry 只绑定当前 execution，不创建新 admission 或 Job。"""
        self._storage.executions.execution_admission(
            admission.session_id, thread_id=admission.thread_id, execution_id=admission.execution_id,
        )
        kind = message.metadata["structured_prompt_kind"]
        decision = ApplySourceLifecycleDecision(
            owner=MutationIntentOwner(session_id=admission.session_id, thread_id=admission.thread_id),
            source_id=f"retry:{message_id}", source_kind=kind, name=kind,
            decision_kind="observe_pending", revision=sha256_jcs(message.content), pending_only=True,
            content=message.content, item_id=f"item-{message_id}",
            execution_id=admission.execution_id,
            extensions={"boxteam.checkpoint.annotations": {
                "extension_version": "1", "protection": "public", "value": dict(message.metadata),
            }},
        )
        self.submit_execution_pending_source(decision)

    def internal_seed_source(self, session_id: str, thread_id: str, execution_id: str) -> ContextSourceSnapshot | None:
        request = self._storage.executions.internal_execution_request_input(session_id, thread_id, execution_id)
        if request["kind"] != "seed_reference":
            return None
        reference = request["reference"]
        source_id = f"thread-seed:{reference['creation_idempotency_key']}"
        revision = sha256_jcs(reference)
        with self._storage.storage.open_read_snapshot(session_id, thread_id) as snapshot:
            execution_owner = snapshot.connection.execute(
                "SELECT session_id,thread_id FROM executions WHERE execution_id=?", (execution_id,),
            ).fetchone()
            if execution_owner is None:
                raise KeyError(f"internal execution 不存在: {execution_id}")
            require_historical_scope(snapshot.connection, *execution_owner, current_owner=(session_id, thread_id))
            imported = execution_owner != (session_id, thread_id)
            if imported:
                row = snapshot.connection.execute(
                    "SELECT assembly_id FROM context_assemblies WHERE execution_id=? "
                    "AND session_id=? AND thread_id=? AND status='terminal' ORDER BY rowid DESC LIMIT 1",
                    (execution_id, *execution_owner),
                ).fetchone()
                if row is None:
                    raise RuntimeError("source-mismatch: fork seed 缺少本地冻结 assembly")
                assembly, _audit = self._storage.assemblies._read_context_assembly_connection(
                    snapshot.connection, session_id, thread_id, assembly_id=row[0], checkpoint_ns="",
                )
                seed_ids = {source.contribution_id for source in assembly.contributions
                            if source.source_kind == "thread_task_seed" and source.source_revision == revision
                            and source.source_binding is not None and source.source_binding.source_ref == source_id}
                selected = tuple(entry for entry in assembly.selection
                                 if entry.included and entry.contribution_id in seed_ids)
                if len(selected) != 1 or selected[0].detail_ref is None:
                    raise RuntimeError("source-mismatch: fork seed 与冻结 reference selection 不一致")
                detail_ref = selected[0].detail_ref
        if imported:
            content = self.read_context_plan_detail(
                session_id, thread_id, detail_ref=detail_ref,
            )["detail"]
            if not isinstance(content, str):
                raise TypeError("fork seed detail 必须是冻结文本")
        else:
            with closing(SessionControlStore(
                self._storage.storage._path_resolver.resolve_session_node(session_id) / CONTROL_DATABASE_NAME,
            )) as control:
                record = control.get_thread_creation_record(reference["creation_idempotency_key"])
                if record.child_thread_id != thread_id:
                    raise RuntimeError("seed reference 与当前 child thread 不一致")
                frozen = frozen_thread_seed(record)
                if frozen.reference != reference:
                    raise RuntimeError("seed reference 与冻结 creation preimage 不一致")
                content = frozen.message.content
        return ContextSourceSnapshot(
            descriptor=ContextSourceDescriptor(
                source_id=source_id,
                source_kind="thread_task_seed", name="冻结委派任务", description="初始 execution 请求输入",
                internal_locator=reference["creation_idempotency_key"], root_placement="tail_only",
            ),
            revision=revision, content=content,
            applied_revision=None, tracked=False,
        )

    def execution_output(self, session_id: str, thread_id: str, execution_id: str) -> tuple[str, InternalDisplayProvenance]:
        return self._storage.executions.execution_output(session_id, thread_id, execution_id)

    def cold_execution_observations(self, session_id: str, thread_id: str, *, job_ids: list[str] | None = None, turn_id: str | None = None) -> list[dict[str, str]]:
        return self._storage.executions.cold_execution_observations(session_id, thread_id, job_ids=job_ids, turn_id=turn_id)

    def runtime_execution_outcome(self, session_id: str, thread_id: str, execution_id: str) -> str:
        return self._storage.executions.runtime_execution_outcome(session_id, thread_id, execution_id)

    def claim_runtime_wakeup(self, wakeup: RuntimeExecutionWakeup) -> int:
        return self._storage.executions.claim_runtime_wakeup(wakeup)

    def finish_runtime_wakeup(
        self, wakeup: RuntimeExecutionWakeup, *, dispatch_generation: int,
        receipt: dict[str, object] | None = None, error: str | None = None,
    ) -> None:
        self._storage.executions.finish_runtime_wakeup(
            wakeup, dispatch_generation=dispatch_generation, receipt=receipt, error=error,
        )

    def accept_turn(
        self,
        session_id: str,
        *,
        thread_id: str,
        accepted_ingress_id: str,
        acceptance_idempotency_key: str,
        job_id: str,
        admission_idempotency_key: str,
        payload: object,
        payload_kind: str = "text",
        checkpoint_ns: str = "",
        branch_id: str | None = None,
        turn_id: str | None = None,
        turn_ordinal: int | None = None,
        root_item_id: str | None = None,
        initial_execution_id: str | None = None,
        extensions: Mapping[str, object] | None = None,
        source_session_id: str | None = None,
        source_accepted_ingress_id: str | None = None,
        source_acceptance_idempotency_key: str | None = None,
        provenance_relations: tuple[ItemProvenanceRelation, ...] = (),
        replay_of_turn_id: str | None = None,
        identity_origin: str = "runtime",
        queue_input: ExecutionQueueInput | None = None,
    ) -> dict[str, object]:
        """在 checkpoint owner 内接受一个真实用户 Turn。"""
        return self._storage.executions.accept_turn(
            session_id,
            thread_id=thread_id,
            accepted_ingress_id=accepted_ingress_id,
            acceptance_idempotency_key=acceptance_idempotency_key,
            job_id=job_id,
            admission_idempotency_key=admission_idempotency_key,
            payload=payload,
            payload_kind=payload_kind,
            checkpoint_ns=checkpoint_ns,
            branch_id=branch_id,
            turn_id=turn_id,
            turn_ordinal=turn_ordinal,
            root_item_id=root_item_id,
            initial_execution_id=initial_execution_id,
            extensions=extensions,
            source_session_id=source_session_id,
            source_accepted_ingress_id=source_accepted_ingress_id,
            source_acceptance_idempotency_key=source_acceptance_idempotency_key,
            provenance_relations=provenance_relations,
            replay_of_turn_id=replay_of_turn_id,
            identity_origin=identity_origin,
            queue_input=queue_input,
        )

    def commit_tool_result(
        self, *, session_id: str, thread_id: str, job_id: str,
        completion: ToolResultCompletion, recovery: bool = False,
        checkpoint_ns: str = "",
    ) -> CanonicalItemRecord:
        """工具终态始终解析原 Job 的 durable admission，不借用后继请求上下文。"""
        if checkpoint_ns:
            raise ValueError(f"canonical 工具终态不接受非空 checkpoint_ns: {checkpoint_ns!r}")
        admission = self.execution_admission_for_job(
            session_id, thread_id=thread_id, job_id=job_id, checkpoint_ns=checkpoint_ns,
        )
        return self._storage.executions.commit_tool_result(
            admission, completion, recovery=recovery, checkpoint_ns=checkpoint_ns,
        )

    def execution_for_turn(
        self,
        session_id: str,
        *,
        turn_id: str,
        thread_id: str,
        checkpoint_ns: str = "",
    ) -> str:
        return self._storage.executions.execution_for_turn(
            session_id,
            turn_id=turn_id,
            thread_id=thread_id,
            checkpoint_ns=checkpoint_ns,
        )

    def execution_admission_for_job(
        self,
        session_id: str,
        *,
        thread_id: str,
        job_id: str,
        checkpoint_ns: str = "",
    ) -> ExecutionAdmission:
        return self._storage.executions.execution_admission_for_job(
            session_id,
            thread_id=thread_id,
            job_id=job_id,
            checkpoint_ns=checkpoint_ns,
        )

    def read_resumable_model_call(self, admission: ExecutionAdmission, model_call_id: str, *, carrier_items: tuple[CanonicalItemRecord, ...]) -> tuple[ModelCallRecord, str] | None:
        return self._storage.executions.read_resumable_model_call(admission, model_call_id, carrier_items=carrier_items)

    def execution_admission(
        self,
        session_id: str,
        *,
        thread_id: str,
        execution_id: str,
        checkpoint_ns: str = "",
    ) -> ExecutionAdmission:
        return self._storage.executions.execution_admission(
            session_id,
            thread_id=thread_id,
            execution_id=execution_id,
            checkpoint_ns=checkpoint_ns,
        )

    def dispatch_replay(
        self,
        session_id: str,
        *,
        thread_id: str,
        turn_id: str,
        checkpoint_ns: str = "",
    ) -> str:
        """通过 Saver 绑定原 Turn dispatch，不创建新的 Turn。"""
        return self._storage.executions.dispatch_replay(
            session_id,
            thread_id=thread_id,
            turn_id=turn_id,
            checkpoint_ns=checkpoint_ns,
        )

    def register_model_call(
        self,
        session_id: str,
        *,
        thread_id: str,
        execution_id: str,
        model_call_id: str,
        attempt: int,
        provider: str,
        provider_request_id: str | None = None,
        retry_of_model_call_id: str | None = None,
        assembly_id: str | None = None,
        dispatch_state: str = "ready",
        checkpoint_ns: str = "",
    ) -> None:
        self._storage.executions.register_model_call(
            session_id,
            thread_id=thread_id,
            execution_id=execution_id,
            model_call_id=model_call_id,
            attempt=attempt,
            provider=provider,
            provider_request_id=provider_request_id,
            retry_of_model_call_id=retry_of_model_call_id,
            assembly_id=assembly_id,
            dispatch_state=dispatch_state,
            checkpoint_ns=checkpoint_ns,
        )

    def update_model_call_outcome(
        self,
        session_id: str,
        *,
        thread_id: str,
        model_call_id: str,
        outcome: str,
        dispatch_state: str | None = None,
        checkpoint_ns: str = "",
    ) -> None:
        self._storage.executions.update_model_call_outcome(
            session_id,
            thread_id=thread_id,
            model_call_id=model_call_id,
            outcome=outcome,
            dispatch_state=dispatch_state,
            checkpoint_ns=checkpoint_ns,
        )

    def converge_execution(
        self,
        session_id: str,
        *,
        thread_id: str,
        turn_id: str | None,
        execution_id: str,
        outcome: str,
        turn_status: str | None,
        items: Sequence[CanonicalItemRecord] = (),
        final_item_id: str | None = None,
        assembly_id: str | None = None,
        checkpoint_ns: str = "",
        loss_reason: str | None = None,
    ) -> int:
        """通过 Saver 原子收敛 execution、output item、assembly 与 Turn。"""
        return self._storage.executions.converge_execution(
            session_id,
            thread_id=thread_id,
            turn_id=turn_id,
            execution_id=execution_id,
            outcome=outcome,
            turn_status=turn_status,
            items=items,
            final_item_id=final_item_id,
            assembly_id=assembly_id,
            checkpoint_ns=checkpoint_ns,
            loss_reason=loss_reason,
        )

    def resume_turn(
        self,
        session_id: str,
        *,
        turn_id: str,
        thread_id: str,
        job_id: str,
        checkpoint_ns: str = "",
    ) -> dict[str, object]:
        return self._storage.executions.resume_turn(
            session_id,
            turn_id=turn_id,
            thread_id=thread_id,
            job_id=job_id,
            checkpoint_ns=checkpoint_ns,
        )

    def mark_execution_lost(
        self,
        session_id: str,
        *,
        turn_id: str | None,
        thread_id: str,
        execution_id: str | None = None,
        checkpoint_ns: str = "",
        reason: str = "provider_result_commit_missing",
    ) -> dict[str, object]:
        """通过 Saver 记录 provider dispatch 后丢失的 execution。"""
        return self._storage.executions.mark_execution_lost(
            session_id,
            turn_id=turn_id,
            thread_id=thread_id,
            execution_id=execution_id,
            checkpoint_ns=checkpoint_ns,
            reason=reason,
        )
