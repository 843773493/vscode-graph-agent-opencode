from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Protocol, TypeVar, cast

from fastapi import Depends, Header, HTTPException, Request

from app.abstractions.job_service import JobServiceProtocol
from app.core.path_utils import SessionCatalogComponents, workspace_context
from app.core.trace_middleware import get_request_id  # noqa: F401
from app.core.workspace_identity import validate_workspace_id
from app.runtime.session_orchestrator import SessionOrchestrator
from app.services.business.agent_service import AgentService
from app.services.business.browser_resources.owner import BrowserResourceOwner
from app.services.business.context_compaction_service import ContextCompactionService
from app.services.business.message_service import MessageService
from app.services.business.session_changes_service import SessionChangesService
from app.services.business.session_context_fork_service import SessionContextForkService
from app.services.business.session_context_query_service import (
    SessionContextQueryService,
)
from app.services.business.session_generation import SessionGenerationService
from app.services.business.session_goal_service import SessionGoalService
from app.services.business.session_information_service import SessionInformationService
from app.services.business.session_interrupt_service import SessionInterruptService
from app.services.business.session_navigation import SessionCatalogService
from app.services.business.session_resource_service import SessionResourceService
from app.services.business.session_service import SessionService
from app.services.business.session_skill_tracking_service import (
    SessionSkillTrackingService,
)
from app.services.business.session_turn_history import SessionTurnHistoryService
from app.services.business.session_turn_replay_service import SessionTurnReplayService
from app.services.business.team.service import TeamCoordinationService
from app.services.business.terminal_resources.owner import TerminalResourceOwner
from app.services.business.workspace_resources.owner import WorkspaceResourceOwner
from app.services.event_service import EventService
from app.services.infrastructure.artifact_service import ArtifactService
from app.services.infrastructure.attachment_blob_catalog.store import (
    AttachmentBlobStore,
)
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.file_tree_settings_service import (
    FileTreeSettingsService,
)
from app.services.infrastructure.llm_request_log_service import LLMRequestLogService
from app.services.infrastructure.log_service import LogService
from app.services.infrastructure.mcp import McpCatalogOwner
from app.services.infrastructure.message_stream_store import MessageStreamStore
from app.services.infrastructure.node_debug.service import NodeDebugService
from app.services.infrastructure.resource_platform.sources.resource_navigation import (
    ResourceNavigation,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.runtime_service import RuntimeService
from app.services.infrastructure.tool_service import ToolService
from app.services.infrastructure.workspace_file_watch_service import (
    WorkspaceFileWatchService,
)
from app.services.infrastructure.workspace_service import WorkspaceService
from app.services.infrastructure.workspace_state_store import WorkspaceActivityService
from app.services.orchestration.communication.service import SessionCommunicationService
from app.services.orchestration.goal_runtime_service import GoalRuntimeService
from app.services.orchestration.thread_residency import ThreadResidencyTracker
from app.tool_testing.service import ToolTestService


class _AppContainerProtocol(Protocol):
    workspace_id: str
    workspace_root: Path
    session_catalog: SessionCatalogComponents
    config_service: ConfigService
    agent_service: AgentService
    artifact_service: ArtifactService
    event_service: EventService
    job_service: JobServiceProtocol
    message_service: MessageService
    attachment_blob_store: AttachmentBlobStore
    runtime_service: RuntimeService
    goal_service: SessionGoalService
    goal_runtime_service: GoalRuntimeService
    session_interrupt_service: SessionInterruptService
    session_skill_tracking_service: SessionSkillTrackingService
    session_context_fork_service: SessionContextForkService
    session_turn_replay_service: SessionTurnReplayService
    session_turn_history_service: SessionTurnHistoryService
    context_compaction_service: ContextCompactionService
    session_changes_service: SessionChangesService
    session_information_service: SessionInformationService
    session_context_query_service: SessionContextQueryService
    session_resource_service: SessionResourceService
    terminal_resource_owner: TerminalResourceOwner
    browser_resource_owner: BrowserResourceOwner
    session_service: SessionService
    llm_request_log_service: LLMRequestLogService
    log_service: LogService
    tool_service: ToolService
    tool_test_service: ToolTestService
    workspace_service: WorkspaceService
    resource_navigation: ResourceNavigation
    workspace_file_watch_service: WorkspaceFileWatchService
    file_tree_settings_service: FileTreeSettingsService
    node_debug_service: NodeDebugService
    session_orchestrator: SessionOrchestrator
    session_catalog_service: SessionCatalogService
    session_generation_service: SessionGenerationService
    session_communication_service: SessionCommunicationService
    team_service: TeamCoordinationService
    mcp_catalog_owner: McpCatalogOwner
    workspace_activity_service: WorkspaceActivityService
    message_stream_store: MessageStreamStore


class _WorkspaceContainerRegistryProtocol(Protocol):
    def get(self, workspace_id: str) -> _AppContainerProtocol: ...


T = TypeVar("T")


def get_terminal_resource_owner(request: Request) -> TerminalResourceOwner:
    return _require_service(request, "terminal_resource_owner", TerminalResourceOwner, "Terminal owner 尚未初始化")



def get_browser_resource_owner(request: Request) -> BrowserResourceOwner:
    return _require_service(request, "browser_resource_owner", BrowserResourceOwner, "Browser owner 尚未初始化")


def get_workspace_resource_owner(
    resource_id: str,
    terminal_owner: Annotated[TerminalResourceOwner, Depends(get_terminal_resource_owner)],
    browser_owner: Annotated[BrowserResourceOwner, Depends(get_browser_resource_owner)],
) -> WorkspaceResourceOwner:
    if resource_id.startswith("term_"):
        return terminal_owner
    if resource_id.startswith("browser_"):
        return browser_owner
    raise HTTPException(status_code=404, detail="资源身份不属于已注册的领域 owner")


def _require_service(
    request: Request,
    attribute: str,
    expected_type: type[T],
    message: str,
) -> T:
    """从应用容器取服务实例；缺失或类型不符时显式报错，不返回虚假默认值。"""
    service = getattr(_get_container(request), attribute, None)
    # 容器装配缺失是启动期状态问题，必须沿用既有 RuntimeError 契约，
    # 不改成 TypeError（那会泄漏成 500 且改变调用方可见的错误类型）。
    if not isinstance(service, expected_type):
        raise RuntimeError(message)  # noqa: TRY004
    return service


def verify_local_token(x_local_token: str | None = Header(default=None)) -> str:
    expected = "local-dev-token"
    if x_local_token != expected:
        raise HTTPException(status_code=401, detail="invalid local token")
    return x_local_token


def get_workspace_container(request: Request) -> _AppContainerProtocol:
    """从显式 path UUID 或 Gateway 内部头取得唯一 workspace 服务图。"""

    path_workspace_id = request.path_params.get("workspace_id")
    header_workspace_id = request.headers.get("X-BoxTeam-Workspace-Id")
    if (
        path_workspace_id is not None
        and header_workspace_id is not None
        and path_workspace_id != header_workspace_id
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "workspace path UUID 与 X-BoxTeam-Workspace-Id 不一致: "
                f"path={path_workspace_id!r}, header={header_workspace_id!r}"
            ),
        )
    workspace_id = path_workspace_id or header_workspace_id
    if not isinstance(workspace_id, str) or not workspace_id:
        raise HTTPException(status_code=400, detail="缺少显式 workspace_id")
    try:
        workspace_id = validate_workspace_id(workspace_id)
    except RuntimeError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    registry = cast(
        _WorkspaceContainerRegistryProtocol | None,
        getattr(request.app.state, "workspace_container_registry", None),
    )
    if registry is None:
        raise RuntimeError("已挂载工作区服务图注册表尚未初始化")
    try:
        return registry.get(workspace_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


async def workspace_container_scope(
    request: Request,
) -> AsyncIterator[_AppContainerProtocol]:
    """在整个请求期间绑定按显式 ID 解析出的工作区根目录上下文。"""

    container = get_workspace_container(request)
    with workspace_context(
        container.workspace_id,
        container.workspace_root,
        session_catalog=container.session_catalog,
    ):
        yield container


def _get_container(request: Request) -> _AppContainerProtocol:
    return get_workspace_container(request)


def get_config_service(request: Request) -> ConfigService:
    service = getattr(_get_container(request), "config_service", None)
    if not isinstance(service, ConfigService):
        raise RuntimeError("ConfigService 尚未在应用启动阶段初始化")  # noqa: TRY004
    return service


def get_agent_service(request: Request) -> AgentService:
    return _require_service(request, "agent_service", AgentService, "AgentService 尚未在应用启动阶段初始化")


def get_artifact_service(request: Request) -> ArtifactService:
    return _require_service(request, "artifact_service", ArtifactService, "ArtifactService 尚未在应用启动阶段初始化")


def get_event_service(request: Request) -> EventService:
    return _require_service(request, "event_service", EventService, "EventService 尚未在应用启动阶段初始化")


def get_job_service(request: Request) -> JobServiceProtocol:
    return _require_service(request, "job_service", JobServiceProtocol, "JobService 尚未在应用启动阶段初始化")


def get_workspace_activity_service(request: Request) -> WorkspaceActivityService:
    return _require_service(request, "workspace_activity_service", WorkspaceActivityService, "Workspace 活动事件服务尚未在应用启动阶段初始化")


def get_node_debug_service(request: Request) -> NodeDebugService:
    return _require_service(request, "node_debug_service", NodeDebugService, "NodeDebugService 尚未在应用启动阶段初始化")


def get_message_service(request: Request) -> MessageService:
    return _require_service(request, "message_service", MessageService, "MessageService 尚未在应用启动阶段初始化")


def get_attachment_blob_store(request: Request) -> AttachmentBlobStore:
    return _require_service(request, "attachment_blob_store", AttachmentBlobStore, "AttachmentBlobStore 尚未在应用启动阶段初始化")


def get_runtime_service(request: Request) -> RuntimeService:
    return _require_service(request, "runtime_service", RuntimeService, "RuntimeService 尚未在应用启动阶段初始化")


def get_goal_service(request: Request) -> SessionGoalService:
    return _require_service(request, "goal_service", SessionGoalService, "SessionGoalService 尚未在应用启动阶段初始化")


def get_goal_runtime_service(request: Request) -> GoalRuntimeService:
    return _require_service(request, "goal_runtime_service", GoalRuntimeService, "GoalRuntimeService 尚未在应用启动阶段初始化")


def get_session_interrupt_service(request: Request) -> SessionInterruptService:
    return _require_service(request, "session_interrupt_service", SessionInterruptService, "SessionInterruptService 尚未在应用启动阶段初始化")


def get_session_skill_tracking_service(request: Request) -> SessionSkillTrackingService:
    return _require_service(request, "session_skill_tracking_service", SessionSkillTrackingService, "SessionSkillTrackingService 尚未在应用启动阶段初始化")


def get_message_stream_store(request: Request) -> MessageStreamStore:
    return _require_service(request, "message_stream_store", MessageStreamStore, "MessageStreamStore 尚未在应用启动阶段初始化")


def get_session_changes_service(request: Request) -> SessionChangesService:
    return _require_service(request, "session_changes_service", SessionChangesService, "SessionChangesService 尚未在应用启动阶段初始化")


def get_session_information_service(request: Request) -> SessionInformationService:
    return _require_service(request, "session_information_service", SessionInformationService, "SessionInformationService 尚未在应用启动阶段初始化")


def get_session_context_query_service(request: Request) -> SessionContextQueryService:
    return _require_service(request, "session_context_query_service", SessionContextQueryService, "SessionContextQueryService 尚未在应用启动阶段初始化")


def get_context_compaction_service(request: Request) -> ContextCompactionService:
    return _require_service(request, "context_compaction_service", ContextCompactionService, "ContextCompactionService 尚未在应用启动阶段初始化")


def get_session_resource_service(request: Request) -> SessionResourceService:
    return _require_service(request, "session_resource_service", SessionResourceService, "SessionResourceService 尚未在应用启动阶段初始化")


def get_session_service(request: Request) -> SessionService:
    return _require_service(request, "session_service", SessionService, "SessionService 尚未在应用启动阶段初始化")


def get_session_context_fork_service(request: Request) -> SessionContextForkService:
    return _require_service(request, "session_context_fork_service", SessionContextForkService, "SessionContextForkService 尚未在应用启动阶段初始化")


def get_session_turn_replay_service(request: Request) -> SessionTurnReplayService:
    return _require_service(request, "session_turn_replay_service", SessionTurnReplayService, "SessionTurnReplayService 尚未在应用启动阶段初始化")


def get_session_turn_history_service(request: Request) -> SessionTurnHistoryService:
    return _require_service(request, "session_turn_history_service", SessionTurnHistoryService, "SessionTurnHistoryService 尚未在应用启动阶段初始化")


def get_llm_request_log_service(request: Request) -> LLMRequestLogService:
    return _require_service(request, "llm_request_log_service", LLMRequestLogService, "LLMRequestLogService 尚未在应用启动阶段初始化")


def get_log_service(request: Request) -> LogService:
    return _require_service(request, "log_service", LogService, "LogService 尚未在应用启动阶段初始化")


def get_tool_service(request: Request) -> ToolService:
    return _require_service(request, "tool_service", ToolService, "ToolService 尚未在应用启动阶段初始化")


def get_tool_test_service(request: Request) -> ToolTestService:
    return _require_service(request, "tool_test_service", ToolTestService, "ToolTestService 尚未在应用启动阶段初始化")


def get_mcp_catalog_owner(request: Request) -> McpCatalogOwner:
    return _require_service(request, "mcp_catalog_owner", McpCatalogOwner, "McpCatalogOwner 尚未在应用启动阶段初始化")


def get_workspace_service(request: Request) -> WorkspaceService:
    return _require_service(request, "workspace_service", WorkspaceService, "WorkspaceService 尚未在应用启动阶段初始化")


def get_resource_navigation(request: Request) -> ResourceNavigation:
    return _require_service(request, "resource_navigation", ResourceNavigation, "ResourceNavigation 尚未在应用启动阶段初始化")


def get_workspace_file_watch_service(request: Request) -> WorkspaceFileWatchService:
    return _require_service(request, "workspace_file_watch_service", WorkspaceFileWatchService, "WorkspaceFileWatchService 尚未在应用启动阶段初始化")


def get_file_tree_settings_service(request: Request) -> FileTreeSettingsService:
    return _require_service(request, "file_tree_settings_service", FileTreeSettingsService, "FileTreeSettingsService 尚未在应用启动阶段初始化")


def get_session_communication_service(request: Request) -> SessionCommunicationService:
    return _require_service(request, "session_communication_service", SessionCommunicationService, "SessionCommunicationService 尚未在应用启动阶段初始化")


def get_team_service(request: Request) -> TeamCoordinationService:
    return _require_service(request, "team_service", TeamCoordinationService, "TeamCoordinationService 尚未初始化")


def get_session_orchestrator(request: Request) -> SessionOrchestrator:
    return _require_service(request, "session_orchestrator", SessionOrchestrator, "SessionOrchestrator 尚未在应用启动阶段初始化")


def get_session_catalog_service(request: Request) -> SessionCatalogService:
    return _require_service(request, "session_catalog_service", SessionCatalogService, "SessionCatalogService 尚未在应用启动阶段初始化")


def get_session_generation_service(request: Request) -> SessionGenerationService:
    return _require_service(request, "session_generation_service", SessionGenerationService, "SessionGenerationService 尚未在应用启动阶段初始化")


def get_thread_residency_tracker(request: Request) -> ThreadResidencyTracker:
    return _require_service(request, "thread_residency_tracker", ThreadResidencyTracker, "ThreadResidencyTracker 尚未初始化")


def get_rollout_checkpoint_saver(request: Request) -> RolloutCheckpointSaver:
    return _require_service(request, "checkpointer", RolloutCheckpointSaver, "RolloutCheckpointSaver 尚未初始化")
