from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from app.schemas.artifact import ArtifactDTO
from app.core.path_utils import ARTIFACTS_DIR


class ArtifactService:
    @staticmethod
    async def get_response(artifact_id: str) -> Any:
        return ArtifactDTO(
            artifact_id=artifact_id,
            job_id="job_001",
            type="markdown",
            name="sample.md",
            path=str(ARTIFACTS_DIR / "sample.md")
        )
    
    @staticmethod
    async def list_by_job(job_id: str) -> list[ArtifactDTO]:
        return [
            ArtifactDTO(
                artifact_id=f"art_{i:03d}",
                job_id=job_id,
                type="markdown",
                name=f"result_{i}.md",
                path=str(ARTIFACTS_DIR / job_id / f"result_{i}.md")
            )
            for i in range(3)
        ]
