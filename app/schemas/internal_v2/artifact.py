from __future__ import annotations

from pydantic import BaseModel, Field


class ArtifactDTO(BaseModel):
    artifact_id: str
    job_id: str
    type: str
    name: str
    path: str
    metadata: dict[str, object] = Field(default_factory=dict)
