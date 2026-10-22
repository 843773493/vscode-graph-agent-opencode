"""执行队列进程屏障：状态由 Session/消息/Job 产品入口生成。"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

from app.core.identifier import create_prefixed_id
from app.core.job_event_bus import JobEventBus
from app.core.path_utils import workspace_context
from app.core.workspace_identity import load_or_create_workspace_id
from app.domain.itemized.execution_queue import ExecutionQueueInput
from app.schemas.internal_v2.common import MessageRole
from app.schemas.internal_v2.message import MessageDTO
from app.services.business.job.service import JobService
from app.services.business.message_service import MessageService
from app.services.infrastructure.message_stream_store import MessageStreamStore
from app.services.infrastructure.pending_request_store import PendingRequestStore
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.runtime_service import RuntimeService
from app.services.infrastructure.trace_event_store import TraceEventStore
from tests.support.background_work import build_background_registry
from tests.unit.core.catalog_workspace_helper import build_catalog_workspace


async def run(root: Path, mode: str, barrier: str) -> None:
    workspace = build_catalog_workspace(root, workspace_id=load_or_create_workspace_id(root / "workspace"))
    saver = RolloutCheckpointSaver(sessions_dir=workspace.sessions_root)
    messages = MessageService(main_thread_resolver=workspace.resolver.main_thread_id,
                              checkpointer=saver, workspace_root=workspace.workspace_root)
    store = PendingRequestStore(sessions_dir=workspace.sessions_root, owner=saver,
                               input_reader=lambda reference: messages.accepted_job_message(reference, display=False))

    class Executor:
        async def run(self, job):
            job.progress_reporter("agent_loop_ready")
            with (root / "executed.jsonl").open("a") as stream:
                stream.write(json.dumps({"job_id": job.job_id, "thread_id": job.thread_id, "message": job.message}) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            saver.mark_job_terminal_status(session_id=job.session_id, thread_id=job.thread_id,
                                           job_id=job.job_id, status="cancelled")
            if mode == "produce" and barrier == "terminal":
                os._exit(84)
            raise asyncio.CancelledError("test_execution_boundary")

    jobs = JobService(job_event_bus=JobEventBus(), job_executor=Executor(), input_owner=messages,
                      pending_request_store=store, terminal_status_writer=saver,
                      session_lifecycle_guard=workspace.resolver.resolve_session_node_for_runtime)
    if mode == "produce":
        session = await workspace.create_session("执行队列硬退出")
        thread = workspace.resolver.main_thread_id(session)
        identities = []
        inputs = []
        for index in range(2):
            identity = jobs.reserve_user_execution_identity()
            now = datetime.now(UTC)
            message = MessageDTO(message_id=create_prefixed_id("msg"), session_id=session, thread_id=thread,
                                 role=MessageRole.user, content=f"不可变输入 {index}", created_at=now, updated_at=now)
            messages.accept_user_message(message, execution_identity=identity, queue_input=ExecutionQueueInput(
                execution_binding=identity, message_id=message.message_id, agent_id="default", provider_id=None,
                delivery_policy="after_turn", gateway_id=None, request_id=None, created_at=now.isoformat()))
            admission = saver.execution_admission_for_job(session, thread_id=thread, job_id=identity.job_id)
            identities.append({"job_id": identity.job_id, "execution_id": admission.execution_id,
                               "turn_id": identity.turn_id, "key": identity.admission_idempotency_key,
                               "message_id": message.message_id, "content": message.content})
            inputs.append((identity, message))
        (root / "identities.json").write_text(json.dumps({"session": session, "thread": thread, "jobs": identities}))
        if barrier == "acceptance":
            os._exit(81)
        if barrier in {"claim", "live_holder"}:
            original = saver.claim_execution_queue
            def exit_claim(*args, **kwargs):
                original(*args, **kwargs)
                if barrier == "live_holder":
                    (root / "holder-ready").write_text("claimed")
                    Event().wait(35)
                    raise AssertionError("父进程未终止 holder")
                os._exit(82)
            saver.claim_execution_queue = exit_claim
        if barrier == "task":
            jobs._start_job_task = lambda _job: os._exit(83)
        identity, message = inputs[0]
        await jobs.start_job(session, message.content, thread_id=thread, execution_binding=identity,
                             message_id=message.message_id, message_created_at=message.created_at.isoformat())
    else:
        async def checkpoint(_job):
            return None
        runtime = RuntimeService(workspace_id=workspace.workspace_id, workspace_root=workspace.workspace_root,
            job_service=jobs, background_task_registry=build_background_registry(workspace.sessions_root),
            trace_event_store=TraceEventStore(sessions_dir=workspace.sessions_root),
            message_stream_store=MessageStreamStore(path_resolver=workspace.resolver), terminal_status_writer=saver,
            pending_tool_checkpoint_id=checkpoint, pending_request_store=store)
        with workspace_context(workspace.workspace_id, workspace.workspace_root):
            await runtime.reconcile_stale_executions()
            await jobs.start_restored_executions()
    for _ in range(1000):
        if not await store.records():
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("队列未终结")
    for job in tuple(jobs._jobs.values()):
        if job.task is not None:
            await job.task
    await jobs.drain_finalizations()
    workspace.close()


if __name__ == "__main__":
    from tests.support.session_catalog_owner import fixture_catalog_scope
    with fixture_catalog_scope():
        asyncio.run(run(Path(sys.argv[1]), sys.argv[2], sys.argv[3]))
