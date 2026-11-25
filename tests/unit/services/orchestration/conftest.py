"""消息流编排单测共用符合真实端口合同的 writer 与结果通知。"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def stream_writer():
    """保留调用 spy，同时遵守 writer.commit 返回已提交事件的合同。"""
    def create(*, turn_stream_id: str = "stream_test") -> SimpleNamespace:
        sequence = 0

        async def commit(event_type, payload, **identity):
            nonlocal sequence
            sequence += 1
            return {
                "type": event_type, "payload": payload, **identity,
                "session_id": "ses_test", "thread_id": "thr_stream",
                "turn_stream_id": turn_stream_id, "event_seq": sequence,
                "event_id": f"event-{sequence}", "emitted_at": datetime.now(UTC).isoformat(),
            }

        return SimpleNamespace(commit=AsyncMock(side_effect=commit), turn_stream_id=turn_stream_id)

    return create


@pytest.fixture
def tool_result_sink() -> AsyncMock:
    """只镜像终态通知；真实 canonical 序列化仍由 Saver 集成测试覆盖。"""
    return AsyncMock(wraps=lambda completion: SimpleNamespace(
        tool_execution=completion.tool_execution,
        payload={"content": completion.content, "tool_outcome": completion.tool_outcome},
    ))
