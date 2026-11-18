from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field


class ModelReferenceDTO(BaseModel):
    """稳定 Provider 与 Model 键组成的模型身份。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    provider_id: str = Field(min_length=2, max_length=64, pattern=r"^[a-zA-Z0-9._-]+$")
    model_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9._-]+$")

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


__all__ = ["ModelReferenceDTO"]
