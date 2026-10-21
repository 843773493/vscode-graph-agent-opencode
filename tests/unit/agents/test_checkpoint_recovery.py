"""原工具 checkpoint 的完整 carrier 与公开 scheduler 恢复边界。"""

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.agents.checkpoint_recovery import (
    pending_tool_assistant,
    pending_tool_checkpoint,
    persisted_pending_tool_candidate,
)
from app.domain.itemized.execution_admission import ExecutionAdmission
from app.domain.itemized.runtime import ModelCallRecord
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)


@pytest.fixture
def recovery_case():
    admission = ExecutionAdmission("session", "thread", "execution", "job", "admission", "user_turn", "turn")
    codec = LangChainMessageCodec()
    raw = AIMessage(content="执行冻结通信。", id="carrier", response_metadata={"model_call_id": "model"}, tool_calls=[
        {"id": f"call-{index}", "name": name, "args": {}, "type": "tool_call"}
        for index, name in enumerate(("send_message_to_session", "wait_for_session"), start=1)
    ])
    items = tuple(codec.items_for_message(raw, item_sequence=1, message_id="carrier", turn_id="turn", execution_id="execution", timestamp="2026-10-09T00:00:00+00:00", model_call_id="model"))
    assistant = codec.from_dict(codec.project_message(items))
    assert isinstance(assistant, AIMessage)
    model = ModelCallRecord("session", "thread", "model", "execution", 1, "fixture")
    config = {"configurable": {"thread_id": "thread", "checkpoint_id": "checkpoint"}}
    reads = []

    class Checkpointer:
        registered = (model, "assembly")

        def read_resumable_model_call(self, owner, model_call_id, *, carrier_items):
            reads.append((owner, model_call_id, carrier_items))
            return self.registered

        async def aget_tuple(self, query):
            messages = [assistant] if query["configurable"].get("checkpoint_id") else [assistant, HumanMessage("队列中的新输入")]
            return SimpleNamespace(config=config, checkpoint={"channel_values": {"messages": messages}, "channel_versions": {"messages": "1"}})

    class Agent:
        snapshot = SimpleNamespace(next=("tools",), tasks=(), values={"messages": [assistant]}, config=config)

        async def aget_state(self, _config):
            return self.snapshot

    return SimpleNamespace(admission=admission, assistant=assistant, items=items, checkpointer=Checkpointer(), agent=Agent(), reads=reads)


@pytest.mark.asyncio
async def test_candidate_excludes_queued_root_and_preserves_complete_group(recovery_case):
    case = recovery_case
    pending = await persisted_pending_tool_candidate(case.checkpointer, case.admission)
    assert pending.checkpoint_id == "checkpoint"
    assert pending.assistant == case.assistant
    owner, model_call_id, members = case.reads[0]
    assert owner == case.admission and model_call_id == "model"
    assert [(item.item_id, item.content_hash, item.projection_group) for item in members] == [
        (item.item_id, item.content_hash, item.projection_group) for item in case.items
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("nodes", [(), ("model",), ("tools", "model")])
async def test_only_public_tools_scheduler_can_continue(recovery_case, nodes):
    case = recovery_case
    case.agent.snapshot.next = nodes
    assert await pending_tool_checkpoint(case.agent, case.checkpointer, case.admission) is None
    assert not case.reads


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupts,error", [((), "失败"), (("显式暂停",), None)])
async def test_scheduler_failure_or_interrupt_is_not_auto_resume(recovery_case, interrupts, error):
    case = recovery_case
    case.agent.snapshot.tasks = (SimpleNamespace(interrupts=interrupts, error=error),)
    assert await pending_tool_checkpoint(case.agent, case.checkpointer, case.admission) is None
    assert not case.reads


@pytest.mark.parametrize("name,args", [("run_command", {}), ("invoke_extension_tool", {"tool_name": "write_file", "arguments": {}})])
def test_one_unknown_effect_rejects_the_whole_batch(recovery_case, name, args):
    assistant = recovery_case.assistant.model_copy(deep=True)
    assistant.tool_calls.append({"id": "unsafe", "name": name, "args": args, "type": "tool_call"})
    assert pending_tool_assistant([assistant]) is None


@pytest.mark.asyncio
async def test_terminal_or_uncommitted_carrier_is_not_recovered(recovery_case):
    case = recovery_case
    case.checkpointer.registered = None
    assert await pending_tool_checkpoint(case.agent, case.checkpointer, case.admission) is None
