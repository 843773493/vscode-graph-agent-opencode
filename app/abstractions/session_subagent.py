"""Session subagent 委派抽象合同（OpenSpec 8.5-B，R25 单链路）。

R25 起 delegated child 不再是独立 Session：``delegate``` 经装配好的
``ThreadCreationService``` 在 owner Session 内创建 durable child
thread（同 owner、kind=child、durable admission intent pending），本模块
只声明该单链路的协议与结果投影。

- 旧 ``SessionStoreProtocol```（create_delegated /
  set_delegation_start_result）已随 delegated child Session 链路物理下线，
  不保留兼容层。
- ``SessionSubagentAccepted``` 不携带 ``message_id```，也不伪造运行中
  Job：``frozen_job_id``` 是 R23 intent 冻结的稳定 binding identity，
  ``admission_state``` 恒为 ``pending```，直到 R26 thread-qualified
  binder 真正消费 intent。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.core.thread_creation import ThreadCreationService
from app.schemas.internal_v2.session import SessionDTO

GENERAL_PURPOSE_SUBAGENT = "general-purpose"


@dataclass(frozen=True, slots=True)
class SessionSubagentAccepted:
    """一次委派创建的最终投影（thread 已 publish，intent 保持 pending）。

    ``owner_session_id``` 是 parent Session（child thread 的唯一 owner）；
    ``child_thread_id``` 是 owner Session 内 ``thread_catalog``` 的
    child row；``delegation_id``` 由 (parent session, parent job, parent
    tool call, subagent type) 确定性派生，同 tool call 重试收敛同一
    delegation；``execution_binding_id``` / ``frozen_job_id``` 是
    R23 intent 冻结的稳定 binding/job identity（非运行中 Job）。
    """

    owner_session_id: str
    child_thread_id: str
    delegation_id: str
    admission_idempotency_key: str
    admission_state: str
    execution_binding_id: str
    frozen_job_id: str


BeforeSubagentStart = Callable[[SessionSubagentAccepted], Awaitable[None]]


@runtime_checkable
class SessionReaderProtocol(Protocol):
    """按 ID 读取 Session DTO 的最小只读协议（委派/协作工具共用）。"""

    async def get(self, session_id: str) -> SessionDTO: ...


@runtime_checkable
class OwnerThreadCreationFactoryProtocol(Protocol):
    """按 owner Session 提供绑定该 Session 的 ThreadCreationService。"""

    def for_owner_session(self, session_id: str) -> ThreadCreationService: ...

    def owner_main_thread_id(self, session_id: str) -> str: ...


@runtime_checkable
class SessionSubagentProtocol(Protocol):
    async def delegate(
        self,
        *,
        parent_session_id: str,
        parent_agent_id: str,
        parent_job_id: str,
        parent_tool_call_id: str,
        description: str,
        subagent_type: str,
        title: str | None = None,
        trusted_context: dict[str, object] | None = None,
        untrusted_instructions: str | None = None,
        before_start: BeforeSubagentStart | None = None,
    ) -> SessionSubagentAccepted: ...
