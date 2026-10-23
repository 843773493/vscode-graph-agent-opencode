"""验证 canonical sink 路由 Saver 的追加与工具终态端口。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.domain.itemized.records import CanonicalItemRecord
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
