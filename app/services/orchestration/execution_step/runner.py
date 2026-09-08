"""step 控制循环：通过显式 ports 协調执行，不继承 public service 状态。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime

from langchain_core.messages import HumanMessage

from app.agents.agent_factory import AGENT_GRAPH_RECURSION_LIMIT, resolve_agent_id
from app.core.checkpoint_config import build_checkpoint_config
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
    reset_current_model_delta_sink,
    set_current_model_delta_sink,
)
from app.core.session_interrupt_state import SessionInterruptState
from app.core.turn_execution_scope import (
    AgentControlInbox,
    AgentLoopControlCoordinator,
    TurnExecutionScopeRegistry,
    reset_current_turn_execution_scope,
    set_current_turn_execution_scope,
)
from app.runtime.agent_runtime import (
    get_configured_custom_tool_names,
    get_workspace_custom_tool_skill_sources,
)
from app.schemas.event import ModelTokenUsagePayload
from app.schemas.internal_v2.message import AttachmentRef
from app.services.business.message_display import DISPLAY_CONTENT_METADATA_KEY
from app.services.business.reasoning_checkpoint_service import (
    persist_intermediate_assistant_reasoning_checkpoint,
    persist_standard_assistant_checkpoint,
    persist_user_message_checkpoint,
)
from app.services.business.user_content_builder import UserContentBuilder
from app.services.infrastructure.message_stream_store import (
    MessageStreamTerminalError,
)
from app.services.orchestration.agent_stream_helpers import (
    build_human_response_metadata,
)
from app.services.orchestration.event_stream.model_events import (
    last_model_token_usage,
)
from app.services.orchestration.execution_step.failures import (
    handle_step_cancelled,
    handle_step_failure,
)
from app.services.orchestration.execution_step.model_call import StepModelCallAdapter
from app.services.orchestration.execution_step.ports import StepExecutionPorts
from app.services.orchestration.execution_step.reminders import (
    custom_tools_requested_by_message as _custom_tools_requested_by_message,
)
from app.services.orchestration.execution_step.retry import run_agent_with_retries
from app.services.orchestration.execution_step.stream_bindings import (
    CanonicalItemSink,
    StepEventPublisher,
)
from app.services.orchestration.message_stream_runtime import MessageStreamRuntime
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
        agent_id: str | None = None,
        job_id: str,
        message_id: str,
        attachments: list[AttachmentRef] | None = None,
        message_created_at: str,
        message_metadata: dict[str, object] | None = None,
        progress_reporter: Callable[[str], None] | None = None,
    ) -> str:
        config_snapshot = self.ports.config_service.get_snapshot()
        with self.ports.config_service.use_snapshot(config_snapshot):
            return await self._run_step_with_snapshot(
                session_id,
                message,
                agent_id=agent_id,
                job_id=job_id,
                message_id=message_id,
                attachments=attachments,
                message_created_at=message_created_at,
                message_metadata=message_metadata,
                progress_reporter=progress_reporter,
            )

    async def _run_step_with_snapshot(
        self,
        session_id: str,
        message: str,
        *,
        agent_id: str | None = None,
        job_id: str,
        message_id: str,
        attachments: list[AttachmentRef] | None = None,
        message_created_at: str,
        message_metadata: dict[str, object] | None = None,
        progress_reporter: Callable[[str], None] | None = None,
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

        if not job_id:
            raise ValueError(
                f"run_step 缺少 job_id: session_id={session_id} agent_id={agent_id}"
            )
        if not message_id:
            raise ValueError(
                f"run_step 缺少用户 message_id: session_id={session_id} job_id={job_id}"
            )
        if not message_created_at:
            raise ValueError(
                f"run_step 缺少用户 message_created_at: session_id={session_id} job_id={job_id}"
            )
        effective_job_id = job_id
        import logging

        logger = logging.getLogger(__name__)
        logger.info(
            "[agent_execution_service] run_step begin: session_id=%s job_id=%s agent_id=%s message_length=%s",
            session_id,
            effective_job_id,
            resolved_agent_id,
            len(message or ""),
        )

        # 注意：业务键（session_id / job_id）不放入 configurable —— session_id
        # 与 thread_id 重复、job_id 已经通过 set_current_job_id 维护在 contextvars。
        # 中间件通过 runtime.configurable 取不到这些键时，会回退到 contextvars
        # （见 LLMLoggingMiddleware._get_job_id 的优先级链）。
        config = {
            **build_checkpoint_config(session_id),
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
            checkpoint_ns=rollout_checkpoint_ns,
        )
        model_call_adapter = StepModelCallAdapter(
            checkpointer=checkpointer,
            session_id=session_id,
            turn_id=effective_job_id,
            checkpoint_ns=rollout_checkpoint_ns,
        )
        if not callable(getattr(checkpointer, "mark_execution_lost", None)):
            raise TypeError("v2 checkpoint saver 缺少可调用端口: mark_execution_lost")

        job_token = set_current_job_id(effective_job_id)
        agent_token = set_current_agent_id(resolved_agent_id)
        interruptible_phase_token = set_interruptible_phase("text")
        active_tool_name_token = set_active_tool_name(None)
        SessionInterruptState.set(
            session_id,
            phase=None,
            tool_name=None,
            clear_active_tools=True,
        )

        event_publisher = StepEventPublisher(
            bus,
            job_id=effective_job_id,
            agent_id=resolved_agent_id,
        )

        message_stream_writer = await self.ports.message_stream_store.open(
            session_id=session_id,
            turn_id=effective_job_id,
            job_id=effective_job_id,
        )
        message_stream_trace_observer = MessageStreamTraceObserver(
            event_publisher.publish
        )
        message_stream_runtime = MessageStreamRuntime(
            message_stream_writer,
            normalized_block_observer=message_stream_trace_observer.observe,
            canonical_item_sink=canonical_sink.append,
            canonical_turn_id=effective_job_id,
            model_call_registrar=model_call_adapter.register,
            model_call_outcome_sink=model_call_adapter.update_outcome,
        )
        turn_scope = self.execution_scope_registry.create(
            message_stream_writer.turn_stream_id
        )
        control_inbox = AgentControlInbox(
            message_stream_writer.turn_stream_id,
            state_path=(
                self.ports.workspace_root
                / ".boxteam"
                / "control"
                / f"{message_stream_writer.turn_stream_id}.json"
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
        if self.ports.resource_manager is not None:
            turn_scope.register_cleanup(
                lambda: self.ports.resource_manager.cancel_turn(
                    message_stream_writer.turn_stream_id
                )
            )
        turn_scope_token = set_current_turn_execution_scope(turn_scope)
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
        execution_overrides = self.ports.tool_selection_store.execution_overrides(
            resolved_agent_id
        )
        model_visibility_overrides = (
            self.ports.tool_selection_store.model_visibility_overrides(
                resolved_agent_id
            )
        )
        configured_custom_tool_names = {
            tool_name
            for tool_name in configured_custom_tool_names
            if execution_overrides.get(tool_name) is not False
        }
        requested_custom_tool_names = _custom_tools_requested_by_message(
            message,
            configured_custom_tool_names,
        )
        resolved_attachments = list(attachments or [])
        resolved_message_metadata = dict(message_metadata or {})
        resolved_message_metadata.pop(DISPLAY_CONTENT_METADATA_KEY, None)
        preferred_provider_id_value = resolved_message_metadata.pop(
            "boxteam_session_provider_id",
            None,
        )
        if preferred_provider_id_value is not None and not isinstance(
            preferred_provider_id_value,
            str,
        ):
            raise TypeError("会话模型 provider id 必须是字符串")
        preferred_provider_id = preferred_provider_id_value
        human_content_result = UserContentBuilder(
            workspace_root=self.ports.workspace_root,
        ).build(message, resolved_attachments)
        human_content = human_content_result.content
        human_response_metadata = build_human_response_metadata(
            message_id=message_id,
            display_content=None,
            attachments=resolved_attachments,
            message_created_at=message_created_at,
            message_metadata=resolved_message_metadata,
            attachment_diagnostics=human_content_result.diagnostics,
        )
        raw_message_metadata = human_response_metadata.get("message_metadata")
        if raw_message_metadata is not None and not isinstance(
            raw_message_metadata,
            dict,
        ):
            raise TypeError("HumanMessage message_metadata 必须是对象")
        human_response_metadata["message_metadata"] = {
            **(raw_message_metadata or {}),
            "turn_id": effective_job_id,
            "job_id": effective_job_id,
        }
        message_source = resolved_message_metadata.get("source")
        message_kind = resolved_message_metadata.get("kind")
        requires_delegated_report = (
            require_delegated_report and message_source == "session_subagent_delegation"
        )
        parent_session_id = resolved_message_metadata.get("parent_session_id")
        if (
            require_delegated_report
            and message_source == "send_message_to_session"
            and message_kind in {"reply", "progress", "result"}
        ):
            session_service = self.ports.session_service_provider()
            current_session = await session_service.get(session_id)
            if current_session.delegation is not None:
                requires_delegated_report = True
                parent_session_id = current_session.delegation.parent_session_id
        if requires_delegated_report and not isinstance(parent_session_id, str):
            raise RuntimeError(
                "委派子会话首轮缺少 parent_session_id 元数据: "
                f"session_id={session_id} job_id={effective_job_id}"
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
        question_sender_session_id = resolved_message_metadata.get("sender_session_id")
        question_communication_id = resolved_message_metadata.get("communication_id")
        if requires_session_question_reply and (
            not isinstance(question_sender_session_id, str)
            or not isinstance(question_communication_id, str)
        ):
            raise RuntimeError(
                "跨会话问题缺少可信回复路由元数据: "
                f"session_id={session_id} job_id={effective_job_id}"
            )

        next_input_messages = [
            HumanMessage(
                id=message_id,
                content=human_content,
                response_metadata=human_response_metadata,
            )
        ]

        try:
            if progress_reporter is not None:
                progress_reporter("agent_start")
            await event_publisher.publish(
                EventType.AGENT_START,
                {
                    "message": "agent 启动，准备处理用户请求",
                    "agent_id": resolved_agent_id,
                },
            )

            # acceptance 必须发生在 runtime/model event 之前，保证 model-call
            # 注册时能够解析到同一 Turn 的 execution。
            await asyncio.to_thread(
                persist_user_message_checkpoint,
                checkpointer=checkpointer,
                session_id=session_id,
                message=next_input_messages[0],
            )

            retry_result = await run_agent_with_retries(
                build_agent=self.ports.agent_factory,
                session_id=session_id,
                resolved_agent_id=resolved_agent_id,
                effective_job_id=effective_job_id,
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
                question_sender_session_id=question_sender_session_id,
                question_communication_id=question_communication_id,
                execution_overrides=execution_overrides,
                model_visibility_overrides=model_visibility_overrides,
                preferred_provider_id=preferred_provider_id,
                include_team_tools=include_team_tools,
            )
            final_text = retry_result.final_text
            latest_model_content_blocks = retry_result.latest_model_content_blocks
            turn_token_usage_parts = list(retry_result.token_usage_parts)
            stream_result = retry_result.stream_result

            if final_text:
                SessionInterruptState.set(
                    session_id,
                    phase=None,
                    tool_name=None,
                    clear_active_tools=True,
                )
                set_interruptible_phase("text")
                set_active_tool_name(None)
            turn_token_usage = last_model_token_usage(turn_token_usage_parts)
            assistant_message_id = create_prefixed_id("msg")
            assistant_message_created_at = datetime.now(UTC)
            persisted = await asyncio.to_thread(
                persist_standard_assistant_checkpoint,
                checkpointer=checkpointer,
                session_id=session_id,
                turn_id=effective_job_id,
                content_blocks=latest_model_content_blocks,
                final_text=final_text,
                message_id=assistant_message_id,
                message_created_at=assistant_message_created_at,
                token_usage=turn_token_usage,
                preserve_content_part_refs=True,
            )
            if final_text and not persisted:
                raise RuntimeError(
                    "最终 assistant 消息未能写入 checkpoint: "
                    f"session_id={session_id} job_id={effective_job_id}"
                )
            # checkpoint 投影消费原有 normalized carrier，避免从 item payload
            # 重建 reasoning 时丢失 provider 字段、保护状态或 block 顺序。
            checkpoint_reasoning_blocks = stream_result.model_content_blocks
            if checkpoint_reasoning_blocks:
                projected_reasoning = await asyncio.to_thread(
                    persist_intermediate_assistant_reasoning_checkpoint,
                    checkpointer=checkpointer,
                    session_id=session_id,
                    model_content_blocks=checkpoint_reasoning_blocks,
                )
                logger.warning(
                    "[itemized-context] checkpoint reasoning projection: "
                    "session_id=%s turn_id=%s groups=%s changed=%s",
                    session_id,
                    effective_job_id,
                    len(checkpoint_reasoning_blocks),
                    projected_reasoning,
                )

            await model_call_adapter.converge_final_checkpoint(
                assistant_message_id if final_text else None
            )

            # provider delta hook 可能晚于 model.completed 到达；stream.completed
            # 是整轮消息流的最终原子边界，提交前必须再次收口 runtime 中仍在
            # 写入的 block，不能让迟到的最终文本留下 running entity。
            await message_stream_runtime.finish_model()
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
                effective_job_id,
                len(final_text),
            )
            return final_text

        except asyncio.CancelledError as cancellation_error:
            await handle_step_cancelled(
                cancellation_error=cancellation_error,
                state=SessionInterruptState.get(session_id),
                message_stream_runtime=message_stream_runtime,
                message_stream_writer=message_stream_writer,
                checkpointer=checkpointer,
                session_id=session_id,
                turn_id=effective_job_id,
                checkpoint_ns=rollout_checkpoint_ns,
                logger=logger,
            )
            raise
        except Exception as error:
            await handle_step_failure(
                error=error,
                interrupt_state=SessionInterruptState.get(session_id),
                message_stream_runtime=message_stream_runtime,
                message_stream_writer=message_stream_writer,
                checkpointer=checkpointer,
                session_id=session_id,
                turn_id=effective_job_id,
                checkpoint_ns=rollout_checkpoint_ns,
                publish=event_publisher.publish,
                logger=logger,
            )
            raise
        finally:
            control_loop_stop_event.set()
            if not control_loop_task.done():
                control_loop_task.cancel()
            await asyncio.gather(control_loop_task, return_exceptions=True)
            reset_current_model_delta_sink(message_delta_token)
            reset_current_turn_execution_scope(turn_scope_token)
            await self.execution_scope_registry.close(
                message_stream_writer.turn_stream_id
            )
            reset_current_job_id(job_token)
            reset_current_agent_id(agent_token)
            reset_interruptible_phase(interruptible_phase_token)
            reset_active_tool_name(active_tool_name_token)
            SessionInterruptState.clear(session_id)
