"""精确 Thread 的执行控制快照；重启后的继续从已提交上下文创建后续执行。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class ThreadExecutionControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["pause", "resume", "interrupt", "stop"]
    expected_execution_id: str | None


class ThreadExecutionControlDTO(BaseModel):
    session_id: str
    thread_id: str
    job_id: str | None
    execution_id: str | None
    state: Literal["idle", "running", "pausing", "paused"]
    advancement_paused: bool
    can_resume: bool
