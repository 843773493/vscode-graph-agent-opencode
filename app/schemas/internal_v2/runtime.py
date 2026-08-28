from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

RuntimeLifecycleState = Literal["ready", "draining", "stopping"]


class RuntimeDrainBlockerDTO(BaseModel):
    kind: Literal["job", "tool", "background_task"]
    resource_id: str
    session_id: str
    status: str
    detail: str | None = None


class RuntimeStorageDTO(BaseModel):
    root: str
    artifact_dir: str
    log_dir: str
    cache_dir: str


class RuntimeStatusDTO(BaseModel):
    pid: int
    uptime_seconds: int
    workspace_id: str
    active_jobs: int
    lifecycle_state: RuntimeLifecycleState
    accepting_jobs: bool
    blockers: list[RuntimeDrainBlockerDTO] = Field(default_factory=list)
    loaded_agents: list[str] = Field(default_factory=list)
    storage: RuntimeStorageDTO


class RuntimeShutdownDTO(BaseModel):
    status: str
    delay_seconds: int


class RuntimeDrainResultDTO(BaseModel):
    lifecycle_state: RuntimeLifecycleState
    accepting_jobs: bool
    blockers: list[RuntimeDrainBlockerDTO] = Field(default_factory=list)
    interrupted_resources: int = 0


class UiSnapshotResultDTO(BaseModel):
    html_path: str
    status: str = "saved"


class RuntimeInfoDTO(BaseModel):
    pid: int
    uptime_seconds: int
    workspace_id: str
    active_jobs: int
    lifecycle_state: RuntimeLifecycleState
    accepting_jobs: bool
    blockers: list[RuntimeDrainBlockerDTO]
    loaded_agents: list[str]
    storage: RuntimeStorageDTO


class RuntimeShutdownResultDTO(BaseModel):
    status: str
    delay_seconds: int
