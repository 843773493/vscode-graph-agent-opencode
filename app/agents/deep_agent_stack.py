from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.permissions import FilesystemPermission
from langchain.agents.middleware import (
    InterruptOnConfig,
    ModelRequest,
    ModelResponse,
    TodoListMiddleware,
)
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool

from app.abstractions.resource_file_content import ResourceFileReader
from app.agents.instruction_producers import (
    InstructionProducerSpec,
    InstructionUpdateBinding,
    register_instruction_producer,
)
from app.agents.middleware_prompts import (
    COMPACT_CONVERSATION_SYSTEM_PROMPT,
    FILESYSTEM_SYSTEM_PROMPT,
    FILESYSTEM_TOOL_DESCRIPTIONS,
    SKILLS_SYSTEM_PROMPT,
    TODO_SYSTEM_PROMPT,
    TODO_TOOL_DESCRIPTION,
)
from app.agents.skill_catalog import (
    PublishedSkillCatalog,
)
from app.agents.skill_runtime import (
    append_skill_middlewares,
)
from app.agents.structured_prompt_validation_middleware import (
    StructuredPromptValidationMiddleware,
)
from app.agents.structured_tool_call_middleware import StructuredToolCallMiddleware
from app.agents.summarization.factory import (
    create_cache_preserving_summarization_middleware,
)
from app.agents.summarization.tool_middleware import (
    CachePreservingSummarizationToolMiddleware,
)
from app.agents.tool_confirmation_middleware import ToolConfirmationMiddleware
from app.agents.tool_identity import tool_definition_name
from app.agents.tool_invocation_context import ToolInvocationContextMiddleware
from app.agents.tool_output_middleware import ToolOutputMiddleware
from app.agents.workspace_filesystem_tools import configure_workspace_filesystem_tools
from app.domain.itemized.hashing import sha256_jcs
from app.services.infrastructure.resource_platform.registry.context_source_reactor import (
    ContextSourceReactor,
)
from app.services.infrastructure.resource_platform.sources.resource_navigation import (
    ResourceNavigation,
)
from app.services.infrastructure.resource_platform.sources.workspace_file_resources import (
    WorkspaceFileResourceRegistry,
)
from app.services.infrastructure.rollout_context.checkpoint.boundary.compaction_boundary_adapter import (
    CompactionPreflightPort,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
)
from app.services.infrastructure.workspace_files.text_owner import (
    WorkspaceTextMutationOwner,
)

ToolDefinition = BaseTool | Callable[..., Any] | dict[str, Any]
FILESYSTEM_INTERNAL_TOOL_DENYLIST = {"execute"}


