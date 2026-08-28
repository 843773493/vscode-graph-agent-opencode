from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from deepagents.middleware.permissions import FilesystemPermission
from deepagents.middleware.skills import append_to_system_message
from langchain.agents import create_agent
from langchain.agents.middleware import InterruptOnConfig
from langchain.agents.middleware.types import AgentMiddleware
from langchain.messages import SystemMessage
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver

from app.abstractions.job_event_bus import JobEventBusProtocol
from app.abstractions.job_service import JobServiceProtocol
from app.abstractions.session_context import (
    SessionContextQueryProtocol,
    WorkspaceSessionContextClientProtocol,
)
from app.abstractions.session_message import SessionMessageDeliveryProtocol
from app.abstractions.session_subagent import SessionSubagentProtocol
from app.abstractions.session_target import SessionTargetResolverProtocol
from app.abstractions.team import TeamCoordinationProtocol
from app.agents.agent_tools import build_default_tools
from app.agents.custom_tools import build_custom_tool_bundle
from app.agents.deep_agent_stack import (
    build_deep_agent_middleware,
)
from app.agents.llm_logging_middleware import LLMLoggingMiddleware
from app.agents.middleware_prompts import TEAM_COORDINATION_SYSTEM_PROMPT
from app.agents.model_capability_routing import (
    CapabilityRoutingMiddleware,
    build_provider_model_candidate,
)
from app.agents.policy import (
    ToolMetadata,
    ToolPolicyResolver,
    catalog_group_for_tool,
    custom_tool_spec_names,
    parse_custom_tool_specs,
    validate_tool_dependencies,
)
from app.agents.provider_api_mode import parse_provider_api_mode
from app.agents.skill_runtime import (
    append_skill_middlewares,
    discover_workspace_skill_sources,
    resolve_bundled_skill_groups,
)
from app.agents.tool_invocation_context import (
    ToolInvocationContext,
    ToolInvocationContextMiddleware,
)
from app.agents.tool_output_middleware import ToolOutputMiddleware
from app.agents.tools.custom_invocation import create_custom_tool_invoker_tool
from app.agents.workspace_backend import build_workspace_backend
from app.core.background_message_bus import BackgroundMessageBus
from app.core.background_task_registry import BackgroundTaskRegistry
from app.services.infrastructure.browser_manager_client import BrowserManagerClient
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.node_debug_service import NodeDebugService
from app.services.infrastructure.resource_manager import ResourceManager
from app.services.infrastructure.terminal_manager_client import TerminalManagerClient
from app.services.infrastructure.tool_output_store import ToolOutputStore

if TYPE_CHECKING:
    from app.services.business.message_service import MessageService
    from app.services.business.session_goal_service import SessionGoalService
    from app.services.business.session_service import SessionService


AGENT_GRAPH_RECURSION_LIMIT = 9999
PROVIDER_REQUEST_OPTION_KEYS = {"overrides", "default_headers"}


def _tool_metadata(
    tool: BaseTool,
    *,
    origin: str,
    group_id: str | None = None,
) -> ToolMetadata:
    """把运行时工具映射到策略使用的稳定元数据。"""

    tool_name = tool.name
    metadata = dict(getattr(tool, "metadata", None) or {})
    mcp_server_id = metadata.get("mcp_server_id")
    if origin == "mcp" and isinstance(mcp_server_id, str) and mcp_server_id:
        return ToolMetadata(
            tool_id=tool_name,
            origin="mcp",
            kind="extension",
            group_id=f"mcp:{mcp_server_id}",
        )
    group = catalog_group_for_tool(tool_name)
    return ToolMetadata(
        tool_id=tool_name,
        origin=origin,
        kind=(
            group.kind
            if group.kind != "default"
            else ("default" if origin == "builtin" else "extension")
        ),
        group_id=group_id or group.group_id,
    )


