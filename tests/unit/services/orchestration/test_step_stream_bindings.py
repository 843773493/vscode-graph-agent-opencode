"""验证 canonical sink 路由 Saver 的追加与工具终态端口。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, field
from threading import Event, get_ident
from typing import Literal
from unittest.mock import MagicMock

import pytest

from app.abstractions.turn_terminal_status import ToolResultCompletion
from app.core.execution_admission_context import (
    execution_admission_scope,
    require_current_execution_admission,
)
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.execution_admission import ExecutionAdmission
from app.domain.itemized.identity.item_projection import ItemProjectionIdentity
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.records import CanonicalItemRecord
from app.services.orchestration.event_stream.contracts import CanonicalItemObserverError
from app.services.orchestration.execution_step.stream_bindings import CanonicalItemSink

SESSION_ID = "session_1"
THREAD_ID = "thr_00000000400070008000000000000002"
CHECKPOINT_NAMESPACE = "agent"


@pytest.fixture
def canonical_item_saver() -> MagicMock:
    return MagicMock(spec=["append_items", "commit_tool_result"])


@pytest.fixture
def canonical_item_sink(canonical_item_saver: MagicMock) -> CanonicalItemSink:
    return CanonicalItemSink(
        canonical_item_saver,
        session_id=SESSION_ID,
        thread_id=THREAD_ID,
        checkpoint_ns=CHECKPOINT_NAMESPACE,
    )


@pytest.fixture
def reasoning_item() -> CanonicalItemRecord:
    return CanonicalItemRecord.create(
        item_sequence=1,
        item_id="item_1",
        semantic_kind="reasoning",
        payload_kind="text",
        status="completed",
        producer_ref={"producer_kind": "provider", "producer_id": "model_1"},
        payload="检查当前工作区",
        turn_id="turn_1",
        turn_scope="turn_member",
    )


def test_canonical_item_sink_requires_append_items_port() -> None:
    with pytest.raises(TypeError, match="必须绑定 Saver append_items"):
        CanonicalItemSink(
            object(),
            session_id=SESSION_ID,
            thread_id=THREAD_ID,
            checkpoint_ns="",
        )


def test_canonical_item_sink_requires_tool_result_commit_port() -> None:
    append_only_saver = MagicMock(spec=["append_items"])
    with pytest.raises(TypeError, match="必须绑定 Saver commit_tool_result"):
        CanonicalItemSink(
            append_only_saver,
            session_id=SESSION_ID,
            thread_id=THREAD_ID,
            checkpoint_ns="",
        )


@pytest.mark.asyncio
async def test_canonical_item_sink_rejects_invalid_items_before_writing(
    canonical_item_sink: CanonicalItemSink,
    canonical_item_saver: MagicMock,
) -> None:
    with pytest.raises(TypeError, match="非法 item"):
        await canonical_item_sink.append([object()])
    canonical_item_saver.append_items.assert_not_called()


@pytest.mark.asyncio
async def test_canonical_item_sink_forwards_original_items_without_retaining_a_copy(
    canonical_item_sink: CanonicalItemSink,
    canonical_item_saver: MagicMock,
    reasoning_item: CanonicalItemRecord,
) -> None:
    await canonical_item_sink.append([reasoning_item])
    canonical_item_saver.append_items.assert_called_once_with(
        SESSION_ID,
        (reasoning_item,),
        thread_id=THREAD_ID,
        checkpoint_ns=CHECKPOINT_NAMESPACE,
    )
    assert not hasattr(canonical_item_sink, "items")


@pytest.mark.asyncio
async def test_canonical_item_sink_propagates_append_failure(
    canonical_item_sink: CanonicalItemSink,
    canonical_item_saver: MagicMock,
    reasoning_item: CanonicalItemRecord,
) -> None:
    canonical_item_saver.append_items.side_effect = OSError("追加失败")
    with pytest.raises(OSError, match="追加失败"):
        await canonical_item_sink.append([reasoning_item])


@dataclass
class CommitObservation:
    """用有限同步屏障验证真实异步入口，不依赖写盘速度或长时间 sleep。"""

    blocked_phase: Literal["commit", "observer"] = "commit"
    failure_phase: Literal["commit", "observer"] | None = None
    error: OSError = field(default_factory=lambda: OSError("原写盘失败"))
    entered: Event = field(default_factory=Event)
    release: Event = field(default_factory=Event)
    phases: list[str] = field(default_factory=list)
    threads: list[int] = field(default_factory=list)
    admissions: list[ExecutionAdmission] = field(default_factory=list)

    def hit(self, phase: Literal["commit", "observer"]) -> None:
        self.phases.append(phase)
        self.threads.append(get_ident())
        self.admissions.append(require_current_execution_admission(langgraph_thread_id=THREAD_ID))
        if phase == self.blocked_phase:
            self.entered.set()
            assert self.release.wait(3), "原 worker 未被测试释放；事件循环可能被同步回调阻塞"
        if phase == self.failure_phase:
            raise self.error

    async def wait_entered(self) -> None:
        async with asyncio.timeout(2):
            while not self.entered.is_set():
                await asyncio.sleep(0)


@pytest.fixture
def commit_observation() -> Iterator[CommitObservation]:
    observation = CommitObservation()
    try:
        yield observation
    finally:
        observation.release.set()


@dataclass(frozen=True)
class SinkCommitCase:
    entry: str
    saver: MagicMock
    observer: MagicMock
    sink: CanonicalItemSink
    item: CanonicalItemRecord
    admission: ExecutionAdmission
    completion: ToolResultCompletion

    async def invoke(self) -> CanonicalItemRecord | None:
        with (
            session_thread_owner_scope(MutationIntentOwner(SESSION_ID, THREAD_ID), langgraph_thread_id=THREAD_ID),
            execution_admission_scope(self.admission, langgraph_thread_id=THREAD_ID),
        ):
            if self.entry == "append":
                await self.sink.append((self.item,))
                return None
            return await self.sink.complete_tool_result(self.completion)


@pytest.fixture(params=["append", "complete"])
def sink_commit_case(
    request: pytest.FixtureRequest,
    canonical_item_saver: MagicMock,
    commit_observation: CommitObservation,
) -> SinkCommitCase:
    completion = ToolResultCompletion(
        model_call_id="model-1", tool_call_id="model-1:tool-call:call-1",
        tool_invocation_id="invocation-1", tool_attempt_id="attempt-1",
        tool_name="invoke_extension_tool", content="真实结果", tool_outcome="unknown",
    )
    item = CanonicalItemRecord.create(
        item_sequence=1, item_id=completion.item_id, semantic_kind="tool_result",
        payload_kind="tool_result", status="completed",
        producer_ref={"producer_kind": "tool", "producer_id": "attempt-1"},
        payload={"tool_call_id": completion.tool_call_id, "result_id": "attempt-1",
                 "name": completion.tool_name, "content": completion.content,
                 "tool_outcome": completion.tool_outcome, "execution_confirmed": False},
        projection_identity=ItemProjectionIdentity(model_call_id=completion.model_call_id), wire_role="tool",
    )

    def commit(*_args: object, **_kwargs: object) -> CanonicalItemRecord:
        commit_observation.hit("commit")
        return item

    def observe(session: str, thread: str, items: tuple[CanonicalItemRecord, ...]) -> None:
        assert (session, thread, items) == (SESSION_ID, THREAD_ID, (item,))
        assert commit_observation.phases == ["commit"]
        commit_observation.hit("observer")

    canonical_item_saver.append_items.side_effect = commit
    canonical_item_saver.commit_tool_result.side_effect = commit
    observer = MagicMock(side_effect=observe)
    return SinkCommitCase(
        entry=request.param, saver=canonical_item_saver, observer=observer,
        sink=CanonicalItemSink(
            canonical_item_saver, session_id=SESSION_ID, thread_id=THREAD_ID,
            checkpoint_ns=CHECKPOINT_NAMESPACE, committed_item_observer=observer,
        ),
        item=item, completion=completion,
        admission=ExecutionAdmission(SESSION_ID, THREAD_ID, "execution-1", "job-1", "admission-1", "user_turn", "turn-1"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["commit", "observer"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_commit_and_observer_keep_context_and_settle_before_cancellation(
    sink_commit_case: SinkCommitCase, commit_observation: CommitObservation,
    phase: Literal["commit", "observer"], cancel: bool,
) -> None:
    commit_observation.blocked_phase = phase
    task = asyncio.create_task(sink_commit_case.invoke())
    try:
        await commit_observation.wait_entered()
        assert not task.done()
        if cancel:
            task.cancel("第一次取消")
            await asyncio.sleep(0)
            task.cancel("再次取消")
            await asyncio.sleep(0)
            assert not task.done(), "取消不能在原 worker 结算前归还调用方准入"
        commit_observation.release.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError, match="第一次取消"):
                await task
        else:
            result = await task
            assert result is (sink_commit_case.item if sink_commit_case.entry == "complete" else None)
    finally:
        commit_observation.release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert commit_observation.phases == ["commit", "observer"]
    assert len(set(commit_observation.threads)) == 1
    assert commit_observation.threads[0] != get_ident()
    assert commit_observation.admissions == [sink_commit_case.admission] * 2
    sink_commit_case.observer.assert_called_once()
    if sink_commit_case.entry == "append":
        sink_commit_case.saver.append_items.assert_called_once_with(
            SESSION_ID, (sink_commit_case.item,), thread_id=THREAD_ID, checkpoint_ns=CHECKPOINT_NAMESPACE,
        )
        sink_commit_case.saver.commit_tool_result.assert_not_called()
    else:
        sink_commit_case.saver.commit_tool_result.assert_called_once_with(
            session_id=SESSION_ID, thread_id=THREAD_ID, job_id="job-1",
            completion=sink_commit_case.completion, checkpoint_ns=CHECKPOINT_NAMESPACE, recovery=True,
        )
        sink_commit_case.saver.append_items.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["commit", "observer"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_commit_and_observer_preserve_original_late_failure(
    sink_commit_case: SinkCommitCase, commit_observation: CommitObservation,
    phase: Literal["commit", "observer"], cancel: bool,
) -> None:
    commit_observation.blocked_phase = commit_observation.failure_phase = phase
    task = asyncio.create_task(sink_commit_case.invoke())
    try:
        await commit_observation.wait_entered()
        if cancel:
            task.cancel("写入期间取消")
            await asyncio.sleep(0)
            assert not task.done()
        commit_observation.release.set()
        if phase == "observer":
            with pytest.raises(CanonicalItemObserverError) as observer_failure:
                await task
            observed_error = observer_failure.value
            assert observed_error.__cause__ is commit_observation.error
            assert observed_error.committed_item_ids == (sink_commit_case.item.item_id,)
        else:
            with pytest.raises(OSError) as commit_failure:
                await task
            observed_error = commit_failure.value
            assert observed_error is commit_observation.error
            sink_commit_case.observer.assert_not_called()
        if cancel:
            assert isinstance(observed_error.__context__, asyncio.CancelledError)
    finally:
        commit_observation.release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert commit_observation.phases == (["commit", "observer"] if phase == "observer" else ["commit"])
