from __future__ import annotations

from typing import Any, Callable, TYPE_CHECKING
from collections.abc import Sequence

from langchain.agents import create_agent
from langchain.agents.middleware import ModelFallbackMiddleware
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
from deepagents.middleware.subagents import SubAgent, CompiledSubAgent
from deepagents.middleware.async_subagents import AsyncSubAgent
from deepagents.middleware.permissions import FilesystemPermission
from langchain.agents.middleware import InterruptOnConfig

from app.core.path_utils import get_workspace_root
from app.agents.agent_tools import build_default_tools
from app.agents.agent_middleware import LLMLoggingMiddleware
from app.agents.deep_agent_stack import (
    build_deep_agent_middleware,
    filter_tools_by_name,
)
from app.agents.custom_tools import build_custom_tool_bundle
from app.agents.skill_runtime import (
    append_skill_middlewares,
    discover_workspace_skill_sources,
)
from app.agents.tools.custom_invocation import create_custom_tool_invoker_tool
from app.agents.provider_capabilities import (
    detect_required_capabilities,
    select_providers_for_capabilities,
)
from app.abstractions.job_event_bus import JobEventBusProtocol
from app.abstractions.job_service import JobServiceProtocol
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.terminal_manager_client import TerminalManagerClient
from app.core.background_message_bus import BackgroundMessageBus
from app.core.background_task_registry import BackgroundTaskRegistry
from app.core.job_event_bus import JobEventBus

if TYPE_CHECKING:
    from app.services.business.message_service import MessageService
    from app.services.business.session_service import SessionService


BASE_AGENT_PROMPT = """响应规范：
- 不要在普通 assistant 正文中输出内部思考、推理过程、执行计划或自我叙述，例如 "The user asked..."、"Let me..."。
- 如果需要调用工具，直接调用工具；最终回答只包含用户需要看到的结果。
- 当下一步是工具调用时，不要输出“正在调用”“我将调用”等过渡正文；描述工具名不等于完成工具调用。
- 当用户或工作区文档要求通过某个工具入口执行动作时，必须发起真实工具调用。
- 使用用户的语言回复，除非用户明确要求其它语言。"""

PROVIDER_REQUEST_OPTION_KEYS = {"extra_body", "default_headers"}
LITELLM_OPENAI_COMPATIBLE_INTERFACES = {"chat.completion", "opencode_zen"}


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


def build_candidate_models_for_agent_content(
    *,
    agent_id: str,
    config_service: ConfigService,
    content: object,
) -> list[Any]:
    """根据输入能力需求构建本次请求可用的模型候选列表。"""
    runtime_config = config_service.get_agent_runtime_config(agent_id)
    providers = runtime_config.get("providers", [])
    if not isinstance(providers, list):
        raise TypeError("agent runtime providers 应为 list")
    selected_providers = select_providers_for_capabilities(
        providers,
        detect_required_capabilities(content),
    )
    return [
        _build_model_from_provider(provider, runtime_config)
        for provider in selected_providers
    ]


