from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Protocol

from app.domain.itemized.execution_admission import ExecutionBinding
from app.schemas.internal_v2.message import AttachmentRef


class JobStepExecutor(Protocol):
    async def run_step(
        self,
        session_id: str,
        message: str,
        *,
        thread_id: str,
        agent_id: str | None = None,
        job_id: str,
        message_id: str,
        execution_binding: ExecutionBinding,
        resume_checkpoint_id: str | None = None,
        attachments: list[AttachmentRef] | None = None,
        message_created_at: str,
        message_metadata: dict[str, object] | None = None,
        progress_reporter: Callable[[str], None] | None = None,
        protected_operations_settled: asyncio.Event | None = None,
    ) -> str:
        ...
