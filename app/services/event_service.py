from __future__ import annotations
from datetime import datetime

from app.schemas.job import EventDTO


class EventService:
    async def list_by_job(self, job_id: str) -> list[EventDTO]:
        now = datetime.now()
        return [
            EventDTO(
                event_id="evt_001",
                job_id=job_id,
                step_id="step_001",
                type="agent_started",
                agent_id="planner",
                payload={"action": "initialize", "status": "started"},
                timestamp=now
            ),
            EventDTO(
                event_id="evt_002",
                job_id=job_id,
                step_id="step_001",
                type="agent_completed",
                agent_id="planner",
                payload={"action": "plan_generated", "steps_count": 5},
                timestamp=now
            ),
            EventDTO(
                event_id="evt_003",
                job_id=job_id,
                step_id="step_002",
                type="tool_called",
                agent_id="executor",
                payload={"tool": "read_file", "path": "/src/main.py"},
                timestamp=now
            ),
            EventDTO(
                event_id="evt_004",
                job_id=job_id,
                step_id=None,
                type="job_progress",
                agent_id=None,
                payload={"progress": 65, "current_phase": "execution"},
                timestamp=now
            )
        ]

    async def get(self, event_id: str) -> EventDTO:
        return EventDTO(
            event_id=event_id,
            job_id="job_123",
            step_id="step_001",
            type="agent_log",
            agent_id="executor",
            payload={"level": "info", "message": "Processing task successfully"},
            timestamp=datetime.now()
        )
