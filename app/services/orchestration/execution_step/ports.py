"""step 的显式依赖与 Agent factory 端口，不传递 public service 实例。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from app.services.orchestration.event_stream.contracts import AgentEventSource

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver

    from app.abstractions.job_event_bus import JobEventBusProtocol
    from app.abstractions.session_changes import SessionChangesRecorderProtocol
    from app.abstractions.session_context import SessionLookupProtocol
    from app.abstractions.tool_selection import ToolSelectionReader
    from app.services.infrastructure.config_service import ConfigService
    from app.services.infrastructure.external_resource_leases import (
        ExternalResourceLeaseLedger,
    )
    from app.services.infrastructure.message_stream_store import MessageStreamStore


class StepAgentFactory(Protocol):
    """按已冻结的本次请求选项构建 Agent，不让 retry 接触构建依赖。"""

    def __call__(
        self,
        *,
        session_id: str,
        agent_id: str,
        execution_overrides: Mapping[str, bool],
        model_visibility_overrides: Mapping[str, bool],
        preferred_provider_id: str | None,
        include_team_tools: bool,
    ) -> AgentEventSource: ...


@dataclass(frozen=True, slots=True)
class StepExecutionPorts:
    """一个 runner 的固定端口；可变执行状态只能存在于单次 run 内。"""

    config_service: ConfigService
    job_event_bus: JobEventBusProtocol
    tool_selection_store: ToolSelectionReader
    session_changes_service: SessionChangesRecorderProtocol
    message_stream_store: MessageStreamStore
    workspace_root: Path
    agent_factory: StepAgentFactory
    checkpointer_provider: Callable[[], BaseCheckpointSaver]
    session_service_provider: Callable[[], SessionLookupProtocol]
    external_resource_leases: ExternalResourceLeaseLedger | None = None
    model_timeout_seconds: float | None = None
