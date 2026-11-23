"""排队期间的管理变更只抑制旧请求，不重跑条件，也不调用模型。"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.services.orchestration.persistent_tasks.dispatch import (
    pending_task_dispatch_reason,
)


@pytest.fixture
def dispatch_case():
    store = Mock()
    job = SimpleNamespace(session_id="session", thread_id="thread", message_metadata={
        "task_id": "task", "task_revision": 4, "task_check_only": False,
    })
    task = SimpleNamespace(revision=4, lifecycle="active", configuration=SimpleNamespace(
        policies=SimpleNamespace(completion="continuous")))
    store.get_automation.return_value = task
    return store, job, task


@pytest.mark.parametrize("lifecycle", ["paused", "terminated", "complete", "needs_attention"])
def test_disabled_task_does_not_start(dispatch_case, lifecycle):
    store, job, task = dispatch_case
    task.lifecycle = lifecycle
    assert pending_task_dispatch_reason(job, store=store) is not None
    store.get_automation.assert_called_once_with("task")


def test_changed_task_does_not_start(dispatch_case):
    store, job, task = dispatch_case
    task.revision += 1
    assert pending_task_dispatch_reason(job, store=store) is not None


@pytest.mark.parametrize("check_only", [False, True])
def test_current_task_and_explicit_check_remain_executable(dispatch_case, check_only):
    store, job, task = dispatch_case
    job.message_metadata["task_check_only"] = check_only
    if check_only:
        task.lifecycle = "paused"
    assert pending_task_dispatch_reason(job, store=store) is None


def test_after_delivery_completion_keeps_accepted_work(dispatch_case):
    store, job, task = dispatch_case
    task.lifecycle, task.revision = "complete", 5
    task.configuration.policies.completion = "after_delivery"
    assert pending_task_dispatch_reason(job, store=store) is None
    task.revision += 1
    assert pending_task_dispatch_reason(job, store=store) is not None


@pytest.mark.parametrize("change", ["unchanged", "paused", "complete", "replaced", "edited", "removed", "other_thread"])
def test_goal_rechecked_against_original_control_revision(dispatch_case, change):
    store, job, _task = dispatch_case
    job.message_metadata = {"goal_continuation": True, "goal_id": "goal", "goal_control_revision": 3}
    goal = SimpleNamespace(goal_id="goal", thread_id="thread", status="active", control_revision=3)
    if change in {"paused", "complete"}:
        goal.status = change
    elif change == "replaced":
        goal.goal_id = "replacement"
    elif change == "edited":
        goal.control_revision += 1
    elif change == "other_thread":
        goal.thread_id = "other-thread"
    store.goal_read.return_value = None if change == "removed" else goal
    assert (pending_task_dispatch_reason(job, store=store) is None) is (change == "unchanged")
    store.get_automation.assert_not_called()
