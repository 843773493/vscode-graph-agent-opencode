from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from deepagents.backends import LocalShellBackend
from deepagents.middleware.skills import SkillMetadata, SkillsMiddleware
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.agents.middleware.types import ExtendedModelResponse
from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool

from app.core.path_utils import get_workspace_root
from app.agents.tool_identity import tool_definition_name

WORKSPACE_SKILLS_SOURCE = "/.boxteam/skills"


class WorkspaceSkillsMiddleware(SkillsMiddleware):
    """加载 workspace skills，但初始提示不暴露 skill-only 工具名。"""

    def _format_skills_list(self, skills: list[SkillMetadata]) -> str:
        if not skills:
            paths = [f"{source_path}" for source_path in self.sources]
            return f"(No skills available yet. You can create skills in {' or '.join(paths)})"

        lines: list[str] = []
        for skill in skills:
            lines.append(f"- **{skill['name']}**: {skill['description']}")
            lines.append(f"  -> Read `{skill['path']}` for full instructions")
        return "\n".join(lines)


def discover_workspace_skill_sources(workspace_root: Path | None = None) -> list[tuple[str, str]]:
    """发现当前工作区中的 skill sources。"""
    resolved_workspace_root = workspace_root or get_workspace_root()
    skills_dir = resolved_workspace_root / ".boxteam" / "skills"
    if not skills_dir.exists():
        return []
    if not skills_dir.is_dir():
        raise RuntimeError(f"工作区 skill 路径不是目录: {skills_dir}")
    return [(WORKSPACE_SKILLS_SOURCE, "Workspace")]


def discover_workspace_skill_metadata(
    workspace_root: Path | None = None,
) -> list[Mapping[str, Any]]:
    """用 deepagents SkillsMiddleware 同源解析器读取 workspace skill metadata。"""
    resolved_workspace_root = workspace_root or get_workspace_root()
    sources = discover_workspace_skill_sources(resolved_workspace_root)
    if not sources:
        return []

    backend = LocalShellBackend(
        root_dir=str(resolved_workspace_root),
        virtual_mode=True,
    )
    middleware = SkillsMiddleware(
        backend=backend,
        sources=sources,
        system_prompt=None,
    )
    update = middleware.before_agent({}, None, {})
    if update is None:
        return []
    load_errors = update.get("skills_load_errors", [])
    if load_errors:
        raise RuntimeError("; ".join(load_errors))
    return list(update.get("skills_metadata", []))


def discover_workspace_skill_tool_map(
    workspace_root: Path | None = None,
    *,
    hidden_tool_names: set[str] | None = None,
) -> dict[str, list[str]]:
    """从规范化 skill metadata 建立 allowed tool 到 skill 名称的映射。"""
    tool_to_skills: dict[str, list[str]] = {}
    for skill in discover_workspace_skill_metadata(workspace_root):
        skill_name = skill.get("name")
        allowed_tools = skill.get("allowed_tools")
        if not isinstance(skill_name, str) or not isinstance(allowed_tools, list):
            continue
        for tool_name in allowed_tools:
            if isinstance(tool_name, str) and tool_name:
                if hidden_tool_names is not None and tool_name not in hidden_tool_names:
                    continue
                tool_to_skills.setdefault(tool_name, []).append(skill_name)
    return tool_to_skills


def tool_names(tools: Iterable[BaseTool]) -> set[str]:
    return {tool.name for tool in tools}


def _normalized_virtual_path(raw_path: object) -> str | None:
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None

    text = raw_path.strip().replace("\\", "/")
    workspace_path = get_workspace_root().as_posix()
    if text == workspace_path:
        text = "/"
    elif text.startswith(f"{workspace_path}/"):
        text = text[len(workspace_path):]

    if not text.startswith("/"):
        text = f"/{text}"

    while "//" in text:
        text = text.replace("//", "/")

    return text.rstrip("/") or "/"