def _resolve_tool_policy(
    resolver: ToolPolicyResolver,
    tool: BaseTool,
    *,
    origin: str,
    execution_overrides: Mapping[str, bool],
    model_visibility_overrides: Mapping[str, bool],
    group_id: str | None = None,
):
    return resolver.resolve(
        _tool_metadata(tool, origin=origin, group_id=group_id),
        execution_override=execution_overrides.get(tool.name),
        model_visibility_override=model_visibility_overrides.get(tool.name),
    )


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


def build_model_from_provider(
    provider: dict[str, Any],
    runtime_config: dict[str, Any],
    *,
    prompt_cache_key: str | None = None,
) -> Any:
    """从单个 provider 配置构建模型实例。"""
    api_mode = parse_provider_api_mode(provider)
    custom_llm_provider = provider.get("custom_llm_provider")
    if not isinstance(custom_llm_provider, str) or not custom_llm_provider:
        raise ValueError(
            f"provider {provider.get('id') or provider.get('model')!r} "
            "缺少 llm.providers[].custom_llm_provider 配置"
        )

    if custom_llm_provider == "chatgpt":
        from app.runtime.chatgpt_auth import (
            configure_litellm_chatgpt_auth_directory,
            ensure_chatgpt_oauth_ready,
            ensure_litellm_chatgpt_model_capabilities,
            is_chatgpt_oauth_provider,
        )

        if not is_chatgpt_oauth_provider(provider):
            raise ValueError(
                "ChatGPT provider 必须配置 auth.type='oauth' 和 "
                "auth.method='chatgpt'"
            )
        if api_mode.protocol != "responses":
            raise ValueError(
                "ChatGPT OAuth provider 必须配置 api_mode.protocol='responses'"
            )
        token_dir = configure_litellm_chatgpt_auth_directory()
        ensure_chatgpt_oauth_ready(token_dir)
        ensure_litellm_chatgpt_model_capabilities(provider["model"])

    request_options = _get_provider_request_options(provider)
    if api_mode.protocol == "responses":
        from app.agents.providers.openai_responses import build_openai_responses_model

        return build_openai_responses_model(
            provider=provider,
            runtime_config=runtime_config,
            request_options=request_options,
            prompt_cache_key=prompt_cache_key,
        )
    if api_mode.protocol == "anthropic_messages":
        if custom_llm_provider != "anthropic":
            raise ValueError(
                "Anthropic Messages provider 必须配置 "
                "custom_llm_provider='anthropic'"
            )
        from app.agents.providers.anthropic_messages import (
            build_anthropic_messages_model,
        )

        return build_anthropic_messages_model(
            provider=provider,
            runtime_config=runtime_config,
            request_options=request_options,
        )
    if api_mode.protocol != "chat_completions":
        raise ValueError(f"provider.api_mode.protocol 不受支持: {api_mode.protocol!r}")

    from app.agents.providers.litellm_chat import build_litellm_chat_model

    return build_litellm_chat_model(
        provider=provider,
        runtime_config=runtime_config,
        request_options=request_options,
        prompt_cache_key=prompt_cache_key,
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


def build_runtime_for_agent(
    agent_id: str,
    config_service: ConfigService | None = None,
    *,
    prompt_cache_key: str | None = None,
    preferred_provider_id: str | None = None,
) -> dict[str, Any]:
    if config_service is None:
        raise RuntimeError("build_runtime_for_agent 需要显式传入 ConfigService")
    service = config_service
    runtime_config = service.get_agent_runtime_config(
        agent_id,
        preferred_provider_id=preferred_provider_id,
    )
    providers = runtime_config["providers"]

    candidates = []
    for provider in providers:
        model = build_model_from_provider(
            provider,
            runtime_config,
            prompt_cache_key=prompt_cache_key,
        )
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
    custom_tool_confirmation_names: frozenset[str] = frozenset(),
    execution_overrides: Mapping[str, bool] | None = None,
    model_visibility_overrides: Mapping[str, bool] | None = None,
    debug: bool = False,
    name: str | None = None,
    background_task_registry: BackgroundTaskRegistry | None = None,
    background_message_bus: BackgroundMessageBus | None = None,
    job_event_bus: JobEventBusProtocol | None = None,
    job_service: JobServiceProtocol | None = None,
    goal_service: SessionGoalService | None = None,
    message_service: MessageService | None = None,
    session_service: SessionService | None = None,
    session_orchestrator: object | None = None,
    session_subagent_service: SessionSubagentProtocol | None = None,
    team_service: TeamCoordinationProtocol | None = None,
    config_service: ConfigService | None = None,
    terminal_manager_client: TerminalManagerClient | None = None,
    browser_manager_client: BrowserManagerClient | None = None,
    node_debug_service: NodeDebugService | None = None,
    session_context_query_service: SessionContextQueryProtocol | None = None,
    workspace_session_context_client: WorkspaceSessionContextClientProtocol | None = None,
    session_target_resolver: SessionTargetResolverProtocol | None = None,
    session_message_delivery_service: SessionMessageDeliveryProtocol | None = None,
    mcp_tools: Sequence[BaseTool] | None = None,
    tool_timeout_seconds: float | None = None,
    resource_manager: ResourceManager | None = None,
    workspace_root: Path,
) -> Any:
    if checkpointer is None:
        raise RuntimeError("create_my_deep_agent 需要显式传入 checkpointer")
    if config_service is None:
        raise RuntimeError("create_my_deep_agent 需要显式传入 ConfigService")

    resolved_sender_agent_id = sender_agent_id or agent_id
    resolved_tool_denylist = set(tool_denylist or set())
    resolved_execution_overrides = dict(execution_overrides or {})
    resolved_model_visibility_overrides = dict(model_visibility_overrides or {})
    policy_resolver = config_service.get_tool_policy_resolver(agent_id)
    tool_invocation_context = ToolInvocationContext(
        tool_timeout_seconds=tool_timeout_seconds,
        resource_manager=resource_manager,
    )

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
    if session_context_query_service is None:
        raise RuntimeError("create_my_deep_agent 需要显式传入 SessionContextQueryService")
    if workspace_session_context_client is None:
        raise RuntimeError("create_my_deep_agent 需要显式传入 WorkspaceSessionContextClient")

    workspace_root = workspace_root.resolve()

    hidden_direct_tool_names: set[str] = set()
    extension_confirmation_names: set[str] = set()
    direct_confirmation_names: set[str] = set()
    extension_policies: dict[str, object] = {}
    if tools is not None:
        resolved_tools = []
        for tool in tools:
            if not isinstance(tool, BaseTool):
                resolved_tools.append(tool)
                continue
            policy = _resolve_tool_policy(
                policy_resolver,
                tool,
                origin="builtin",
                execution_overrides=resolved_execution_overrides,
                model_visibility_overrides=resolved_model_visibility_overrides,
            )
            if not policy.execution_enabled:
                continue
            resolved_tools.append(tool)
            if not policy.model_visible:
                hidden_direct_tool_names.add(tool.name)
            if policy.confirmation_required:
                direct_confirmation_names.add(tool.name)
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
            goal_service=goal_service,
            message_service=message_service,
            session_service=session_service,
            session_orchestrator=session_orchestrator,
            session_subagent_service=session_subagent_service,
            team_service=team_service,
            config_service=config_service,
            terminal_manager_client=terminal_manager_client,
            invocation_context=tool_invocation_context,
            workspace_root=workspace_root,
            session_message_delivery_service=session_message_delivery_service,
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
            session_target_resolver=session_target_resolver,
            session_orchestrator=session_orchestrator,
            config_service=config_service,
            terminal_manager_client=terminal_manager_client,
            browser_manager_client=browser_manager_client,
            invocation_context=tool_invocation_context,
            node_debug_service=node_debug_service,
        )
        custom_specs_by_name = {
            spec.name: spec
            for spec in parse_custom_tool_specs(
                custom_tool_specs or [],
                context=f"agent {agent_id} 的 tools.custom",
            )
        }
        custom_tools = []
        for tool in custom_tool_bundle.tools:
            if tool.name in resolved_tool_denylist:
                continue
            spec = custom_specs_by_name.get(tool.name)
            group_id = None
            if spec is not None:
                module_name = spec.factory_path.split(":", 1)[0].rsplit(".", 1)[-1]
                known_group = catalog_group_for_tool(tool.name)
                group_id = (
                    known_group.group_id
                    if known_group.kind != "default"
                    else f"extension:{module_name}"
                )
            policy = _resolve_tool_policy(
                policy_resolver,
                tool,
                origin="custom",
                group_id=group_id,
                execution_overrides=resolved_execution_overrides,
                model_visibility_overrides=resolved_model_visibility_overrides,
            )
            extension_policies[tool.name] = policy
            if policy.execution_enabled:
                custom_tools.append(tool)
                if policy.confirmation_required:
                    extension_confirmation_names.add(tool.name)
        mcp_tools_for_agent = []
        for tool in mcp_tools or []:
            if tool.name in resolved_tool_denylist:
                continue
            policy = _resolve_tool_policy(
                policy_resolver,
                tool,
                origin="mcp",
                execution_overrides=resolved_execution_overrides,
                model_visibility_overrides=resolved_model_visibility_overrides,
            )
            extension_policies[tool.name] = policy
            if policy.execution_enabled:
                mcp_tools_for_agent.append(tool)
                if policy.confirmation_required:
                    extension_confirmation_names.add(tool.name)
        extension_tools = [
            *custom_tools,
            *mcp_tools_for_agent,
        ]
        resolved_tools = []
        hidden_direct_tool_names: set[str] = set()
        for tool in visible_tools:
            if tool.name in resolved_tool_denylist:
                continue
            policy = _resolve_tool_policy(
                policy_resolver,
                tool,
                origin="builtin",
                execution_overrides=resolved_execution_overrides,
                model_visibility_overrides=resolved_model_visibility_overrides,
            )
            if not policy.execution_enabled:
                continue
            resolved_tools.append(tool)
            if not policy.model_visible:
                hidden_direct_tool_names.add(tool.name)
            if policy.confirmation_required:
                direct_confirmation_names.add(tool.name)
        if extension_tools:
            resolved_tools.append(
                create_custom_tool_invoker_tool(
                    extension_tools,
                    model_visible_tool_names={
                        tool.name
                        for tool in extension_tools
                        if _resolve_tool_policy(
                            policy_resolver,
                            tool,
                            origin=(
                                "mcp"
                                if dict(getattr(tool, "metadata", None) or {}).get(
                                    "mcp_server_id"
                                )
                                else "custom"
                            ),
                            execution_overrides=resolved_execution_overrides,
                            model_visibility_overrides=resolved_model_visibility_overrides,
                        ).model_visible
                    },
                    is_tool_execution_enabled=lambda target: bool(
                        getattr(
                            extension_policies.get(target.name),
                            "execution_enabled",
                            False,
                        )
                    ),
                )
            )
    if enabled_tool_names is not None:
        resolved_tools = [tool for tool in resolved_tools if getattr(tool, "name", "") in enabled_tool_names]
    resolved_interrupt_on = dict(interrupt_on or {})
    resolved_interrupt_on.update(
        {tool_name: True for tool_name in direct_confirmation_names}
    )
    resolved_tool_names = {
        getattr(tool, "name", "")
        for tool in resolved_tools
    }
    validate_tool_dependencies(
        resolved_tool_names,
        context=f"agent {agent_id} 的运行时工具策略",
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

    resolved_bundled_skill_groups = resolve_bundled_skill_groups()
    resolved_skills = (
        discover_workspace_skill_sources(
            workspace_root,
            bundled_skill_groups=resolved_bundled_skill_groups,
        )
        if skills is None
        else list(skills)
    )
    backend = build_workspace_backend(
        workspace_root,
        bundled_skill_groups=resolved_bundled_skill_groups,
    )
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
        interrupt_on=resolved_interrupt_on,
        runtime_middleware=runtime_middleware,
        model_routing_middleware=model_routing_middleware,
        tool_invocation_context_middleware=tool_invocation_context_middleware,
        tool_output_middleware=tool_output_middleware,
        memory=memory,
        custom_tool_confirmation_names=frozenset(
            set(custom_tool_confirmation_names) | extension_confirmation_names
        ),
        model_hidden_tool_names=frozenset(hidden_direct_tool_names),
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
    goal_service: SessionGoalService | None = None,
    message_service: MessageService | None = None,
    session_service: SessionService | None = None,
    session_orchestrator: object | None = None,
    session_subagent_service: SessionSubagentProtocol | None = None,
    team_service: TeamCoordinationProtocol | None = None,
    sender_agent_id: str | None = None,
    enabled_tool_names: set[str] | None = None,
    enabled_runtime_middleware_names: set[str] | None = None,
    tool_denylist: set[str] | None = None,
    execution_overrides: Mapping[str, bool] | None = None,
    model_visibility_overrides: Mapping[str, bool] | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    terminal_manager_client: TerminalManagerClient | None = None,
    browser_manager_client: BrowserManagerClient | None = None,
    node_debug_service: NodeDebugService | None = None,
    session_context_query_service: SessionContextQueryProtocol | None = None,
    workspace_session_context_client: WorkspaceSessionContextClientProtocol | None = None,
    session_target_resolver: SessionTargetResolverProtocol | None = None,
    session_message_delivery_service: SessionMessageDeliveryProtocol | None = None,
    mcp_tools: Sequence[BaseTool] | None = None,
    name: str | None = None,
    override_model: Any = None,
    model_routing_enabled: bool = True,
    preferred_provider_id: str | None = None,
    tool_timeout_seconds: float | None = None,
    resource_manager: ResourceManager | None = None,
    workspace_root: Path,
):
    if config_service is None:
        raise RuntimeError("create_runtime_deep_agent_for_session 需要显式传入 ConfigService")
    service = config_service
    runtime = build_runtime_for_agent(
        agent_id=agent_id,
        config_service=service,
        prompt_cache_key=session_id,
        preferred_provider_id=preferred_provider_id,
    )
    tool_config = service.get_agent_tool_config(agent_id)
    tool_policy = service.resolve_agent_tool_policy(agent_id)
    confirmation_tool_names = (
        service.resolve_agent_confirmation_tool_names(agent_id)
        & tool_policy.enabled_names
    )
    custom_tool_specs = list(tool_config.get("custom", []))
    configured_custom_tool_names = custom_tool_spec_names(
        custom_tool_specs,
        context=f"agent {agent_id} 的 tools.custom",
    )
    custom_tool_confirmation_names = (
        confirmation_tool_names & configured_custom_tool_names
    )
    direct_confirmation_tool_names = (
        confirmation_tool_names - configured_custom_tool_names
    )

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
        tool_denylist=set(tool_denylist or set()),
        execution_overrides=execution_overrides,
        model_visibility_overrides=model_visibility_overrides,
        custom_tool_specs=custom_tool_specs,
        name=name or agent_id,
        background_task_registry=background_task_registry,
        background_message_bus=background_message_bus,
        job_event_bus=job_event_bus,
        job_service=job_service,
        goal_service=goal_service,
        message_service=message_service,
        session_service=session_service,
        session_orchestrator=session_orchestrator,
        session_subagent_service=session_subagent_service,
        team_service=team_service,
        terminal_manager_client=terminal_manager_client,
        browser_manager_client=browser_manager_client,
        node_debug_service=node_debug_service,
        session_context_query_service=session_context_query_service,
        workspace_session_context_client=workspace_session_context_client,
        session_target_resolver=session_target_resolver,
        session_message_delivery_service=session_message_delivery_service,
        mcp_tools=mcp_tools,
        tool_timeout_seconds=tool_timeout_seconds,
        resource_manager=resource_manager,
        interrupt_on={tool_name: True for tool_name in direct_confirmation_tool_names},
        custom_tool_confirmation_names=custom_tool_confirmation_names,
        config_service=service,
        workspace_root=workspace_root,
    )
