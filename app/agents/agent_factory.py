from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from deepagents.middleware.permissions import FilesystemPermission
from langchain.agents.middleware import InterruptOnConfig
from langchain.agents.middleware.types import AgentMiddleware
from langchain.agents.structured_output import ResponseFormat
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver

from app.abstractions.communication import SessionCommunicationProtocol
from app.abstractions.job_event_bus import JobEventBusProtocol
from app.abstractions.job_service import JobServiceProtocol
from app.abstractions.resource_file_content import ResourceFileContent
from app.abstractions.session_context import (
    SessionContextQueryProtocol,
    WorkspaceSessionContextClientProtocol,
)
from app.abstractions.session_subagent import SessionSubagentProtocol
from app.abstractions.session_target import SessionTargetResolverProtocol
from app.abstractions.team import TeamCoordinationProtocol
from app.abstractions.tool_selection import ToolSelectionReader
from app.agents.agent_tools import build_default_tools
from app.agents.custom_tools import build_custom_tool_bundle
from app.agents.deep_agent_stack import (
    build_deep_agent_middleware,
)
from app.agents.graph_binding import (
    DEEP_AGENT_GRAPH_BINDING,
    GRAPH_FACTORY_REGISTRY,
    GraphBindingOwnerKey,
    GraphBindingStorePort,
    resolve_graph_binding,
)
from app.agents.instruction_producers import (
    InstructionProducerSpec,
    register_instruction_producer,
    runtime_instruction_producers,
)
from app.agents.llm_logging_middleware import LLMLoggingMiddleware
from app.agents.middleware_prompts import TEAM_COORDINATION_SYSTEM_PROMPT
from app.agents.model_call_execution import ModelCallExecution
from app.agents.model_call_graph import create_model_call_agent
from app.agents.model_capability_routing import (
    CapabilityRoutingMiddleware,
    build_provider_model_candidate,
)
from app.agents.model_tool_schema import export_model_tool_json_schema
from app.agents.policy import (
    ToolMetadata,
    ToolPolicyResolver,
    catalog_group_for_tool,
    custom_tool_spec_names,
    parse_custom_tool_specs,
    validate_tool_dependencies,
)
from app.agents.policy.tool_capability import metadata_for_tool
from app.agents.provider_api_mode import parse_provider_api_mode
from app.agents.sealed_assembly_dispatch import ProviderInvocationPurpose
from app.agents.skill_runtime import (
    PublishedSkillCatalog,
    append_skill_middlewares,
    build_workspace_skill_catalog,
)
from app.agents.summarization.preflight import NoDurableOwnerCompactionPreflight
from app.agents.tool_identity import TOOL_DISCOVERY_NAME, tool_definition_name
from app.agents.tool_invocation_context import (
    ThreadRuntimeBinding,
    ToolInvocationContext,
    ToolInvocationContextMiddleware,
)
from app.agents.tool_output_middleware import ToolOutputMiddleware
from app.agents.tool_set_producer import RegisteredToolSetProducer
from app.agents.tools.custom_invocation import (
    create_extension_tool_invoker_tool,
    seal_extension_catalog_binding_from_tools,
)
from app.agents.tools.skill_loading import create_skill_load_tool
from app.agents.tools.tool_discovery import create_search_tools_tool
from app.agents.workspace_backend import build_workspace_backend
from app.core.background_message_bus import BackgroundMessageBus
from app.core.background_task_registry import BackgroundTaskRegistry
from app.core.lifecycle import LifetimeScope
from app.core.phase_observer import observe_phase_sync
from app.core.resource_uri import parse_session_artifact_uri
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.services.business.browser_resources.owner import BrowserResourceOwner
from app.services.business.terminal_resources.owner import TerminalResourceOwner
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.events.channel_events import (
    ContextSourceEvent,
    ContextSourceEventPublisher,
)
from app.services.infrastructure.mcp.catalog_activation import (
    McpCatalogResourcePublisher,
)
from app.services.infrastructure.mcp.catalog_owner import McpCatalogOwner
from app.services.infrastructure.mcp.extension_catalog import (
    ExtensionCatalogBindingError,
    ExtensionCatalogUnavailableError,
    ExtensionTargetResolutionError,
    extension_schema_hash,
)
from app.services.infrastructure.mcp.tool_definitions import (
    FrozenToolCatalog,
    FrozenToolDefinition,
    tool_catalog_from_activation,
)
from app.services.infrastructure.node_debug.service import NodeDebugService
from app.services.infrastructure.resource_platform.registry.context_source_reactor import (
    ContextSourceReactor,
)
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.infrastructure.resource_platform.sources.resource_navigation import (
    ResourceNavigation,
)
from app.services.infrastructure.resource_platform.sources.workspace_file_resources import (
    WorkspaceFileResourceRegistry,
)
from app.services.infrastructure.rollout_context.activation.common import (
    ResourceActivationStoreError,
)
from app.services.infrastructure.rollout_context.checkpoint.boundary.compaction_boundary_adapter import (
    CompactionPreflightPort,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceControlStatePort,
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.models import (
    SkillCatalogSnapshotConflict,
    SkillLoadMode,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.skill_catalog import (
    skill_catalog_from_activation,
)
from app.services.infrastructure.tool_output_store import ToolOutputStore
from app.services.infrastructure.workspace_files.text_owner import (
    WorkspaceTextMutationOwner,
)
from app.services.orchestration.resource_activation import ResourceActivationCoordinator

if TYPE_CHECKING:
    from app.services.business.message_service import MessageService
    from app.services.business.session_goal_service import SessionGoalService
    from app.services.business.session_service import SessionService


AGENT_GRAPH_RECURSION_LIMIT = 9999
PROVIDER_REQUEST_OPTION_KEYS = {"overrides", "default_headers", "no_proxy"}
logger = logging.getLogger(__name__)


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
        metadata_for_tool(tool, origin=origin, group_id=group_id),
        execution_override=execution_overrides.get(tool.name),
        model_visibility_override=model_visibility_overrides.get(tool.name),
    )


def build_model_from_provider(
    provider: dict[str, Any],
    runtime_config: dict[str, Any],
    *,
    prompt_cache_key: str | None = None,
    invocation_purpose: ProviderInvocationPurpose = "agent",
) -> Any:
    """从 provider 构建模型；直连用途由实际调用方显式给出，默认必须封存。"""
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

        model = build_openai_responses_model(
            provider=provider,
            runtime_config=runtime_config,
            request_options=request_options,
            prompt_cache_key=prompt_cache_key,
            invocation_purpose=invocation_purpose,
        )
    else:
        if api_mode.protocol == "anthropic_messages" and custom_llm_provider != "anthropic":
            raise ValueError(
                "Anthropic Messages provider 必须配置 "
                "custom_llm_provider='anthropic'"
            )
        if api_mode.protocol not in {"chat_completions", "anthropic_messages"}:
            raise ValueError(f"provider.api_mode.protocol 不受支持: {api_mode.protocol!r}")

        from app.agents.providers.litellm_chat import build_litellm_chat_model

        model = build_litellm_chat_model(
            provider=provider,
            runtime_config=runtime_config,
            request_options=request_options,
            prompt_cache_key=prompt_cache_key,
            invocation_purpose=invocation_purpose,
        )
    if api_mode.model_info.max_input_tokens is not None:
        # 容量属于模型元数据，不向上游发送，也不据此设置输出上限。
        model.profile = {
            **(model.profile or {}),
            "max_input_tokens": api_mode.model_info.max_input_tokens,
        }
    return model


def provider_configuration_error(
    provider: dict[str, Any],
    runtime_config: dict[str, Any],
) -> str | None:
    """返回单个 provider 的可展示配置错误，不让坏配置阻断 Agent 列表。"""
    try:
        build_model_from_provider(provider, runtime_config)
    except Exception as error:  # noqa: BLE001 - 配置错误需要逐 provider 隔离展示
        return str(error) or type(error).__name__
    return None


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
    no_proxy = request_options.get("no_proxy", False)
    if not isinstance(no_proxy, bool):
        raise TypeError("provider.request_options.no_proxy 必须是布尔值")
    return {
        "overrides": dict(overrides),
        "default_headers": dict(default_headers),
        "no_proxy": no_proxy,
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
    for index, provider in enumerate(providers):
        try:
            model = build_model_from_provider(
                provider,
                runtime_config,
                prompt_cache_key=prompt_cache_key,
            )
        except Exception as error:
            provider_id = str(provider.get("id") or provider.get("model") or "<unknown>")
            if index == 0:
                raise RuntimeError(
                    f"当前选择的模型配置不可用: provider_id={provider_id}; {error}"
                ) from error
            logger.exception(
                "跳过配置不可用的 fallback provider: provider_id=%s",
                provider_id,
            )
            continue
        candidates.append(
            build_provider_model_candidate(provider=provider, model=model)
        )

    if not candidates:
        raise RuntimeError("未能构建任何模型实例")

    return {
        "model": candidates[0].model,
        "model_routing": CapabilityRoutingMiddleware(candidates),
        "instruction_producers": runtime_instruction_producers(
            agent_id=agent_id, content=runtime_config["system_prompt"],
            config_revision=service.get_revision(),
            runtime_identity_description=runtime_config["runtime_identity_description"],
            provider_candidates=[
                (candidate.provider_id, candidate.model_id)
                for candidate in candidates
            ],
        ),
    }


def resolve_agent_id(agent_id: str | None, config_service: ConfigService | None = None) -> str:
    if config_service is None:
        raise RuntimeError("resolve_agent_id 需要显式传入 ConfigService")
    service = config_service
    return service.resolve_agent_id(agent_id)


# 编译函数只装配一个 generation 的图与纯 thread 依赖；复用与释放归 tracker 所有。

def create_my_deep_agent(
    *,
    model: BaseChatModel,
    instruction_producers: Sequence[InstructionProducerSpec],
    tool_selection_reader: ToolSelectionReader,
    checkpointer: BaseCheckpointSaver | None = None,
    session_id: str,
    thread_id: str,
    agent_id: str,
    model_routing_middleware: CapabilityRoutingMiddleware | None = None,
    sender_agent_id: str | None = None,
    enabled_tool_names: set[str] | None = None,
    enabled_runtime_middleware_names: set[str] | None = None,
    tool_denylist: set[str] | None = None,
    custom_tool_specs: Sequence[object] | None = None,
    tools: Sequence[BaseTool | Callable[..., Any] | dict[str, Any]] | None = None,
    middleware: Sequence[AgentMiddleware] | None = None,
    response_format: ResponseFormat[Any] | type | dict[str, Any] | None = None,
    skill_catalog: PublishedSkillCatalog | None = None,
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
    terminal_resource_owner: TerminalResourceOwner | None = None,
    browser_resource_owner: BrowserResourceOwner | None = None,
    node_debug_service: NodeDebugService | None = None,
    session_context_query_service: SessionContextQueryProtocol | None = None,
    workspace_session_context_client: WorkspaceSessionContextClientProtocol
    | None = None,
    session_target_resolver: SessionTargetResolverProtocol | None = None,
    session_communication_service: SessionCommunicationProtocol | None = None,
    mcp_tools: Sequence[BaseTool] | None = None,
    mcp_catalog_owner: McpCatalogOwner | None = None,
    tool_timeout_seconds: float | None = None,
    workspace_file_resource_registry: WorkspaceFileResourceRegistry | None = None,
    reactor_lifetime_scope: LifetimeScope | None = None,
    resource_registry: ResourceRegistry,
    resource_activation: ResourceActivationCoordinator | None,
    workspace_root: Path,
    resource_navigation: ResourceNavigation,
    text_mutations: WorkspaceTextMutationOwner,
    include_team_tools: bool = False,
    owns_thread: bool = True,
) -> Any:
    if checkpointer is None:
        raise RuntimeError("create_my_deep_agent 需要显式传入 checkpointer")
    if config_service is None:
        raise RuntimeError("create_my_deep_agent 需要显式传入 ConfigService")
    if session_service is None:
        raise RuntimeError("create_my_deep_agent 需要显式传入 SessionService")

    resolved_sender_agent_id = sender_agent_id or agent_id
    resolved_tool_denylist = set(tool_denylist or set())
    resolved_execution_overrides = dict(execution_overrides or {})
    resolved_model_visibility_overrides = dict(model_visibility_overrides or {})

    policy_resolver = config_service.get_tool_policy_resolver(agent_id)
    thread_binding = (
        ThreadRuntimeBinding(
            session_id=session_id,
            thread_id=thread_id,
        )
        if owns_thread
        else None
    )
    tool_invocation_context = ToolInvocationContext(
        tool_timeout_seconds=tool_timeout_seconds,
        thread_binding=thread_binding,
    )

    if background_task_registry is None:
        raise RuntimeError("create_my_deep_agent 需要显式传入 BackgroundTaskRegistry")
    if background_message_bus is None:
        raise RuntimeError("create_my_deep_agent 需要显式传入 BackgroundMessageBus")
    if job_event_bus is None:
        raise RuntimeError("create_my_deep_agent 需要显式传入 JobEventBus")
    if message_service is None:
        raise RuntimeError("create_my_deep_agent 需要显式传入 MessageService")
    if session_orchestrator is None:
        raise RuntimeError("create_my_deep_agent 需要显式传入 SessionOrchestrator")
    if session_subagent_service is None:
        raise RuntimeError("create_my_deep_agent 需要显式传入 SessionSubagentService")
    if include_team_tools and team_service is None:
        raise RuntimeError("create_my_deep_agent 需要显式传入 TeamCoordinationService")
    if job_service is None:
        raise RuntimeError("create_my_deep_agent 需要显式传入 JobService")
    if session_context_query_service is None:
        raise RuntimeError("create_my_deep_agent 需要显式传入 SessionContextQueryService")
    if workspace_session_context_client is None:
        raise RuntimeError("create_my_deep_agent 需要显式传入 WorkspaceSessionContextClient")

    workspace_root = workspace_root.resolve()

    # 合成工具目录只构建固定 schema；只有真实 thread 消费 Skill catalog。
    resolved_skills = None
    if owns_thread:
        resolved_skills = (
            build_workspace_skill_catalog(
                workspace_root,
                registry=resource_registry,
                file_registry=workspace_file_resource_registry,
            )
            if skill_catalog is None and workspace_file_resource_registry is not None
            else skill_catalog
        )
        if resolved_skills is None:
            raise RuntimeError(
                "构建 Workspace Skill catalog 需要 WorkspaceFileResourceRegistry"
            )
    tool_output_store = ToolOutputStore(workspace_root=workspace_root)

    def read_artifact(file_id: str, max_bytes: int) -> ResourceFileContent:
        binding = tool_invocation_context.thread_binding
        if not owns_thread or binding is None:
            raise PermissionError("产物读取缺少当前 Agent SessionThread")
        parsed = parse_session_artifact_uri(file_id)
        if parsed.tail_segments[0] != binding.session_id:
            raise PermissionError("产物 URI 不属于当前 Agent SessionThread")
        if parsed.session_artifact_kind == "attachments":
            if not isinstance(checkpointer, RolloutCheckpointSaver):
                raise PermissionError("附件读取缺少原 checkpoint owner")
            return checkpointer.read_tool_call_attachment(
                binding.session_id, thread_id=binding.thread_id, file_id=file_id,
                tool_call_id=tool_invocation_context.require_tool_call_id(),
                model_call_id=tool_invocation_context.require_model_call_id(),
            )
        return tool_output_store.read(binding.session_id, file_id, max_bytes)

    backend = build_workspace_backend(workspace_root)
    # CSM 的控制状态必须经唯一 RolloutCheckpointSaver/ContextStore owner 持久化。
    # 生产 checkpointer 就是该 owner；其它（测试替身）checkpointer 没有
    # ContextStore，只能退化为纯内存 CSM。合成 session（例如工具清单检查用的
    # tools_inspection_session）没有权威会话节点，同样没有 ContextStore，
    # 不允许为它伪造持久化 owner。
    context_source_owner: ContextSourceOwnerKey | None = None
    context_source_control_port: ContextSourceControlStatePort | None = None
    if isinstance(checkpointer, RolloutCheckpointSaver):
        candidate_owner = ContextSourceOwnerKey(
            session_id=session_id,
            thread_id=thread_id,
        )
        if checkpointer.context_source_control_owner_available(candidate_owner):
            context_source_owner = candidate_owner
            context_source_control_port = checkpointer
    # compaction preflight 端口：生产 checkpointer 即唯一 Saver owner；
    # 测试替身按合成装配合同拿到显式失败端口，禁止静默跳过。
    compaction_preflight: CompactionPreflightPort = (
        checkpointer
        if isinstance(checkpointer, RolloutCheckpointSaver)
        else NoDurableOwnerCompactionPreflight()
    )
    # OpenSpec 3.8-C：CSM 的 commit/untrack 成功边界发布 context.source/* 轻量
    # 内存事件。CSM 边界拿不到 workspace_id，channel 参数使用最近稳定 identity
    # session_id（与 CSM owner 一致）；事件服务复用文件来源 registry 的进程级
    # channel 服务（ResourcePlatformBootstrap 落地后应由 bootstrap 持有并注入）。
    # 来源确认事件只在真实 Saver seal 成功后发布。
    context_source_event_sink: Callable[[ContextSourceEvent], None] | None = None
    if owns_thread and workspace_file_resource_registry is not None:
        context_source_event_sink = ContextSourceEventPublisher(
            event_service=(
                workspace_file_resource_registry.observation_channel.event_service
            ),
            scope_id=session_id,
        )
    context_source_manager = ContextSourceManager(
        owner=context_source_owner,
        control_state_port=context_source_control_port,
        lifecycle_event_sink=context_source_event_sink,
    )
    # 事件驱动 reaction：reactor 订阅来源 owner 的轻量 change 通知，并把
    # 「有新 revision」标记为 CSM 的 pending observation；before_model 只消费
    # 已排队的 observation，不再遍历 descriptor 轮询内存快照。
    # 订阅必须随 agent 生命周期释放；调用方没有提供 scope 时在代码内留 TODO，
    # 不允许在这里新建第二套 dispose 抽象。
    context_source_reactor: ContextSourceReactor | None = None
    if owns_thread and workspace_file_resource_registry is not None:
        if reactor_lifetime_scope is None:
            raise RuntimeError(
                "启用事件驱动 context source reaction 时必须提供 "
                "reactor_lifetime_scope；否则订阅无法随 agent 生命周期释放"
            )
        context_source_reactor = ContextSourceReactor(
            sources=workspace_file_resource_registry,
            context_sources=context_source_manager,
            lifetime_scope=reactor_lifetime_scope,
            reactor_id=f"agent:{agent_id}:session:{session_id}",
        )
    extension_confirmation_names: set[str] = set()
    direct_confirmation_names: set[str] = set()
    extension_policies: dict[str, object] = {}
    custom_metadata: dict[str, ToolMetadata] = {}
    extension_tools: list[BaseTool] = []
    custom_tools: list[BaseTool] = []
    extension_invoker: BaseTool | None = None

    def _absorb_builtin_tool(tool: BaseTool) -> None:
        """内置工具的唯一准入路径：策略解析 + 可见性/确认登记。

        显式 ``tools`` 与默认工具集两条来源只在「是否过滤 denylist、是否跳过
        非 BaseTool 项」上不同，策略准入与登记必须共用这一条实现。
        """
        policy = _resolve_tool_policy(
            policy_resolver,
            tool,
            origin="builtin",
            execution_overrides=resolved_execution_overrides,
            model_visibility_overrides=resolved_model_visibility_overrides,
        )
        if not policy.execution_enabled:
            return
        resolved_tools.append(tool)
        if policy.confirmation_required:
            direct_confirmation_names.add(tool.name)

    if tools is not None:
        resolved_tools = []
        for tool in tools:
            if not isinstance(tool, BaseTool):
                resolved_tools.append(tool)
                continue
            _absorb_builtin_tool(tool)
    else:
        if browser_resource_owner is None:
            raise RuntimeError(
                "create_my_deep_agent 构建默认工具集时需要显式传入 BrowserResourceOwner"
            )
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
            terminal_resource_owner=terminal_resource_owner,
            invocation_context=tool_invocation_context,
            workspace_root=workspace_root,
            session_communication_service=session_communication_service,
            include_test_tools=config_service.development_test_tools_enabled(),
            include_team_tools=include_team_tools,
            filesystem_permissions=permissions,
            text_mutations=text_mutations,
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
            session_communication_service=session_communication_service,
            session_orchestrator=session_orchestrator,
            config_service=config_service,
            terminal_resource_owner=terminal_resource_owner,
            browser_resource_owner=browser_resource_owner,
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
            custom_metadata[tool.name] = metadata_for_tool(tool, origin="custom", group_id=group_id)
            extension_policies[tool.name] = policy
            custom_tools.append(tool)
            if policy.execution_enabled and policy.confirmation_required:
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
            mcp_tools_for_agent.append(tool)
            if policy.execution_enabled and policy.confirmation_required:
                extension_confirmation_names.add(tool.name)
        extension_tools = [
            *custom_tools,
            *mcp_tools_for_agent,
        ]
        resolved_tools = []
        for tool in visible_tools:
            if tool.name in resolved_tool_denylist:
                continue
            _absorb_builtin_tool(tool)
    # 固定信封即使当前没有可执行 target 也必须存在；target 的启停只改变
    # 封存目录与执行准入，不改变 Provider 工具面。
    target_resolver = None
    skill_snapshot_resolver = None

    def discovery_catalog_resolver() -> FrozenToolCatalog:
        raise ExtensionCatalogUnavailableError("search_tools 需要调用所属的冻结完整目录")

    def extension_context_policies(metadata: Sequence[ToolMetadata]):
        selection = tool_selection_reader.snapshot(agent_id)
        execution_overrides = selection.execution_overrides
        visibility_overrides = selection.model_visibility_overrides
        policies = {}
        for entry in metadata:
            policy = policy_resolver.resolve(
                entry, execution_override=execution_overrides.get(entry.tool_id),
                model_visibility_override=visibility_overrides.get(entry.tool_id),
            )
            if entry.tool_id in resolved_tool_denylist:
                policy = replace(policy, execution_enabled=False, model_visible=False)
            policies[entry.tool_id] = policy
        return policies

    if resource_activation is not None:
        if not isinstance(checkpointer, RolloutCheckpointSaver) or mcp_catalog_owner is None:
            raise RuntimeError("正式 runtime 需要唯一 Saver 与真实 MCP Owner")
        if reactor_lifetime_scope is None:
            raise RuntimeError("正式 MCP publication 订阅需要 generation LifetimeScope")

        def enabled_extension(target: BaseTool) -> bool:
            metadata = target.metadata or {}
            origin = "mcp" if metadata.get("mcp_server_id") else "custom"
            group_id = custom_metadata[target.name].group_id if target.name in custom_metadata else None
            return extension_context_policies((metadata_for_tool(
                target, origin=origin, group_id=group_id,
            ),))[target.name].execution_enabled

        publisher = McpCatalogResourcePublisher(
            registry=resource_registry, custom_tools=custom_tools,
            policy_resolver=extension_context_policies, custom_metadata=custom_metadata,
        )
        unsubscribe = mcp_catalog_owner.subscribe_publication(publisher.publish)
        try:
            reactor_lifetime_scope.register(unsubscribe, label="MCP catalog publication")
        except BaseException:
            unsubscribe()
            raise

        def republish_extension_context() -> None:
            # 与 relist 共用原 Owner 发布锁，不能用锁外读到的旧目录覆盖新 publication。
            with mcp_catalog_owner.publication_lock:
                publisher.publish(mcp_catalog_owner.publication_snapshot())

        unsubscribe_selection = tool_selection_reader.subscribe(agent_id, republish_extension_context)
        try:
            reactor_lifetime_scope.register(unsubscribe_selection, label="extension tool selection publication")
        except BaseException:
            unsubscribe_selection()
            raise

        def resolve_skill_snapshot():
            try:
                snapshot, _model_call_id = checkpointer.load_tool_call_resource_activation(
                    session_id, thread_id=thread_id,
                    tool_call_id=tool_invocation_context.require_tool_call_id(),
                    model_call_id=tool_invocation_context.require_model_call_id(),
                )
                frozen_catalog = skill_catalog_from_activation(
                    snapshot, lambda frozen, binding: checkpointer.read_resource_activation_payload(
                        frozen, binding, storage_owner=(session_id, thread_id),
                    ),
                )
            except (ExtensionCatalogBindingError, ResourceActivationStoreError) as error:
                raise SkillCatalogSnapshotConflict(
                    "skill-catalog-snapshot-conflict: 原 tool-call 的冻结 Skill 正文无法恢复"
                ) from error
            if frozen_catalog is None:
                raise SkillCatalogSnapshotConflict("skill-catalog-snapshot-conflict: 原 tool-call 缺少冻结 Skill catalog")
            return frozen_catalog

        skill_snapshot_resolver = resolve_skill_snapshot

        def discovery_catalog_resolver() -> FrozenToolCatalog:
            snapshot, model_call_id = checkpointer.load_tool_call_resource_activation(
                session_id, thread_id=thread_id,
                tool_call_id=tool_invocation_context.require_tool_call_id(),
                model_call_id=tool_invocation_context.require_model_call_id(),
            )
            return tool_catalog_from_activation(
                snapshot, lambda frozen, binding: checkpointer.read_resource_activation_payload(
                    frozen, binding, storage_owner=(session_id, thread_id),
                ), model_call_id=model_call_id,
            )

        def resolve_catalog():
            return checkpointer.resolve_extension_dispatch_for_tool_call(
                session_id, thread_id=thread_id,
                tool_call_id=tool_invocation_context.require_tool_call_id(),
                model_call_id=tool_invocation_context.require_model_call_id(),
            ).binding_ref

        initial_targets = {tool.name: tool for tool in extension_tools}

        def resolve_target(binding):
            initial = initial_targets.get(binding.target_id)
            if initial is not None and extension_schema_hash(export_model_tool_json_schema(initial)) == binding.schema_hash:
                return initial
            current = mcp_catalog_owner.publication_snapshot().tools.get(binding.target_id)
            if current is None or extension_schema_hash(export_model_tool_json_schema(current)) != binding.schema_hash:
                raise ExtensionTargetResolutionError(
                    f"sealed target 缺少匹配 schema 的执行 handle: {binding.target_id}"
                )
            return current

        target_resolver = resolve_target
        permission_check = enabled_extension
    else:
        resolve_catalog = seal_extension_catalog_binding_from_tools(extension_tools)
        permission_check = lambda target: bool(
            getattr(extension_policies.get(target.name), "execution_enabled", False)
        )
    extension_invoker = create_extension_tool_invoker_tool(
        extension_tools,
        catalog_binding_resolver=resolve_catalog,
        target_resolver=target_resolver,
        is_tool_execution_enabled=permission_check,
        invocation_context=tool_invocation_context,
    )
    resolved_tools.append(extension_invoker)

    def discovery_policies(catalog: FrozenToolCatalog):
        return extension_context_policies(tuple(definition.metadata for definition in catalog.definitions))

    async def write_discovered_definition(definition: FrozenToolDefinition, entry: dict[str, object]):
        call_id = tool_invocation_context.require_tool_call_id()
        return await asyncio.to_thread(
            tool_output_store.materialize, session_id=session_id, tool_name=TOOL_DISCOVERY_NAME,
            tool_call_id=call_id, text=canonical_json_bytes(entry).decode("utf-8"),
            output_key=f"{call_id}:{definition.provenance.resource_id}:{definition.provenance.revision}",
        )

    discovery_tool = create_search_tools_tool(
        catalog_resolver=discovery_catalog_resolver, policy_resolver=discovery_policies,
        definition_writer=write_discovered_definition,
        response_byte_limit=min(tool_output_store.inline_byte_limit, policy_resolver.resolve(
            ToolMetadata(TOOL_DISCOVERY_NAME, "builtin", "default", "default"),
        ).max_result_bytes),
    )
    resolved_tools.append(discovery_tool)
    if (
        context_source_manager is not None
        and (tools is not None or "skill_load" not in resolved_tool_denylist)
        and not any(getattr(tool, "name", None) == "skill_load" for tool in resolved_tools)
    ):
        def skill_control_admission(name: str, mode: SkillLoadMode) -> AbstractAsyncContextManager[None]:
            return session_service.session_context_control_admission(
                session_id, thread_id,
                operation_id=tool_invocation_context.require_operation_id("skill_load"),
                payload={"kind": "skill_load", "name": name, "mode": mode},
                recovery_ref=tool_invocation_context.require_tool_call_id(),
            )

        _absorb_builtin_tool(
            create_skill_load_tool(
                context_source_manager, activation_snapshot_resolver=skill_snapshot_resolver,
                control_admission=skill_control_admission,
            )
        )
    if enabled_tool_names is not None:
        resolved_tools = [tool for tool in resolved_tools if getattr(tool, "name", "") in enabled_tool_names]
        for fixed_tool in (extension_invoker, discovery_tool):
            if fixed_tool not in resolved_tools:
                resolved_tools.append(fixed_tool)
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
    for producer in instruction_producers:
        register_instruction_producer(context_source_manager, producer)
    register_instruction_producer(context_source_manager, InstructionProducerSpec(
        producer_id="R03", source_id="instruction:team-coordination-policy",
        source_kind="team_coordination_policy", name="团队协作规则",
        root_placement="root_eligible", content=TEAM_COORDINATION_SYSTEM_PROMPT,
        revision=sha256_jcs({"content": TEAM_COORDINATION_SYSTEM_PROMPT}),
        policy_key="team_coordination",
    ))

    runtime_middleware: list[AgentMiddleware] = []
    append_skill_middlewares(
        runtime_middleware,
        catalog=None,
    )
    runtime_middleware.extend(
        list(middleware) if middleware is not None else [LLMLoggingMiddleware()]
    )
    if enabled_runtime_middleware_names is not None:
        runtime_middleware = [
            item for item in runtime_middleware if item.__class__.__name__ in enabled_runtime_middleware_names
        ]

    tool_output_middleware = ToolOutputMiddleware(
        session_id=session_id,
        store=tool_output_store,
    )
    tool_invocation_context_middleware = ToolInvocationContextMiddleware(
        tool_invocation_context
    )

    deepagent_middleware = build_deep_agent_middleware(
        model=model,
        backend=backend,
        workspace_root=workspace_root,
        resource_navigation=resource_navigation,
        text_mutations=text_mutations,
        artifact_reader=read_artifact,
        permissions=permissions,
        resolved_skills=resolved_skills,
        execution_owned_skill_catalog=resource_activation is not None,
        compaction_preflight=compaction_preflight,
        context_source_manager=context_source_manager,
        source_registry=(
            workspace_file_resource_registry if owns_thread else None
        ),
        context_source_reactor=context_source_reactor,
        resolved_tool_denylist=resolved_tool_denylist,
        interrupt_on=resolved_interrupt_on,
        runtime_middleware=runtime_middleware,
        model_routing_middleware=model_routing_middleware,
        tool_invocation_context_middleware=tool_invocation_context_middleware,
        tool_output_middleware=tool_output_middleware,
        custom_tool_confirmation_names=frozenset(
            set(custom_tool_confirmation_names) | extension_confirmation_names
        ),
    )

    registered_tools = [
        *resolved_tools,
        *(tool for item in deepagent_middleware for tool in getattr(item, "tools", ())),
    ]
    tool_set_producer = RegisteredToolSetProducer(
        agent_id=agent_id, selection_reader=tool_selection_reader,
        policy_resolver=policy_resolver, tools=registered_tools,
        metadata={
            tool_definition_name(tool): ToolMetadata(
                tool_id=tool_definition_name(tool), origin="builtin",
                kind=catalog_group_for_tool(tool_definition_name(tool)).kind,
                group_id=catalog_group_for_tool(tool_definition_name(tool)).group_id,
            ) for tool in registered_tools
        },
    )
    # routing 和所有 request producer 的真实 attempt 直接调用唯一执行 owner。
    execution = ModelCallExecution(
        checkpointer=checkpointer, session_id=session_id, thread_id=thread_id,
        context_source_manager=context_source_manager,
        source_message_grouping_supplier=config_service.get_source_message_grouping,
        tool_set_producer=tool_set_producer,
        extension_policy_resolver=extension_context_policies,
        resource_activation=resource_activation,
        context_read_admission=lambda: session_service.session_read_admission(session_id),
        context_control_admission=lambda operation_id, payload, recovery_ref: session_service.session_context_control_admission(
            session_id, thread_id, operation_id=operation_id,
            payload=payload, recovery_ref=recovery_ref,
        ),
    )
    agent = create_model_call_agent(
        model,
        execution=execution,
        system_prompt=None,
        tools=list(resolved_tools) if resolved_tools else None,
        middleware=deepagent_middleware,
        response_format=response_format,
        context_schema=None,
        checkpointer=checkpointer,
        store=None,
        debug=debug,
        name=name,
        cache=None,
    )

    if hasattr(agent, "with_config"):
        runtime_agent = agent.with_config(
            {
                "recursion_limit": AGENT_GRAPH_RECURSION_LIMIT,
                "metadata": {
                    "ls_integration": "deepagents",
                    "versions": {"deepagents": "custom"},
                    "lc_agent_name": name,
                },
            }
        )
    else:
        runtime_agent = agent

    # 仅记录真实构建对象的进程内身份；compiled graph 不进入业务库或 trace。
    observe_phase_sync(thread_id, "agent.graph.compiled", details={
        "session_id": session_id, "thread_id": thread_id, "owns_thread": owns_thread,
        "compiled_graph_object_id": id(agent),
        "compiled_graph_type": f"{type(agent).__module__}.{type(agent).__qualname__}",
        "agent_object_id": id(runtime_agent),
        "scope_object_id": id(reactor_lifetime_scope) if reactor_lifetime_scope is not None else None,
    })
    return runtime_agent


# OpenSpec 8.4：代码内闭集注册 deep-agent graph family（不提供运行时可配置的
# 注册扩展点）。resolve 命中即返回本 factory callable；进程重启后 runtime 以
# 持久化 binding 重新解析到同一 callable，再以精确 thread generation 的
# ThreadRuntimeBinding 注入归属；factory 不捕获 invocation admission。
GRAPH_FACTORY_REGISTRY.register(DEEP_AGENT_GRAPH_BINDING, create_my_deep_agent)


def create_runtime_deep_agent_for_session(
    *,
    session_id: str,
    thread_id: str,
    agent_id: str,
    tool_selection_reader: ToolSelectionReader,
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
    terminal_resource_owner: TerminalResourceOwner | None = None,
    browser_resource_owner: BrowserResourceOwner | None = None,
    node_debug_service: NodeDebugService | None = None,
    session_context_query_service: SessionContextQueryProtocol | None = None,
    workspace_session_context_client: WorkspaceSessionContextClientProtocol
    | None = None,
    session_target_resolver: SessionTargetResolverProtocol | None = None,
    session_communication_service: SessionCommunicationProtocol | None = None,
    mcp_tools: Sequence[BaseTool] | None = None,
    mcp_catalog_owner: McpCatalogOwner | None = None,
    name: str | None = None,
    override_model: Any = None,
    response_format: ResponseFormat[Any] | type | dict[str, Any] | None = None,
    model_routing_enabled: bool = True,
    preferred_provider_id: str | None = None,
    tool_timeout_seconds: float | None = None,
    workspace_file_resource_registry: WorkspaceFileResourceRegistry | None = None,
    reactor_lifetime_scope: LifetimeScope | None = None,
    skill_catalog: PublishedSkillCatalog | None = None,
    resource_registry: ResourceRegistry,
    resource_activation: ResourceActivationCoordinator | None,
    workspace_root: Path,
    include_team_tools: bool = False,
    graph_binding_store: GraphBindingStorePort,
    resource_navigation: ResourceNavigation,
    text_mutations: WorkspaceTextMutationOwner,
    owns_thread: bool = True,
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
    if session_service is None:
        raise RuntimeError(
            "create_runtime_deep_agent_for_session 需要显式传入 SessionService"
        )

    model = override_model if override_model is not None else runtime["model"]

    # 真实 thread 只由 creation 冻结的精确 selector 分派；合成工具面没有 durable owner。
    binding = (
        resolve_graph_binding(
            store=graph_binding_store,
            owner=GraphBindingOwnerKey(session_id, thread_id),
        )
        if owns_thread
        else DEEP_AGENT_GRAPH_BINDING
    )
    builder = GRAPH_FACTORY_REGISTRY.resolve(binding)
    return builder(
        model=model,
        response_format=response_format,
        instruction_producers=runtime["instruction_producers"],
        tool_selection_reader=tool_selection_reader,
        checkpointer=checkpointer,
        session_id=session_id,
        thread_id=thread_id,
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
        terminal_resource_owner=terminal_resource_owner,
        browser_resource_owner=browser_resource_owner,
        node_debug_service=node_debug_service,
        session_context_query_service=session_context_query_service,
        workspace_session_context_client=workspace_session_context_client,
        session_target_resolver=session_target_resolver,
        session_communication_service=session_communication_service,
        mcp_tools=mcp_tools,
        mcp_catalog_owner=mcp_catalog_owner,
        tool_timeout_seconds=tool_timeout_seconds,
        workspace_file_resource_registry=workspace_file_resource_registry,
        reactor_lifetime_scope=reactor_lifetime_scope,
        skill_catalog=skill_catalog,
        resource_registry=resource_registry,
        resource_activation=resource_activation,
        include_team_tools=include_team_tools,
        resource_navigation=resource_navigation,
        text_mutations=text_mutations,
        interrupt_on={tool_name: True for tool_name in direct_confirmation_tool_names},
        custom_tool_confirmation_names=custom_tool_confirmation_names,
        config_service=service,
        workspace_root=workspace_root,
        owns_thread=owns_thread,
    )
