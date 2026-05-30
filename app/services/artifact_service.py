from __future__ import annotations

from datetime import datetime
from typing import Any

from app.schemas.artifact import ArtifactDTO
from app.core.path_utils import get_artifacts_dir, safe_join


class ArtifactService:
    def __init__(self):
        pass

    async def get_response(self, artifact_id: str) -> Any:
        artifacts_dir = get_artifacts_dir()
        return ArtifactDTO(
            artifact_id=artifact_id,
            job_id="job_001",
            type="markdown",
            name="sample.md",
            path=str(safe_join(artifacts_dir, "sample.md"))
        )
    
    async def list_by_job(self, job_id: str) -> list[ArtifactDTO]:
        artifacts_dir = get_artifacts_dir()
        return [
            ArtifactDTO(
                artifact_id=f"art_{i:03d}",
                job_id=job_id,
                type="markdown",
                name=f"result_{i}.md",
                path=str(safe_join(artifacts_dir, job_id, f"result_{i}.md"))
            )
            for i in range(3)
        ]
