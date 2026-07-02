from __future__ import annotations

from typing import Any, Callable, TYPE_CHECKING
from collections.abc import Sequence

from langchain.agents import create_agent
from langchain.agents.middleware import ModelFallbackMiddleware
from langchain.agents.middleware import TodoListMiddleware, HumanInTheLoopMiddleware
from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ContextT,
    ResponseT,
    _InputAgentState,
    _OutputAgentState,
)
from langchain.messages import SystemMessage
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langchain_openai import ChatOpenAI
from deepagents.backends import LocalShellBackend
from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.subagents import SubAgentMiddleware, SubAgent, CompiledSubAgent
from deepagents.middleware.async_subagents import AsyncSubAgent
from deepagents.middleware.skills import SkillsMiddleware
from deepagents.middleware.summarization import create_summarization_middleware
from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
from deepagents.middleware.permissions import FilesystemPermission
from langchain.agents.middleware import InterruptOnConfig

from app.core.checkpoint_saver import FileSystemCheckpointSaver
from app.core.path_utils import get_checkpoints_dir, get_workspace_root
from app.agents.agent_tools import build_default_tools
from app.agents.agent_middleware import LLMLoggingMiddleware
from app.abstractions.job_event_bus import JobEventBusProtocol
from app.abstractions.job_service import JobServiceProtocol
from app.services.infrastructure.config_service import ConfigService
from app.core.background_message_bus import BackgroundMessageBus
from app.core.background_task_registry import BackgroundTaskRegistry
from app.core.job_event_bus import JobEventBus

if TYPE_CHECKING:
    from app.services.business.message_service import MessageService
    from app.services.business.session_service import SessionService


BASE_AGENT_PROMPT = """响应规范：
- 不要在普通 assistant 正文中输出内部思考、推理过程、执行计划或自我叙述，例如 "The user asked..."、"Let me..."。
- 如果需要调用工具，直接调用工具；最终回答只包含用户需要看到的结果。
- 使用用户的语言回复，除非用户明确要求其它语言。"""

PROVIDER_REQUEST_OPTION_KEYS = {"extra_body", "default_headers"}
LITELLM_OPENAI_COMPATIBLE_INTERFACES = {"chat.completion", "opencode_zen"}


GENERAL_PURPOSE_SUBAGENT = {
    "name": "general-purpose",
    "description": "一个通用用途的子代理，可以处理各种任务。",
    "system_prompt": """你是一个通用用途的子代理。

你的能力：
- 使用提供的工具完成各种任务
- 文件读取、编辑和执行命令
- 根据任务需求选择合适的工具

请根据用户指令完成相应任务。""",
}


def _build_model_from_provider(provider: dict[str, Any], runtime_config: dict[str, Any]) -> Any:
    """从单个 provider 配置构建模型实例。"""
    interface = provider.get("interface")
    request_options = _get_provider_request_options(provider)
    if interface in LITELLM_OPENAI_COMPATIBLE_INTERFACES:
        from app.agents.providers.litellm_chat import build_litellm_chat_model

        return build_litellm_chat_model(
            provider=provider,
            runtime_config=runtime_config,
            openai_compatible=True,
            request_options=request_options,
        )
    elif interface == "responses":
        return ChatOpenAI(
            model=provider["model"],
            api_key=provider["api_key"],
            base_url=provider["endpoint"],
            use_responses_api=True,
            temperature=runtime_config["temperature"],
            top_p=runtime_config["top_p"],
            max_tokens=runtime_config["max_output_tokens"],
            max_retries=3,
            **request_options,
        )
    elif interface == "litellm":
        from app.agents.providers.litellm_chat import build_litellm_chat_model

        return build_litellm_chat_model(
            provider=provider,
            runtime_config=runtime_config,
            openai_compatible=False,
            request_options=request_options,
        )
    else:
        raise ValueError(
            f"不支持的 provider.interface: {interface!r}。"
            "请使用 chat.completion、opencode_zen、responses 或 litellm。"
        )


