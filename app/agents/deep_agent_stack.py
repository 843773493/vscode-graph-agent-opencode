from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
from deepagents.middleware.permissions import FilesystemPermission
from langchain.agents.middleware import (
    HumanInTheLoopMiddleware,
    InterruptOnConfig,
    TodoListMiddleware,
)
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool

from app.agents.cache_preserving_summarization import (
    CachePreservingSummarizationToolMiddleware,
    create_cache_preserving_summarization_middleware,
)
from app.agents.custom_tool_confirmation_middleware import (
    CustomToolConfirmationMiddleware,
)
from app.agents.middleware_prompts import (
    COMPACT_CONVERSATION_SYSTEM_PROMPT,
    FILESYSTEM_SYSTEM_PROMPT,
    FILESYSTEM_TOOL_DESCRIPTIONS,
    MEMORY_SYSTEM_PROMPT,
    SKILLS_SYSTEM_PROMPT,
    TODO_SYSTEM_PROMPT,
    TODO_TOOL_DESCRIPTION,
)
from app.agents.model_tool_visibility import ModelToolVisibilityMiddleware
from app.agents.skill_runtime import (
    PublishedSkillCatalog,
    append_skill_middlewares,
)
from app.agents.structured_memory_middleware import StructuredMemoryMiddleware
from app.agents.structured_prompt_validation_middleware import (
    StructuredPromptValidationMiddleware,
)
from app.agents.structured_tool_call_middleware import StructuredToolCallMiddleware
from app.agents.tool_identity import tool_definition_name
from app.agents.tool_invocation_context import ToolInvocationContextMiddleware
from app.agents.tool_output_middleware import ToolOutputMiddleware
from app.agents.workspace_filesystem_tools import configure_workspace_filesystem_tools
from app.services.infrastructure.resource_platform.registry.context_source_reactor import (
    ContextSourceReactor,
)
from app.services.infrastructure.resource_platform.sources.workspace_file_resources import (
    WorkspaceFileResourceRegistry,
)
from app.services.infrastructure.rollout_context.checkpoint.compaction_boundary_adapter import (
    CompactionPreflightPort,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
)

ToolDefinition = BaseTool | Callable[..., Any] | dict[str, Any]
FILESYSTEM_INTERNAL_TOOL_DENYLIST = {"execute"}


def filter_tools_by_name(
    tools: list[ToolDefinition],
    denylist: set[str],
) -> list[ToolDefinition]:
    if not denylist:
        return tools
    return [tool for tool in tools if tool_definition_name(tool) not in denylist]


def _filter_middleware_tools(middleware: Any, denylist: set[str]) -> None:
    if not denylist:
        return

    middleware_tools = getattr(middleware, "tools", None)
    if not isinstance(middleware_tools, list):
        return

    filtered_tools = filter_tools_by_name(list(middleware_tools), denylist)
    middleware.tools = filtered_tools


def _build_summarization_middleware(
    model: BaseChatModel,
    backend: BackendProtocol,
    *,
    compact_tool_enabled: bool,
    compaction_preflight: CompactionPreflightPort,
) -> list[AgentMiddleware]:
    summarization = create_cache_preserving_summarization_middleware(
        model,
        backend,
        compaction_preflight=compaction_preflight,
    )
    if not compact_tool_enabled:
        return [summarization]

    tool_middleware = CachePreservingSummarizationToolMiddleware(
        summarization,
        system_prompt=COMPACT_CONVERSATION_SYSTEM_PROMPT,
    )
    return [
        summarization,
        tool_middleware,
    ]


def build_deep_agent_middleware(
    *,
    model: BaseChatModel,
    backend: BackendProtocol,
    workspace_root: Path,
    permissions: list[FilesystemPermission] | None,
    resolved_skills: PublishedSkillCatalog | None,
    compaction_preflight: CompactionPreflightPort,
    context_source_manager: ContextSourceManager | None = None,
    source_registry: WorkspaceFileResourceRegistry | None = None,
    context_source_reactor: ContextSourceReactor | None = None,
    resolved_tool_denylist: set[str],
    interrupt_on: dict[str, bool | InterruptOnConfig] | None,
    runtime_middleware: list[AgentMiddleware],
    model_routing_middleware: AgentMiddleware | None,
    tool_invocation_context_middleware: ToolInvocationContextMiddleware,
    tool_output_middleware: ToolOutputMiddleware,
    memory: list[str] | None,
    custom_tool_confirmation_names: frozenset[str] = frozenset(),
    model_hidden_tool_names: frozenset[str] = frozenset(),
) -> list[AgentMiddleware]:
    deepagent_middleware: list[AgentMiddleware] = [
        tool_invocation_context_middleware,
        tool_output_middleware,
    ]
    if "write_todos" not in resolved_tool_denylist:
        deepagent_middleware.append(
            TodoListMiddleware(
                system_prompt=TODO_SYSTEM_PROMPT,
                tool_description=TODO_TOOL_DESCRIPTION,
            )
        )
    append_skill_middlewares(
        deepagent_middleware,
        catalog=resolved_skills,
        system_prompt=(
            SKILLS_SYSTEM_PROMPT if "read_file" not in resolved_tool_denylist else None
        ),
        context_source_manager=context_source_manager,
        source_registry=source_registry,
        context_source_reactor=context_source_reactor,
    )
    filesystem_middleware = FilesystemMiddleware(
        backend=backend,
        system_prompt=FILESYSTEM_SYSTEM_PROMPT,
        custom_tool_descriptions=FILESYSTEM_TOOL_DESCRIPTIONS,
        _permissions=permissions,
        tool_token_limit_before_evict=None,
    )
    configure_workspace_filesystem_tools(
        filesystem_middleware,
        workspace_root=workspace_root,
    )
    _filter_middleware_tools(
        filesystem_middleware,
        resolved_tool_denylist | FILESYSTEM_INTERNAL_TOOL_DENYLIST,
    )
    if filesystem_middleware.tools:
        deepagent_middleware.append(filesystem_middleware)

    deepagent_middleware.extend(
        [
            *_build_summarization_middleware(
                model,
                backend,
                compact_tool_enabled="compact_conversation"
                not in resolved_tool_denylist,
                compaction_preflight=compaction_preflight,
            ),
            PatchToolCallsMiddleware(),
            StructuredToolCallMiddleware(),
        ]
    )
    # D3-B 起 AGENTS 来源注册由 WorkspaceSkillsMiddleware 的唯一 CSM 链承载，
    # 不再有独立的 AGENTS 注入 middleware 槽位。

    if model_hidden_tool_names:
        deepagent_middleware.append(
            ModelToolVisibilityMiddleware(model_hidden_tool_names)
        )

    if runtime_middleware:
        deepagent_middleware.extend(runtime_middleware)
    if model_routing_middleware is not None:
        deepagent_middleware.append(model_routing_middleware)

    for middleware_item in deepagent_middleware:
        _filter_middleware_tools(middleware_item, resolved_tool_denylist)

    if memory:
        deepagent_middleware.append(
            StructuredMemoryMiddleware(
                backend=backend,
                sources=memory,
                system_prompt=MEMORY_SYSTEM_PROMPT,
            )
        )

    if custom_tool_confirmation_names:
        deepagent_middleware.append(
            CustomToolConfirmationMiddleware(custom_tool_confirmation_names)
        )
    if interrupt_on:
        deepagent_middleware.append(HumanInTheLoopMiddleware(interrupt_on=interrupt_on))

    # 放在所有会改写模型请求的 middleware 之后，确保派生的压缩请求也会被验证。
    deepagent_middleware.append(StructuredPromptValidationMiddleware())

    return deepagent_middleware
