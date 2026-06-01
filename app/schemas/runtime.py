from __future__ import annotations

from pydantic import BaseModel


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
    loaded_agents: list[str] = []
    storage: RuntimeStorageDTO


class RuntimeShutdownDTO(BaseModel):
    status: str
    delay_seconds: int


class UiSnapshotResultDTO(BaseModel):
    html_path: str
    status: str = "saved"


class RuntimeInfoDTO(BaseModel):
    pid: int
    uptime_seconds: int
    workspace_id: str
    active_jobs: int
    loaded_agents: list[str]
    storage: RuntimeStorageDTO


class RuntimeShutdownResultDTO(BaseModel):
    status: str
    delay_seconds: int
