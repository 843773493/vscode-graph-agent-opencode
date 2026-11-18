"""各类持久任务共用单调记账；生命周期转换仍由具体应用决定。"""

from typing import TypeVar

from app.schemas.internal_v2.task_usage import TaskJobAccountingDTO, TaskUsageStateDTO

UsageState = TypeVar("UsageState", bound=TaskUsageStateDTO)


def budget_reached(tokens_used: int, token_budget: int | None) -> bool:
    return token_budget is not None and tokens_used >= token_budget


def account_usage(state: UsageState, *, job_id: str, tokens: int, elapsed_seconds: int,
                  close_time: bool = False, settled: bool = False) -> UsageState:
    """累计值取最大水位；冻结时间后仍允许补记迟到 Token。"""
    previous = state.accounted_jobs.get(job_id, TaskJobAccountingDTO())
    current = previous.model_copy(update={
        "tokens": max(tokens, previous.tokens),
        "elapsed_seconds": max(elapsed_seconds, previous.elapsed_seconds),
        "time_closed": previous.time_closed or close_time,
        "settled": previous.settled or settled,
    })
    if current == previous and job_id in state.accounted_jobs:
        return state
    return state.model_copy(update={
        "tokens_used": state.tokens_used + current.tokens - previous.tokens,
        "time_used_seconds": state.time_used_seconds + (
            0 if previous.time_closed else current.elapsed_seconds - previous.elapsed_seconds),
        "last_accounted_job_id": job_id,
        "accounted_jobs": {**state.accounted_jobs, job_id: current},
    })
