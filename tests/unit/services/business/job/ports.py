"""Job 单元测试的显式端口替身；SQLite/崩溃正确性由集成测试证明。"""
from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest

from app.domain.itemized.execution_admission import (
    ExecutionAdmission,
    ExecutionAdmissionCursor,
    UserExecutionIdentity,
)
from app.domain.itemized.execution_queue import (
    ExecutionQueueInput,
    ExecutionQueueRecord,
)
from app.schemas.internal_v2.message import MessageDTO
from app.schemas.internal_v2.pending_request import PendingRequestDTO
from app.services.business.job.service import JobService
from app.services.business.message_display import project_message_for_display


def queue_input(job) -> ExecutionQueueInput:
    return ExecutionQueueInput(execution_binding=job.execution_binding, message_id=job.message_id,
        agent_id=job.agent_id, provider_id=job.message_metadata.get("boxteam_session_provider_id"),
        delivery_policy=job.delivery_policy or "after_turn", gateway_id=job.gateway_id,
        request_id=job.request_id, created_at=job.message_created_at)


def enqueue_job(service, job, policy):
    """控制类单测显式装配准入端口回执，不声称由真实业务入口生成。"""
    job.delivery_policy = policy
    job.queue_record = service._input_owner.accept_job_input(job, queue_input=queue_input(job))
    return service._pending_queue.append(job.session_id, job.job_id, policy,
        thread_id=job.thread_id, enqueue_sequence=job.queue_record.cursor.commit_id)


@pytest.fixture
def job_service_factory():
    def make(**kwargs):
        if "input_owner" in kwargs:
            return JobService(**kwargs)
        states = {}
        records = {}
        revision = 0
        owner = Mock()
        store = Mock()

        def accept(job, *, queue_input):
            nonlocal revision
            if job.job_id in records:
                return records[job.job_id]
            binding = job.execution_binding
            user = isinstance(binding, UserExecutionIdentity)
            admission = ExecutionAdmission(job.session_id, job.thread_id,
                f"execution-{job.job_id}" if user else binding.execution_id,
                job.job_id, binding.admission_idempotency_key, "user_turn" if user else "internal",
                binding.turn_id if user else None)
            record = ExecutionQueueRecord(ExecutionAdmissionCursor(admission, len(records) + 1,
                "acceptance" if user else "execution_admission"), queue_input,
                queue_input.delivery_policy, "queued", None, 0, queue_input.created_at, "unknown")
            states[job.job_id] = job
            records[job.job_id] = record
            revision += 1
            return record

        def message(reference, *, display):
            job = states[reference.job_id]
            projection = project_message_for_display(job.message, job.message_metadata)
            return MessageDTO(message_id=job.message_id, session_id=job.session_id, thread_id=job.thread_id,
                role="user", content=projection.content if display else job.message,
                metadata=projection.metadata if display else job.message_metadata, attachments=job.attachments,
                created_at=job.message_created_at, updated_at=job.message_created_at)

        async def load(session_id):
            result = []
            for identity, job in states.items():
                record = records[identity]
                if job.session_id != session_id or job.status.value != "queued" or record.phase != "queued":
                    continue
                binding = job.execution_binding
                result.append(PendingRequestDTO(job_id=identity, execution_binding=binding, message_id=job.message_id,
                    session_id=job.session_id, thread_id=job.thread_id,
                    turn_id=binding.turn_id if isinstance(binding, UserExecutionIdentity) else None,
                    agent_id=job.agent_id, gateway_id=job.gateway_id, request_id=job.request_id,
                    content=job.message, attachments=job.attachments, message_metadata=job.message_metadata,
                    message_created_at=job.message_created_at, enqueue_sequence=record.cursor.commit_id,
                    delivery_policy=job.delivery_policy, created_at=job.created_at, updated_at=job.updated_at,
                    snapshot_version=revision, position=len(result)))
            return result

        async def claim(record):
            nonlocal revision
            identity = record.cursor.admission.job_id
            record = replace(record, phase="claimed", claim_owner="unit-holder", claim_generation=record.claim_generation + 1)
            records[identity] = record
            revision += 1
            handle = Mock(record=record)
            async def start():
                records[identity] = replace(record, phase="started")
            handle.start = AsyncMock(side_effect=start)
            handle.close = AsyncMock()
            return handle

        async def update(reference, policy, *, expected_snapshot_version):
            nonlocal revision
            if expected_snapshot_version is not None and expected_snapshot_version != revision:
                raise RuntimeError("队列快照已过期")
            records[reference.job_id] = replace(records[reference.job_id], delivery_policy=policy)
            revision += 1

        owner.accept_job_input.side_effect = accept
        owner.accepted_job_message.side_effect = message
        owner.accepted_job_outcome.return_value = "unknown"
        store.load = AsyncMock(side_effect=load)
        store.record = AsyncMock(side_effect=lambda _session, _thread, identity: records[identity])
        store.records = AsyncMock(return_value=())
        store.snapshot_version = AsyncMock(side_effect=lambda _session: revision)
        store.claim = AsyncMock(side_effect=claim)
        store.update_policy = AsyncMock(side_effect=update)
        return JobService(input_owner=owner, pending_request_store=store, **kwargs)
    return make
