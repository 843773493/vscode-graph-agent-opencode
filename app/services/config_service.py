from __future__ import annotations
from typing import Optional

from app.schemas.config import ConfigDTO, ConfigUpdateRequest


class ConfigService:
    async def get(self) -> ConfigDTO:
        return ConfigDTO(
            default_model="gpt-4.1",
            default_orchestration="hierarchical",
            max_concurrent_agents=4,
            allow_shell_tools=False,
            ignored_paths=[".git", "node_modules", "__pycache__", ".venv"],
            auto_summarize=True,
            metadata={
                "version": "1.0.0",
                "environment": "development"
            }
        )

    async def update(self, update_request: ConfigUpdateRequest) -> ConfigDTO:
        current = await self.get()
        update_data = update_request.model_dump(exclude_unset=True)
        return ConfigDTO(**{**current.model_dump(), **update_data})
