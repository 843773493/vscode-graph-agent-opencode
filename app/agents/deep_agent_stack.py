from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.memory import MemoryMiddleware
from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
from deepagents.middleware.permissions import FilesystemPermission
from deepagents.middleware.summarization import (
    CompactConversationSchema,
    SummarizationToolMiddleware,
    create_summarization_middleware,
)
from langchain.agents.middleware import HumanInTheLoopMiddleware, InterruptOnConfig
from langchain.agents.middleware import TodoListMiddleware
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool

from app.agents.skill_runtime import (
    append_skill_middlewares,
    append_workspace_agents_middleware,
)
from app.agents.llm_logging_middleware import LLMLoggingMiddleware
from app.agents.middleware_prompts import (
    COMPACT_CONVERSATION_SYSTEM_PROMPT,
    FILESYSTEM_SYSTEM_PROMPT,
    FILESYSTEM_TOOL_DESCRIPTIONS,
    MEMORY_SYSTEM_PROMPT,
    SKILLS_SYSTEM_PROMPT,
    TODO_SYSTEM_PROMPT,
    TODO_TOOL_DESCRIPTION,
)
from app.agents.request_replay_middleware import PromptReplayCaptureMiddleware
from app.agents.structured_tool_call_middleware import StructuredToolCallMiddleware
from app.agents.tool_identity import tool_definition_name
from app.agents.tool_invocation_context import ToolInvocationContextMiddleware
from app.agents.tool_output_middleware import ToolOutputMiddleware


ToolDefinition = BaseTool | Callable[..., Any] | dict[str, Any]


_PROMPT_REPLAY_LABELS = {
    "TodoListMiddleware": "任务规划指令",
    "WorkspaceSkillsMiddleware": "Skills 索引",
    "FilesystemMiddleware": "文件系统与环境信息",
    "SummarizationMiddleware": "上下文压缩指令",
    "SummarizationToolMiddleware": "上下文压缩工具指令",
    "WorkspaceAgentsMiddleware": "工作区 AGENTS.md",
    "MemoryMiddleware": "Agent 记忆",
}


def _prompt_replay_label(middleware: AgentMiddleware) -> str:
    class_name = middleware.__class__.__name__
    return _PROMPT_REPLAY_LABELS.get(class_name, class_name)


def _instrument_prompt_replay(
    middleware_stack: list[AgentMiddleware],
) -> list[AgentMiddleware]:
    logging_middleware = [
        item for item in middleware_stack if isinstance(item, LLMLoggingMiddleware)
    ]
    if not logging_middleware:
        return middleware_stack
    if len(logging_middleware) > 1:
        raise ValueError("LLMLoggingMiddleware 只能注册一次，否则同一次请求会产生重复日志")

    request_middleware = [
        item for item in middleware_stack if not isinstance(item, LLMLoggingMiddleware)
    ]
    instrumented: list[AgentMiddleware] = [
        PromptReplayCaptureMiddleware(
            source="agent_factory",
            label="默认指令",
            capture_id="initial",
        )
    ]
    for index, middleware_item in enumerate(request_middleware, start=1):
        instrumented.append(middleware_item)
        instrumented.append(
            PromptReplayCaptureMiddleware(
                source=middleware_item.__class__.__name__,
                label=_prompt_replay_label(middleware_item),
                capture_id=f"{index}:{middleware_item.name}",
            )
        )
    # 日志器必须在所有请求改写 middleware 之后，才能拿到最终模型、Prompt 和工具集。
    instrumented.extend(logging_middleware)
    return instrumented


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
    setattr(middleware, "tools", filtered_tools)


def _build_summarization_middleware(
    model: BaseChatModel,
    backend: BackendProtocol,
    *,
    compact_tool_enabled: bool,
) -> list[AgentMiddleware]:
    summarization = create_summarization_middleware(model, backend)
    if not compact_tool_enabled:
        return [summarization]

    tool_middleware = SummarizationToolMiddleware(
        summarization,
        system_prompt=COMPACT_CONVERSATION_SYSTEM_PROMPT,
    )
    for tool in tool_middleware.tools:
        if getattr(tool, "name", "") == "compact_conversation":
            tool.args_schema = CompactConversationSchema
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
    resolved_skills: list[Any] | None,
    resolved_tool_denylist: set[str],
    interrupt_on: dict[str, bool | InterruptOnConfig] | None,
    runtime_middleware: list[AgentMiddleware],
    model_routing_middleware: AgentMiddleware | None,
    tool_invocation_context_middleware: ToolInvocationContextMiddleware,
    tool_output_middleware: ToolOutputMiddleware,
    memory: list[str] | None,
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
        backend=backend,
        skills=resolved_skills,
        system_prompt=(
            SKILLS_SYSTEM_PROMPT
            if "read_file" not in resolved_tool_denylist
            else None
        ),
    )
    filesystem_middleware = FilesystemMiddleware(
        backend=backend,
        system_prompt=FILESYSTEM_SYSTEM_PROMPT,
        custom_tool_descriptions=FILESYSTEM_TOOL_DESCRIPTIONS,
        _permissions=permissions,
        tool_token_limit_before_evict=None,
    )
    _filter_middleware_tools(filesystem_middleware, resolved_tool_denylist)
    if filesystem_middleware.tools:
        deepagent_middleware.append(filesystem_middleware)

    deepagent_middleware.extend(
        [
            *_build_summarization_middleware(
                model,
                backend,
                compact_tool_enabled="compact_conversation" not in resolved_tool_denylist,
            ),
            PatchToolCallsMiddleware(),
            StructuredToolCallMiddleware(),
        ]
    )
    # 必须位于 summarization middleware 之后，才能在同一轮看到新压缩事件，
    # 并把最新 AGENTS.md 重新追加到压缩后的 system prompt 尾部。
    append_workspace_agents_middleware(
        deepagent_middleware,
        workspace_root=workspace_root,
    )

    if runtime_middleware:
        deepagent_middleware.extend(runtime_middleware)
    if model_routing_middleware is not None:
        deepagent_middleware.append(model_routing_middleware)

    for middleware_item in deepagent_middleware:
        _filter_middleware_tools(middleware_item, resolved_tool_denylist)

    if memory:
        deepagent_middleware.append(
            MemoryMiddleware(
                backend=backend,
                sources=memory,
                system_prompt=MEMORY_SYSTEM_PROMPT,
            )
        )

    if interrupt_on:
        deepagent_middleware.append(HumanInTheLoopMiddleware(interrupt_on=interrupt_on))

    return _instrument_prompt_replay(deepagent_middleware)
