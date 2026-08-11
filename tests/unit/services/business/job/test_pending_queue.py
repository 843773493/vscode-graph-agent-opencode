from app.services.business.job.pending_queue import JobPendingQueue


def test_peek_next_group_only_returns_contiguous_steering_jobs() -> None:
    queue = JobPendingQueue()
    queue.restore(
        "session_queue",
        [
            ("steering_1", "steering"),
            ("queued_1", "queued"),
            ("steering_2", "steering"),
        ],
    )

    assert queue.peek_next_group("session_queue") == ("steering_1",)
    assert queue.pop_next_group("session_queue") == ("steering_1",)
    assert queue.ids("session_queue") == ("queued_1", "steering_2")
