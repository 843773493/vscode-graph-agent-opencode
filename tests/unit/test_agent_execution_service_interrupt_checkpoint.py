from __future__ import annotations

from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage

from app.services.business.system_reminder_checkpoint_service import persist_interrupt_checkpoint


class FakeCheckpointer:
    def __init__(self, checkpoint: dict):
        self.checkpoint = checkpoint
        self.config = {"configurable": {"thread_id": "sess_interrupt"}}
        self.saved_checkpoint: dict | None = None
        self.saved_metadata: dict | None = None
        self.saved_new_versions: dict | None = None

    def get_tuple(self, config: dict):
        return SimpleNamespace(config=self.config, checkpoint=self.checkpoint)

    def get_next_version(self, current: object, channel: object) -> str:
        return "v2"

    def put(
        self,
        *,
        config: dict,
        checkpoint: dict,
        metadata: dict,
        new_versions: dict,
    ) -> None:
        self.saved_checkpoint = checkpoint
        self.saved_metadata = metadata
        self.saved_new_versions = new_versions


def test_persist_interrupt_checkpoint_uses_independent_human_reminder() -> None:
    checkpoint = {
        "channel_values": {
            "messages": [
                HumanMessage(content="第一轮用户请求"),
                AIMessage(content=""),
            ]
        },
        "channel_versions": {"messages": "v1"},
    }
    checkpointer = FakeCheckpointer(checkpoint)

    persist_interrupt_checkpoint(
        checkpointer=checkpointer,
        session_id="sess_interrupt",
        current_text="已经生成的部分回复",
        active_tool_name=None,
    )

    assert checkpointer.saved_checkpoint is not None
    messages = checkpointer.saved_checkpoint["channel_values"]["messages"]
    assert len(messages) == 3
    assert isinstance(messages[0], HumanMessage)
    assert isinstance(messages[1], AIMessage)
    assert isinstance(messages[2], HumanMessage)
    assert messages[1].content == "已经生成的部分回复"
    assert "<system_reminder>" not in messages[1].content
    assert "<system_reminder>" in messages[2].content
    assert "文本生成" in messages[2].content
    assert messages[2].response_metadata["source"] == "interrupt"
    assert checkpointer.saved_new_versions == {"messages": "v2"}


def test_persist_interrupt_checkpoint_does_not_append_empty_tool_assistant() -> None:
    checkpoint = {
        "channel_values": {
            "messages": [
                HumanMessage(content="调用工具"),
                AIMessage(content=""),
            ]
        },
        "channel_versions": {"messages": "v1"},
    }
    checkpointer = FakeCheckpointer(checkpoint)

    persist_interrupt_checkpoint(
        checkpointer=checkpointer,
        session_id="sess_interrupt",
        current_text="",
        active_tool_name="python_exec",
    )

    assert checkpointer.saved_checkpoint is not None
    messages = checkpointer.saved_checkpoint["channel_values"]["messages"]
    assert len(messages) == 2
    assert isinstance(messages[0], HumanMessage)
    assert isinstance(messages[1], HumanMessage)
    assert "<system_reminder>" in messages[1].content
    assert "python_exec" in messages[1].content
    assert messages[1].response_metadata["phase"] == "tool"
