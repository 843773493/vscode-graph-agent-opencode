from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Callable

from deepagents.middleware.permissions import FilesystemPermission
from deepagents.middleware.skills import append_to_system_message
from langchain.agents import create_agent
from langchain.agents.middleware import InterruptOnConfig
from langchain.agents.middleware.types import AgentMiddleware
from langchain.messages import SystemMessage
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver

from app.core.path_utils import get_workspace_root
from app.agents.agent_tools import build_default_tools
from app.agents.llm_logging_middleware import LLMLoggingMiddleware
from app.agents.middleware_prompts import TEAM_COORDINATION_SYSTEM_PROMPT
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
from app.agents.model_capability_routing import (
    CapabilityRoutingMiddleware,
    build_provider_model_candidate,
)
from app.agents.tool_invocation_context import (
    ToolInvocationContext,
    ToolInvocationContextMiddleware,
)
from app.agents.tool_output_middleware import ToolOutputMiddleware
from app.agents.workspace_backend import build_workspace_backend
from app.abstractions.job_event_bus import JobEventBusProtocol
from app.abstractions.job_service import JobServiceProtocol
from app.abstractions.session_context import (
    SessionContextQueryProtocol,
    WorkspaceSessionContextClientProtocol,
)
from app.abstractions.session_subagent import SessionSubagentProtocol
from app.abstractions.team import TeamCoordinationProtocol
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.terminal_manager_client import TerminalManagerClient
from app.services.infrastructure.browser_manager_client import BrowserManagerClient
from app.core.background_message_bus import BackgroundMessageBus
from app.core.background_task_registry import BackgroundTaskRegistry
from app.services.infrastructure.tool_output_store import ToolOutputStore

if TYPE_CHECKING:
    from app.services.business.message_service import MessageService
    from app.services.business.session_service import SessionService


AGENT_GRAPH_RECURSION_LIMIT = 9999
PROVIDER_REQUEST_OPTION_KEYS = {"overrides", "default_headers"}


def _team_aware_system_prompt(
    system_prompt: str | SystemMessage,
    *,
    enabled: bool,
) -> str | SystemMessage:
    if not enabled:
        return system_prompt
    base_message = (
        system_prompt
        if isinstance(system_prompt, SystemMessage)
        else SystemMessage(content=system_prompt)
    )
    return append_to_system_message(base_message, TEAM_COORDINATION_SYSTEM_PROMPT)


def build_model_from_provider(provider: dict[str, Any], runtime_config: dict[str, Any]) -> Any:
    """从单个 provider 配置构建模型实例。"""
    custom_llm_provider = provider.get("custom_llm_provider")
    if not isinstance(custom_llm_provider, str) or not custom_llm_provider:
        raise ValueError(
            f"provider {provider.get('id') or provider.get('model')!r} "
            "缺少 llm.providers[].custom_llm_provider 配置"
        )

    request_options = _get_provider_request_options(provider)
    from app.agents.providers.litellm_chat import build_litellm_chat_model

    return build_litellm_chat_model(
        provider=provider,
        runtime_config=runtime_config,
        request_options=request_options,
    )


def _get_provider_request_options(provider: dict[str, Any]) -> dict[str, Any]:
    """读取 provider 级请求选项，并在拼错字段时直接报错。"""
    request_options = provider.get("request_options") or {}
    if not isinstance(request_options, dict):
        raise TypeError("provider.request_options 必须是对象")

    unknown_keys = sorted(set(request_options) - PROVIDER_REQUEST_OPTION_KEYS)
    if unknown_keys:
        raise ValueError(f"provider.request_options 包含不支持的字段: {', '.join(unknown_keys)}")

    overrides = request_options.get("overrides") or {}
    if not isinstance(overrides, dict):
        raise TypeError("provider.request_options.overrides 必须是对象")
    default_headers = request_options.get("default_headers") or {}
    if not isinstance(default_headers, dict):
        raise TypeError("provider.request_options.default_headers 必须是对象")
    return {
        "overrides": dict(overrides),
        "default_headers": dict(default_headers),
    }


