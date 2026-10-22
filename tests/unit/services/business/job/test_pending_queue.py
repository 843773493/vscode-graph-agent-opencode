import pytest

from app.services.business.job.pending_queue import JobPendingQueue, QueueEntry


def test_queue_is_strict_fifo_and_tail_policy_update_preserves_order() -> None:
    queue = JobPendingQueue()
    first = queue.append("session", "job_1", "after_turn", thread_id="thread", enqueue_sequence=10)
    second = queue.append("session", "job_2", "after_tool_result", thread_id="thread", enqueue_sequence=14)

    assert first.enqueue_sequence == 10
    assert second.enqueue_sequence == 14
    assert queue.ids("session") == ("job_1", "job_2")

    updated = queue.update_policy("session", "job_2", "after_interrupt")

    assert updated.delivery_policy == "after_interrupt"
    assert queue.ids("session") == ("job_1", "job_2")
    assert queue.peek_head("session", "thread") is first


def test_remove_head_makes_successor_the_new_head() -> None:
    queue = JobPendingQueue()
    first = queue.append("session", "job_1", "after_turn", thread_id="thread", enqueue_sequence=4)
    second = queue.append("session", "job_2", "after_turn", thread_id="thread", enqueue_sequence=8)

    queue.remove("session", first.job_id)

    assert queue.peek_head("session", "thread") is second
    assert queue.ids("session") == ("job_2",)


def test_boundary_eligibility_never_allows_later_item_to_bypass_head() -> None:
    queue = JobPendingQueue()
    head = queue.append("session", "job_head", "after_interrupt", thread_id="thread", enqueue_sequence=3)
    queue.append("session", "job_tail", "after_tool_result", thread_id="thread", enqueue_sequence=9)

    assert queue.take_head("session", "after_tool_result", thread_id="thread") is None
    assert head.waiting_reason == "等待已提交的 interrupt 边界"
    assert queue.peek_head("session", "thread") is head

    assert queue.take_head("session", "after_interrupt", thread_id="thread") is head
    assert queue.ids("session") == ("job_tail",)


def test_tool_result_policy_falls_back_only_when_no_tool_result_exists() -> None:
    queue = JobPendingQueue()
    entry = queue.append("session", "job", "after_tool_result", thread_id="thread", enqueue_sequence=2)

    assert queue.take_head(
        "session",
        "after_turn", thread_id="thread",
        tool_result_available=True,
    ) is None
    assert queue.take_head(
        "session",
        "after_turn", thread_id="thread",
        tool_result_available=False,
    ) is entry


