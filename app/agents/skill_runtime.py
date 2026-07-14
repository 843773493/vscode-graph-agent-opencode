from __future__ import annotations

import difflib
import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Annotated, Any, NotRequired, TypedDict

from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.skills import (
    SkillMetadata,
    SkillsMiddleware,
    append_to_system_message,
)
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.agents.middleware.types import (
    AgentState,
    ExtendedModelResponse,
    PrivateStateAttr,
)
from langchain_core.messages import BaseMessage, HumanMessage
from langgraph.runtime import Runtime

from app.core.path_utils import get_workspace_root
from app.agents.workspace_backend import build_workspace_backend

WORKSPACE_AGENTS_FILE = "AGENTS.md"
WORKSPACE_SKILLS_SOURCE = "/.boxteam/skills"


class WorkspaceAgentsSnapshot(TypedDict):
    applied_content: str
    observed_content: NotRequired[str]
    compaction_marker: str | None


class WorkspaceAgentsState(AgentState):
    _workspace_agents_snapshot: Annotated[
        NotRequired[WorkspaceAgentsSnapshot],
        PrivateStateAttr,
    ]


class WorkspaceAgentsMiddleware(AgentMiddleware[Any, Any, Any]):
    """冻结会话 AGENTS.md system prompt，并把后续变化追加为 reminder。"""

    state_schema = WorkspaceAgentsState

    def __init__(self, *, workspace_root: Path) -> None:
        self._workspace_root = workspace_root

    def _agents_path(self) -> Path:
        return self._workspace_root / WORKSPACE_AGENTS_FILE

    def _load_agents_content(self) -> str:
        agents_path = self._agents_path()
        if not agents_path.exists():
            return ""
        if not agents_path.is_file():
            raise RuntimeError(f"工作区 AGENTS.md 路径不是文件: {agents_path}")
        content = agents_path.read_text(encoding="utf-8")
        return content if content.strip() else ""

    @staticmethod
    def _format_agents_text(content: str) -> str:
        if not content:
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

    @staticmethod
    def _compaction_marker(event: object) -> str | None:
        if event is None:
            return None
        if not isinstance(event, Mapping):
            raise TypeError("_summarization_event 必须是 mapping")
        cutoff_index = event.get("cutoff_index")
        if not isinstance(cutoff_index, int) or cutoff_index < 0:
            raise TypeError("_summarization_event.cutoff_index 必须是非负整数")
        summary_message = event.get("summary_message")
        if isinstance(summary_message, BaseMessage):
            serialized_summary: object = summary_message.model_dump(mode="json")
        elif isinstance(summary_message, Mapping):
            serialized_summary = dict(summary_message)
        else:
            raise TypeError("_summarization_event.summary_message 必须是消息对象")
        marker_source = json.dumps(
            {
                "cutoff_index": cutoff_index,
                "file_path": event.get("file_path"),
                "summary_message": serialized_summary,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(marker_source.encode("utf-8")).hexdigest()

    @staticmethod
    def _snapshot(state: WorkspaceAgentsState) -> WorkspaceAgentsSnapshot | None:
        snapshot = state.get("_workspace_agents_snapshot")
        if snapshot is None:
            return None
        if not isinstance(snapshot, Mapping):
            raise TypeError("_workspace_agents_snapshot 必须是 mapping")
        applied_content = snapshot.get("applied_content")
        observed_content = snapshot.get("observed_content")
        compaction_marker = snapshot.get("compaction_marker")
        if not isinstance(applied_content, str):
            raise TypeError("_workspace_agents_snapshot.applied_content 必须是字符串")
        if observed_content is not None and not isinstance(observed_content, str):
            raise TypeError("_workspace_agents_snapshot.observed_content 必须是字符串")
        if compaction_marker is not None and not isinstance(compaction_marker, str):
            raise TypeError("_workspace_agents_snapshot.compaction_marker 必须是字符串")
        normalized: WorkspaceAgentsSnapshot = {
            "applied_content": applied_content,
            "compaction_marker": compaction_marker,
        }
        if observed_content is not None:
            normalized["observed_content"] = observed_content
        return normalized

    @staticmethod
    def _build_change_reminder(previous: str, current: str) -> str:
        diff = "".join(
            difflib.unified_diff(
                previous.splitlines(keepends=True),
                current.splitlines(keepends=True),
                fromfile="AGENTS.md（会话已应用版本）",
                tofile="AGENTS.md（当前工作区版本）",
            )
        )
        if not diff:
            raise RuntimeError("AGENTS.md 内容变化但没有生成差异")
        return (
            "<system_reminder>\n"
            "工作区根目录 AGENTS.md 在当前会话期间发生变化。为保持模型提示缓存，"
            "本轮不会替换 system prompt 中已加载的完整版本；以下增量变更从现在起生效。"
            "会话上下文完成压缩后，当前工作区的最新完整 AGENTS.md 将重新加载到 "
            "system prompt。\n\n"
            "<workspace_agents_md_change path=\"/AGENTS.md\">\n"
            f"{diff}"
            "</workspace_agents_md_change>\n"
            "</system_reminder>"
        )

    def before_model(
        self,
        state: WorkspaceAgentsState,
        runtime: Runtime,
    ) -> dict[str, Any] | None:
        del runtime
        current_content = self._load_agents_content()
        current_marker = self._compaction_marker(state.get("_summarization_event"))
        snapshot = self._snapshot(state)
        if snapshot is None:
            return {
                "_workspace_agents_snapshot": {
                    "applied_content": current_content,
                    "compaction_marker": current_marker,
                }
            }

        if current_marker is not None and current_marker != snapshot["compaction_marker"]:
            return {
                "_workspace_agents_snapshot": {
                    "applied_content": current_content,
                    "compaction_marker": current_marker,
                }
            }

        previous_content = snapshot.get(
            "observed_content",
            snapshot["applied_content"],
        )
        if current_content == previous_content:
            return None

        reminder = self._build_change_reminder(previous_content, current_content)
        return {
            "_workspace_agents_snapshot": {
                **snapshot,
                "observed_content": current_content,
            },
            "messages": [
                HumanMessage(
                    content=reminder,
                    response_metadata={
                        "source": "workspace_agents_change",
                        "path": "/AGENTS.md",
                    },
                )
            ],
        }

    def modify_request(self, request: ModelRequest[Any]) -> ModelRequest[Any]:
        snapshot = self._snapshot(request.state)
        if snapshot is None:
            raise RuntimeError(
                "WorkspaceAgentsMiddleware.before_model 未初始化会话 AGENTS.md 快照"
            )
        agents_text = self._format_agents_text(snapshot["applied_content"])
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

    backend = build_workspace_backend(resolved_workspace_root)
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
    backend: BackendProtocol | None,
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