def _get_provider_request_options(provider: dict[str, Any]) -> dict[str, Any]:
    """读取 provider 级请求选项，并在拼错字段时直接报错。"""
    request_options = provider.get("request_options") or {}
    if not isinstance(request_options, dict):
        raise TypeError("provider.request_options 必须是对象")

    unknown_keys = sorted(set(request_options) - PROVIDER_REQUEST_OPTION_KEYS)
    if unknown_keys:
        raise ValueError(f"provider.request_options 包含不支持的字段: {', '.join(unknown_keys)}")

    return {key: value for key, value in request_options.items() if value is not None}


def build_runtime_for_agent(agent_id: str, config_service: ConfigService | None = None) -> dict[str, Any]:
    if config_service is None:
        raise RuntimeError("build_runtime_for_agent 需要显式传入 ConfigService")
    service = config_service
    runtime_config = service.get_agent_runtime_config(agent_id)
    providers = runtime_config["providers"]

    models = []
    for provider in providers:
        model = _build_model_from_provider(provider, runtime_config)
        models.append(model)

    if not models:
        raise RuntimeError("未能构建任何模型实例")

    return {
        "model": models[0],
        "fallback": ModelFallbackMiddleware(*models[1:]) if len(models) > 1 else None,
        "system_prompt": runtime_config["system_prompt"],
    }


def resolve_agent_id(agent_id: str | None, config_service: ConfigService | None = None) -> str:
    if config_service is None:
        raise RuntimeError("resolve_agent_id 需要显式传入 ConfigService")
    service = config_service
    return service.resolve_agent_id(agent_id)


def _filter_tools_by_name(tools: list[BaseTool | Callable[..., Any] | dict[str, Any]], denylist: set[str]) -> list[BaseTool | Callable[..., Any] | dict[str, Any]]:
    if not denylist:
        return tools
    return [tool for tool in tools if getattr(tool, "name", None) not in denylist]


def _filter_middleware_tools(middleware: Any, denylist: set[str]) -> None:
    if not denylist:
        return

    middleware_tools = getattr(middleware, "tools", None)
    if not isinstance(middleware_tools, list):
        return

    filtered_tools = _filter_tools_by_name(list(middleware_tools), denylist)
    setattr(middleware, "tools", filtered_tools)


def _filter_subagent_specs(
    subagent_specs: list[Any],
    denylist: set[str]
) -> list[Any]:
    if not denylist:
        return subagent_specs

    filtered_specs: list[Any] = []
    for spec in subagent_specs:
        processed_spec = dict(spec)
        if processed_spec.get("tools") is not None:
            processed_spec["tools"] = _filter_tools_by_name(list(processed_spec["tools"]), denylist)

        nested_subagents = processed_spec.get("subagents")
        if isinstance(nested_subagents, list):
            processed_spec["subagents"] = _filter_subagent_specs(nested_subagents, denylist)

        filtered_specs.append(processed_spec)

    return filtered_specs


