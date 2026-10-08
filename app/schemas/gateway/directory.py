from __future__ import annotations

from pydantic import BaseModel, Field


class GatewayDirectoryEntryDTO(BaseModel):
    name: str
    path: str


class GatewayDirectoryListDTO(BaseModel):
    path: str
    parent_path: str | None = None
    home_path: str
    entries: list[GatewayDirectoryEntryDTO] = Field(default_factory=list)
    truncated: bool = False
    limit: int