def build_runtime_for_agent(agent_id: str, config_service: ConfigService | None = None) -> dict[str, Any]:
    if config_service is None:
        raise RuntimeError("build_runtime_for_agent 需要显式传入 ConfigService")
    service = config_service
    runtime_config = service.get_agent_runtime_config(agent_id)
    providers = runtime_config["providers"]

    candidates = []
    for provider in providers:
        model = build_model_from_provider(provider, runtime_config)
        candidates.append(
            build_provider_model_candidate(provider=provider, model=model)
        )

    if not candidates:
        raise RuntimeError("未能构建任何模型实例")

    return {
        "model": candidates[0].model,
        "model_routing": CapabilityRoutingMiddleware(candidates),
        "system_prompt": runtime_config["system_prompt"],
    }


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
    model_routing_middleware: CapabilityRoutingMiddleware | None = None,
    sender_agent_id: str | None = None,
    enabled_tool_names: set[str] | None = None,
    enabled_runtime_middleware_names: set[str] | None = None,
    tool_denylist: set[str] | None = None,
    custom_tool_specs: Sequence[object] | None = None,
    tools: Sequence[BaseTool | Callable[..., Any] | dict[str, Any]] | None = None,
    middleware: Sequence[AgentMiddleware] | None = None,
    skills: list[Any] | None = None,
    memory: list[str] | None = None,
    permissions: list[FilesystemPermission] | None = None,
    interrupt_on: dict[str, bool | InterruptOnConfig] | None = None,
    debug: bool = False,
    name: str | None = None,
    background_task_registry: BackgroundTaskRegistry | None = None,
    background_message_bus: BackgroundMessageBus | None = None,
    job_event_bus: JobEventBusProtocol | None = None,
    job_service: JobServiceProtocol | None = None,
    message_service: MessageService | None = None,
    session_service: SessionService | None = None,
    session_orchestrator: object | None = None,
    session_subagent_service: SessionSubagentProtocol | None = None,
    team_service: TeamCoordinationProtocol | None = None,
    config_service: ConfigService | None = None,
    terminal_manager_client: TerminalManagerClient | None = None,
    browser_manager_client: BrowserManagerClient | None = None,
    session_context_query_service: SessionContextQueryProtocol | None = None,
    workspace_session_context_client: WorkspaceSessionContextClientProtocol | None = None,
) -> Any:
    if checkpointer is None:
        raise RuntimeError("create_my_deep_agent 需要显式传入 checkpointer")

    resolved_sender_agent_id = sender_agent_id or agent_id
    resolved_tool_denylist = set(tool_denylist or set())
    tool_invocation_context = ToolInvocationContext()

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
    if session_subagent_service is None:
        raise RuntimeError("create_my_deep_agent 需要显式传入 SessionSubagentService")
    if team_service is None:
        raise RuntimeError("create_my_deep_agent 需要显式传入 TeamCoordinationService")
    if job_service is None:
        raise RuntimeError("create_my_deep_agent 需要显式传入 JobService")
    if config_service is None:
        raise RuntimeError("create_my_deep_agent 需要显式传入 ConfigService")
    if session_context_query_service is None:
        raise RuntimeError("create_my_deep_agent 需要显式传入 SessionContextQueryService")
    if workspace_session_context_client is None:
        raise RuntimeError("create_my_deep_agent 需要显式传入 WorkspaceSessionContextClient")

    workspace_root = get_workspace_root()

    if tools is not None:
        resolved_tools = list(tools)
    else:
        if browser_manager_client is None:
            raise RuntimeError("create_my_deep_agent 构建默认工具集时需要显式传入 BrowserManagerClient")
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
            session_subagent_service=session_subagent_service,
            team_service=team_service,
            config_service=config_service,
            terminal_manager_client=terminal_manager_client,
            invocation_context=tool_invocation_context,
            workspace_root=workspace_root,
            include_test_tools=config_service.development_test_tools_enabled(),
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
            session_context_query_service=session_context_query_service,
            workspace_session_context_client=workspace_session_context_client,
            session_orchestrator=session_orchestrator,
            config_service=config_service,
            terminal_manager_client=terminal_manager_client,
            browser_manager_client=browser_manager_client,
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
    resolved_tool_names = {
        getattr(tool, "name", "")
        for tool in resolved_tools
    }
    session_delegation_tools = {"task", "create_team_member"}
    if (
        session_delegation_tools & resolved_tool_names
        and "send_message_to_session" not in resolved_tool_names
    ):
        raise ValueError(
            "task/create_team_member 依赖 send_message_to_session 完成 Agent 通信；"
            "不能启用委派工具时单独禁用 send_message_to_session"
        )
    resolved_system_prompt = _team_aware_system_prompt(
        system_prompt,
        enabled="create_team" in resolved_tool_names,
    )

    runtime_middleware: list[AgentMiddleware] = []
    append_skill_middlewares(
        runtime_middleware,
        backend=None,
        skills=None,
    )
    runtime_middleware.extend(list(middleware) if middleware is not None else [LLMLoggingMiddleware()])
    if enabled_runtime_middleware_names is not None:
        runtime_middleware = [
            item for item in runtime_middleware if item.__class__.__name__ in enabled_runtime_middleware_names
        ]

    resolved_skills = discover_workspace_skill_sources(workspace_root) if skills is None else list(skills)
    backend = build_workspace_backend(workspace_root)
    tool_output_middleware = ToolOutputMiddleware(
        session_id=session_id,
        store=ToolOutputStore(workspace_root=workspace_root),
    )
    tool_invocation_context_middleware = ToolInvocationContextMiddleware(
        tool_invocation_context
    )

    deepagent_middleware = build_deep_agent_middleware(
        model=model,
        backend=backend,
        workspace_root=workspace_root,
        permissions=permissions,
        resolved_skills=resolved_skills,
        resolved_tool_denylist=resolved_tool_denylist,
        interrupt_on=interrupt_on,
        runtime_middleware=runtime_middleware,
        model_routing_middleware=model_routing_middleware,
        tool_invocation_context_middleware=tool_invocation_context_middleware,
        tool_output_middleware=tool_output_middleware,
        memory=memory,
    )

    agent = create_agent(
        model,
        system_prompt=resolved_system_prompt,
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
                "recursion_limit": AGENT_GRAPH_RECURSION_LIMIT,
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
    session_subagent_service: SessionSubagentProtocol | None = None,
    team_service: TeamCoordinationProtocol | None = None,
    sender_agent_id: str | None = None,
    enabled_tool_names: set[str] | None = None,
    enabled_runtime_middleware_names: set[str] | None = None,
    tool_denylist: set[str] | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    terminal_manager_client: TerminalManagerClient | None = None,
    browser_manager_client: BrowserManagerClient | None = None,
    session_context_query_service: SessionContextQueryProtocol | None = None,
    workspace_session_context_client: WorkspaceSessionContextClientProtocol | None = None,
    name: str | None = None,
    override_model: Any = None,
    model_routing_enabled: bool = True,
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
        model_routing_middleware=runtime["model_routing"]
        if model_routing_enabled and override_model is None
        else None,
        sender_agent_id=sender_agent_id,
        enabled_tool_names=enabled_tool_names,
        enabled_runtime_middleware_names=enabled_runtime_middleware_names,
        tool_denylist=set(tool_config.get("denylist", [])) | set(tool_denylist or set()),
        custom_tool_specs=custom_tool_specs,
        name=name or agent_id,
        background_task_registry=background_task_registry,
        background_message_bus=background_message_bus,
        job_event_bus=job_event_bus,
        job_service=job_service,
        message_service=message_service,
        session_service=session_service,
        session_orchestrator=session_orchestrator,
        session_subagent_service=session_subagent_service,
        team_service=team_service,
        terminal_manager_client=terminal_manager_client,
        browser_manager_client=browser_manager_client,
        session_context_query_service=session_context_query_service,
        workspace_session_context_client=workspace_session_context_client,
        config_service=service,
    )
