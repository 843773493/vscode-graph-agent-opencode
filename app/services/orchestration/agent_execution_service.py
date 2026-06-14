from __future__ import annotations

import time
from typing import Dict, Any, List

from app.abstractions.job_event_bus import JobEventBusProtocol
from app.core.background_message_bus import BackgroundMessageBus
from app.core.background_task_registry import BackgroundTaskRegistry
from app.core.job_context import (
    get_recent_tool_results,
    reset_current_agent_id,
    reset_current_job_id,
    reset_last_turn_status,
    reset_recent_tool_results,
    set_current_agent_id,
    set_current_job_id,
    set_last_turn_status,
    set_recent_tool_results,
)
from app.core.job_event_bus import EventType
from app.core.session_interrupt_state import SessionInterruptState
from app.agents.agent_factory import resolve_agent_id
from app.services.infrastructure.config_service import ConfigService
from app.abstractions.job_step_executor import JobStepExecutor
from app.runtime.agent_runtime import AgentRuntimeDependencyProvider, build_session_agent_runtime


class AgentExecutionService(JobStepExecutor):
    def __init__(
        self,
        *,
        config_service: ConfigService,
        background_task_registry: BackgroundTaskRegistry,
        background_message_bus: BackgroundMessageBus,
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
            system_reminder_trigger_registry=self._dependency_provider.get_system_reminder_trigger_registry(),
        )

        self._agent_cache[cache_key] = agent
        return agent

    def _extract_final_text(self, result: Dict[str, Any]) -> str:
        messages = result.get("messages", []) if isinstance(result, dict) else []
        for message in reversed(messages):
            content = getattr(message, "content", None)
            if content is None:
                continue
            if isinstance(content, list):
                content = "".join(str(part) for part in content)
            text = str(content).strip()
            if text:
                return text
        raise RuntimeError(
            "Agent 执行完成但没有提取到任何最终文本。"
            f" session_id={result.get('session_id') if isinstance(result, dict) else 'unknown'}"
            " 这通常表示最终消息不是 assistant 文本，或者消息链路中出现了空响应。"
        )

    async def run_step(self, session_id: str, message: str, agent_id: str | None = None, job_id: str | None = None) -> str:
        if self._config_service is None:
            raise RuntimeError("AgentExecutionService 未绑定 ConfigService")
        if self._background_task_registry is None:
            raise RuntimeError("AgentExecutionService 未绑定 BackgroundTaskRegistry")
        if self._background_message_bus is None:
            raise RuntimeError("AgentExecutionService 未绑定 BackgroundMessageBus")
        resolved_agent_id = resolve_agent_id(agent_id, self._config_service)
        agent = self._get_or_create_agent(session_id, resolved_agent_id)
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

        config = {
            "configurable": {
                "session_id": session_id,
                "thread_id": session_id,
                "job_id": effective_job_id,
            }
        }

        job_token = set_current_job_id(effective_job_id)
        agent_token = set_current_agent_id(resolved_agent_id)
        last_turn_token = set_last_turn_status("ok")
        tool_results_token = set_recent_tool_results([])
        SessionInterruptState.set(session_id, phase=None, tool_name=None)

        async def _publish(event_type: str, payload: dict[str, Any]) -> None:
            await bus.publish(
                job_id=effective_job_id,
                event_type=event_type,
                payload=payload,
                agent_id=resolved_agent_id,
            )

        collected_text_parts: list[str] = []
        final_text = ""
        active_tool_call_id: str | None = None
        active_tool_name: str | None = None
        active_tool_args: dict[str, Any] = {}

        try:
            await _publish(EventType.AGENT_START, {
                "message": "agent 启动，准备处理用户请求",
                "agent_id": resolved_agent_id,
            })

            logger.info("[agent_execution_service] agent.astream_events begin: job_id=%s", effective_job_id)

            async for event in agent.astream_events(
                {"messages": [{"role": "user", "content": message}]},
                config=config,
                version="v2",
            ):
                event_type = event.get("event")
                name = event.get("name", "")
                data = event.get("data", {})
                metadata = event.get("metadata", {})

                if event_type == "on_chat_model_start" and name == "ChatOpenAI":
                    model_name = metadata.get("ls_model_name") or "unknown_model"
                    await _publish(EventType.LLM_REQUEST, {
                        "model": model_name,
                        "timestamp": int(time.time() * 1000),
                    })

                elif event_type == "on_chat_model_stream" and name == "ChatOpenAI":
                    chunk = data.get("chunk")
                    if chunk is None:
                        continue
                    content = getattr(chunk, "content", None) or ""
                    tool_calls = getattr(chunk, "tool_calls", None) or []

                    if isinstance(content, list):
                        content = "".join(str(part) for part in content)
                    content = str(content)

                    if content.strip() and not collected_text_parts:
                        SessionInterruptState.set(session_id, phase="text", tool_name=None)
                        await _publish(EventType.TEXT_START, {})

                    if content:
                        collected_text_parts.append(content)
                        SessionInterruptState.set(
                            session_id,
                            current_text="".join(collected_text_parts),
                        )
                        await _publish(EventType.TEXT_DELTA, {"text": content})

                    for tc in tool_calls:
                        tc_id = tc.get("id")
                        tc_name = tc.get("name")
                        tc_args = tc.get("args") or {}
                        if tc_id and tc_id != active_tool_call_id:
                            if active_tool_call_id is not None:
                                await _publish(EventType.TOOL_CALL_END, {
                                    "tool_name": active_tool_name or "unknown_tool",
                                    "result": "",
                                    "agent_id": resolved_agent_id,
                                })
                            active_tool_call_id = tc_id
                            active_tool_name = tc_name
                            active_tool_args = dict(tc_args) if isinstance(tc_args, dict) else {}
                            SessionInterruptState.set(
                                session_id,
                                phase="tool",
                                tool_name=active_tool_name or "unknown_tool",
                            )
                            await _publish(EventType.TOOL_CALL_START, {
                                "tool_name": active_tool_name or "unknown_tool",
                                "args": active_tool_args,
                                "agent_id": resolved_agent_id,
                            })
                        elif tc_id == active_tool_call_id and isinstance(tc_args, dict):
                            active_tool_args.update(tc_args)

                elif event_type == "on_chat_model_end" and name == "ChatOpenAI":
                    pass

                elif event_type == "on_tool_end":
                    tool_name = name
                    output = data.get("output")
                    result_text = str(output) if output is not None else ""
                    recent_results = get_recent_tool_results()
                    if recent_results is not None:
                        recent_results.append({
                            "tool_name": tool_name,
                            "result": result_text,
                            "interrupted": False,
                        })
                    SessionInterruptState.set(session_id, phase=None, tool_name=None)
                    await _publish(EventType.TOOL_CALL_END, {
                        "tool_name": tool_name,
                        "result": result_text,
                        "agent_id": resolved_agent_id,
                    })

            final_text = "".join(collected_text_parts).strip()

            if collected_text_parts:
                SessionInterruptState.set(session_id, phase=None, tool_name=None)
                await _publish(EventType.TEXT_END, {"text": final_text})

            await _publish(EventType.AGENT_END, {
                "final_text": final_text,
                "agent_id": resolved_agent_id,
            })

            logger.info("[agent_execution_service] response ready: job_id=%s response_length=%s", effective_job_id, len(final_text))
            return final_text

        except Exception as e:
            await _publish(EventType.ERROR, {"error": str(e), "phase": "agent_execution"})
            logger.exception("[agent_execution_service] ERROR published: job_id=%s error=%s", effective_job_id, str(e))
            raise
        finally:
            reset_current_job_id(job_token)
            reset_current_agent_id(agent_token)
            reset_last_turn_status(last_turn_token)
            reset_recent_tool_results(tool_results_token)
            SessionInterruptState.clear(session_id)

    def get_for_session(self, session_id: str, agent_id: str | None = None):
        return self._get_or_create_agent(session_id, agent_id)

    def get_available_tools(self) -> List[Dict[str, Any]]:
        session_id = "tools_inspection_session"
        agent = self._get_or_create_agent(session_id)

        tool_map = {}
        graph_view = agent.get_graph()
        nodes = getattr(graph_view, "nodes", {}) or {}

        for _, node in nodes.items():
            candidate = getattr(node, "data", node)
            if hasattr(candidate, "tools_by_name"):
                tool_map.update(candidate.tools_by_name)

        if not tool_map:
            raise RuntimeError(
                "无法从Agent实例中提取工具列表！\n"
                "Agent图中未找到包含tools_by_name属性的节点。\n"
                "这是严重错误，需要立即修复，不能静默降级。"
            )

        tools = []
        for tool_name, tool in tool_map.items():
            tool_def = {
                "id": tool_name,
                "name": tool_name,
                "description": getattr(tool, "description", ""),
                "parameters": tool.args_schema.schema() if hasattr(tool, 'args_schema') else {"type": "object", "properties": {}},
                "category": "general"
            }
            tools.append(tool_def)

        return tools