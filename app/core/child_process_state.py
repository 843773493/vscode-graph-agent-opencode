"""可持久化的子进程身份与退出状态。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Literal

from pydantic import ConfigDict


@dataclass(frozen=True, slots=True)
class ChildProcessState:
    __pydantic_config__: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    instance_id: str
    phase: Literal["reserved", "running", "exited", "not_started"] = "reserved"
    pid: int | None = None
    created_at: float | None = None
    returncode: int | None = None
    exit_verified: bool = False

    def __post_init__(self) -> None:
        if not self.instance_id or self.pid is not None and self.pid <= 0:
            raise ValueError("子进程实例身份或 PID 无效")
        if self.exit_verified != (self.phase in {"exited", "not_started"}):
            raise ValueError("子进程退出核验与阶段不一致")
        if self.phase == "exited" and (self.pid is None or self.returncode is None):
            raise ValueError("已退出子进程缺少原 PID 或 wait 退出码")
        if self.phase in {"reserved", "not_started"} and (self.pid is not None or self.returncode is not None):
            raise ValueError("尚未创建子进程不能带运行身份")
