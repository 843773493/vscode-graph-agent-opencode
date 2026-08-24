import pytest

from app.services.business.job.pending_queue import JobPendingQueue, QueueEntry


def test_queue_is_strict_fifo_and_tail_policy_update_preserves_order() -> None:
    queue = JobPendingQueue()
    first = queue.append("session", "job_1", "after_turn")
    second = queue.append("session", "job_2", "after_tool_result")

    assert first.enqueue_sequence == 1
    assert second.enqueue_sequence == 2
    assert queue.ids("session") == ("job_1", "job_2")

    updated = queue.update_policy("session", "job_2", "after_interrupt")

    assert updated.delivery_policy == "after_interrupt"
    assert queue.ids("session") == ("job_1", "job_2")
    assert queue.peek_head("session") is first


def test_remove_head_makes_successor_the_new_head() -> None:
    queue = JobPendingQueue()
    first = queue.append("session", "job_1", "after_turn")
    second = queue.append("session", "job_2", "after_turn")

    queue.remove("session", first.job_id)

    assert queue.peek_head("session") is second
    assert queue.ids("session") == ("job_2",)


def test_boundary_eligibility_never_allows_later_item_to_bypass_head() -> None:
    queue = JobPendingQueue()
    head = queue.append("session", "job_head", "after_interrupt")
    queue.append("session", "job_tail", "after_tool_result")

    assert queue.take_head("session", "after_tool_result") is None
    assert head.waiting_reason == "等待已提交的 interrupt 边界"
    assert queue.peek_head("session") is head

    assert queue.take_head("session", "after_interrupt") is head
    assert queue.ids("session") == ("job_tail",)


def test_tool_result_policy_falls_back_only_when_no_tool_result_exists() -> None:
    queue = JobPendingQueue()
    entry = queue.append("session", "job", "after_tool_result")

    assert queue.take_head(
        "session",
        "after_turn",
        tool_result_available=True,
    ) is None
    assert queue.take_head(
        "session",
        "after_turn",
        tool_result_available=False,
    ) is entry


def test_take_head_removes_message_before_execution() -> None:
    queue = JobPendingQueue()
    entry = queue.append("session", "job", "after_turn")
    taken = queue.take_head("session", "idle")

    assert taken is entry
    assert queue.ids("session") == ()
    with pytest.raises(ValueError, match="不存在"):
        queue.entry("job")


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        (
            [
                QueueEntry("job_1", 1, "after_turn"),
                QueueEntry("job_1_duplicate", 1, "after_turn"),
            ],
            "重复入队序号",
        ),
        (
            [
                QueueEntry("job_2", 2, "after_turn"),
                QueueEntry("job_1", 1, "after_turn"),
            ],
            "未严格递增",
        ),
    ],
)
def test_restore_rejects_invalid_sequence(entries: list[QueueEntry], message: str) -> None:
    with pytest.raises(RuntimeError, match=message):
        JobPendingQueue().restore("session", entries)


def test_reorder_and_promotion_are_explicitly_rejected() -> None:
    with pytest.raises(ValueError, match="不支持重排"):
        JobPendingQueue().reject_reorder("session")


def test_restore_only_restores_still_queued_messages() -> None:
    queue = JobPendingQueue()
    queue.restore("session", [QueueEntry("job", 1, "after_turn")])

    assert queue.ids("session") == ("job",)
