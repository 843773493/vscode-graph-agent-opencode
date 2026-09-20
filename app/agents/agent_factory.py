from __future__ import annotations

import logging
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
from app.agents.cache_preserving_summarization import (
    NoDurableOwnerCompactionPreflight,
)
from app.agents.custom_tools import build_custom_tool_bundle
from app.agents.deep_agent_stack import (
    build_deep_agent_middleware,
)
from app.agents.graph_binding import (
    DEEP_AGENT_GRAPH_BINDING,
    GRAPH_FACTORY_REGISTRY,
    GraphBindingOwnerKey,
    GraphBindingStorePort,
)
from app.agents.itemized_context_middleware import SealedAssemblyDispatchBridge
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
    PublishedSkillCatalog,
    append_skill_middlewares,
    build_workspace_skill_catalog,
    resolve_bundled_skill_groups,
)
from app.agents.tool_invocation_context import (
    ThreadRuntimeBinding,
    ToolInvocationContext,
    ToolInvocationContextMiddleware,
)
from app.agents.tool_output_middleware import ToolOutputMiddleware
from app.agents.tools.custom_invocation import (
    create_extension_tool_invoker_tool,
    seal_extension_catalog_binding_from_tools,
)
from app.agents.tools.session_wait import CommunicationWaitBindingLookupPort
from app.agents.tools.skill_loading import create_skill_load_tool
from app.agents.workspace_backend import build_workspace_backend
from app.core.background_message_bus import BackgroundMessageBus
from app.core.background_task_registry import BackgroundTaskRegistry
from app.core.lifecycle import LifetimeScope
from app.services.infrastructure.browser_manager_client import BrowserManagerClient
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.events.channel_events import (
    ContextSourceEvent,
    ContextSourceEventPublisher,
)
from app.services.infrastructure.node_debug_service import NodeDebugService
from app.services.infrastructure.resource_platform.registry.context_source_reactor import (
    ContextSourceReactor,
    ReactorCreatedCallback,
)
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.infrastructure.resource_platform.sources.workspace_file_resources import (
    WorkspaceFileResourceRegistry,
)
from app.services.infrastructure.rollout_context.checkpoint.compaction_boundary_adapter import (
    CompactionPreflightPort,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    MAIN_THREAD_ID,
    ContextSourceControlStatePort,
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
)
from app.services.infrastructure.terminal_manager_client import TerminalManagerClient
from app.services.infrastructure.tool_output_store import ToolOutputStore
from app.services.orchestration.communication.binding_lookup import (
    SessionControlStoreWaitBindingLookup,
)

if TYPE_CHECKING:
    from app.services.business.message_service import MessageService
    from app.services.business.session_goal_service import SessionGoalService
    from app.services.business.session_service import SessionService


AGENT_GRAPH_RECURSION_LIMIT = 9999
PROVIDER_REQUEST_OPTION_KEYS = {"overrides", "default_headers", "no_proxy"}
logger = logging.getLogger(__name__)


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


