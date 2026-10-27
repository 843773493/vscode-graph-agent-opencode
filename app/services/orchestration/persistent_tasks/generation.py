"""会话动作的一次推进；等待执行时归还统一 worker 槽位。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.exceptions import NotFoundError
from app.core.job_event_bus import EventType
from app.core.phase_observer import observe_phase
from app.core.trace_middleware import (
    reset_current_gateway_id,
    reset_current_request_id,
    set_current_gateway_id,
    set_current_request_id,
)
from app.schemas.event import JobFailedEvent
from app.schemas.internal_v2.persistent_task import (
    PersistentTaskConfigurationDTO,
    PersistentTaskRunDTO,
    SessionGenerationActionDTO,
    TaskDeliveryDTO,
)
from app.schemas.internal_v2.session_navigation.generation_state import (
    GenerationActionState,
)
from app.schemas.internal_v2.session_navigation.models import (
    SessionGenerationExecuteRequest,
    SessionGenerationPreviewRequest,
)
from app.services.business.session_generation.reporting import generation_report_message
from app.services.orchestration.persistent_tasks.contexts import action_context

if TYPE_CHECKING:
    from app.services.orchestration.persistent_tasks.runtime import (
        PersistentTaskRuntime,
    )


def generation_request(configuration: PersistentTaskConfigurationDTO, run: PersistentTaskRunDTO,
                       state: GenerationActionState, workspace_id: str) -> SessionGenerationExecuteRequest:
    action = configuration.action
    assert isinstance(action, SessionGenerationActionDTO)
    definition = action.generation
    return SessionGenerationExecuteRequest(
        generator_type=definition.generator_type, name=configuration.name, config=definition.config,
        placement=definition.placement, context_source=definition.context_source, session_strategy=definition.session_strategy,
        execution_workspace_id=workspace_id, generator_id=run.task_id, run_id=run.run_id,
        idempotency_key=run.trigger_key, title=state.title, navigation_path=state.navigation_path,
    )


async def _child_outcome(runtime: PersistentTaskRuntime, state: GenerationActionState) -> tuple[str, str]:
    assert state.session_id is not None and state.thread_id is not None and state.execution_identity is not None
    async with runtime._sessions.session_read_admission(state.session_id):
        runtime._sessions.path_resolver.resolve_thread_node(state.session_id, state.thread_id)
        admission = runtime._saver.execution_admission_for_job(
            state.session_id, thread_id=state.thread_id, job_id=state.execution_identity.job_id,
        )
        if (admission.turn_id != state.execution_identity.turn_id
                or admission.admission_idempotency_key != state.execution_identity.admission_idempotency_key):
            raise RuntimeError("会话动作的执行引用与原准入身份不一致")
        outcome = runtime._saver.runtime_execution_outcome(state.session_id, state.thread_id, admission.execution_id)
    return admission.execution_id, outcome


async def _terminal_error(runtime: PersistentTaskRuntime, session_id: str, job_id: str, outcome: str) -> str:
    async with runtime._sessions.session_read_admission(session_id):
        for event in reversed(runtime.trace.read_events(session_id)):
            if event.job_id == job_id and isinstance(event, JobFailedEvent):
                detail = f": {event.payload.error}" if event.payload.error else ""
                code = f" (code: {event.payload.code})" if event.payload.code else ""
                return f"job_failed{detail}{code}"
    return f"job_{outcome}"


async def _advance(runtime: PersistentTaskRuntime, configuration: PersistentTaskConfigurationDTO,
                   run: PersistentTaskRunDTO) -> None:
    action = configuration.action
    assert isinstance(action, SessionGenerationActionDTO)
    definition = action.generation
    store = runtime.store
    state = store.generation_state(run)
    async with runtime.locks.lock_for(run.task_id):
        accepted = None
        if state is not None and state.execution_identity is not None and state.session_id is not None and state.thread_id is not None:
            async with runtime._sessions.session_read_admission(state.session_id):
                runtime._sessions.path_resolver.resolve_thread_node(state.session_id, state.thread_id)
                try:
                    accepted = runtime._saver.execution_admission_for_job(
                        state.session_id, thread_id=state.thread_id, job_id=state.execution_identity.job_id,
                    )
                except KeyError:
                    if state.prepared_message is None:
                        raise RuntimeError("已记录接受的会话动作缺少 canonical 准入") from None
        if accepted is None:
            task = store.get_automation(run.task_id)
            if task.revision != run.task_revision or task.lifecycle != "active":
                store.advance(run, phase="suppressed", now=runtime.clock(), error="任务已变化，未接受会话动作")
                runtime.notify(run.task_id, run.run_id)
                return
            if state is None:
                preview = await runtime.generation.preview(SessionGenerationPreviewRequest(
                    name=configuration.name, title_template=definition.title_template, path_template=definition.path_template,
                    session_title=str(definition.config.get("session_title") or configuration.name),
                    generated_at=run.scheduled_for or run.created_at, placement_kind=definition.placement.kind,
                    placement_node_id=definition.placement.session_id or definition.placement.folder_id,
                    session_strategy=definition.session_strategy.mode,
                    target_session_id=definition.session_strategy.target.session_id if definition.session_strategy.target is not None else None,
                ))
                state = GenerationActionState(title=preview.title, navigation_path=preview.path_segments)
                store.record_generation(run, state, runtime.clock())
            payload = generation_request(configuration, run, state, store.workspace_id)
            # Provider 先校验；不能创建会话后才发现类型或配置不可执行。
            prompt = runtime.generation.prompt(payload)
            if configuration.context.content or configuration.condition.type_id != "builtin.always" or run.trigger_payload is not None:
                prompt += "\n\n" + action_context(configuration, run).content
            session = await runtime.generation.create_or_resume(payload, session_id=state.session_id)
            thread_id = (definition.session_strategy.target.thread_id if definition.session_strategy.mode == "continue_existing"
                         and definition.session_strategy.target is not None else await runtime._sessions.resolve_main_thread(session.session_id))
            await observe_phase(run.run_id, "task.generation.session_created_before_record", details={"task_id": run.task_id, "session_id": session.session_id})
            if state.session_id is None:
                state = state.model_copy(update={"session_id": session.session_id, "thread_id": thread_id})
                store.record_generation(run, state, runtime.clock())
            if state.prepared_message is None:
                phase = "continue_existing" if definition.session_strategy.mode == "continue_existing" else (
                    "fork_child" if definition.session_strategy.mode == "fork_new_and_report_back" else "new_session")
                message = await runtime._orchestrator.prepare_user_message(
                    session.session_id, prompt, thread_id=thread_id,
                    metadata={"boxteam_generation_run_id": run.run_id, "boxteam_generator_id": run.task_id,
                              "boxteam_generation_phase": phase},
                )
                state = state.model_copy(update={"prepared_message": message, "message_id": message.message_id,
                                                 "execution_identity": runtime._orchestrator.reserve_user_execution_identity()})
                store.record_generation(run, state, runtime.clock())
            if state.execution_identity is None:
                state = state.model_copy(update={"execution_identity": runtime._orchestrator.reserve_user_execution_identity()})
                store.record_generation(run, state, runtime.clock())
            assert state.execution_identity is not None and state.prepared_message is not None
            await observe_phase(run.run_id, "task.generation.prepared_before_accept", details={"task_id": run.task_id})
            receipt = await runtime._orchestrator.dispatch_existing_message(
                session.session_id, state.prepared_message, execution_binding=state.execution_identity,
            )
            if receipt.job_id != state.execution_identity.job_id or receipt.message_id != state.message_id:
                raise RuntimeError("会话动作准入改变了冻结消息或 Job 身份")
            await observe_phase(run.run_id, "task.generation.accepted_before_record", details={
                "task_id": run.task_id, "session_id": session.session_id, "job_id": receipt.job_id,
            })
        assert state is not None and state.session_id is not None and state.thread_id is not None and state.execution_identity is not None
        execution_id, outcome = await _child_outcome(runtime, state)
        if state.prepared_message is not None or run.delivery is None:
            # canonical 接受后移除准备正文；任务运行只保存原身份和引用。
            state = state.model_copy(update={"prepared_message": None})
            assert state.execution_identity is not None
            store.record_generation(run, state, runtime.clock(), delivery=TaskDeliveryDTO(
                state="accepted", source_id=f"generation:{run.task_id}",
                job_id=state.execution_identity.job_id, execution_id=execution_id,
            ), lifecycle="complete" if configuration.policies.completion == "after_delivery" else None)
        runtime.notify(run.task_id, run.run_id)

    assert state.session_id is not None and state.thread_id is not None and state.execution_identity is not None
    session_id, thread_id, execution_identity = state.session_id, state.thread_id, state.execution_identity
    # 先登记等待再读 canonical 终态，避免并发完成通知落在登记之前。
    runtime._waiting_actions.add(run.run_id)
    _execution_id, outcome = await _child_outcome(runtime, state)
    if outcome == "unknown":
        await runtime.jobs.list(session_id)
        return
    successful = outcome in {"completed", "completed_empty"}
    failure = None if successful else "子会话 Job 失败: " + await _terminal_error(runtime, session_id, execution_identity.job_id, outcome)
    report = definition.session_strategy.mode == "fork_new_and_report_back" and definition.session_strategy.report_back != "none"
    if report:
        target = definition.session_strategy.target
        assert target is not None
        key = f"generation-report:{run.task_id}:{run.run_id}"
        async with runtime.locks.lock_for(run.task_id):
            wakeup = runtime._dispatcher.submitted(target.session_id, target.thread_id, idempotency_key=key)
            if wakeup is None:
                async with runtime._sessions.session_read_admission(session_id):
                    content, provenance = runtime.messages.execution_output_for_job(
                        session_id, thread_id=thread_id, job_id=execution_identity.job_id,
                    )
                payload = generation_request(configuration, run, state, store.workspace_id)
                message = generation_report_message(payload, provenance=provenance,
                    terminal_status=EventType.JOB_COMPLETED if successful else EventType.JOB_CANCELLED if outcome == "cancelled" else EventType.JOB_FAILED,
                    branch_result=content[-16_384:] if content else "（分支会话没有产生可见的 Assistant 回复）")
                await runtime._orchestrator.submit_runtime_event(
                    target.session_id, message, thread_id=target.thread_id,
                    source_id=f"generation:{run.task_id}:{run.run_id}:result", idempotency_key=key,
                )
                await observe_phase(run.run_id, "task.generation.report_accepted_before_record", details={"task_id": run.task_id})
                wakeup = runtime._dispatcher.submitted(target.session_id, target.thread_id, idempotency_key=key)
                if wakeup is None:
                    raise RuntimeError("会话回报接受后缺少原 durable wakeup")
            if state.report_back_job_id is not None and state.report_back_job_id != wakeup.job_id:
                raise RuntimeError("会话回报恢复改变了原 Job 身份")
            state = state.model_copy(update={"status": "reporting", "report_back_job_id": wakeup.job_id})
            store.record_generation(run, state, runtime.clock())
            runtime.notify(run.task_id, run.run_id)
        async with runtime._sessions.session_read_admission(target.session_id):
            runtime._sessions.path_resolver.resolve_thread_node(target.session_id, target.thread_id)
            report_outcome = runtime._saver.runtime_execution_outcome(target.session_id, target.thread_id, wakeup.execution_id)
        if report_outcome == "unknown":
            await runtime.jobs.list(target.session_id)
            return
        if report_outcome not in {"completed", "completed_empty"}:
            report_failure = "回写 Job 失败: " + await _terminal_error(runtime, target.session_id, wakeup.job_id, report_outcome)
            failure = "; ".join(value for value in (failure, report_failure) if value)
            successful = False
    runtime._waiting_actions.discard(run.run_id)
    state = state.model_copy(update={"status": "completed" if successful else "failed"})
    store.record_generation(run, state, runtime.clock(), phase="delivered" if successful else "failed",
                            error=failure,
                            lifecycle="complete" if configuration.policies.completion == "after_delivery" else None)
    runtime.notify(run.task_id, run.run_id)


async def advance_generation(runtime: PersistentTaskRuntime, configuration: PersistentTaskConfigurationDTO,
                             run: PersistentTaskRunDTO) -> None:
    action = configuration.action
    assert isinstance(action, SessionGenerationActionDTO)
    gateway_token = set_current_gateway_id(action.generation.gateway_id)
    request_token = set_current_request_id(run.request_id)
    try:
        await _advance(runtime, configuration, run)
    except (NotFoundError, KeyError, ValueError) as error:
        runtime._waiting_actions.discard(run.run_id)
        state = runtime.store.generation_state(run)
        reason = f"会话动作失败: {type(error).__name__}: {error}"
        lifecycle = "needs_attention" if action.generation.mount_missing == "pause" else None
        if state is None:
            runtime.store.advance(run, phase="failed", now=runtime.clock(), error=reason, lifecycle=lifecycle)
        else:
            runtime.store.record_generation(run, state.model_copy(update={"status": "failed"}), runtime.clock(),
                                            phase="failed", error=reason, lifecycle=lifecycle)
        runtime.notify(run.task_id, run.run_id)
    finally:
        reset_current_request_id(request_token)
        reset_current_gateway_id(gateway_token)
