"""工作区发现查询共用端到端期限；单页调度预算仍由各正文／名称 owner 管理。"""

import math
import time
from dataclasses import dataclass

NAME_SEARCH_DEADLINE_HEADER = "X-BoxTeam-Name-Search-Deadline"
_QUERY_SECONDS = 15.0


@dataclass(frozen=True)
class NameSearchBudget:
    deadline: float

    @classmethod
    def create(cls, deadline_at: float | None) -> "NameSearchBudget":
        now = time.monotonic()
        deadline = now + _QUERY_SECONDS
        if deadline_at is not None:
            if not math.isfinite(deadline_at):
                raise ValueError("查询执行期限必须为有限 UTC 时间戳")
            deadline = min(deadline, now + deadline_at - time.time())
        budget = cls(deadline)
        budget.check()
        return budget

    def check(self) -> None:
        if time.monotonic() >= self.deadline:
            raise TimeoutError("查询超过本次总执行期限，请重新查询")

    def remaining(self) -> float:
        self.check()
        return self.deadline - time.monotonic()
