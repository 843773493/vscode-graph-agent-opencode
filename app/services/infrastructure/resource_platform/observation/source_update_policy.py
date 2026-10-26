"""来源触发策略与获取协议分离；所有触发均进入原 owner 的唯一对账入口。"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SourceUpdatePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["event", "poll", "manual", "event_poll"] = "event"
    poll_interval: str | None = Field(default=None, pattern=r"^[1-9][0-9]*(m|h)$")

    @model_validator(mode="after")
    def validate_interval(self) -> Self:
        if (self.mode in {"poll", "event_poll"}) != (self.poll_interval is not None):
            raise ValueError("仅 poll/event_poll 必须配置分钟或小时 poll_interval；event/manual 不接受轮询间隔")
        return self

    @property
    def poll_seconds(self) -> int | None:
        if self.poll_interval is None:
            return None
        return int(self.poll_interval[:-1]) * (60 if self.poll_interval.endswith("m") else 3600)
