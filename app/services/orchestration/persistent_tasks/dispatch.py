"""持续任务在真正执行前核对原 owner 的控制状态；不重跑触发和条件判断。"""

from typing import cast

from app.schemas.internal_v2.goal import GoalStatus
from app.services.business.job.pending_request_controller import PendingJob
from app.services.infrastructure.persistent_tasks.store import PersistentTaskStore


def pending_task_dispatch_reason(job: PendingJob, *, store: PersistentTaskStore) -> str | None:
    metadata = job.message_metadata
    if metadata.get("goal_continuation") is True:
        goal = store.goal_read(job.session_id)
        status = GoalStatus.budget_limited if metadata.get("goal_budget_limited") else GoalStatus.active
        if (goal is None or goal.goal_id != metadata["goal_id"] or goal.thread_id != job.thread_id
                or goal.status != status or goal.control_revision != metadata["goal_control_revision"]):
            return "持续目标已变化或停止，取消尚未执行的续跑"
        return None

    task_id = cast(str, metadata["task_id"])
    try:
        task = store.get_automation(task_id)
    except KeyError:
        return "持久任务已移除，取消尚未执行的请求"
    revision = cast(int, metadata["task_revision"])
    if task.revision == revision and (task.lifecycle == "active" or metadata["task_check_only"]):
        return None
    # “投递后完成”只停止后续触发，已经接受的本次工作仍须执行。
    if (task.lifecycle == "complete" and task.configuration.policies.completion == "after_delivery"
            and task.revision == revision + 1):
        return None
    return "持久任务已变化或停止，取消尚未执行的请求"
