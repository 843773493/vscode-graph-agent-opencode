"""来源触发策略与获取协议分离；所有触发均进入原 owner 的唯一对账入口。"""

from __future__ import annotations

from sys import float_info
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SourceUpdatePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["event", "poll", "manual", "event_poll"] = "event"
    poll_interval: str | None = Field(default=None, pattern=r"^[1-9][0-9]*(m|h)$")

    @model_validator(mode="after")
    def validate_interval(self) -> Self:
        if self.uses_poll != (self.poll_interval is not None):
            raise ValueError("仅 poll/event_poll 必须配置分钟或小时 poll_interval；event/manual 不接受轮询间隔")
        seconds = self.poll_seconds
        if seconds is not None and seconds > float_info.max:
            raise ValueError("poll_interval 必须能表示为有限秒数")
        return self

    @property
    def uses_events(self) -> bool:
        return self.mode in {"event", "event_poll"}

    @property
    def uses_poll(self) -> bool:
        return self.mode in {"poll", "event_poll"}

    @property
    def automatic(self) -> bool:
        return self.uses_events or self.uses_poll

    def require_event_capability(self, supported: bool) -> None:
        if self.uses_events and not supported:
            raise ValueError("获取能力尚未登记事件协议；不自动回退到 poll")

    @property
    def poll_seconds(self) -> int | None:
        if self.poll_interval is None:
            return None
        return int(self.poll_interval[:-1]) * (60 if self.poll_interval.endswith("m") else 3600)