def resolve_agent_id(agent_id: str | None, config_service: ConfigService | None = None) -> str:
    if config_service is None:
        raise RuntimeError("resolve_agent_id 需要显式传入 ConfigService")
    service = config_service
    return service.resolve_agent_id(agent_id)


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
    custom_tool_specs: Sequence[object] | None = None,
    tools: Sequence[BaseTool | Callable[..., Any] | dict[str, Any]] | None = None,
    middleware: Sequence[AgentMiddleware] | None = None,
    subagents: Sequence[SubAgent | CompiledSubAgent | AsyncSubAgent] | None = None,
    skills: list[Any] | None = None,
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
    terminal_manager_client: TerminalManagerClient | None = None,
) -> Any:
    if checkpointer is None:
        raise RuntimeError("create_my_deep_agent 需要显式传入 checkpointer")

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

    workspace_root = get_workspace_root()

    if tools is not None:
        resolved_tools = list(tools)
    else:
        visible_tools = build_default_tools(
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
            terminal_manager_client=terminal_manager_client,
        )
        custom_tool_bundle = build_custom_tool_bundle(
            custom_tool_specs or [],
            session_id=session_id,
            agent_id=agent_id,
            sender_agent_id=resolved_sender_agent_id,
            workspace_root=workspace_root,
            background_task_registry=background_task_registry,
            background_message_bus=background_message_bus,
            job_event_bus=job_event_bus,
            job_service=job_service,
            message_service=message_service,
            session_service=session_service,
            session_orchestrator=session_orchestrator,
            config_service=config_service,
            terminal_manager_client=terminal_manager_client,
        )
        custom_tools = filter_tools_by_name(
            custom_tool_bundle.tools,
            resolved_tool_denylist,
        )
        resolved_tools = [
            *visible_tools,
            create_custom_tool_invoker_tool(custom_tools),
        ]
    resolved_tools = filter_tools_by_name(resolved_tools, resolved_tool_denylist)
    if enabled_tool_names is not None:
        resolved_tools = [tool for tool in resolved_tools if getattr(tool, "name", "") in enabled_tool_names]

    runtime_middleware: list[AgentMiddleware] = []
    append_skill_middlewares(
        runtime_middleware,
        backend=None,
        skills=None,
    )
    runtime_middleware.extend(list(middleware) if middleware is not None else [LLMLoggingMiddleware()])
    if fallback_middleware is not None:
        runtime_middleware.append(fallback_middleware)
    if enabled_runtime_middleware_names is not None:
        runtime_middleware = [
            item for item in runtime_middleware if item.__class__.__name__ in enabled_runtime_middleware_names
        ]

    resolved_skills = discover_workspace_skill_sources(workspace_root) if skills is None else list(skills)
    backend = LocalShellBackend(
        root_dir=str(workspace_root),
        virtual_mode=True,
    )

    deepagent_middleware = build_deep_agent_middleware(
        model=model,
        backend=backend,
        workspace_root=workspace_root,
        permissions=permissions,
        resolved_skills=resolved_skills,
        resolved_tools=resolved_tools,
        subagents=subagents,
        resolved_tool_denylist=resolved_tool_denylist,
        interrupt_on=interrupt_on,
        runtime_middleware=runtime_middleware,
        memory=memory,
    )

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
    terminal_manager_client: TerminalManagerClient | None = None,
    name: str | None = None,
    override_model: Any = None,
    fallback_middleware_enabled: bool = True,
):
    if config_service is None:
        raise RuntimeError("create_runtime_deep_agent_for_session 需要显式传入 ConfigService")
    service = config_service
    runtime = build_runtime_for_agent(agent_id=agent_id, config_service=service)
    tool_config = service.get_agent_tool_config(agent_id)
    custom_tool_specs = list(tool_config.get("custom", []))

    model = override_model if override_model is not None else runtime["model"]

    return create_my_deep_agent(
        model=model,
        system_prompt=runtime["system_prompt"],
        checkpointer=checkpointer,
        session_id=session_id,
        agent_id=agent_id,
        fallback_middleware=runtime["fallback"]
        if fallback_middleware_enabled and override_model is None
        else None,
        sender_agent_id=sender_agent_id,
        enabled_tool_names=enabled_tool_names,
        enabled_runtime_middleware_names=enabled_runtime_middleware_names,
        tool_denylist=set(tool_config.get("denylist", [])) if tool_denylist is None else tool_denylist,
        custom_tool_specs=custom_tool_specs,
        name=name or agent_id,
        background_task_registry=background_task_registry,
        background_message_bus=background_message_bus,
        job_event_bus=job_event_bus,
        job_service=job_service,
        message_service=message_service,
        session_service=session_service,
        session_orchestrator=session_orchestrator,
        terminal_manager_client=terminal_manager_client,
        config_service=service,
    )
