"""跨 Session 通信的 canonical 地址与 main binding 值对象。

GlobalThreadAddress 是 send/read/wait 双端记录共享的稳定业务地址；
session/thread 形态复用 catalog 层 canonical 校验，本层不重复实现。
ResolvedMainTargetBinding 固定"目标只解析 main thread"的合同：resolver
声明的 main pointer 与实际目标地址不一致时立即失败，不清洗、不降级。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.session_catalog_store import validate_session_id, validate_thread_id
from app.services.business.communication.errors import CommunicationContractError


@dataclass(frozen=True, slots=True)
class GlobalThreadAddress:
    """稳定的跨进程 thread 地址 (gateway_id, workspace_id, session_id, thread_id)。

    前两者是持久身份，不含连接实例、channel epoch 或 route locator 等瞬时状态。
    """

    gateway_id: str
    workspace_id: str
    session_id: str
    thread_id: str

    def __post_init__(self) -> None:
        for field_name in ("gateway_id", "workspace_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise CommunicationContractError(
                    "communication-target-not-main",
                    f"GlobalThreadAddress.{field_name} 必须是非空字符串",
                )
        try:
            validate_session_id(self.session_id)
            validate_thread_id(self.thread_id)
        except (TypeError, ValueError) as error:
            raise CommunicationContractError(
                "communication-target-not-main",
                f"GlobalThreadAddress 的 canonical ID 形态非法: {error}",
            ) from error


@dataclass(frozen=True, slots=True)
class ResolvedMainTargetBinding:
    """send/read/wait 目标的 canonical main binding。

    target_main_thread_id 来自目标 Session catalog 的权威 main pointer；
    目标地址必须逐字段等于该 pointer，防止把 child thread 或漂移后的
    locator 当成投递目标。
    """

    target_session_id: str
    target_main_thread_id: str
    target: GlobalThreadAddress

    def __post_init__(self) -> None:
        if self.target.session_id != self.target_session_id:
            raise CommunicationContractError(
                "communication-target-not-main",
                "target 地址的 session_id 与解析出的目标 Session 不一致: "
                f"{self.target.session_id!r} != {self.target_session_id!r}",
            )
        if self.target.thread_id != self.target_main_thread_id:
            raise CommunicationContractError(
                "communication-target-not-main",
                "send/read/wait 目标只解析 main thread；目标地址 thread_id "
                f"{self.target.thread_id!r} 与 main pointer "
                f"{self.target_main_thread_id!r} 不一致",
            )
