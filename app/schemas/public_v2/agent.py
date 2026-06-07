from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class AgentDTO(BaseModel):
    agent_id: str
    name: str
    description: Optional[str] = None
    model: str
    tools: list[str]
    capabilities: list[str]