def test_take_head_removes_message_before_execution() -> None:
    queue = JobPendingQueue()
    entry = queue.append("session", "job", "after_turn", thread_id="thread", enqueue_sequence=1)
    taken = queue.take_head("session", "idle", thread_id="thread")

    assert taken is entry
    assert queue.ids("session") == ()
    with pytest.raises(ValueError, match="不存在"):
        queue.entry("job")


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        (
            [
                QueueEntry("job_1", "thread", 1, "after_turn"),
                QueueEntry("job_1_duplicate", "thread", 1, "after_turn"),
            ],
            "同 Thread 准入顺序错误",
        ),
        (
            [
                QueueEntry("job_2", "thread", 2, "after_turn"),
                QueueEntry("job_1", "thread", 1, "after_turn"),
            ],
            "同 Thread 准入顺序错误",
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
    queue.restore("session", [QueueEntry("job", "thread", 1, "after_turn")])

    assert queue.ids("session") == ("job",)


def test_interrupt_boundary_also_releases_after_turn_head() -> None:
    """interrupt 边界本身已是 turn 终止边界，不得让 after_turn 队首永久阻塞。"""
    queue = JobPendingQueue()
    entry = queue.append("session", "job", "after_turn", thread_id="thread", enqueue_sequence=11)

    assert queue.take_head("session", "after_tool_result", thread_id="thread") is None
    assert queue.take_head("session", "after_interrupt", thread_id="thread") is entry
    assert queue.ids("session") == ()


def test_promoted_head_clears_stale_waiting_reason() -> None:
    """队首被取走后，接替的新队首不得残留定位式的「等待队首」理由。

    「等待队首」只是位置含义：只有非队首项才等待队首。改前 ``_bump`` 用
    ``已有理由 or 新理由`` 计算，一旦写入「等待队首」就再也不会被清掉，导致
    原队长被取走、接替者成为新队首后仍对外宣称「等待队首」（前端把它渲染成
    排队条目的 hover 提示），而它当前恰恰就是队首、可立即投递。
    """
    queue = JobPendingQueue()
    queue.append("session", "job_1", "after_turn", thread_id="thread", enqueue_sequence=1)
    second = queue.append("session", "job_2", "after_turn", thread_id="thread", enqueue_sequence=2)
    assert second.waiting_reason == "等待队首"

    assert queue.take_head("session", "idle", thread_id="thread").job_id == "job_1"

    assert queue.peek_head("session", "thread") is second
    assert second.waiting_reason is None


def test_promoted_head_after_remove_records_no_waiting_reason() -> None:
    """撤回队首后接替者同样不得残留「等待队首」。"""
    queue = JobPendingQueue()
    queue.append("session", "job_1", "after_turn", thread_id="thread", enqueue_sequence=1)
    second = queue.append("session", "job_2", "after_turn", thread_id="thread", enqueue_sequence=2)

    queue.remove("session", "job_1")

    assert queue.peek_head("session", "thread") is second
    assert second.waiting_reason is None
    assert queue.ids("session") == ("job_2",)


def test_head_keeps_boundary_waiting_reason_when_bumped() -> None:
    """队首的边界理由由投递边界决定，``_bump`` 不得把它误清成 None。"""
    queue = JobPendingQueue()
    head = queue.append("session", "job_head", "after_interrupt", thread_id="thread", enqueue_sequence=1)
    queue.append("session", "job_tail", "after_turn", thread_id="thread", enqueue_sequence=2)

    assert queue.take_head("session", "after_tool_result", thread_id="thread") is None
    assert head.waiting_reason == "等待已提交的 interrupt 边界"
    assert queue.peek_head("session", "thread") is head


def test_clear_releases_queue_entries_and_snapshot_revision_is_cached_from_owner() -> None:
    """清空内存队列；快照 revision 只接受 owner 发布的缓存值。"""
    queue = JobPendingQueue()
    for session_id in ("ses_a", "ses_b", "ses_c"):
        queue.append(session_id, f"job_{session_id}", "after_turn", thread_id="thread", enqueue_sequence=5)
        queue.clear(session_id)

    assert queue._waiting == {}
    assert queue._entries == {}
    assert queue._snapshot_versions == {}
    assert queue.snapshot_version("ses_a") == 0
    queue.set_snapshot_version("ses_a", 23)
    assert queue.snapshot_version("ses_a") == 23


def test_clear_is_idempotent_and_reusable_session_keeps_admission_cursor() -> None:
    """clear 幂等；再次入队继续使用调用方提供的 canonical cursor。"""
    queue = JobPendingQueue()
    queue.append("session", "job_1", "after_turn", thread_id="thread", enqueue_sequence=7)
    queue.clear("session")
    assert queue.clear("session") == ()

    entry = queue.append("session", "job_2", "after_turn", thread_id="thread", enqueue_sequence=19)

    assert entry.enqueue_sequence == 19
    assert entry.waiting_reason is None
    assert queue.snapshot_version("session") == 0


def test_policy_update_keeps_positional_reason_semantics() -> None:
    """改策略不改位置：队首保持无位置理由，非队首标注「等待队首」。"""
    queue = JobPendingQueue()
    head = queue.append("session", "job_head", "after_turn", thread_id="thread", enqueue_sequence=3)
    tail = queue.append("session", "job_tail", "after_turn", thread_id="thread", enqueue_sequence=8)

    assert queue.update_policy("session", "job_head", "after_tool_result") is head
    assert head.waiting_reason is None
    assert queue.update_policy("session", "job_tail", "after_interrupt") is tail
    assert tail.waiting_reason == "等待队首"
    assert queue.peek_head("session", "thread") is head


def test_admission_cursor_may_have_gaps_but_same_thread_may_not_move_backwards() -> None:
    queue = JobPendingQueue()
    first = queue.append("session", "job_1", "after_turn", thread_id="thread", enqueue_sequence=6)
    second = queue.append("session", "job_2", "after_turn", thread_id="thread", enqueue_sequence=21)

    assert [first.enqueue_sequence, second.enqueue_sequence] == [6, 21]
    with pytest.raises(RuntimeError, match="同 Thread 的准入顺序"):
        queue.append("session", "job_3", "after_turn", thread_id="thread", enqueue_sequence=20)


def test_distinct_threads_may_have_the_same_admission_cursor() -> None:
    queue = JobPendingQueue()
    main = queue.append("session", "job_main", "after_turn", thread_id="main", enqueue_sequence=12)
    child = queue.append("session", "job_child", "after_turn", thread_id="child", enqueue_sequence=12)

    assert queue.peek_head("session", "main") is main
    assert queue.peek_head("session", "child") is child
    assert queue.ids("session", "main") == ("job_main",)
    assert queue.ids("session", "child") == ("job_child",)
