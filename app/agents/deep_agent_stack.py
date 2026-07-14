from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.async_subagents import AsyncSubAgent
from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.memory import MemoryMiddleware
from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
from deepagents.middleware.permissions import FilesystemPermission
from deepagents.middleware.subagents import CompiledSubAgent, SubAgent, SubAgentMiddleware
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
from app.agents.request_replay_middleware import PromptReplayCaptureMiddleware
from app.agents.structured_tool_call_middleware import StructuredToolCallMiddleware
from app.agents.tool_identity import tool_definition_name
from app.agents.tool_output_middleware import ToolOutputMiddleware


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


ToolDefinition = BaseTool | Callable[..., Any] | dict[str, Any]


_PROMPT_REPLAY_LABELS = {
    "TodoListMiddleware": "任务规划指令",
    "WorkspaceSkillsMiddleware": "Skills 索引",
    "FilesystemMiddleware": "文件系统与环境信息",
    "SubAgentMiddleware": "子代理指令",
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


def _filter_subagent_specs(
    subagent_specs: list[Any],
    denylist: set[str],
) -> list[Any]:
    if not denylist:
        return subagent_specs

    filtered_specs: list[Any] = []
    for spec in subagent_specs:
        processed_spec = dict(spec)
        if processed_spec.get("tools") is not None:
            processed_spec["tools"] = filter_tools_by_name(
                list(processed_spec["tools"]),
                denylist,
            )

        nested_subagents = processed_spec.get("subagents")
        if isinstance(nested_subagents, list):
            processed_spec["subagents"] = _filter_subagent_specs(
                nested_subagents,
                denylist,
            )

        filtered_specs.append(processed_spec)

    return filtered_specs


def _build_summarization_middleware(
    model: BaseChatModel,
    backend: BackendProtocol,
) -> list[AgentMiddleware]:
    summarization = create_summarization_middleware(model, backend)
    tool_middleware = SummarizationToolMiddleware(summarization)
    for tool in tool_middleware.tools:
        if getattr(tool, "name", "") == "compact_conversation":
            tool.args_schema = CompactConversationSchema
    return [
        summarization,
        tool_middleware,
    ]


def _base_subagent_middleware(
    *,
    model: BaseChatModel,
    backend: BackendProtocol,
    workspace_root: Path,
    permissions: list[FilesystemPermission] | None,
    skills: list[Any] | None,
    model_routing_middleware: AgentMiddleware | None,
    tool_output_middleware: ToolOutputMiddleware,
) -> list[AgentMiddleware]:
    middleware = [
        tool_output_middleware,
        TodoListMiddleware(),
        FilesystemMiddleware(
            backend=backend,
            _permissions=permissions,
            tool_token_limit_before_evict=None,
        ),
        *_build_summarization_middleware(model, backend),
        PatchToolCallsMiddleware(),
        StructuredToolCallMiddleware(),
    ]
    if model_routing_middleware is not None:
        middleware.append(model_routing_middleware)
    append_workspace_agents_middleware(
        middleware,
        workspace_root=workspace_root,
    )
    append_skill_middlewares(
        middleware,
        backend=backend,
        skills=skills,
    )
    return middleware


def build_deep_agent_middleware(
    *,
    model: BaseChatModel,
    backend: BackendProtocol,
    workspace_root: Path,
    permissions: list[FilesystemPermission] | None,
    resolved_skills: list[Any] | None,
    resolved_tools: list[ToolDefinition],
    subagents: Sequence[SubAgent | CompiledSubAgent | AsyncSubAgent] | None,
    resolved_tool_denylist: set[str],
    interrupt_on: dict[str, bool | InterruptOnConfig] | None,
    runtime_middleware: list[AgentMiddleware],
    model_routing_middleware: AgentMiddleware | None,
    tool_output_middleware: ToolOutputMiddleware,
    memory: list[str] | None,
) -> list[AgentMiddleware]:
    gp_middleware = _base_subagent_middleware(
        model=model,
        backend=backend,
        workspace_root=workspace_root,
        permissions=permissions,
        skills=resolved_skills,
        model_routing_middleware=model_routing_middleware,
        tool_output_middleware=tool_output_middleware,
    )
    general_purpose_spec = {
        **GENERAL_PURPOSE_SUBAGENT,
        "model": model,
        "tools": list(resolved_tools) if resolved_tools else [],
        "middleware": gp_middleware,
    }
    if interrupt_on:
        general_purpose_spec["interrupt_on"] = interrupt_on

    inline_subagents: list[dict[str, Any]] = []
    if subagents:
        for spec in _filter_subagent_specs(list(subagents), resolved_tool_denylist):
            subagent_interrupt_on = spec.get("interrupt_on", interrupt_on)
            subagent_middleware = _base_subagent_middleware(
                model=model,
                backend=backend,
                workspace_root=workspace_root,
                permissions=spec.get("permissions"),
                skills=spec.get("skills"),
                model_routing_middleware=(
                    model_routing_middleware if "model" not in spec else None
                ),
                tool_output_middleware=tool_output_middleware,
            )
            subagent_middleware.extend(spec.get("middleware", []))

            processed_spec = {
                **spec,
                "model": spec.get("model", model),
                "tools": spec.get(
                    "tools",
                    list(resolved_tools) if resolved_tools else [],
                ),
                "middleware": subagent_middleware,
            }
            if subagent_interrupt_on:
                processed_spec["interrupt_on"] = subagent_interrupt_on
            inline_subagents.append(processed_spec)

    if not any(spec.get("name") == GENERAL_PURPOSE_SUBAGENT["name"] for spec in inline_subagents):
        inline_subagents.insert(0, general_purpose_spec)

    deepagent_middleware = [
        tool_output_middleware,
        TodoListMiddleware(),
    ]
    append_skill_middlewares(
        deepagent_middleware,
        backend=backend,
        skills=resolved_skills,
    )
    deepagent_middleware.extend(
        [
            FilesystemMiddleware(
                backend=backend,
                tool_token_limit_before_evict=None,
            ),
            SubAgentMiddleware(
                backend=backend,
                subagents=inline_subagents,
            ),
            *_build_summarization_middleware(model, backend),
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
        deepagent_middleware.append(MemoryMiddleware(backend=backend, sources=memory))

    if interrupt_on:
        deepagent_middleware.append(HumanInTheLoopMiddleware(interrupt_on=interrupt_on))

    return _instrument_prompt_replay(deepagent_middleware)
