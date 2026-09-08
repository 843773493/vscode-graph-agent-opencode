"""验证 canonical sink 仅路由 Saver 端口并透明传播失败。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.domain.itemized.records import CanonicalItemRecord
from app.services.orchestration.execution_step.stream_bindings import CanonicalItemSink


@pytest.fixture
def saver() -> MagicMock:
    return MagicMock(spec=["append_items"])


@pytest.fixture
def sink(saver: MagicMock) -> CanonicalItemSink:
    return CanonicalItemSink(saver, session_id="session_1", checkpoint_ns="agent")


@pytest.fixture
def item() -> CanonicalItemRecord:
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


def test_sink_requires_persistence_port() -> None:
    with pytest.raises(TypeError, match="必须绑定 Saver append_items"):
        CanonicalItemSink(object(), session_id="session_1", checkpoint_ns="")


@pytest.mark.asyncio
async def test_sink_rejects_invalid_items_before_writing(sink: CanonicalItemSink, saver: MagicMock) -> None:
    with pytest.raises(TypeError, match="非法 item"):
        await sink.append([object()])
    saver.append_items.assert_not_called()


@pytest.mark.asyncio
async def test_sink_routes_original_items_without_retaining_a_copy(
    sink: CanonicalItemSink, saver: MagicMock, item: CanonicalItemRecord
) -> None:
    await sink.append([item])
    saver.append_items.assert_called_once_with("session_1", (item,), checkpoint_ns="agent")
    assert not hasattr(sink, "items")


@pytest.mark.asyncio
async def test_sink_propagates_commit_failure(
    sink: CanonicalItemSink, saver: MagicMock, item: CanonicalItemRecord
) -> None:
    saver.append_items.side_effect = OSError("提交失败")
    with pytest.raises(OSError, match="提交失败"):
        await sink.append([item])
