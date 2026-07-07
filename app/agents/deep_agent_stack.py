from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from deepagents.backends import LocalShellBackend
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

from app.agents.skill_runtime import append_skill_middlewares
from app.agents.summarization_paths import apply_boxteam_summarization_paths
from app.agents.tool_identity import tool_definition_name


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
    backend: LocalShellBackend,
) -> list[AgentMiddleware]:
    summarization = create_summarization_middleware(model, backend)
    apply_boxteam_summarization_paths(summarization)
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
    backend: LocalShellBackend,
    permissions: list[FilesystemPermission] | None,
    skills: list[Any] | None,
    hidden_tool_names: set[str],
) -> list[AgentMiddleware]:
    middleware = [
        TodoListMiddleware(),
        FilesystemMiddleware(backend=backend, _permissions=permissions),
        *_build_summarization_middleware(model, backend),
        PatchToolCallsMiddleware(),
    ]
    append_skill_middlewares(
        middleware,
        backend=backend,
        skills=skills,
        hidden_tool_names=hidden_tool_names,
    )
    return middleware


def build_deep_agent_middleware(
    *,
    model: BaseChatModel,
    backend: LocalShellBackend,
    permissions: list[FilesystemPermission] | None,
    resolved_skills: list[Any] | None,
    hidden_tool_names: set[str],
    resolved_tools: list[ToolDefinition],
    subagents: Sequence[SubAgent | CompiledSubAgent | AsyncSubAgent] | None,
    resolved_tool_denylist: set[str],
    interrupt_on: dict[str, bool | InterruptOnConfig] | None,
    runtime_middleware: list[AgentMiddleware],
    memory: list[str] | None,
) -> list[AgentMiddleware]:
    gp_middleware = _base_subagent_middleware(
        model=model,
        backend=backend,
        permissions=permissions,
        skills=resolved_skills,
        hidden_tool_names=hidden_tool_names,
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
                permissions=spec.get("permissions"),
                skills=spec.get("skills"),
                hidden_tool_names=hidden_tool_names,
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
        TodoListMiddleware(),
    ]
    append_skill_middlewares(
        deepagent_middleware,
        backend=backend,
        skills=resolved_skills,
        hidden_tool_names=set(),
    )
    deepagent_middleware.extend(
        [
            FilesystemMiddleware(backend=backend),
            SubAgentMiddleware(
                backend=backend,
                subagents=inline_subagents,
            ),
            *_build_summarization_middleware(model, backend),
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

    return deepagent_middleware
