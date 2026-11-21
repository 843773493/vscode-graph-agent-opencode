"""统一实时连接的控制信封；业务参数继续交原 API 校验。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RealtimeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["auth", "subscribe", "unsubscribe", "ack"]
    token: str | None = None
    subscription_id: int | None = Field(default=None, ge=1, le=2**32 - 1)
    path: str | None = None
    method: Literal["GET", "POST"] | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | None = None
    consumed_bytes: int | None = Field(default=None, ge=1)
