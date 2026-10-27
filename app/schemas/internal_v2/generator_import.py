"""旧 Gateway 生成器的一次性导入输入；正常生产入口只使用持久任务合同。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.internal_v2.legacy_generators import GenerationOutputDTO
from app.schemas.internal_v2.persistent_task import PersistentTaskConfigurationDTO


class LegacyGenerationRun(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    run_id: str = Field(min_length=1)
    generator_id: str
    idempotency_key: str = Field(min_length=1)
    status: Literal["planned", "dispatching", "running", "reporting", "completed", "partial", "failed", "cancelled", "skipped"]
    trigger_type: Literal["manual", "interval", "cron"]
    scheduled_for: datetime
    outputs: list[GenerationOutputDTO] = Field(default_factory=list)
    execution_workspace_id: str | None = None
    message_id: str | None = None
    job_id: str | None = None
    report_back_job_id: str | None = None
    error: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None


class SessionGeneratorImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    task_id: str = Field(min_length=1)
    source_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    configuration: PersistentTaskConfigurationDTO
    revision: int = Field(ge=1)
    lifecycle: Literal["active", "paused", "needs_attention"]
    reason: str | None = None
    created_at: datetime
    updated_at: datetime
    next_run_at: datetime | None = None
    workspace_ids: dict[str, str]
    runs: list[LegacyGenerationRun]
