from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class AttachmentRef(BaseModel):
    file_id: str
    name: Optional[str] = None
    content_type: Optional[str] = None
    data_url: Optional[str] = None
