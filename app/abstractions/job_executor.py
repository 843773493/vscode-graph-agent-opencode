from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from app.domain.itemized.execution_admission import ExecutionBinding
from app.schemas.internal_v2.common import JobStatus
from app.schemas.internal_v2.message import AttachmentRef, MessageDTO
from app.schemas.internal_v2.pending_request import PendingRequestControl


class JobRuntimeStateProtocol(Protocol):
    job_id: str
    session_id: str
    thread_id: str
    execution_binding: ExecutionBinding
    message: str
    agent_id: str
    message_id: str
    attachments: list[AttachmentRef]
    message_created_at: str
    message_metadata: dict[str, object]
    # 创建该 Job 的权威 request_id（独立执行根身份）；缺失为 None。
    request_id: str | None
    resume_checkpoint_id: str | None
    status: JobStatus
    progress: int
    current_step: str | None
    error_message: str | None
    result: str | None
    created_at: datetime
    updated_at: datetime
    ended_at: datetime | None
    progress_reporter: Callable[[str], None] | None
    protected_operations_settled: asyncio.Event


class JobExecutorProtocol(Protocol):
    async def run(self, job: JobRuntimeStateProtocol) -> str:
        ...

    async def fail(self, job: JobRuntimeStateProtocol, error: Exception) -> None:
        ...


class JobInputOwnerProtocol(Protocol):
    """入队前冻结准入；只读正文由唯一 canonical input owner 提供。"""

    def accept_job_input(self, job: JobRuntimeStateProtocol) -> None: ...

    def accepted_job_message(
        self, reference: PendingRequestControl, *, display: bool,
    ) -> MessageDTO: ...

    def accepted_job_outcome(self, reference: PendingRequestControl) -> str: ...