def create_my_deep_agent(
    *,
    model: BaseChatModel,
    system_prompt: str | SystemMessage,
    checkpointer: BaseCheckpointSaver | None = None,
    session_id: str,
    agent_id: str,
    fallback_middleware: ModelFallbackMiddleware | None = None,
    sender_agent_id: str | None = None,
    enabled_tool_names: set[str] | None = None,
    enabled_runtime_middleware_names: set[str] | None = None,
    tool_denylist: set[str] | None = None,
    tools: Sequence[BaseTool | Callable[..., Any] | dict[str, Any]] | None = None,
    middleware: Sequence[AgentMiddleware] | None = None,
    subagents: Sequence[SubAgent | CompiledSubAgent | AsyncSubAgent] | None = None,
    skills: list[str] | None = None,
    memory: list[str] | None = None,
    permissions: list[FilesystemPermission] | None = None,
    interrupt_on: dict[str, bool | InterruptOnConfig] | None = None,
    debug: bool = False,
    name: str | None = None,
    background_task_registry: BackgroundTaskRegistry | None = None,
    background_message_bus: BackgroundMessageBus | None = None,
    job_event_bus: JobEventBusProtocol | None = None,
    job_service: object | None = None,
    message_service: MessageService | None = None,
    session_service: SessionService | None = None,
    session_orchestrator: object | None = None,
    config_service: ConfigService | None = None,
) -> Any:
    if checkpointer is None:
        checkpoint_base = get_checkpoints_dir()
        checkpoint_base.mkdir(parents=True, exist_ok=True)
        checkpointer = FileSystemCheckpointSaver(base_dir=checkpoint_base)

    resolved_sender_agent_id = sender_agent_id or agent_id
    resolved_tool_denylist = set(tool_denylist or set())

    if background_task_registry is None:
        raise RuntimeError("create_my_deep_agent 需要显式传入 BackgroundTaskRegistry")
    if background_message_bus is None:
        raise RuntimeError("create_my_deep_agent 需要显式传入 BackgroundMessageBus")
    if job_event_bus is None:
        raise RuntimeError("create_my_deep_agent 需要显式传入 JobEventBus")
    if message_service is None:
        raise RuntimeError("create_my_deep_agent 需要显式传入 MessageService")
    if session_service is None:
        raise RuntimeError("create_my_deep_agent 需要显式传入 SessionService")
    if session_orchestrator is None:
        raise RuntimeError("create_my_deep_agent 需要显式传入 SessionOrchestrator")
    if config_service is None:
        raise RuntimeError("create_my_deep_agent 需要显式传入 ConfigService")

    resolved_tools = list(tools) if tools is not None else build_default_tools(
        session_id=session_id,
        agent_id=agent_id,
        sender_agent_id=resolved_sender_agent_id,
        background_task_registry=background_task_registry,
        background_message_bus=background_message_bus,
        job_event_bus=job_event_bus,
        job_service=job_service,
        message_service=message_service,
        session_service=session_service,
        session_orchestrator=session_orchestrator,
        config_service=config_service,
    )
    resolved_tools = _filter_tools_by_name(resolved_tools, resolved_tool_denylist)
    if enabled_tool_names is not None:
        resolved_tools = [tool for tool in resolved_tools if getattr(tool, "name", "") in enabled_tool_names]

    runtime_middleware = list(middleware) if middleware is not None else [LLMLoggingMiddleware()]
    if fallback_middleware is not None:
        runtime_middleware.append(fallback_middleware)
    if enabled_runtime_middleware_names is not None:
        runtime_middleware = [
            item for item in runtime_middleware if item.__class__.__name__ in enabled_runtime_middleware_names
        ]

    workspace_root = get_workspace_root()
    backend = LocalShellBackend(
        root_dir=str(workspace_root),
        virtual_mode=True,
    )

    gp_middleware = [
        TodoListMiddleware(),
        FilesystemMiddleware(backend=backend, _permissions=permissions),
        create_summarization_middleware(model, backend),
        PatchToolCallsMiddleware(),
    ]
    if skills:
        gp_middleware.append(SkillsMiddleware(backend=backend, sources=skills))

    general_purpose_spec = {
        **GENERAL_PURPOSE_SUBAGENT,
        "model": model,
        "tools": list(resolved_tools) if resolved_tools else [],
        "middleware": gp_middleware,
    }
    if interrupt_on:
        general_purpose_spec["interrupt_on"] = interrupt_on

    inline_subagents: list[dict] = []
    if subagents:
        for spec in _filter_subagent_specs(list(subagents), resolved_tool_denylist):
            subagent_interrupt_on = spec.get("interrupt_on", interrupt_on)

            subagent_middleware = [
                TodoListMiddleware(),
                FilesystemMiddleware(backend=backend, _permissions=spec.get("permissions")),
                create_summarization_middleware(model, backend),
                PatchToolCallsMiddleware(),
            ]
            if spec.get("skills"):
                subagent_middleware.append(
                    SkillsMiddleware(backend=backend, sources=spec["skills"])
                )
            subagent_middleware.extend(spec.get("middleware", []))

            processed_spec = {
                **spec,
                "model": spec.get("model", model),
                "tools": spec.get("tools", list(resolved_tools) if resolved_tools else []),
                "middleware": subagent_middleware,
            }
            if subagent_interrupt_on:
                processed_spec["interrupt_on"] = subagent_interrupt_on
            inline_subagents.append(processed_spec)

    if not any(spec.get("name") == GENERAL_PURPOSE_SUBAGENT["name"] for spec in inline_subagents):
        inline_subagents.insert(0, general_purpose_spec)

    deepagent_middleware = [
        TodoListMiddleware(),
    ]

    if skills:
        deepagent_middleware.append(SkillsMiddleware(backend=backend, sources=skills))

    deepagent_middleware.extend(
        [
            FilesystemMiddleware(backend=backend),
            SubAgentMiddleware(
                backend=backend,
                subagents=inline_subagents,
            ),
            create_summarization_middleware(model, backend),
            PatchToolCallsMiddleware(),
        ]
    )

    if runtime_middleware:
        deepagent_middleware.extend(runtime_middleware)

    for middleware_item in deepagent_middleware:
        _filter_middleware_tools(middleware_item, resolved_tool_denylist)

    if memory:
        deepagent_middleware.append(MemoryMiddleware(backend=backend, sources=memory))

    if interrupt_on:
        deepagent_middleware.append(HumanInTheLoopMiddleware(interrupt_on=interrupt_on))

    if isinstance(system_prompt, SystemMessage):
        final_system_prompt = SystemMessage(
            content_blocks=[
                *system_prompt.content_blocks,
                {"type": "text", "text": f"\n\n{BASE_AGENT_PROMPT}"},
            ]
        )
    else:
        final_system_prompt = f"{system_prompt}\n\n{BASE_AGENT_PROMPT}"

    agent = create_agent(
        model,
        system_prompt=final_system_prompt,
        tools=list(resolved_tools) if resolved_tools else None,
        middleware=deepagent_middleware,
        response_format=None,
        context_schema=None,
        checkpointer=checkpointer,
        store=None,
        debug=debug,
        name=name,
        cache=None,
    )

    if hasattr(agent, "with_config"):
        return agent.with_config(
            {
                "recursion_limit": 9999,
                "metadata": {
                    "ls_integration": "deepagents",
                    "versions": {"deepagents": "custom"},
                    "lc_agent_name": name,
                },
            }
        )

    return agent


