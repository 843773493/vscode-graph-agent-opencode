from __future__ import annotations

from typing import Protocol


class JobStepExecutor(Protocol):
    async def run_step(
        self,
        session_id: str,
        message: str,
        agent_id: str | None = None,
        job_id: str | None = None,
    ) -> str:
        ...