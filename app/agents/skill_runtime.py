from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from deepagents.backends import LocalShellBackend
from deepagents.middleware.skills import (
    SkillMetadata,
    SkillsMiddleware,
    append_to_system_message,
)
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.agents.middleware.types import ExtendedModelResponse

from app.core.path_utils import get_workspace_root

WORKSPACE_AGENTS_FILE = "AGENTS.md"
WORKSPACE_SKILLS_SOURCE = "/.boxteam/skills"


class WorkspaceAgentsMiddleware(AgentMiddleware[Any, Any, Any]):
    """把 workspace 根目录 AGENTS.md 注入模型 system message。"""

    def __init__(self, *, workspace_root: Path) -> None:
        self._workspace_root = workspace_root

    def _agents_path(self) -> Path:
        return self._workspace_root / WORKSPACE_AGENTS_FILE

    def _load_agents_text(self) -> str:
        agents_path = self._agents_path()
        if not agents_path.exists():
            return ""
        if not agents_path.is_file():
            raise RuntimeError(f"工作区 AGENTS.md 路径不是文件: {agents_path}")
        content = agents_path.read_text(encoding="utf-8")
        if not content.strip():
            return ""
        return (
            "## Workspace AGENTS.md\n\n"
            "以下内容自动加载自当前工作区根目录 `AGENTS.md`。"
            "它是本地工作区指令；若与更高优先级系统/开发者指令冲突，"
            "以后者为准。\n\n"
            "<workspace_agents_md path=\"/AGENTS.md\">\n"
            f"{content}\n"
            "</workspace_agents_md>"
        )

    def modify_request(self, request: ModelRequest[Any]) -> ModelRequest[Any]:
        agents_text = self._load_agents_text()
        if not agents_text:
            return request
        return request.override(
            system_message=append_to_system_message(
                request.system_message,
                agents_text,
            )
        )

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any] | ExtendedModelResponse[Any]:
        return handler(self.modify_request(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any] | ExtendedModelResponse[Any]:
        return await handler(self.modify_request(request))


class WorkspaceSkillsMiddleware(SkillsMiddleware):
    """加载 workspace skills，但初始提示不展开 allowed-tools。"""

    def _format_skills_list(self, skills: list[SkillMetadata]) -> str:
        if not skills:
            paths = [f"{source_path}" for source_path in self.sources]
            return f"(No skills available yet. You can create skills in {' or '.join(paths)})"

        lines: list[str] = []
        for skill in skills:
            lines.append(f"- **{skill['name']}**: {skill['description']}")
            lines.append(
                f"  -> 用户请求匹配本 skill 描述时，先读取 `{skill['path']}`；"
                "扩展工具调用入口、目标名称和参数以该文件为准"
            )
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


def discover_workspace_custom_tool_skill_map(
    workspace_root: Path | None = None,
    *,
    custom_tool_names: set[str] | None = None,
) -> dict[str, list[str]]:
    """从规范化 skill metadata 建立自定义扩展工具到 skill 名称的映射。"""
    tool_to_skills: dict[str, list[str]] = {}
    for skill in discover_workspace_skill_metadata(workspace_root):
        skill_name = skill.get("name")
        allowed_tools = skill.get("allowed_tools")
        if not isinstance(skill_name, str) or not isinstance(allowed_tools, list):
            continue
        for tool_name in allowed_tools:
            if isinstance(tool_name, str) and tool_name:
                if custom_tool_names is not None and tool_name not in custom_tool_names:
                    continue
                tool_to_skills.setdefault(tool_name, []).append(skill_name)
    return tool_to_skills


def append_skill_middlewares(
    middleware_stack: list[AgentMiddleware],
    *,
    backend: LocalShellBackend | None,
    skills: list[Any] | None,
) -> None:
    """集中维护 workspace skill metadata middleware 顺序。"""
    if skills:
        if backend is None:
            raise RuntimeError("添加 SkillsMiddleware 时必须提供 backend")
        middleware_stack.append(WorkspaceSkillsMiddleware(backend=backend, sources=skills))


def append_workspace_agents_middleware(
    middleware_stack: list[AgentMiddleware],
    *,
    workspace_root: Path,
) -> None:
    """集中维护 workspace 根 AGENTS.md 自动注入 middleware。"""
    middleware_stack.append(WorkspaceAgentsMiddleware(workspace_root=workspace_root))