def _normalize_tool_args(raw_args: object) -> dict[str, Any]:
    if raw_args is None:
        return {}
    if isinstance(raw_args, dict):
        return raw_args
    if isinstance(raw_args, str):
        parsed = json.loads(raw_args)
        if not isinstance(parsed, dict):
            raise TypeError(f"工具参数 JSON 解析后应为 object，实际类型: {type(parsed).__name__}")
        return parsed
    raise TypeError(f"工具参数应为 object 或 JSON string，实际类型: {type(raw_args).__name__}")


def _extract_tool_call_name_and_args(tool_call: object) -> tuple[str | None, dict[str, Any]]:
    if not isinstance(tool_call, dict):
        return None, {}

    if isinstance(tool_call.get("function"), dict):
        function_def = tool_call["function"]
        return str(function_def.get("name") or "") or None, _normalize_tool_args(
            function_def.get("arguments")
        )

    return str(tool_call.get("name") or "") or None, _normalize_tool_args(
        tool_call.get("args", tool_call.get("arguments"))
    )


def _read_skill_paths_from_messages(messages: Sequence[object]) -> set[str]:
    paths: set[str] = set()
    for message in messages:
        if not isinstance(message, AIMessage):
            continue
        tool_calls = list(getattr(message, "tool_calls", []) or [])
        raw_tool_calls = message.additional_kwargs.get("tool_calls")
        if isinstance(raw_tool_calls, list):
            tool_calls.extend(raw_tool_calls)

        for tool_call in tool_calls:
            name, args = _extract_tool_call_name_and_args(tool_call)
            if name != "read_file":
                continue
            path = _normalized_virtual_path(args.get("file_path") or args.get("path"))
            if path:
                paths.add(path)
    return paths


def _activated_hidden_tool_names(
    *,
    messages: Sequence[object],
    skills_metadata: object,
    hidden_tool_names: set[str],
) -> set[str]:
    if not isinstance(skills_metadata, list):
        return set()

    read_paths = _read_skill_paths_from_messages(messages)
    if not read_paths:
        return set()

    activated: set[str] = set()
    for skill in skills_metadata:
        if not isinstance(skill, dict):
            continue
        skill_path = _normalized_virtual_path(skill.get("path"))
        if skill_path not in read_paths:
            continue
        allowed_tools = skill.get("allowed_tools")
        if not isinstance(allowed_tools, list):
            continue
        activated.update(
            tool_name
            for tool_name in allowed_tools
            if isinstance(tool_name, str) and tool_name in hidden_tool_names
        )
    return activated


class SkillToolExposureMiddleware(AgentMiddleware[Any, Any, Any]):
    """只在 skill 被读取后向模型暴露对应隐藏工具。"""

    def __init__(self, *, hidden_tool_names: set[str]) -> None:
        self._hidden_tool_names = hidden_tool_names

    def _filter_request(self, request: ModelRequest[Any]) -> ModelRequest[Any]:
        if not self._hidden_tool_names:
            return request

        activated = _activated_hidden_tool_names(
            messages=request.messages,
            skills_metadata=request.state.get("skills_metadata"),
            hidden_tool_names=self._hidden_tool_names,
        )
        filtered_tools = [
            tool_def
            for tool_def in request.tools
            if (name := tool_definition_name(tool_def)) is None
            or name not in self._hidden_tool_names
            or name in activated
        ]
        return request.override(tools=filtered_tools)

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any] | ExtendedModelResponse[Any]:
        return handler(self._filter_request(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any] | ExtendedModelResponse[Any]:
        return await handler(self._filter_request(request))


def append_skill_middlewares(
    middleware_stack: list[AgentMiddleware],
    *,
    backend: LocalShellBackend | None,
    skills: list[Any] | None,
    hidden_tool_names: set[str],
) -> None:
    """集中维护 skill metadata 和隐藏工具暴露的 middleware 顺序。"""
    if skills:
        if backend is None:
            raise RuntimeError("添加 SkillsMiddleware 时必须提供 backend")
        middleware_stack.append(WorkspaceSkillsMiddleware(backend=backend, sources=skills))
    if hidden_tool_names:
        middleware_stack.append(
            SkillToolExposureMiddleware(hidden_tool_names=hidden_tool_names)
        )
