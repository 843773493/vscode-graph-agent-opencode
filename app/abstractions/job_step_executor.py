from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from app.schemas.public_v2.message import AttachmentRef


class JobStepExecutor(Protocol):
    async def run_step(
        self,
        session_id: str,
        message: str,
        *,
        agent_id: str | None = None,
        job_id: str,
        message_id: str,
        attachments: list[AttachmentRef] | None = None,
        message_created_at: str,
        message_metadata: dict[str, object] | None = None,
        yield_requested: Callable[[], bool] | None = None,
    ) -> str:
        ...
