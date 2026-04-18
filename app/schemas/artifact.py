from pydantic import BaseModel, Field
from typing import Any, Optional


class ArtifactDTO(BaseModel):
    artifact_id: str
    job_id: str
    type: str
    name: str
    path: str
    metadata: dict[str, Any] = Field(default_factory=dict)
