from pydantic import BaseModel
from typing import Optional, List


class AgentDTO(BaseModel):
    agent_id: str
    name: str
    description: Optional[str] = None
    model: str
    tools: list[str]
    capabilities: list[str]