def _runtime_identity_system_prompt(
    system_prompt: str | SystemMessage,
    *,
    workspace_root: Path | None,
    provider_candidates: Sequence[tuple[str, str]],
) -> SystemMessage:
    """向模型注入不可由工作区文件推断的运行时身份和路径边界。"""
    if not provider_candidates:
        raise ValueError("运行时身份至少需要一个 provider/model 候选")

    primary_provider_id, primary_model_id = provider_candidates[0]
    fallback_text = ", ".join(
        f"{provider_id}/{model_id}"
        for provider_id, model_id in provider_candidates[1:]
    )
    workspace_text = (
        str(workspace_root.resolve())
        if workspace_root is not None
        else "由当前 Agent runtime 显式提供"
    )
    identity_prompt = (
        "## 当前运行时身份与路径边界（系统权威元数据）\n"
        f"- workspace 根目录（exec_command 未提供 workdir 时的 cwd）：`{workspace_text}`\n"
        f"- 当前首选 provider id：`{primary_provider_id}`\n"
        f"- 当前首选 model id：`{primary_model_id}`\n"
        f"- 已配置 fallback provider/model（按顺序）：`{fallback_text or '无'}`\n"
        "这些字段来自当前运行时配置，不是从 project.godot 或其他工作区文件推断的。\n"
        "回答 provider/model 身份时必须使用上述元数据；project.godot 的 `config/name` 只是项目显示名，绝不是模型名或 provider 名。\n"
        "文件工具优先使用相对于 workspace 根目录的相对路径；上面列出的绝对根目录只用于 exec_command 的 cwd，不要把它当作文件工具的 path。项目位于子目录时必须保留该前缀：例如 `parry_arena/project.godot` 和 `parry_arena/godot_export/parry_arena.html`。只有在 exec_command 的 cwd 明确为 `parry_arena` 时，`godot_export/parry_arena.html` 才是同一文件的项目相对路径；不能因为 workspace 根下没有不带前缀的路径就报告文件不存在。工作区内的绝对路径会被自动归一化为相对路径，但你应直接给出相对路径。\n"
        "exec_command 的相对 workdir 只相对于上述 workspace 根目录解析一次；不要在命令中再次 cd 到同一个 workdir。工具结果中的 cwd 是实际执行目录，应以它解释相对路径。"
    )
    base_message = (
        system_prompt
        if isinstance(system_prompt, SystemMessage)
        else SystemMessage(content=system_prompt)
    )
    return append_to_system_message(base_message, identity_prompt)


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
    if api_mode.protocol == "anthropic_messages" and custom_llm_provider != "anthropic":
        raise ValueError(
            "Anthropic Messages provider 必须配置 "
            "custom_llm_provider='anthropic'"
        )
    if api_mode.protocol not in {"chat_completions", "anthropic_messages"}:
        raise ValueError(f"provider.api_mode.protocol 不受支持: {api_mode.protocol!r}")

    from app.agents.providers.litellm_chat import build_litellm_chat_model

    return build_litellm_chat_model(
        provider=provider,
        runtime_config=runtime_config,
        request_options=request_options,
        prompt_cache_key=prompt_cache_key,
    )


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
    workspace_root: Path | None = None,
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
        "system_prompt": _runtime_identity_system_prompt(
            runtime_config["system_prompt"],
            workspace_root=workspace_root,
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


# OpenSpec 8.4 缓存审计结论（R6a）：create_my_deep_agent 本体每次调用都全新
# 执行 create_agent 并新建工具/middleware 闭包，本模块内不存在任何跨 invocation
# 的已编译图缓存（无 lru_cache、无模块级实例表）。已知的调用方级例外：
# AgentExecutionService._get_or_create_agent 以
# (session_id, resolved_agent_id, config_revision, execution_overrides,
# model_visibility_overrides) 为 key 缓存整个已编译 agent——key 含 session_id
# 与配置 revision，不会跨 thread 泄漏闭包，且真实 step 路径每步全新构建不经
# 该缓存；但被缓存的图仍捕获 session 闭包，与「只复用不捕获 thread 的
# blueprint/topology」红线有差距。该缓存有专门回归测试
# （test_agent_cache_rebuilds_after_config_revision_changes）锁定行为，blueprint
# 与 invocation 依赖的拆分由 OpenSpec 8.4 后续轮次处理（TODO）。
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
    skill_catalog: PublishedSkillCatalog | None = None,
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
    communication_binding_lookup: CommunicationWaitBindingLookupPort | None = None,
    mcp_tools: Sequence[BaseTool] | None = None,
    tool_timeout_seconds: float | None = None,
    workspace_file_resource_registry: WorkspaceFileResourceRegistry | None = None,
    reactor_lifetime_scope: LifetimeScope | None = None,
    on_reactor_created: ReactorCreatedCallback | None = None,
    workspace_root: Path,
    include_team_tools: bool = False,
    graph_binding_store: GraphBindingStorePort | None = None,
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
        # 单会话 Agent 运行在 main thread；这里把受信 (session_id, thread_id)
        # 交给扩展工具，按 SessionThread 隔离的调试工具不再自行猜测归属。
        thread_binding=ThreadRuntimeBinding(
            session_id=session_id,
            thread_id=MAIN_THREAD_ID,
        ),
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
    if include_team_tools and team_service is None:
        raise RuntimeError("create_my_deep_agent 需要显式传入 TeamCoordinationService")
    if job_service is None:
        raise RuntimeError("create_my_deep_agent 需要显式传入 JobService")
    if session_context_query_service is None:
        raise RuntimeError("create_my_deep_agent 需要显式传入 SessionContextQueryService")
    if workspace_session_context_client is None:
        raise RuntimeError("create_my_deep_agent 需要显式传入 WorkspaceSessionContextClient")

    workspace_root = workspace_root.resolve()

    resolved_bundled_skill_groups = resolve_bundled_skill_groups()
    # SkillCatalog 的语义 revision 由 ResourceRegistry 唯一发布;catalog 是
    # 不可变快照,重入装配时允许调用方复用已发布实例。
    skill_registry = ResourceRegistry()
    resolved_skills = (
        build_workspace_skill_catalog(
            workspace_root,
            registry=skill_registry,
            bundled_skill_groups=resolved_bundled_skill_groups,
        )
        if skill_catalog is None
        else skill_catalog
    )
    backend = build_workspace_backend(
        workspace_root,
    )
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
            thread_id=MAIN_THREAD_ID,
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
    # OpenSpec 2.4-B4：commit 事件发布已随 commit_model_call_pending 落在
    # durable 成功之后；channel 参数沿用最近稳定 identity session_id。
    context_source_event_sink: Callable[[ContextSourceEvent], None] | None = None
    if workspace_file_resource_registry is not None:
        context_source_event_sink = ContextSourceEventPublisher(
            event_service=(
                workspace_file_resource_registry.observation_channel.event_service
            ),
            scope_id=session_id,
        )
    context_source_manager = ContextSourceManager(
        owner=context_source_owner,
        control_state_port=context_source_control_port,
        mutation_intent_port=context_source_control_port,
        lifecycle_event_sink=context_source_event_sink,
    )
    # 事件驱动 reaction：reactor 订阅来源 owner 的轻量 change 通知，并把
    # 「有新 revision」标记为 CSM 的 pending observation；before_model 只消费
    # 已排队的 observation，不再遍历 descriptor 轮询内存快照。
    # 订阅必须随 agent 生命周期释放；调用方没有提供 scope 时在代码内留 TODO，
    # 不允许在这里新建第二套 dispose 抽象。
    context_source_reactor: ContextSourceReactor | None = None
    if workspace_file_resource_registry is not None:
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
        if on_reactor_created is not None:
            on_reactor_created((session_id, agent_id), context_source_reactor)
    hidden_direct_tool_names: set[str] = set()
    extension_confirmation_names: set[str] = set()
    direct_confirmation_names: set[str] = set()
    extension_policies: dict[str, object] = {}
    extension_tools: list[BaseTool] = []
    extension_invoker: BaseTool | None = None
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
            raise RuntimeError(
                "create_my_deep_agent 构建默认工具集时需要显式传入 BrowserManagerClient"
            )
        if communication_binding_lookup is None:
            raise RuntimeError(
                "create_my_deep_agent 构建默认工具集时需要显式传入 communication "
                "binding lookup（wait_for_session 的跨会话执行绑定解析）"
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
            terminal_manager_client=terminal_manager_client,
            invocation_context=tool_invocation_context,
            workspace_root=workspace_root,
            session_message_delivery_service=session_message_delivery_service,
            communication_binding_lookup=communication_binding_lookup,
            include_test_tools=config_service.development_test_tools_enabled(),
            include_team_tools=include_team_tools,
            context_source_manager=context_source_manager,
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
    # 固定信封即使当前没有可执行 target 也必须存在；target 的启停只改变
    # 封存目录与执行准入，不改变 Provider 工具面。
    extension_invoker = create_extension_tool_invoker_tool(
        extension_tools,
        # E4：旧 tool call 只按装配期封存 binding 解析；
        # 正式 activation owner 接线前由快照封存承担 sealed ref。
        catalog_binding_resolver=seal_extension_catalog_binding_from_tools(
            extension_tools
        ),
        is_tool_execution_enabled=lambda target: bool(
            getattr(
                extension_policies.get(target.name),
                "execution_enabled",
                False,
            )
        ),
        invocation_context=tool_invocation_context,
    )
    resolved_tools.append(extension_invoker)
    if context_source_manager is not None and not any(
        getattr(tool, "name", None) == "skill_load" for tool in resolved_tools
    ):
        resolved_tools.append(
            create_skill_load_tool(context_source_manager)
        )
    if enabled_tool_names is not None:
        resolved_tools = [tool for tool in resolved_tools if getattr(tool, "name", "") in enabled_tool_names]
        if extension_invoker is not None and extension_invoker not in resolved_tools:
            resolved_tools.append(extension_invoker)
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
        catalog=None,
    )
    runtime_middleware.append(
        SealedAssemblyDispatchBridge(checkpointer=checkpointer)
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
        compaction_preflight=compaction_preflight,
        context_source_manager=context_source_manager,
        source_registry=workspace_file_resource_registry,
        context_source_reactor=context_source_reactor,
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

    # OpenSpec 8.4：deep agent 构建路径产出 GraphBinding。持久化的是 factory
    # selector 四元组（见 app/agents/graph_binding.py），不是 CompiledStateGraph。
    # 构建即 fail-fast 校验当前 revision 仍可被 registry 解析：descriptor 与
    # 注册一旦脱节（改了图骨架却没 bump revision / 注册），立即失败而不是让
    # 重启后的 resolve 才暴露。
    GRAPH_FACTORY_REGISTRY.resolve(DEEP_AGENT_GRAPH_BINDING)
    if graph_binding_store is not None:
        # 持久化 owner 是精确 (session_id, thread_id)；当前单会话 Agent 运行在
        # main thread。装配方（container）接线该 store 前保持 None，不伪造
        # 持久化成功。
        # TODO(OpenSpec 8.4 装配轮)：由 container.py 经统一会话路径解析器构造
        # thread 节点附属目录的 JsonFileGraphBindingStore 并传入；本轮并行约束
        # 禁止修改 container.py。
        graph_binding_store.save_graph_binding(
            GraphBindingOwnerKey(
                session_id=session_id,
                thread_id=MAIN_THREAD_ID,
            ),
            DEEP_AGENT_GRAPH_BINDING,
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


# OpenSpec 8.4：代码内闭集注册 deep-agent graph family（不提供运行时可配置的
# 注册扩展点）。resolve 命中即返回本 factory callable；进程重启后 runtime 以
# 持久化 binding 重新解析到同一 callable，再以每次 invocation 的
# ThreadRuntimeBinding 注入 session/thread 依赖，factory 自身不闭包捕获 thread。
GRAPH_FACTORY_REGISTRY.register(DEEP_AGENT_GRAPH_BINDING, create_my_deep_agent)


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
    workspace_file_resource_registry: WorkspaceFileResourceRegistry | None = None,
    reactor_lifetime_scope: LifetimeScope | None = None,
    on_reactor_created: ReactorCreatedCallback | None = None,
    workspace_root: Path,
    include_team_tools: bool = False,
    graph_binding_store: GraphBindingStorePort | None = None,
):
    if config_service is None:
        raise RuntimeError("create_runtime_deep_agent_for_session 需要显式传入 ConfigService")
    service = config_service
    runtime = build_runtime_for_agent(
        agent_id=agent_id,
        config_service=service,
        prompt_cache_key=session_id,
        preferred_provider_id=preferred_provider_id,
        workspace_root=workspace_root,
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
    communication_binding_lookup = SessionControlStoreWaitBindingLookup(
        path_resolver=session_service.path_resolver,
    )

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
        communication_binding_lookup=communication_binding_lookup,
        mcp_tools=mcp_tools,
        tool_timeout_seconds=tool_timeout_seconds,
        workspace_file_resource_registry=workspace_file_resource_registry,
        reactor_lifetime_scope=reactor_lifetime_scope,
        on_reactor_created=on_reactor_created,
        include_team_tools=include_team_tools,
        interrupt_on={tool_name: True for tool_name in direct_confirmation_tool_names},
        custom_tool_confirmation_names=custom_tool_confirmation_names,
        config_service=service,
        workspace_root=workspace_root,
        graph_binding_store=graph_binding_store,
    )
