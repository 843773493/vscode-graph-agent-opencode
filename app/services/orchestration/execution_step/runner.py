"""step 控制循环：通过显式 ports 协調执行，不继承 public service 状态。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime

from langchain_core.messages import HumanMessage

from app.agents.agent_factory import AGENT_GRAPH_RECURSION_LIMIT, resolve_agent_id
from app.agents.checkpoint_recovery import (
    CheckpointRecoveryRejectedError,
    pending_tool_checkpoint,
)
from app.agents.graph_binding import GraphBindingUnavailableError
from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import (
    reset_current_execution_admission,
    set_current_execution_admission,
)
from app.core.identifier import create_prefixed_id
from app.core.job_context import (
    reset_active_tool_name,
    reset_current_agent_id,
    reset_current_job_id,
    reset_interruptible_phase,
    set_active_tool_name,
    set_current_agent_id,
    set_current_job_id,
    set_interruptible_phase,
)
from app.core.job_event_bus import EventType
from app.core.model_delta_context import (
    clear_model_run_id_queue,
    reset_current_model_delta_sink,
    set_current_model_delta_sink,
)
from app.core.phase_observer import observe_phase
from app.core.session_interrupt_state import SessionInterruptState
from app.core.session_thread_context import session_thread_owner_scope
from app.core.shielded_cleanup import run_cleanup_shielded
from app.core.turn_execution_scope import (
    AgentControlInbox,
    AgentLoopControlCoordinator,
    TurnExecutionScopeRegistry,
    reset_current_turn_execution_scope,
    set_current_turn_execution_scope,
)
from app.domain.itemized.execution_admission import (
    ExecutionAdmission,
    ExecutionBinding,
    InternalExecutionIntent,
    UserExecutionIdentity,
)
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.resource_activation import ResourceActivationSnapshotRef
from app.runtime.agent_runtime import (
    get_configured_custom_tool_names,
    get_workspace_custom_tool_skill_sources,
)
from app.schemas.event import ModelTokenUsagePayload
from app.schemas.internal_v2.message import AttachmentRef
from app.services.business.message_display import DISPLAY_CONTENT_METADATA_KEY
from app.services.business.reasoning_checkpoint_service import (
    persist_standard_assistant_checkpoint,
    persist_user_message_checkpoint,
)
from app.services.infrastructure.message_stream_store import (
    MessageStreamTerminalError,
    MessageStreamWriter,
)
from app.services.orchestration.event_stream.model_events import (
    last_model_token_usage,
)
from app.services.orchestration.execution_step.failures import (
    handle_step_cancelled,
    handle_step_failure,
)
from app.services.orchestration.execution_step.model_call import StepModelCallAdapter
from app.services.orchestration.execution_step.ports import (
    StepAgentRuntime,
    StepExecutionPorts,
)
from app.services.orchestration.execution_step.reminders import (
    custom_tools_requested_by_message as _custom_tools_requested_by_message,
)
from app.services.orchestration.execution_step.retry import run_agent_with_retries
from app.services.orchestration.execution_step.stream_bindings import (
    CanonicalItemSink,
    StepEventPublisher,
)
from app.services.orchestration.message_stream_runtime import MessageStreamRuntime
from app.services.orchestration.resource_activation.contracts import (
    reset_current_execution_resource_activation,
    resource_activation_policy_from_config,
    set_current_execution_resource_activation,
)
from app.services.orchestration.thread_residency import ThreadRuntimeLease
from app.services.orchestration.trace_observer import MessageStreamTraceObserver


class StepRunner:
    def __init__(
        self,
        ports: StepExecutionPorts,
        execution_scope_registry: TurnExecutionScopeRegistry,
    ) -> None:
        self.ports = ports
        self.execution_scope_registry = execution_scope_registry

    async def run_step(
        self,
        session_id: str,
        message: str,
        *,
        thread_id: str,
        agent_id: str | None = None,
        job_id: str,
        message_id: str,
        attachments: list[AttachmentRef] | None = None,
        message_created_at: str,
        message_metadata: dict[str, object] | None = None,
        execution_binding: ExecutionBinding,
        progress_reporter: Callable[[str], None] | None = None,
        protected_operations_settled: asyncio.Event | None = None,
        resume_checkpoint_id: str | None = None,
    ) -> str:
        config_snapshot = self.ports.config_service.get_snapshot()
        with self.ports.config_service.use_snapshot(config_snapshot):
            return await self._run_step_with_snapshot(
                session_id,
                message,
                thread_id=thread_id,
                agent_id=agent_id,
                job_id=job_id,
                message_id=message_id,
                attachments=attachments,
                message_created_at=message_created_at,
                message_metadata=message_metadata,
                execution_binding=execution_binding,
                progress_reporter=progress_reporter,
                protected_operations_settled=protected_operations_settled,
                resume_checkpoint_id=resume_checkpoint_id,
            )

    async def _run_step_with_snapshot(
        self,
        session_id: str,
        message: str,
        *,
        thread_id: str,
        agent_id: str | None = None,
        job_id: str,
        message_id: str,
        attachments: list[AttachmentRef] | None = None,
        message_created_at: str,
        message_metadata: dict[str, object] | None = None,
        execution_binding: ExecutionBinding,
        progress_reporter: Callable[[str], None] | None = None,
        protected_operations_settled: asyncio.Event | None = None,
        resume_checkpoint_id: str | None = None,
    ) -> str:
        # _run_step_with_snapshot 也作为显式测试/内部边界公开调用，不能假定
        # run_step 一定是其父调用；这里必须绑定 scope 并在异常退出时复原。
        with session_thread_owner_scope(
            MutationIntentOwner(session_id=session_id, thread_id=thread_id),
            langgraph_thread_id=thread_id,
        ):
            return await self._run_step_bound_owner(
                session_id,
                message,
                thread_id=thread_id,
                agent_id=agent_id,
                job_id=job_id,
                message_id=message_id,
                attachments=attachments,
                message_created_at=message_created_at,
                message_metadata=message_metadata,
                execution_binding=execution_binding,
                progress_reporter=progress_reporter,
                protected_operations_settled=protected_operations_settled,
                resume_checkpoint_id=resume_checkpoint_id,
            )

    async def _run_step_bound_owner(
        self,
        session_id: str,
        message: str,
        *,
        thread_id: str,
        agent_id: str | None = None,
        job_id: str,
        message_id: str,
        attachments: list[AttachmentRef] | None = None,
        message_created_at: str,
        message_metadata: dict[str, object] | None = None,
        execution_binding: ExecutionBinding,
        progress_reporter: Callable[[str], None] | None = None,
        protected_operations_settled: asyncio.Event | None = None,
        resume_checkpoint_id: str | None = None,
    ) -> str:
        if self.ports.config_service is None:
            raise RuntimeError("AgentExecutionService 未绑定 ConfigService")
        config_snapshot = self.ports.config_service.get_snapshot()
        with self.ports.config_service.use_snapshot(config_snapshot):
            resolved_agent_id = resolve_agent_id(agent_id, self.ports.config_service)
            agent_runtime_config = self.ports.config_service.get_agent_runtime_config(
                resolved_agent_id
            )
            require_delegated_report = agent_runtime_config.get(
                "require_delegated_report",
                False,
            )
            if not isinstance(require_delegated_report, bool):
                raise TypeError(
                    "Agent 运行时配置 require_delegated_report 必须是布尔值"
                )
            mode_getter = getattr(self.ports.config_service, "get_agent_run_mode", None)
            run_mode = mode_getter() if callable(mode_getter) else None
            include_team_tools = (
                run_mode == "team" if isinstance(run_mode, str) else False
            )
        if self.ports.job_event_bus is None:
            raise RuntimeError("AgentExecutionService 未绑定 JobEventBus")
        bus = self.ports.job_event_bus

        if not isinstance(thread_id, str) or not thread_id:
            raise ValueError(
                f"run_step 缺少 thread_id: session_id={session_id} agent_id={agent_id}"
            )
        if not job_id:
            raise ValueError(
                f"run_step 缺少 job_id: session_id={session_id} agent_id={agent_id}"
            )
        if isinstance(execution_binding, UserExecutionIdentity):
            if execution_binding.job_id != job_id:
                raise ValueError(
                    "user execution identity 的 job_id 与当前 Job 不一致: "
                    f"identity={execution_binding.job_id!r} job={job_id!r}"
                )
            turn_id = execution_binding.turn_id
            admission_idempotency_key = execution_binding.admission_idempotency_key
        elif isinstance(execution_binding, InternalExecutionIntent):
            if execution_binding.message_id != message_id:
                raise ValueError(
                    "internal execution identity 与当前消息 identity 不一致: "
                    f"intent={execution_binding.message_id!r}, message={message_id!r}"
                )
            turn_id = None
            admission_idempotency_key = execution_binding.admission_idempotency_key
        else:
            raise TypeError(
                f"未知 Agent execution binding: {type(execution_binding).__name__}"
            )
        if not admission_idempotency_key:
            raise ValueError(
                "run_step 缺少独立 admission_idempotency_key: "
                f"session_id={session_id} job_id={job_id}"
            )
        if not message_id:
            raise ValueError(
                f"run_step 缺少 message_id: session_id={session_id} job_id={job_id}"
            )
        if not message_created_at:
            raise ValueError(
                f"run_step 缺少用户 message_created_at: session_id={session_id} job_id={job_id}"
            )
        import logging

        logger = logging.getLogger(__name__)
        logger.info(
            "[agent_execution_service] run_step begin: session_id=%s job_id=%s agent_id=%s message_length=%s",
            session_id,
            job_id,
            resolved_agent_id,
            len(message or ""),
        )

        # 只把 LangGraph 要求的真实 thread_id 放入 configurable；session_id 和
        # job_id 由显式 owner/job context 传递。
        # 中间件通过 runtime.configurable 取不到这些键时，会回退到 contextvars
        # （见 LLMLoggingMiddleware._get_job_id 的优先级链）。
        config = {
            **build_checkpoint_config(thread_id, checkpoint_id=resume_checkpoint_id),
            "recursion_limit": AGENT_GRAPH_RECURSION_LIMIT,
        }
        rollout_checkpoint_ns = str(config["configurable"].get("checkpoint_ns", ""))

        checkpointer = self.ports.checkpointer_provider()
        if checkpointer is None:
            raise RuntimeError(
                "AgentRuntimeDependencyProvider 必须显式提供 checkpointer"
            )
        canonical_sink = CanonicalItemSink(
            checkpointer,
            session_id=session_id,
            thread_id=thread_id,
            checkpoint_ns=rollout_checkpoint_ns,
            committed_item_observer=self.ports.background_message_bus.acknowledge_tool_results,
        )
        model_call_adapter = StepModelCallAdapter(
            checkpointer=checkpointer,
            session_id=session_id,
            thread_id=thread_id,
            job_id=job_id,
            checkpoint_ns=rollout_checkpoint_ns,
        )
        if not callable(getattr(checkpointer, "mark_execution_lost", None)):
            raise TypeError("v2 checkpoint saver 缺少可调用端口: mark_execution_lost")

        resolved_message_metadata = dict(message_metadata or {})
        preferred_provider_id_value = resolved_message_metadata.get(
            "boxteam_session_provider_id"
        )
        if isinstance(execution_binding, UserExecutionIdentity):
            resolved_message_metadata.pop(DISPLAY_CONTENT_METADATA_KEY, None)
            resolved_message_metadata.pop("boxteam_session_provider_id", None)
        if preferred_provider_id_value is not None and not isinstance(
            preferred_provider_id_value,
            str,
        ):
            raise TypeError("会话模型 provider id 必须是字符串")
        preferred_provider_id = preferred_provider_id_value
        read_input_message = getattr(
            checkpointer,
            "accepted_user_message" if isinstance(execution_binding, UserExecutionIdentity) else "accepted_internal_message",
            None,
        )
        if not callable(read_input_message):
            raise TypeError("v2 checkpoint saver 缺少已接受输入 reader")
        next_input_messages: list[HumanMessage] = []
        message_source = resolved_message_metadata.get("source")
        message_kind = resolved_message_metadata.get("kind")
        requires_delegated_report = (
            require_delegated_report and message_source == "session_subagent_delegation"
        )
        parent_session_id = resolved_message_metadata.get("parent_session_id")
        if requires_delegated_report and not isinstance(parent_session_id, str):
            raise RuntimeError(
                "委派子会话首轮缺少 parent_session_id 元数据: "
                f"session_id={session_id} job_id={job_id}"
            )
        delegated_report_allowed_kinds = (
            frozenset({"question", "progress", "result"})
            if message_source == "send_message_to_session"
            and message_kind == "progress"
            else frozenset({"question", "result"})
        )
        requires_session_question_reply = (
            resolved_message_metadata.get("source") == "send_message_to_session"
            and resolved_message_metadata.get("kind") == "question"
            and resolved_message_metadata.get("reply_required") is True
        )
        from app.services.business.communication.addresses import GlobalThreadAddress

        source_address = resolved_message_metadata.get("source_address")
        question_source = GlobalThreadAddress(**source_address) if requires_session_question_reply and isinstance(source_address, dict) else None
        question_communication_id = resolved_message_metadata.get("communication_id")
        if requires_session_question_reply and (
            question_source is None
            or not isinstance(question_communication_id, str)
        ):
            raise RuntimeError(
                "跨会话问题缺少可信回复路由元数据: "
                f"session_id={session_id} job_id={job_id}"
            )

        read_admission = getattr(checkpointer, "execution_admission_for_job", None)
        if not callable(read_admission):
            raise TypeError("v2 checkpoint saver 缺少 execution_admission_for_job 端口")
        expected_origin = (
            "internal"
            if isinstance(execution_binding, InternalExecutionIntent)
            else "user_turn"
        )
        settled = protected_operations_settled or asyncio.Event()
        admission: ExecutionAdmission | None = None
        message_stream_writer: MessageStreamWriter | None = None
        message_stream_runtime: MessageStreamRuntime | None = None
        admission_write_task: asyncio.Task[bool] | None = None
        admission_read_task: asyncio.Task[ExecutionAdmission] | None = None
        input_read_task: asyncio.Task[HumanMessage] | None = None
        stream_open_task: asyncio.Task[MessageStreamWriter] | None = None
        assistant_write_task: asyncio.Task[bool] | None = None
        runtime_acquire_task: (
            asyncio.Task[ThreadRuntimeLease[StepAgentRuntime]] | None
        ) = None
        runtime_lease: ThreadRuntimeLease[StepAgentRuntime] | None = None
        activation_freeze_task: asyncio.Task[ResourceActivationSnapshotRef] | None = None
        activation_token = None
        job_token = agent_token = interruptible_phase_token = active_tool_name_token = None
        turn_scope = control_loop_stop_event = control_loop_task = None
        turn_scope_token = message_delta_token = execution_admission_token = None
        event_publisher = StepEventPublisher(
            bus, job_id=job_id, agent_id=resolved_agent_id,
        )

        def validate_admission(value: ExecutionAdmission) -> None:
            if not isinstance(value, ExecutionAdmission):
                raise TypeError("checkpoint saver 返回了非法 execution admission")
            if (
                value.session_id != session_id
                or value.thread_id != thread_id
                or value.job_id != job_id
                or value.admission_idempotency_key != admission_idempotency_key
                or value.execution_origin != expected_origin
                or value.turn_id != turn_id
                or (
                    isinstance(execution_binding, InternalExecutionIntent)
                    and value.execution_id != execution_binding.execution_id
                )
            ):
                raise RuntimeError(
                    "当前 Step 的 execution admission 与真实 SessionThread/job/origin 不一致: "
                    f"session_id={session_id}, thread_id={thread_id}, job_id={job_id}"
                )

        async def settle_started_operations() -> BaseException | None:
            nonlocal admission, message_stream_writer, runtime_lease
            tasks = tuple(
                task
                for task in (
                    admission_write_task,
                    admission_read_task,
                    input_read_task,
                    stream_open_task,
                    assistant_write_task,
                    runtime_acquire_task,
                    activation_freeze_task,
                )
                if task is not None
            )
            # shield 保留原 handle；重复 cancel 不得让线程写入或已落盘 open 脱离 owner。
            results = await asyncio.gather(*tasks, return_exceptions=True)
            if (
                stream_open_task is not None
                and not stream_open_task.cancelled()
                and stream_open_task.exception() is None
            ):
                message_stream_writer = stream_open_task.result()
            if (
                runtime_acquire_task is not None
                and not runtime_acquire_task.cancelled()
                and runtime_acquire_task.exception() is None
            ):
                runtime_lease = runtime_acquire_task.result()
            if admission is None and (admission_write_task is not None or input_read_task is not None):
                try:
                    admission = await asyncio.to_thread(
                        read_admission, session_id, thread_id=thread_id,
                        job_id=job_id, checkpoint_ns=rollout_checkpoint_ns,
                    )
                except KeyError:
                    # 准入没有提交时只清理已创建资源，绝不补 execution 或 Turn。
                    pass
                else:
                    validate_admission(admission)
            return next(
                (result for result in results if isinstance(result, BaseException)),
                None,
            )

        settled.clear()
        try:
            # 用户、notice 和 seed/reference 的冻结读取都归 active Job 的取消排空。
            input_options = ({"intent": execution_binding}
                             if isinstance(execution_binding, InternalExecutionIntent) else {})
            input_read_task = asyncio.create_task(asyncio.to_thread(
                read_input_message,
                session_id, thread_id=thread_id, job_id=job_id, message_id=message_id,
                checkpoint_ns=rollout_checkpoint_ns, **input_options,
            ))
            next_input_messages = [await asyncio.shield(input_read_task)]
            if resume_checkpoint_id is None:
                admission_write_task = asyncio.create_task(asyncio.to_thread(
                    persist_user_message_checkpoint,
                    checkpointer=checkpointer,
                    session_id=session_id,
                    thread_id=thread_id,
                    job_id=job_id,
                    admission_idempotency_key=admission_idempotency_key,
                    message=(None if isinstance(execution_binding, InternalExecutionIntent)
                             and execution_binding.input_reference_json is not None else next_input_messages[0]),
                    execution_intent=(
                        execution_binding
                        if isinstance(execution_binding, InternalExecutionIntent)
                        else None
                    ),
                    checkpoint_ns=rollout_checkpoint_ns,
                ))
                await asyncio.shield(admission_write_task)
            admission_read_task = asyncio.create_task(asyncio.to_thread(
                read_admission, session_id, thread_id=thread_id,
                job_id=job_id, checkpoint_ns=rollout_checkpoint_ns,
            ))
            admission = await asyncio.shield(admission_read_task)
            validate_admission(admission)

            job_token = set_current_job_id(job_id)
            agent_token = set_current_agent_id(resolved_agent_id)
            interruptible_phase_token = set_interruptible_phase("text")
            active_tool_name_token = set_active_tool_name(None)
            SessionInterruptState.set(
                session_id, thread_id=thread_id,
                phase=None,
                tool_name=None,
                clear_active_tools=True,
            )

            if turn_id is not None:
                stream_open_task = asyncio.create_task(self.ports.message_stream_store.open(
                    session_id=session_id,
                    thread_id=admission.thread_id,
                    turn_id=turn_id,
                    job_id=job_id,
                ))
                message_stream_writer = await asyncio.shield(stream_open_task)
            message_stream_trace_observer = MessageStreamTraceObserver(
                event_publisher.publish
            )
            message_stream_runtime = MessageStreamRuntime(
                message_stream_writer,
                normalized_block_observer=message_stream_trace_observer.observe,
                canonical_item_sink=canonical_sink.append,
                tool_result_sink=canonical_sink.complete_tool_result,
                canonical_turn_id=turn_id,
                model_call_registrar=model_call_adapter.register,
                model_call_outcome_sink=model_call_adapter.update_outcome,
            )
            turn_scope = self.execution_scope_registry.create(
                message_stream_writer.turn_stream_id
                if message_stream_writer is not None
                else admission.execution_id
            )
            turn_scope_token = set_current_turn_execution_scope(turn_scope)
            if message_stream_writer is not None:
                control_inbox = AgentControlInbox(
                    message_stream_writer.turn_stream_id,
                    session_id=session_id,
                    state_path=self.ports.control_inbox_state_path(
                        session_id, message_stream_writer.turn_stream_id
                    ),
                )
                self.execution_scope_registry.register_inbox(
                    message_stream_writer.turn_stream_id,
                    control_inbox,
                )
                control_loop_stop_event = asyncio.Event()
                control_loop_task = asyncio.create_task(
                    AgentLoopControlCoordinator(
                        scope=turn_scope,
                        inbox=control_inbox,
                        writer=message_stream_writer,
                    ).run(control_loop_stop_event)
                )
                if self.ports.external_resource_leases is not None:
                    turn_scope.register_cleanup(
                        lambda: self.ports.external_resource_leases.release_turn_leases(
                            message_stream_writer.turn_stream_id
                        )
                    )
            message_delta_token = set_current_model_delta_sink(message_stream_runtime)
            final_text = ""
            latest_model_content_blocks: tuple[dict[str, object], ...] = ()
            turn_token_usage_parts: list[ModelTokenUsagePayload] = []
            with self.ports.config_service.use_snapshot(config_snapshot):
                configured_custom_tool_names = get_configured_custom_tool_names(
                    agent_id=resolved_agent_id,
                    config_service=self.ports.config_service,
                )
                custom_tool_skill_sources = get_workspace_custom_tool_skill_sources(
                    agent_id=resolved_agent_id,
                    config_service=self.ports.config_service,
                )
            tool_selection = self.ports.tool_selection_store.snapshot(resolved_agent_id)
            execution_overrides = tool_selection.execution_overrides
            model_visibility_overrides = tool_selection.model_visibility_overrides
            configured_custom_tool_names = {
                tool_name
                for tool_name in configured_custom_tool_names
                if execution_overrides.get(tool_name) is not False
            }
            requested_custom_tool_names = _custom_tools_requested_by_message(
                message,
                configured_custom_tool_names,
            )
            if progress_reporter is not None:
                progress_reporter("agent_start")
            await event_publisher.publish(
                EventType.AGENT_START,
                {
                    "message": "agent 启动，准备处理用户请求",
                    "agent_id": resolved_agent_id,
                },
            )
            execution_admission_token = set_current_execution_admission(
                admission,
                langgraph_thread_id=thread_id,
            )

            # 获取 lease 留在 admission/stream 的取消闭包中；建图在飞时不得释放 Job 槽。
            runtime_acquire_task = asyncio.create_task(
                self.ports.agent_factory(
                    admission=admission,
                    agent_id=resolved_agent_id,
                    execution_overrides=execution_overrides,
                    model_visibility_overrides=model_visibility_overrides,
                    preferred_provider_id=preferred_provider_id,
                    include_team_tools=include_team_tools,
                )
            )
            try:
                runtime_lease = await asyncio.shield(runtime_acquire_task)
            except GraphBindingUnavailableError as error:
                if resume_checkpoint_id is None:
                    raise
                raise CheckpointRecoveryRejectedError(f"原 GraphBinding 无法精确重建: {error}") from error
            await observe_phase(admission.execution_id, "thread.runtime.acquired", details={
                "session_id": runtime_lease.session_id, "thread_id": runtime_lease.thread_id,
                "generation": runtime_lease.generation,
                "runtime_object_id": id(runtime_lease.runtime),
                "agent_object_id": id(runtime_lease.runtime.agent),
                "execution_id": admission.execution_id, "job_id": admission.job_id,
                "turn_id": admission.turn_id, "execution_origin": admission.execution_origin,
            })
            # root 冻结属于实际 active slot；取消时与已启动准入/建图一起排空。
            activation_freeze_task = asyncio.create_task(
                runtime_lease.runtime.resource_activation.freeze_execution(
                    admission=admission,
                    policy=resource_activation_policy_from_config(config_snapshot.to_dict()),
                    checkpoint_ns=rollout_checkpoint_ns,
                )
            )
            frozen_activation = await asyncio.shield(activation_freeze_task)
            activation_token = set_current_execution_resource_activation(
                frozen_activation, admission=admission,
            )
            settled.set()

            if resume_checkpoint_id is not None:
                pending = await pending_tool_checkpoint(runtime_lease.runtime.agent, checkpointer, admission, checkpoint_id=resume_checkpoint_id)
                if pending is None or pending.checkpoint_id != resume_checkpoint_id:
                    raise CheckpointRecoveryRejectedError("恢复 Job 缺少原 pending tools scheduler 证明，拒绝重新执行输入")
                try:
                    await message_stream_runtime.restore_pending_model(pending.assistant, pending.model_call)
                except RuntimeError as error:
                    raise CheckpointRecoveryRejectedError(f"原 TurnStream 无法证明相同工具调用: {error}") from error
                model_call_adapter.restore_pending_model_call(pending.model_call, pending.assembly_id)

            if isinstance(execution_binding, InternalExecutionIntent) and execution_binding.input_reference_json is not None:
                next_input_messages = []
            retry_result = await run_agent_with_retries(
                agent=runtime_lease.runtime.agent,
                checkpointer=checkpointer,
                admission=admission,
                session_id=session_id,
                thread_id=thread_id,
                resolved_agent_id=resolved_agent_id,
                job_id=job_id,
                turn_id=turn_id,
                config=config,
                next_input_messages=next_input_messages,
                custom_tool_skill_sources=custom_tool_skill_sources,
                publish=event_publisher.publish,
                session_changes_service=self.ports.session_changes_service,
                workspace_root=self.ports.workspace_root,
                model_timeout_seconds=self.ports.model_timeout_seconds,
                message_stream_runtime=message_stream_runtime,
                turn_scope=turn_scope,
                progress_reporter=progress_reporter,
                requested_custom_tool_names=requested_custom_tool_names,
                requires_delegated_report=requires_delegated_report,
                parent_session_id=parent_session_id,
                delegated_report_allowed_kinds=delegated_report_allowed_kinds,
                requires_session_question_reply=requires_session_question_reply,
                question_source=question_source,
                question_communication_id=question_communication_id,
                resume_from_checkpoint=resume_checkpoint_id is not None,
            )
            final_text = retry_result.final_text
            latest_model_content_blocks = retry_result.latest_model_content_blocks
            turn_token_usage_parts = list(retry_result.token_usage_parts)

            if final_text:
                SessionInterruptState.set(
                    session_id, thread_id=thread_id,
                    phase=None,
                    tool_name=None,
                    clear_active_tools=True,
                )
                set_interruptible_phase("text")
                set_active_tool_name(None)
            turn_token_usage = last_model_token_usage(turn_token_usage_parts)
            assistant_message_id = create_prefixed_id("msg")
            assistant_message_created_at = datetime.now(UTC)
            settled.clear()
            assistant_write_task = asyncio.create_task(asyncio.to_thread(
                persist_standard_assistant_checkpoint,
                checkpointer=checkpointer,
                session_id=session_id,
                thread_id=thread_id,
                turn_id=admission.turn_id,
                content_blocks=latest_model_content_blocks,
                final_text=final_text,
                message_id=assistant_message_id,
                message_created_at=assistant_message_created_at,
                token_usage=turn_token_usage,
                preserve_content_part_refs=True,
                before_persist=(
                    model_call_adapter.converge_final_message
                    if final_text
                    else None
                ),
            ))
            persisted = await asyncio.shield(assistant_write_task)
            if final_text and not persisted:
                raise RuntimeError(
                    "最终 assistant 消息未能写入 checkpoint: "
                    f"session_id={session_id} job_id={job_id}"
                )
            await model_call_adapter.converge_final_checkpoint(
                assistant_message_id if final_text else None
            )

            # provider delta hook 可能晚于 model.completed 到达；stream.completed
            # 是整轮消息流的最终原子边界，提交前必须再次收口 runtime 中仍在
            # 写入的 block，不能让迟到的最终文本留下 running entity。
            await message_stream_runtime.finish_model()
            if message_stream_writer is not None:
                try:
                    await message_stream_writer.close_completed()
                except MessageStreamTerminalError:
                    # 中断请求可能在最终业务校验与 stream.completed 之间线性化。
                    # 此时执行已经停止，必须确认中断事实，不能把用户请求覆盖为完成。
                    stream_state = await self.ports.message_stream_store.get_state(
                        message_stream_writer.turn_stream_id
                    )
                    interrupt_state = stream_state.get("interrupt_state")
                    interrupt_request_id = (
                        interrupt_state.get("request_id")
                        if isinstance(interrupt_state, dict)
                        and interrupt_state.get("status") == "requested"
                        else None
                    )
                    if stream_state.get(
                        "stream_status"
                    ) != "interrupting" or not isinstance(interrupt_request_id, str):
                        raise
                    await message_stream_writer.close_interrupted(interrupt_request_id)

            await event_publisher.publish(
                EventType.AGENT_END,
                {
                    "final_text": final_text,
                    "agent_id": resolved_agent_id,
                    "token_usage": turn_token_usage.model_dump(mode="json"),
                },
            )

            logger.info(
                "[agent_execution_service] response ready: job_id=%s response_length=%s",
                job_id,
                len(final_text),
            )
            return final_text

        except asyncio.CancelledError as cancellation_error:
            settled.clear()

            async def converge_cancellation(
                cancellation: asyncio.CancelledError = cancellation_error,
            ) -> None:
                operation_error = await settle_started_operations()
                await handle_step_cancelled(
                    cancellation_error=cancellation,
                    state=SessionInterruptState.get(session_id, thread_id=thread_id),
                    message_stream_runtime=message_stream_runtime,
                    message_stream_writer=message_stream_writer,
                    checkpointer=checkpointer,
                    session_id=session_id,
                    thread_id=thread_id,
                    job_id=job_id,
                    turn_id=admission.turn_id if admission is not None else None,
                    execution_id=admission.execution_id if admission is not None else None,
                    checkpoint_ns=rollout_checkpoint_ns,
                    logger=logger,
                )
                if operation_error is not None:
                    raise operation_error

            try:
                await run_cleanup_shielded(converge_cancellation)
            except Exception as cleanup_error:
                raise cancellation_error from cleanup_error
            raise
        except Exception as error:
            settled.clear()

            async def converge_failure(failure: Exception = error) -> None:
                operation_error = await settle_started_operations()
                await handle_step_failure(
                    error=failure,
                    interrupt_state=SessionInterruptState.get(session_id, thread_id=thread_id),
                    message_stream_runtime=message_stream_runtime,
                    message_stream_writer=message_stream_writer,
                    checkpointer=checkpointer,
                    session_id=session_id,
                    thread_id=thread_id,
                    job_id=job_id,
                    turn_id=admission.turn_id if admission is not None else None,
                    execution_id=admission.execution_id if admission is not None else None,
                    checkpoint_ns=rollout_checkpoint_ns,
                    publish=event_publisher.publish,
                    logger=logger,
                )
                if operation_error is not None and operation_error is not failure:
                    raise operation_error

            try:
                await run_cleanup_shielded(converge_failure)
            except Exception as cleanup_error:
                raise error from cleanup_error
            raise
        finally:
            settled.clear()
            # ContextVar token 必须由创建它的原 task 复原；异步资源清理由独立 task 排空。
            if activation_token is not None:
                reset_current_execution_resource_activation(activation_token)
            if execution_admission_token is not None:
                reset_current_execution_admission(execution_admission_token)
            if message_delta_token is not None:
                reset_current_model_delta_sink(message_delta_token)
            if message_stream_runtime is not None:
                clear_model_run_id_queue(message_stream_runtime)
            if turn_scope_token is not None:
                reset_current_turn_execution_scope(turn_scope_token)
            if job_token is not None:
                reset_current_job_id(job_token)
            if agent_token is not None:
                reset_current_agent_id(agent_token)
            if interruptible_phase_token is not None:
                reset_interruptible_phase(interruptible_phase_token)
            if active_tool_name_token is not None:
                reset_active_tool_name(active_tool_name_token)

            async def release_resources() -> None:
                if control_loop_stop_event is not None:
                    control_loop_stop_event.set()
                if control_loop_task is not None:
                    if not control_loop_task.done():
                        control_loop_task.cancel()
                    await asyncio.gather(control_loop_task, return_exceptions=True)
                try:
                    if turn_scope is not None:
                        await self.execution_scope_registry.close(
                            turn_scope.turn_stream_id
                        )
                finally:
                    # 执行任务已排空；Turn 清理错误仍传播，但不得遗失 thread 使用权。
                    if runtime_lease is not None:
                        runtime_lease.release()

            await run_cleanup_shielded(release_resources)
            SessionInterruptState.clear(session_id, thread_id=thread_id)
