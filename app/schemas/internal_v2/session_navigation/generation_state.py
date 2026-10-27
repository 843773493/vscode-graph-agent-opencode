"""任务运行内的会话动作进度；正文接受后只保留原执行引用。"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from app.domain.itemized.execution_admission import UserExecutionIdentity
from app.schemas.internal_v2.message import MessageDTO
from app.schemas.internal_v2.session_navigation.models import (
    SessionGenerationExecuteResultDTO,
    SessionGenerationOutputDTO,
)


class GenerationActionState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["action"] = "action"
    title: str
    navigation_path: list[str] = Field(default_factory=list)
    status: Literal["queued", "reporting", "completed", "failed"] = "queued"
    session_id: str | None = None
    thread_id: str | None = None
    message_id: str | None = None
    execution_identity: UserExecutionIdentity | None = None
    prepared_message: MessageDTO | None = None
    report_back_job_id: str | None = None

    def result(self, *, run_id: str, workspace_id: str, error: str | None) -> SessionGenerationExecuteResultDTO:
        return SessionGenerationExecuteResultDTO(
            run_id=run_id, status=self.status,
            outputs=[SessionGenerationOutputDTO(
                workspace_id=workspace_id, session_id=self.session_id, title=self.title, navigation_path=self.navigation_path,
            )] if self.session_id is not None else [],
            message_id=self.message_id,
            job_id=self.execution_identity.job_id if self.execution_identity is not None else None,
            report_back_job_id=self.report_back_job_id, error=error,
        )


class GenerationHistoricalState(BaseModel):
    """迁移的不可变终态，允许原会话已删除；不参与动作推进。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["historical"] = "historical"
    value: SessionGenerationExecuteResultDTO

    def result(self, *, run_id: str, workspace_id: str, error: str | None) -> SessionGenerationExecuteResultDTO:
        if self.value.run_id != run_id or any(output.workspace_id != workspace_id for output in self.value.outputs):
            raise RuntimeError("迁移的会话动作终态归属冲突")
        return self.value.model_copy(update={"error": error})


GenerationState = Annotated[GenerationActionState | GenerationHistoricalState, Field(discriminator="kind")]
GENERATION_STATE = TypeAdapter(GenerationState)
