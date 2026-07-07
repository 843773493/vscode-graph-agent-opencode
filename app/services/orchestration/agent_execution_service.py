from __future__ import annotations

import asyncio
from typing import Dict, Any

from langchain_core.messages import AIMessage, HumanMessage

from app.abstractions.background_message_bus import BackgroundMessageBusProtocol
from app.abstractions.job_event_bus import JobEventBusProtocol
from app.core.background_task_registry import BackgroundTaskRegistry
from app.core.job_context import (
    reset_current_agent_id,
    reset_current_job_id,
    reset_active_tool_name,
    reset_interruptible_phase,
    set_active_tool_name,
    set_current_agent_id,
    set_current_job_id,
    set_interruptible_phase,
)
from app.core.job_event_bus import EventType
from app.core.checkpoint_config import build_checkpoint_config
from app.core.session_interrupt_state import SessionInterruptState
from app.schemas.public_v2.message import AttachmentRef
from app.agents.agent_factory import resolve_agent_id
from app.services.infrastructure.attachment_content_service import build_human_content
from app.services.infrastructure.config_service import ConfigService
from app.abstractions.job_step_executor import JobStepExecutor
from app.runtime.agent_runtime import (
    AgentRuntimeDependencyProvider,
    build_agent_tool_definitions,
    build_candidate_models_for_session_request,
    build_session_agent_runtime,
    get_workspace_skill_tool_sources,
)
from app.services.business.reasoning_checkpoint_service import (
    persist_standard_assistant_checkpoint,
)
from app.services.mapping.agent_content_mapper import split_agent_content
from app.services.business.system_reminder_checkpoint_service import (
    persist_interrupt_checkpoint,
)
from app.services.orchestration.agent_pseudo_tool_recovery import (
    recover_or_sanitize_final_text,
)
from app.services.orchestration.agent_event_stream_processor import (
    process_agent_event_stream,
)
from app.services.orchestration.agent_stream_helpers import (
    build_human_response_metadata,
    unwrap_json_string_tool_result,
)


