"""runtime producer 的生命周期准入与持久 wakeup 消费。"""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import closing
from typing import cast

from langchain_core.messages import HumanMessage

from app.abstractions.internal_message import PreparedInternalMessage
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
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.runtime_wakeup import RuntimeExecutionWakeup
from app.prompting.validation import validate_internal_message
from app.schemas.internal_v2.job import JobDispatchSnapshotDTO, JobDispatchStatus
from app.schemas.internal_v2.message import MessageRunAccepted
from app.schemas.internal_v2.pending_request import DeliveryPolicy
from app.services.business.job.service import JobState
from app.services.business.session_service import SessionService
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)


class RuntimeSourceDispatcher:
    def __init__(
        self, *, saver: RolloutCheckpointSaver,
        session_service: SessionService, job_service: JobServiceProtocol,
    ) -> None:
        self._saver = saver
        self._sessions = session_service
        self._jobs = job_service
        self._dispatch_locks = KeyLockPool()

    async def can_restart_before_provider(self, job: JobState) -> bool:
        """未准备 Provider 的原 runtime 准入留给既有 wakeup 恢复，不重放已封存调用。"""
        binding = job.execution_binding
        if not isinstance(binding, InternalExecutionIntent):
            return False
        wakeup = await asyncio.to_thread(
            self._saver.runtime_wakeup_before_provider,
            job.session_id, job.thread_id, execution_id=binding.execution_id,
            job_id=job.job_id, admission_idempotency_key=binding.admission_idempotency_key,
        )
        if wakeup is None:
            return False
        if binding.message_id != job.message_id or wakeup.source_item_id != binding.notice_item_id:
            raise RuntimeError("未派发 runtime Job 与冻结 wakeup 消息 identity 不一致")
        return True

    async def submit(
        self, session_id: str, thread_id: str, message: PreparedInternalMessage,
        *, source_id: str, idempotency_key: str,
        delivery_policy: DeliveryPolicy = "after_turn",
    ) -> MessageRunAccepted:
        validate_internal_message(message.content, message.metadata)
        kind = message.metadata["structured_prompt_kind"]
        if not isinstance(kind, str):
            raise TypeError("runtime source kind 必须是字符串")
        digest = hashlib.sha256(
            f"runtime-source\0{session_id}\0{thread_id}\0{idempotency_key}".encode(),
        ).hexdigest()
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
            with closing(SessionControlStore(
                self._sessions.path_resolver.resolve_session_node(session_id) / CONTROL_DATABASE_NAME,
            )) as control:
                threads = (str(control.get_main_thread()["thread_id"]), *(row.thread_id for row in control.list_child_thread_rows()))
            for thread_id in threads:
                for wakeup in self._saver.pending_runtime_wakeups(session_id, thread_id):
                    await self.dispatch(wakeup)
