from __future__ import annotations

from typing import Protocol

from app.schemas.public_v2.message import AttachmentRef


class JobStepExecutor(Protocol):
    async def run_step(
        self,
        session_id: str,
        message: str,
        agent_id: str | None = None,
        job_id: str | None = None,
        message_id: str | None = None,
        attachments: list[AttachmentRef] | None = None,
        message_created_at: str | None = None,
    ) -> str:
        ...