class AgentExecutionService(JobStepExecutor):
    def __init__(
        self,
        *,
        config_service: ConfigService,
        background_task_registry: BackgroundTaskRegistry,
        background_message_bus: BackgroundMessageBusProtocol,
        job_event_bus: JobEventBusProtocol,
        dependency_provider: AgentRuntimeDependencyProvider,
    ):
        self._agent_cache = {}
        self._config_service = config_service
        self._background_task_registry = background_task_registry
        self._background_message_bus = background_message_bus
        self._bus = job_event_bus
        self._dependency_provider = dependency_provider

    def _get_or_create_agent(self, session_id: str, agent_id: str | None = None):
        if self._config_service is None:
            raise RuntimeError("AgentExecutionService 未绑定 ConfigService")

        resolved_agent_id = resolve_agent_id(agent_id, self._config_service)
        cache_key = f"{session_id}::{resolved_agent_id}"
        if cache_key in self._agent_cache:
            return self._agent_cache[cache_key]

        agent = build_session_agent_runtime(
            session_id=session_id,
            agent_id=agent_id or resolved_agent_id,
            config_service=self._config_service,
            background_task_registry=self._background_task_registry,
            background_message_bus=self._background_message_bus,
            job_event_bus=self._bus,
            dependency_provider=self._dependency_provider,
        )

        self._agent_cache[cache_key] = agent
        return agent

    def _extract_final_text(self, result: Dict[str, Any]) -> str:
        messages = result.get("messages", []) if isinstance(result, dict) else []
        for message in reversed(messages):
            if not isinstance(message, AIMessage):
                continue
            content = getattr(message, "content", None)
            if content is None:
                continue
            _, text = split_agent_content(content)
            text = text.strip()
            if text:
                return text
        raise RuntimeError(
            "Agent 执行完成但没有提取到任何最终文本。"
            f" session_id={result.get('session_id') if isinstance(result, dict) else 'unknown'}"
            " 这通常表示最终消息不是 assistant 文本，或者消息链路中出现了空响应。"
        )

    async def run_step(
        self,
        session_id: str,
        message: str,
        agent_id: str | None = None,
        job_id: str | None = None,
        message_id: str | None = None,
        attachments: list[AttachmentRef] | None = None,
        message_created_at: str | None = None,
    ) -> str:
        if self._config_service is None:
            raise RuntimeError("AgentExecutionService 未绑定 ConfigService")
        if self._background_task_registry is None:
            raise RuntimeError("AgentExecutionService 未绑定 BackgroundTaskRegistry")
        if self._background_message_bus is None:
            raise RuntimeError("AgentExecutionService 未绑定 BackgroundMessageBus")
        resolved_agent_id = resolve_agent_id(agent_id, self._config_service)
        if self._bus is None:
            raise RuntimeError("AgentExecutionService 未绑定 JobEventBus")
        bus = self._bus

        if job_id is None:
            raise ValueError(f"job_id is required for run_step. session_id={session_id}, agent_id={agent_id}. "
                           f"Do not fallback to session_id. Always pass a valid job_id. "
                           f"This is a deliberate design choice following local agent principles: "
                           f"fail fast, never hide errors, never return fake defaults.")
        effective_job_id = job_id
        import logging
        logger = logging.getLogger(__name__)
        logger.info("[agent_execution_service] run_step begin: session_id=%s job_id=%s agent_id=%s message_length=%s", session_id, effective_job_id, resolved_agent_id, len(message or ""))

        # 注意：业务键（session_id / job_id）不放入 configurable —— session_id
        # 与 thread_id 重复、job_id 已经通过 set_current_job_id 维护在 contextvars。
        # 中间件通过 runtime.configurable 取不到这些键时，会回退到 contextvars
        # （见 LLMLoggingMiddleware._get_job_id 的优先级链）。
        config = build_checkpoint_config(session_id)

        job_token = set_current_job_id(effective_job_id)
        agent_token = set_current_agent_id(resolved_agent_id)
        interruptible_phase_token = set_interruptible_phase("text")
        active_tool_name_token = set_active_tool_name(None)
        SessionInterruptState.set(session_id, phase=None, tool_name=None)

        async def _publish(event_type: str, payload: dict[str, Any]) -> None:
            await bus.publish(
                job_id=effective_job_id,
                event_type=event_type,
                payload=payload,
                agent_id=resolved_agent_id,
            )

        final_text = ""
        latest_model_reasoning_text = ""
        skill_tool_sources = get_workspace_skill_tool_sources(
            agent_id=resolved_agent_id,
            config_service=self._config_service,
        )
        resolved_attachments = list(attachments or [])
        human_content = build_human_content(message, resolved_attachments)
        human_response_metadata = build_human_response_metadata(
            message_id=message_id,
            display_content=message,
            attachments=resolved_attachments,
            message_created_at=message_created_at,
        )

        try:
            await _publish(EventType.AGENT_START, {
                "message": "agent 启动，准备处理用户请求",
                "agent_id": resolved_agent_id,
            })

            logger.info("[agent_execution_service] agent.astream_events begin: job_id=%s", effective_job_id)

            # 为 fallback 收集需要的模型配置；多模态请求只使用声明了对应输入能力的 provider。
            candidate_models = build_candidate_models_for_session_request(
                agent_id=resolved_agent_id,
                config_service=self._config_service,
                content=human_content,
            )
            if not candidate_models:
                raise RuntimeError("当前 agent 没有可用的模型 provider")

            current_model_index = 0
            final_text = ""

            while True:
                try:
                    if current_model_index > 0:
                        await _publish(EventType.AGENT_START, {
                            "message": f"回退到模型 #{current_model_index} ({candidate_models[current_model_index].__class__.__name__}) 继续处理",
                            "agent_id": resolved_agent_id,
                        })
                        logger.info("[agent_execution_service] fallback to model #%s: job_id=%s", current_model_index, effective_job_id)

                    agent = build_session_agent_runtime(
                        session_id=session_id,
                        agent_id=resolved_agent_id,
                        config_service=self._config_service,
                        background_task_registry=self._background_task_registry,
                        background_message_bus=self._background_message_bus,
                        job_event_bus=self._bus,
                        dependency_provider=self._dependency_provider,
                        override_model=candidate_models[current_model_index],
                        fallback_middleware_enabled=False,
                    )

                    stream_result = await process_agent_event_stream(
                        agent=agent,
                        input_payload={
                            "messages": [
                                HumanMessage(
                                    content=human_content,
                                    response_metadata=human_response_metadata,
                                )
                            ]
                        },
                        config=config,
                        session_id=session_id,
                        agent_id=resolved_agent_id,
                        skill_tool_sources=skill_tool_sources,
                        publish=_publish,
                    )
                    final_text = stream_result.final_text
                    final_text = unwrap_json_string_tool_result(
                        final_text,
                        stream_result.last_tool_result_text,
                    )
                    final_text = await recover_or_sanitize_final_text(
                        agent=agent,
                        final_text=final_text,
                        session_id=session_id,
                        agent_id=resolved_agent_id,
                        job_id=effective_job_id,
                        publish=_publish,
                        logger=logger,
                    )
                    latest_model_reasoning_text = stream_result.latest_model_reasoning_text
                    break

                except Exception as e:
                    # 检查是否还有 fallback 模型可用
                    if current_model_index + 1 < len(candidate_models):
                        logger.warning("[agent_execution_service] model #%s failed, trying fallback #%s: job_id=%s error=%s", current_model_index, current_model_index + 1, effective_job_id, str(e))
                        current_model_index += 1
                        continue
                    else:
                        # 所有模型都失败了
                        logger.error("[agent_execution_service] all models failed: job_id=%s last_error=%s", effective_job_id, str(e))
                        raise

            if final_text:
                SessionInterruptState.set(session_id, phase=None, tool_name=None)
                set_interruptible_phase("text")
                set_active_tool_name(None)
                await _publish(EventType.TEXT_END, {"text": final_text})

            checkpointer = getattr(self._dependency_provider, "get_checkpointer", lambda: None)()
            if checkpointer is not None:
                persist_standard_assistant_checkpoint(
                    checkpointer=checkpointer,
                    session_id=session_id,
                    reasoning_text=latest_model_reasoning_text,
                    final_text=final_text,
                )

            await _publish(EventType.AGENT_END, {
                "final_text": final_text,
                "agent_id": resolved_agent_id,
            })

            logger.info("[agent_execution_service] response ready: job_id=%s response_length=%s", effective_job_id, len(final_text))
            return final_text

        except asyncio.CancelledError:
            state = SessionInterruptState.get(session_id)
            if state.user_interrupt_reminder_injected:
                logger.info(
                    "[agent_execution_service] job cancelled after user interrupt reminder persisted: job_id=%s",
                    effective_job_id,
                )
            else:
                persist_interrupt_checkpoint(
                    checkpointer=getattr(self._dependency_provider, "get_checkpointer", lambda: None)(),
                    session_id=session_id,
                    current_text=state.current_text,
                    active_tool_name=state.tool_name,
                )
                logger.info("[agent_execution_service] job cancelled and checkpoint persisted: job_id=%s", effective_job_id)
            raise
        except Exception as e:
            await _publish(EventType.ERROR, {"error": str(e), "phase": "agent_execution"})
            logger.exception("[agent_execution_service] ERROR published: job_id=%s error=%s", effective_job_id, str(e))
            raise
        finally:
            reset_current_job_id(job_token)
            reset_current_agent_id(agent_token)
            reset_interruptible_phase(interruptible_phase_token)
            reset_active_tool_name(active_tool_name_token)
            SessionInterruptState.clear(session_id)

    def get_for_session(self, session_id: str, agent_id: str | None = None):
        return self._get_or_create_agent(session_id, agent_id)

    def get_available_tools(self) -> list[dict[str, Any]]:
        session_id = "tools_inspection_session"
        agent = self._get_or_create_agent(session_id)
        return build_agent_tool_definitions(agent)
