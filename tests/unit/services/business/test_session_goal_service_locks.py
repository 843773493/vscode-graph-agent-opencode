import asyncio

import pytest

from app.services.business.session_goal_service import SessionGoalService


class _SessionService:
    async def resolve_main_thread(self, session_id: str) -> str:
        return f"thr_{session_id}"


class _Store:
    def read(self, session_id):
        return None

    def write(self, goal):
        pass

    def clear(self, session_id):
        return False

    def list_existing(self):
        return []


class _Bus:
    async def publish(self, **event):
        pass


@pytest.fixture
def goal_service():
    return SessionGoalService(
        store=_Store(), session_service=_SessionService(), job_event_bus=_Bus()
    )


def test_same_session_always_gets_the_same_lock(goal_service):
    assert goal_service.lock_for("sess_1") is goal_service.lock_for("sess_1")


def test_lock_table_stays_bounded_across_many_sessions(goal_service):
    # 锁池必须对 session 数量有固定上界；每个新 session 都新建一把锁会导致
    # 长时间运行的进程随历史会话数无界增长。
    distinct = {id(goal_service.lock_for(f"sess_{index}")) for index in range(5000)}
    assert len(distinct) <= 64


@pytest.mark.asyncio
async def test_concurrent_same_session_critical_section_is_serialized(goal_service):
    lock = goal_service.lock_for("sess_1")
    concurrent = 0
    peak = 0

    async def worker():
        nonlocal concurrent, peak
        async with lock:
            concurrent += 1
            peak = max(peak, concurrent)
            await asyncio.sleep(0.01)
            concurrent -= 1

    await asyncio.gather(*(worker() for _ in range(20)))
    assert peak == 1
