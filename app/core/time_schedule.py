"""时区感知的计划点计算；调用方负责游标、事务及实际执行。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

MisfirePolicy = Literal["skip", "run_latest", "catch_up"]


@dataclass(frozen=True, slots=True)
class TimeSchedule:
    kind: Literal["interval", "cron"]
    interval_seconds: int | None = None
    expression: str | None = None
    timezone_name: str = "UTC"

    def __post_init__(self) -> None:
        if self.kind == "interval" and (self.interval_seconds is None or self.interval_seconds < 1):
            raise ValueError("interval trigger 需要正整数 interval_seconds")
        if self.kind == "cron" and (not self.expression or not croniter.is_valid(self.expression)):
            raise ValueError(f"cron expression 非法: {self.expression}")
        try:
            ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError as error:
            raise ValueError(f"未知时区: {self.timezone_name}") from error

    def next_time(self, previous: datetime) -> datetime:
        if self.kind == "interval":
            if self.interval_seconds is None:
                raise ValueError("interval trigger 缺少 interval_seconds")
            return previous + timedelta(seconds=self.interval_seconds)
        zone = ZoneInfo(self.timezone_name)
        assert self.expression is not None
        result = croniter(self.expression, previous.astimezone(zone)).get_next(datetime)
        return result.astimezone(UTC)

    def latest_due(self, anchor: datetime, now: datetime) -> datetime:
        if self.kind == "interval":
            if self.interval_seconds is None:
                raise ValueError("interval trigger 缺少 interval_seconds")
            intervals = int(max(0, (now - anchor).total_seconds()) // self.interval_seconds)
            return anchor + timedelta(seconds=intervals * self.interval_seconds)
        # get_prev 严格小于基点；包含恰好到期的当前计划点。
        zone = ZoneInfo(self.timezone_name)
        assert self.expression is not None
        result = croniter(self.expression, (now + timedelta(microseconds=1)).astimezone(zone)).get_prev(datetime)
        return result.astimezone(UTC)


def evaluate_schedule(schedule: TimeSchedule, *, anchor: datetime, candidate: datetime,
                      now: datetime, misfire: MisfirePolicy) -> tuple[list[datetime], list[datetime], datetime]:
    """沿用生成器补跑合同，每批最多计算100个计划点并返回下一游标。"""
    due: list[datetime] = []
    while candidate <= now and len(due) < 100:
        due.append(candidate)
        candidate = schedule.next_time(candidate)
    if candidate <= now:
        if misfire == "catch_up":
            return due, [], candidate
        latest = schedule.latest_due(anchor, now)
        return ([], [latest], schedule.next_time(latest)) if misfire == "skip" else ([latest], [], schedule.next_time(latest))
    if len(due) <= 1 or misfire == "catch_up":
        return due, [], candidate
    return ([], [due[-1]], candidate) if misfire == "skip" else ([due[-1]], [], candidate)
