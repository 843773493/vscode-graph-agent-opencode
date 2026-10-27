"""共享计划计算保留补跑语义，覆盖大积压和时区边界。"""

from datetime import UTC, datetime, timedelta

import pytest

from app.core.time_schedule import TimeSchedule, evaluate_schedule


@pytest.mark.parametrize("misfire,count,skipped", [("skip", 0, 1), ("run_latest", 1, 0), ("catch_up", 100, 0)])
def test_exact_cron_boundary_is_included_even_with_large_backlog(misfire, count, skipped):
    anchor = datetime(2026, 10, 27, tzinfo=UTC)
    schedule = TimeSchedule("cron", expression="* * * * *", timezone_name="Asia/Shanghai")
    now = anchor + timedelta(minutes=250)
    due, missed, following = evaluate_schedule(schedule, anchor=anchor, candidate=schedule.next_time(anchor), now=now, misfire=misfire)
    assert len(due) == count and len(missed) == skipped
    if misfire == "catch_up":
        assert due == [anchor + timedelta(minutes=i) for i in range(1, 101)]
        assert following == anchor + timedelta(minutes=101)
    else:
        assert (due or missed) == [now]
        assert following == now + timedelta(minutes=1)


def test_daily_local_time_follows_timezone_offset_change():
    schedule = TimeSchedule("cron", expression="0 9 * * *", timezone_name="Europe/Berlin")
    before = datetime(2026, 10, 24, 7, tzinfo=UTC)
    # 次日欧洲结束夏令时；当地09:00对应UTC08:00。
    assert schedule.next_time(before) == datetime(2026, 10, 25, 8, tzinfo=UTC)
