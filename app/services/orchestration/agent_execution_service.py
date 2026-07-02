from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Dict, Any, List

from langchain_core.messages import AIMessage, HumanMessage

from app.abstractions.job_event_bus import JobEventBusProtocol
from app.core.background_message_bus import BackgroundMessageBus
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
from app.agents.agent_factory import resolve_agent_id
from app.services.infrastructure.config_service import ConfigService
from app.abstractions.job_step_executor import JobStepExecutor
from app.runtime.agent_runtime import AgentRuntimeDependencyProvider, build_session_agent_runtime
from app.services.business.reasoning_checkpoint_service import (
    persist_standard_assistant_checkpoint,
)
from app.services.mapping.agent_content_mapper import split_agent_content

CHAT_MODEL_EVENT_NAMES = {
    "ChatOpenAI",
    "BoxteamLiteLLMChatModel",
    "ChatLiteLLM",
    "ChatLiteLLMRouter",
}


def _message_has_content(message: Any) -> bool:
    content = getattr(message, "content", None)
    if content is None:
        return False
    if isinstance(content, list):
        return any(bool(part) for part in content)
    return bool(str(content).strip())


def _normalize_tool_args(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    return {"input": value}


def _serialize_tool_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _extract_tool_result_text(output: Any) -> str:
    content = getattr(output, "content", None)
    if content is not None:
        return _serialize_tool_value(content)
    return _serialize_tool_value(output)


def _is_tracked_chat_model_event(name: str) -> bool:
    return name in CHAT_MODEL_EVENT_NAMES


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

    def _persist_interrupt_checkpoint(
        self,
        session_id: str,
        current_text: str,
        active_tool_name: str | None,
    ) -> None:
        """在任务被取消时，把已生成的部分 assistant 消息和独立 <system_reminder> 写入 checkpoint。

        提醒作为独立 HumanMessage 持久化，避免把内部提醒混入 assistant 正文。
        下一次运行模型时 LangGraph 会从 checkpoint 加载到该提醒。
        """
        checkpointer = getattr(self._dependency_provider, "get_checkpointer", lambda: None)()
        if checkpointer is None:
            return

        config = build_checkpoint_config(session_id)
        try:
            tup = checkpointer.get_tuple(config)
        except Exception:
            return

        if tup is None:
            return

        checkpoint = tup.checkpoint.copy()
        channel_values = dict(checkpoint.get("channel_values", {}))
        messages = list(channel_values.get("messages", []))

        # 过滤掉可能存在的空 assistant 占位消息
        messages = [
            msg for msg in messages
            if not (isinstance(msg, AIMessage) and not _message_has_content(msg))
        ]

        phase = "tool" if active_tool_name else "text"
        interrupted_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        if phase == "tool" and active_tool_name:
            reminder = (
                f"用户在你调用工具（{active_tool_name}）的过程中于 {interrupted_at} 打断。"
                f"当前工具调用已被取消，请停止当前操作，根据已有信息回应用户最新请求。"
            )
        else:
            reminder = (
                f"用户在文本生成过程中于 {interrupted_at} 打断。"
                f"请停止当前输出，根据已有信息回应用户最新请求。"
            )

        # 追加部分 assistant 文本；如果没有正文，不制造空 assistant 占位。
        content = current_text if phase == "text" else ""
        if content.strip():
            messages.append(
                AIMessage(
                    content=content,
                    tool_calls=[],
                    response_metadata={
                        "phase": phase,
                        "tool_name": active_tool_name,
                        "source": "interrupt",
                    },
                )
            )

        reminder_message = HumanMessage(
            content=f"<system_reminder>\n{reminder}\n</system_reminder>",
            response_metadata={
                "phase": phase,
                "tool_name": active_tool_name,
                "source": "interrupt",
            },
        )
        messages.append(reminder_message)

        channel_values["messages"] = messages
        checkpoint["channel_values"] = channel_values
        checkpoint["id"] = str(uuid.uuid4())

        # 走 checkpointer.get_next_version（实例方法）获取 messages 通道的新版本号。
        # 避免使用模块级 next_channel_version 重复实现 saver 内部的 version 算法。
        channel_versions = dict(checkpoint.get("channel_versions", {}))
        messages_version = checkpointer.get_next_version(
            channel_versions.get("messages"), None
        )
        channel_versions["messages"] = messages_version
        checkpoint["channel_versions"] = channel_versions
        # updated_channels 字段在业务代码中不读（仅 decode_checkpoint.py / 测试中用），
        # 这里不再手工维护；如未来需要从 saver 推导可补在 FileSystemCheckpointSaver.put。

        try:
            checkpointer.put(
                config=tup.config,
                checkpoint=checkpoint,
                metadata={"source": "interrupt", "step": -1, "writes": {}},
                new_versions={"messages": messages_version},
            )
        except Exception:
            pass

    async def run_step(self, session_id: str, message: str, agent_id: str | None = None, job_id: str | None = None, message_id: str | None = None) -> str:
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

        collected_text_parts: list[str] = []
        collected_reasoning_parts: list[str] = []
        latest_model_reasoning_parts: list[str] = []
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

            # 为 fallback 收集需要的模型配置
            runtime_config = self._config_service.get_agent_runtime_config(resolved_agent_id)
            providers = runtime_config.get("providers", [])
            fallback_models = []
            for provider in providers[1:]:
                from app.agents.agent_factory import _build_model_from_provider
                fallback_models.append(_build_model_from_provider(provider, runtime_config))

            current_model_index = 0
            final_text = ""

            while True:
                try:
                    if current_model_index == 0:
                        agent = self._get_or_create_agent(session_id, resolved_agent_id)
                    else:
                        await _publish(EventType.AGENT_START, {
                            "message": f"回退到模型 #{current_model_index} ({fallback_models[current_model_index - 1].__class__.__name__}) 继续处理",
                            "agent_id": resolved_agent_id,
                        })
                        logger.info("[agent_execution_service] fallback to model #%s: job_id=%s", current_model_index, effective_job_id)
                        from app.runtime.agent_runtime import build_session_agent_runtime
                        agent = build_session_agent_runtime(
                            session_id=session_id,
                            agent_id=resolved_agent_id,
                            config_service=self._config_service,
                            background_task_registry=self._background_task_registry,
                            background_message_bus=self._background_message_bus,
                            job_event_bus=self._bus,
                            dependency_provider=self._dependency_provider,
                            override_model=fallback_models[current_model_index - 1],
                        )

                    collected_text_parts.clear()
                    collected_reasoning_parts.clear()
                    latest_model_reasoning_parts.clear()
                    active_tool_call_id = None
                    active_tool_name = None
                    active_tool_args = {}

                    async for event in agent.astream_events(
                        {"messages": [HumanMessage(content=message, response_metadata={"message_id": message_id} if message_id else {})]},
                        config=config,
                        version="v2",
                    ):
                        event_type = event.get("event")
                        name = event.get("name", "")
                        data = event.get("data", {})
                        metadata = event.get("metadata", {})

                        if event_type == "on_chat_model_start" and _is_tracked_chat_model_event(name):
                            latest_model_reasoning_parts.clear()
                            model_name = metadata.get("ls_model_name") or "unknown_model"
                            await _publish(EventType.LLM_REQUEST, {
                                "model": model_name,
                                "timestamp": int(time.time() * 1000),
                            })

                        elif event_type == "on_chat_model_stream" and _is_tracked_chat_model_event(name):
                            chunk = data.get("chunk")
                            if chunk is None:
                                continue
                            # ChatGenerationChunk 没有 .content 属性，需要从 .message 获取
                            chunk_message = getattr(chunk, "message", None)
                            if chunk_message is not None:
                                content = getattr(chunk_message, "content", None) or ""
                                tool_calls = getattr(chunk_message, "tool_calls", None) or []
                            else:
                                content = getattr(chunk, "content", None) or ""
                                tool_calls = getattr(chunk, "tool_calls", None) or []
                            reasoning_content, text_content = split_agent_content(content)

                            # 处理 reasoning 阶段
                            if reasoning_content.strip():
                                if not collected_text_parts and not collected_reasoning_parts:
                                    # reasoning 开始前先发送 TEXT_START（标记 assistant 开始输出）
                                    SessionInterruptState.set(session_id, phase="text", tool_name=None)
                                    set_interruptible_phase("text")
                                    await _publish(EventType.TEXT_START, {})
                                collected_reasoning_parts.append(reasoning_content)
                                latest_model_reasoning_parts.append(reasoning_content)
                                SessionInterruptState.set(
                                    session_id,
                                    current_text="".join(collected_text_parts),
                                )
                                await _publish(
                                    EventType.TEXT_DELTA,
                                    {"text": reasoning_content, "kind": "reasoning"},
                                )

                            # 处理普通 text 内容
                            if (
                                text_content.strip()
                                and not collected_text_parts
                                and not collected_reasoning_parts
                            ):
                                SessionInterruptState.set(session_id, phase="text", tool_name=None)
                                set_interruptible_phase("text")
                                await _publish(EventType.TEXT_START, {})

                            if text_content and (text_content.strip() or collected_text_parts):
                                collected_text_parts.append(text_content)
                                SessionInterruptState.set(
                                    session_id,
                                    current_text="".join(collected_text_parts),
                                )
                                await _publish(EventType.TEXT_DELTA, {"text": text_content, "kind": "text"})

                            for tc in tool_calls:
                                tc_id = tc.get("id")
                                tc_name = tc.get("name")
                                tc_args = tc.get("args") or {}
                                if tc_id and tc_id != active_tool_call_id:
                                    active_tool_call_id = tc_id
                                    active_tool_name = tc_name
                                    active_tool_args = _normalize_tool_args(tc_args)
                                elif tc_id == active_tool_call_id and isinstance(tc_args, dict):
                                    active_tool_args.update(tc_args)

                        elif event_type == "on_chat_model_end" and _is_tracked_chat_model_event(name):
                            pass

                        elif event_type == "on_tool_start":
                            tool_name = name or active_tool_name or "unknown_tool"
                            tool_args = _normalize_tool_args(data.get("input"))
                            active_tool_name = tool_name
                            active_tool_args = tool_args
                            SessionInterruptState.set(
                                session_id,
                                phase="tool",
                                tool_name=tool_name,
                            )
                            set_interruptible_phase("tool")
                            set_active_tool_name(tool_name)
                            await _publish(EventType.TOOL_CALL_START, {
                                "tool_name": tool_name,
                                "args": active_tool_args,
                                "agent_id": resolved_agent_id,
                            })

                        elif event_type == "on_tool_end":
                            tool_name = name
                            output = data.get("output")
                            result_text = _extract_tool_result_text(output)
                            SessionInterruptState.set(session_id, phase=None, tool_name=None)
                            set_interruptible_phase("text")
                            set_active_tool_name(None)
                            await _publish(EventType.TOOL_CALL_END, {
                                "tool_name": tool_name,
                                "result": result_text,
                                "agent_id": resolved_agent_id,
                            })

                    # 如果成功完成循环，跳出 while
                    final_text = "".join(collected_text_parts).strip()
                    break

                except Exception as e:
                    # 检查是否还有 fallback 模型可用
                    if current_model_index < len(fallback_models):
                        logger.warning("[agent_execution_service] model #%s failed, trying fallback #%s: job_id=%s error=%s", current_model_index, current_model_index + 1, effective_job_id, str(e))
                        current_model_index += 1
                        continue
                    else:
                        # 所有模型都失败了
                        logger.error("[agent_execution_service] all models failed: job_id=%s last_error=%s", effective_job_id, str(e))
                        raise

            if collected_text_parts:
                SessionInterruptState.set(session_id, phase=None, tool_name=None)
                set_interruptible_phase("text")
                set_active_tool_name(None)
                await _publish(EventType.TEXT_END, {"text": final_text})

            checkpointer = getattr(self._dependency_provider, "get_checkpointer", lambda: None)()
            if checkpointer is not None:
                persist_standard_assistant_checkpoint(
                    checkpointer=checkpointer,
                    session_id=session_id,
                    reasoning_text="".join(latest_model_reasoning_parts),
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
            self._persist_interrupt_checkpoint(
                session_id,
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
