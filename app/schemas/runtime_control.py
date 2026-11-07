"""本机进程控制合同；不属于工作区业务 API。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RuntimeShutdownRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    process_id: int = Field(gt=0)
    backend_generation: str = Field(min_length=1)


class RuntimeShutdownAccepted(RuntimeShutdownRequest):
    accepted: Literal[True]


class WorkspaceServiceShutdownRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    process_id: int = Field(gt=0)
    workspace_id: str = Field(min_length=1)
