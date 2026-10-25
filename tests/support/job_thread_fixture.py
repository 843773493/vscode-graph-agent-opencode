"""Job 控制单元测试的显式 thread 与 execution identity 工厂。"""

from datetime import UTC, datetime
from functools import lru_cache

from app.domain.itemized.execution_admission import UserExecutionIdentity
from tests.support.canonical_id_at import thread_id_for_name_at


@lru_cache(maxsize=128)
def job_fixture_thread_id(session_id: str) -> str:
    return thread_id_for_name_at(session_id, datetime(2026, 6, 1, tzinfo=UTC))


def job_fixture_execution_binding(job_id: str) -> UserExecutionIdentity:
    return UserExecutionIdentity(job_id=job_id, turn_id=f"turn-{job_id}", admission_idempotency_key=f"admission-{job_id}")
