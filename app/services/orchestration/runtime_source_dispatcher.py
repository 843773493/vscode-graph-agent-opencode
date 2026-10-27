"""runtime producer 的生命周期准入与持久 wakeup 消费。"""

from __future__ import annotations

import hashlib
import json
from contextlib import closing
from typing import cast

from langchain_core.messages import HumanMessage

from app.abstractions.internal_message import (
    PreparedInternalMessage,
    RuntimeContextReceipt,
)
from app.abstractions.job_service import JobServiceProtocol
from app.core.key_lock_pool import KeyLockPool
from app.core.session_control_primitives import CONTROL_DATABASE_NAME
from app.core.session_control_store import SessionControlStore
from app.core.trace_middleware import (
    get_current_gateway_id,
    get_current_request_id,
    reset_current_gateway_id,
    reset_current_request_id,
    set_current_gateway_id,
    set_current_request_id,
)
from app.domain.itemized.execution_admission import InternalExecutionIntent
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.domain.itemized.mutation_intents import (
    ApplySourceLifecycleDecision,
    MutationIntentOwner,
)
from app.domain.itemized.runtime_wakeup import RuntimeExecutionWakeup
from app.prompting.validation import validate_internal_message
from app.schemas.internal_v2.job import JobDispatchSnapshotDTO, JobDispatchStatus
from app.schemas.internal_v2.message import MessageRunAccepted
from app.schemas.internal_v2.pending_request import DeliveryPolicy
from app.services.business.session_service import SessionService
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)

_CONTEXT_RECOVERY_REF = "runtime-context:v1:"


