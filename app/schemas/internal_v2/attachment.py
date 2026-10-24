from __future__ import annotations

from pydantic import BaseModel


class AttachmentRef(BaseModel):
    file_id: str
    directory_uri: str | None = None
    name: str | None = None
    content_type: str | None = None
    data_url: str | None = None
