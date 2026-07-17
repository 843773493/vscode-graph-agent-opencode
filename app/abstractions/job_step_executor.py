from __future__ import annotations

from typing import Protocol

from app.schemas.public_v2.message import AttachmentRef
from app.schemas.public_v2.common import MessageRole


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
        message_role: MessageRole = MessageRole.user,
        message_metadata: dict[str, object] | None = None,
    ) -> str:
        ...