class RuntimeSourceDispatcher:
    def __init__(
        self, *, saver: RolloutCheckpointSaver,
        session_service: SessionService, job_service: JobServiceProtocol,
    ) -> None:
        self._saver = saver
        self._sessions = session_service
        self._jobs = job_service
        self._dispatch_locks = KeyLockPool()

    @staticmethod
    def _digest(session_id: str, thread_id: str, idempotency_key: str) -> str:
        return hashlib.sha256(
            f"runtime-source\0{session_id}\0{thread_id}\0{idempotency_key}".encode(),
        ).hexdigest()

    def submitted(
        self, session_id: str, thread_id: str, *, idempotency_key: str,
    ) -> RuntimeExecutionWakeup | None:
        """区分已提交的准入恢复与尚未发生的新准入，不以邮箱事实代替。"""
        digest = self._digest(session_id, thread_id, idempotency_key)
        return self._saver.runtime_wakeup_for_admission(session_id, thread_id, f"radm_{digest}")

    async def publish_context(
        self, session_id: str, thread_id: str, message: PreparedInternalMessage,
        *, source_id: str, idempotency_key: str,
    ) -> RuntimeContextReceipt:
        """仅发布事件，沿唯一 source owner 入库；没有 wakeup、执行或伪用户输入。"""
        validate_internal_message(message.content, message.metadata)
        kind = message.metadata["structured_prompt_kind"]
        if not isinstance(kind, str):
            raise TypeError("runtime source kind 必须是字符串")
        digest = self._digest(session_id, thread_id, idempotency_key)
        revision = sha256_jcs({"content": message.content, "metadata": message.metadata})
        item_id = f"item-rmsg_{digest}"
        decision = ApplySourceLifecycleDecision(
            owner=MutationIntentOwner(session_id=session_id, thread_id=thread_id),
            source_id=source_id, source_kind=kind, name=kind,
            decision_kind="observe_pending", revision=revision, pending_only=True,
            content=message.content, item_id=item_id, turn_scope="ambient",
            extensions={"boxteam.checkpoint.annotations": {
                "extension_version": "1", "protection": "public", "value": dict(message.metadata),
            }},
        )
        async with self._sessions.session_write_admission(session_id):
            resolver = self._sessions.path_resolver
            resolver.resolve_thread_node(session_id, thread_id)
            with closing(SessionControlStore(
                resolver.resolve_session_node(session_id) / CONTROL_DATABASE_NAME,
            )) as control:
                lease = control.create_or_get_lease(
                    operation_kind="context_control", operation_identity=f"radm_{digest}",
                    preimage_hash=sha256_jcs({
                        "session_id": session_id, "thread_id": thread_id,
                        "source_id": source_id, "prompt": message.content,
                        "metadata": message.metadata, "delivery_mode": "context_only",
                    }).rsplit(":", 1)[1], recovery_ref=_CONTEXT_RECOVERY_REF + canonical_json_bytes({
                        "thread_id": thread_id, "item_id": item_id,
                    }).decode(),
                )
                if lease.state == "active":
                    lease = control.mark_lease_settling(
                        lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token,
                    )
                if lease.state not in {"settling", "completed"}:
                    raise RuntimeError("runtime context 的生命周期准入已失效")
                self._saver.consume_mutation_intent(decision)
                if lease.state == "settling":
                    control.settle_lease(
                        lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token,
                        outcome="completed",
                    )
        return RuntimeContextReceipt(session_id, thread_id, source_id, item_id, revision)

    def recover_context_publications(self, session_id: str) -> int:
        """调用方持 Session exclusive；按具名恢复引用核对原提交，不重放正文。"""
        resolver = self._sessions.path_resolver
        count = 0
        with closing(SessionControlStore(resolver.resolve_session_node(session_id) / CONTROL_DATABASE_NAME)) as control:
            for lease in control.list_non_terminal_leases():
                if lease.operation_kind != "context_control" or lease.recovery_ref is None or not lease.recovery_ref.startswith(_CONTEXT_RECOVERY_REF):
                    continue
                reference = json.loads(lease.recovery_ref.removeprefix(_CONTEXT_RECOVERY_REF))
                if not isinstance(reference, dict) or reference.keys() != {"thread_id", "item_id"}:
                    raise ValueError("runtime context 恢复引用结构非法")
                thread_id, item_id = reference["thread_id"], reference["item_id"]
                if not isinstance(thread_id, str) or not isinstance(item_id, str):
                    raise TypeError("runtime context 恢复引用必须明确 Thread 和 item")
                resolver.resolve_thread_node(session_id, thread_id)
                preimage = self._saver.runtime_context_publication_preimage(session_id, thread_id, item_id)
                if preimage is not None and preimage != lease.preimage_hash:
                    raise RuntimeError("runtime context 已提交正文与原准入 preimage 不一致")
                lease = control.takeover_lease(lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token)
                lease = control.mark_lease_settling(lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token)
                control.settle_lease(lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token,
                                    outcome="completed" if preimage is not None else "failed")
                count += 1
        return count

    async def submit(
        self, session_id: str, thread_id: str, message: PreparedInternalMessage,
        *, source_id: str, idempotency_key: str,
        delivery_policy: DeliveryPolicy = "after_turn",
    ) -> MessageRunAccepted:
        validate_internal_message(message.content, message.metadata)
        kind = message.metadata["structured_prompt_kind"]
        if not isinstance(kind, str):
            raise TypeError("runtime source kind 必须是字符串")
        digest = self._digest(session_id, thread_id, idempotency_key)
        intent = InternalExecutionIntent(
            message_id=f"rmsg_{digest}", execution_id=f"rexec_{digest}",
            admission_idempotency_key=f"radm_{digest}",
        )
        async with self._sessions.session_write_admission(session_id):
            session = await self._sessions.get(session_id)
            frozen_wakeup = self._saver.runtime_wakeup_for_admission(
                session_id, thread_id, intent.admission_idempotency_key,
            )
            provider_id = session.current_provider_id
            if frozen_wakeup is not None:
                frozen_metadata = self._saver.runtime_wakeup_message(frozen_wakeup).response_metadata
                provider_id = frozen_metadata["boxteam_session_provider_id"]
            message_metadata = {
                **dict(message.metadata),
                "boxteam_session_provider_id": provider_id,
            }
            resolver = self._sessions.path_resolver
            resolver.resolve_thread_node(session_id, thread_id)
            with closing(SessionControlStore(
                resolver.resolve_session_node(session_id) / CONTROL_DATABASE_NAME,
            )) as control:
                lease = control.create_or_get_lease(
                    operation_kind="context_control",
                    operation_identity=intent.admission_idempotency_key,
                    preimage_hash=sha256_jcs({
                        "session_id": session_id, "thread_id": thread_id,
                        "source_id": source_id, "prompt": message.content,
                        "metadata": message_metadata, "delivery_policy": delivery_policy,
                    }).rsplit(":", 1)[1],
                    recovery_ref=intent.execution_id,
                )
                if lease.state == "active":
                    lease = control.mark_lease_settling(
                        lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token,
                    )
                created_at = lease.created_at
                wakeup = RuntimeExecutionWakeup(
                    session_id=session_id, thread_id=thread_id, source_id=source_id,
                    source_item_id=intent.notice_item_id, execution_id=intent.execution_id,
                    job_id=f"rjob_{digest}",
                    admission_idempotency_key=intent.admission_idempotency_key,
                    agent_id=session.current_agent_id, message_created_at=created_at,
                    gateway_id=get_current_gateway_id(), request_id=get_current_request_id(),
                    delivery_policy=delivery_policy,
                    lease_id=lease.lease_id, lifecycle_generation=lease.captured_lifecycle_generation,
                    lease_fencing_token=lease.fencing_token,
                )
                wakeup = self._saver.commit_runtime_source_wakeup(
                    wakeup, source_kind=kind, intent=intent,
                    message=HumanMessage(
                        id=intent.message_id, content=message.content,
                        response_metadata={
                            **message_metadata, "message_id": intent.message_id,
                            "created_at": created_at,
                        },
                    ),
                )
                if lease.state == "settling":
                    control.settle_lease(
                        lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token,
                        outcome="completed",
                    )
        return await self.dispatch(wakeup)

    async def dispatch(self, wakeup: RuntimeExecutionWakeup) -> MessageRunAccepted:
        async with self._dispatch_locks.lock_for(wakeup.admission_idempotency_key), self._sessions.session_write_admission(wakeup.session_id):
            resolver = self._sessions.path_resolver
            resolver.resolve_thread_node(wakeup.session_id, wakeup.thread_id)
            with closing(SessionControlStore(resolver.resolve_session_node(wakeup.session_id) / CONTROL_DATABASE_NAME)) as control:
                lease = control.get_lease(wakeup.lease_id)
                if (
                    control.get_fence() != ("active", wakeup.lifecycle_generation)
                    or lease.fencing_token != wakeup.lease_fencing_token
                    or lease.state not in {"settling", "completed"}
                ):
                    raise RuntimeError("runtime wakeup 的生命周期准入或 lease fencing 已失效")
                # source 已提交、lease 尚未终结的窗口以同一 identity 收敛。
                if lease.state == "settling":
                    control.settle_lease(lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token, outcome="completed")
            generation = self._saver.claim_runtime_wakeup(wakeup)
            return await self._dispatch_claimed(wakeup, generation)


    async def _dispatch_claimed(self, wakeup: RuntimeExecutionWakeup, generation: int) -> MessageRunAccepted:
        message = self._saver.runtime_wakeup_message(wakeup)
        message_id = message.id
        if not isinstance(message_id, str) or not message_id:
            raise RuntimeError("runtime wakeup canonical input 缺少 projection message identity")
        intent = InternalExecutionIntent(message_id=message_id, execution_id=wakeup.execution_id,
                                         admission_idempotency_key=wakeup.admission_idempotency_key)
        gateway_token = set_current_gateway_id(wakeup.gateway_id)
        request_token = set_current_request_id(wakeup.request_id)
        try:
            outcome = self._saver.runtime_execution_outcome(wakeup.session_id, wakeup.thread_id, wakeup.execution_id)
            terminal = {"completed": "completed", "completed_empty": "completed", "failed": "failed",
                        "cancelled": "cancelled", "interrupted": "timed_out", "execution_lost": "failed"}
            if outcome in terminal:
                dispatch = JobDispatchSnapshotDTO(
                    session_id=wakeup.session_id, thread_id=wakeup.thread_id, job_id=wakeup.job_id,
                    job_status=cast(JobDispatchStatus, terminal[outcome]),
                    queued_jobs_ahead=0, queued_job_count=0, pending_job_count=0,
                )
            else:
                dispatch = await self._jobs.start_job(
                    wakeup.session_id, str(message.content), thread_id=wakeup.thread_id,
                    execution_binding=intent, reserved_job_id=wakeup.job_id,
                    agent_id=wakeup.agent_id, message_id=message_id,
                    message_created_at=wakeup.message_created_at,
                    message_metadata=dict(message.response_metadata),
                    delivery_policy=cast(DeliveryPolicy, wakeup.delivery_policy),
                )
            accepted = MessageRunAccepted(message_id=message_id, job_id=wakeup.job_id, turn_id=None,
                                          status=dispatch.job_status, dispatch=dispatch, thread_id=wakeup.thread_id)
            self._saver.finish_runtime_wakeup(wakeup, dispatch_generation=generation,
                                            receipt=accepted.model_dump(mode="json"))
            return accepted
        except Exception as error:
            self._saver.finish_runtime_wakeup(wakeup, dispatch_generation=generation,
                                            error=f"{type(error).__name__}: {error}")
            raise
        finally:
            reset_current_gateway_id(gateway_token)
            reset_current_request_id(request_token)

    async def start(self) -> None:
        """backend startup 只消费 catalog/thread 与 pending 状态索引。"""
        _revision, nodes = self._sessions.path_resolver.catalog_store.read_nodes_snapshot()
        for node in nodes:
            if node.kind != "session" or node.state != "active":
                continue
            session_id = node.node_id
            async with self._sessions.session_write_admission(session_id):
                self.recover_context_publications(session_id)
            with closing(SessionControlStore(
                self._sessions.path_resolver.resolve_session_node(session_id) / CONTROL_DATABASE_NAME,
            )) as control:
                threads = (str(control.get_main_thread()["thread_id"]), *(row.thread_id for row in control.list_child_thread_rows()))
            for thread_id in threads:
                for wakeup in self._saver.pending_runtime_wakeups(session_id, thread_id):
                    await self.dispatch(wakeup)