class SourceOwnedTodoListMiddleware(TodoListMiddleware):
    """保留第三方 Todo 工具/状态合同，说明只由 R04 source 进入 assembly。"""

    def wrap_model_call(
        self, request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        return handler(request)

    async def awrap_model_call(
        self, request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        return await handler(request)


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
    compaction_preflight: CompactionPreflightPort,
) -> list[AgentMiddleware]:
    summarization = create_cache_preserving_summarization_middleware(
        model,
        backend,
        compaction_preflight=compaction_preflight,
    )
    tool_middleware = CachePreservingSummarizationToolMiddleware(
        summarization,
        system_prompt=None,
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
    resource_navigation: ResourceNavigation,
    text_mutations: WorkspaceTextMutationOwner,
    artifact_reader: ResourceFileReader,
    permissions: list[FilesystemPermission] | None,
    resolved_skills: PublishedSkillCatalog | None,
    compaction_preflight: CompactionPreflightPort,
    context_source_manager: ContextSourceManager | None = None,
    source_registry: WorkspaceFileResourceRegistry | None = None,
    context_source_reactor: ContextSourceReactor | None = None,
    execution_owned_skill_catalog: bool = False,
    resolved_tool_denylist: set[str],
    interrupt_on: dict[str, bool | InterruptOnConfig] | None,
    runtime_middleware: list[AgentMiddleware],
    model_routing_middleware: AgentMiddleware | None,
    tool_invocation_context_middleware: ToolInvocationContextMiddleware,
    tool_output_middleware: ToolOutputMiddleware,
    custom_tool_confirmation_names: frozenset[str] = frozenset(),
) -> list[AgentMiddleware]:
    deepagent_middleware: list[AgentMiddleware] = [
        tool_invocation_context_middleware,
        tool_output_middleware,
    ]
    if "write_todos" not in resolved_tool_denylist:
        deepagent_middleware.append(
            SourceOwnedTodoListMiddleware(
                system_prompt="",
                tool_description=TODO_TOOL_DESCRIPTION,
            )
        )
    append_skill_middlewares(
        deepagent_middleware,
        catalog=resolved_skills,
        system_prompt=(
            SKILLS_SYSTEM_PROMPT if "skill_load" not in resolved_tool_denylist else None
        ),
        context_source_manager=context_source_manager,
        source_registry=source_registry,
        context_source_reactor=context_source_reactor,
        execution_owned_catalog=execution_owned_skill_catalog,
    )
    filesystem_middleware = FilesystemMiddleware(
        backend=backend,
        system_prompt="",
        custom_tool_descriptions=FILESYSTEM_TOOL_DESCRIPTIONS,
        _permissions=permissions,
        tool_token_limit_before_evict=None,
    )
    configure_workspace_filesystem_tools(
        filesystem_middleware,
        workspace_root=workspace_root,
        resource_navigation=resource_navigation,
        text_mutations=text_mutations,
        artifact_reader=artifact_reader,
    )
    _filter_middleware_tools(
        filesystem_middleware,
        resolved_tool_denylist | FILESYSTEM_INTERNAL_TOOL_DENYLIST,
    )
    if filesystem_middleware.tools:
        deepagent_middleware.append(filesystem_middleware)

    # after_model 按注册顺序逆序执行；压缩 continuation 必须先跳到下一
    # model task，避免审批 hook 再次处理历史中已经执行完毕的工具调用。
    if custom_tool_confirmation_names or interrupt_on:
        deepagent_middleware.append(
            ToolConfirmationMiddleware(custom_tool_confirmation_names, interrupt_on=interrupt_on)
        )

    deepagent_middleware.extend(
        [
            *_build_summarization_middleware(
                model,
                backend,
                compaction_preflight=compaction_preflight,
            ),
            StructuredToolCallMiddleware(),
        ]
    )
    # D3-B 起 AGENTS 来源注册由 WorkspaceSkillsMiddleware 的唯一 CSM 链承载，
    # 不再有独立的 AGENTS 注入 middleware 槽位。

    if runtime_middleware:
        deepagent_middleware.extend(runtime_middleware)
    if model_routing_middleware is not None:
        deepagent_middleware.append(model_routing_middleware)

    for middleware_item in deepagent_middleware:
        _filter_middleware_tools(middleware_item, resolved_tool_denylist)

    if context_source_manager is not None:
        for producer_id, source_name, content, policy_key, present in (
            ("R04", "todo-list", TODO_SYSTEM_PROMPT, "todo_list", "write_todos" not in resolved_tool_denylist),
            ("R06", "filesystem-rules", FILESYSTEM_SYSTEM_PROMPT, "filesystem_rules", bool(filesystem_middleware.tools)),
            ("R07", "compact-tool-usage", COMPACT_CONVERSATION_SYSTEM_PROMPT, "compact_conversation", "compact_conversation" not in resolved_tool_denylist),
        ):
            if present:
                publication_revision = sha256_jcs({"content": content})
                register_instruction_producer(context_source_manager, InstructionProducerSpec(
                    producer_id=producer_id, source_id=f"instruction:{source_name}",
                    source_kind=source_name, name=source_name, content=content,
                    revision=publication_revision,
                    root_placement="root_eligible", policy_key=policy_key,
                    update_binding=InstructionUpdateBinding("software_release", publication_revision, "epoch"),
                ))

    # 放在所有会改写模型请求的 middleware 之后，确保派生的压缩请求也会被验证。
    deepagent_middleware.append(StructuredPromptValidationMiddleware())

    return deepagent_middleware