def create_runtime_deep_agent_for_session(
    *,
    session_id: str,
    agent_id: str,
    config_service: ConfigService | None = None,
    background_task_registry: BackgroundTaskRegistry | None = None,
    background_message_bus: BackgroundMessageBus | None = None,
    job_event_bus: JobEventBusProtocol | None = None,
    job_service: JobServiceProtocol | None = None,
    message_service: MessageService | None = None,
    session_service: SessionService | None = None,
    session_orchestrator: object | None = None,
    sender_agent_id: str | None = None,
    enabled_tool_names: set[str] | None = None,
    enabled_runtime_middleware_names: set[str] | None = None,
    tool_denylist: set[str] | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    name: str | None = None,
    override_model: Any = None,
):
    if config_service is None:
        raise RuntimeError("create_runtime_deep_agent_for_session 需要显式传入 ConfigService")
    service = config_service
    runtime = build_runtime_for_agent(agent_id=agent_id, config_service=service)
    tool_config = service.get_agent_tool_config(agent_id)

    model = override_model if override_model is not None else runtime["model"]

    return create_my_deep_agent(
        model=model,
        system_prompt=runtime["system_prompt"],
        checkpointer=checkpointer,
        session_id=session_id,
        agent_id=agent_id,
        fallback_middleware=runtime["fallback"],
        sender_agent_id=sender_agent_id,
        enabled_tool_names=enabled_tool_names,
        enabled_runtime_middleware_names=enabled_runtime_middleware_names,
        tool_denylist=set(tool_config.get("denylist", [])) if tool_denylist is None else tool_denylist,
        name=name or agent_id,
        background_task_registry=background_task_registry,
        background_message_bus=background_message_bus,
        job_event_bus=job_event_bus,
        job_service=job_service,
        message_service=message_service,
        session_service=session_service,
        session_orchestrator=session_orchestrator,
        config_service=service,
    )
