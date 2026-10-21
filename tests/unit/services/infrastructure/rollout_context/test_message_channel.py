"""消息 Delta 控制输入与官方 reducer 的身份、顺序合同。"""

import pytest
from langchain_core.messages import AIMessage, RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES, add_messages

from app.services.infrastructure.rollout_context.checkpoint.codec.message_channel import (
    MessagesDeltaChannel,
    replay_message_values,
)


@pytest.mark.parametrize("writes", [
    [[RemoveMessage(id="a"), AIMessage(id="a", content="同批替换")]],
    [[RemoveMessage(id="a"), RemoveMessage(id="a")]],
    [[RemoveMessage(id="a")], [AIMessage(id="a", content="后批新增")]],
    [[RemoveMessage(id=REMOVE_ALL_MESSAGES), AIMessage(id="c", content="清空后新增")]],
])
def test_control_batches_preserve_official_message_identity_and_order(writes):
    initial = [AIMessage(id="a", content="旧 a"), AIMessage(id="b", content="旧 b")]
    expected = initial
    for batch in writes:
        expected = add_messages(expected, batch)
    actual = replay_message_values(initial, writes)
    assert [message.model_dump() for message in actual] == [message.model_dump() for message in expected]


def test_cold_replay_rejects_message_without_persisted_id():
    channel = MessagesDeltaChannel().from_checkpoint([])
    with pytest.raises(ValueError, match="稳定 ID"):
        channel.replay_writes([("task", "messages", [AIMessage(content="损坏的写入")])])
