"""LangGraph 控制槽更新、普通写入冲突和冷读取合同。"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import WRITES_IDX_MAP, Checkpoint
from langgraph.types import Interrupt

from app.core.checkpoint_config import build_checkpoint_config
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.integration.backend.sessions.rollout_context.test_rollout_checkpoint_saver import (
    SESSION_ID,
    _accept_user_execution,
    _checkpoint,
    _execution_scope,
)


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_control_slots_survive_interrupt_resume_and_cold_read(
    tmp_path: Path, session_bundle_factory, asynchronous: bool,
) -> None:
    sessions = tmp_path / "sessions"
    session_bundle_factory(sessions, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions)
    thread = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id
    user = HumanMessage(content="审批控制槽", id="control-input")
    admission = _accept_user_execution(saver, SESSION_ID, thread, user, turn_id="control-turn")
    with _execution_scope(admission):
        config = saver.put(cast(RunnableConfig, build_checkpoint_config(thread)),
                           cast(Checkpoint, _checkpoint("control-cp", [user], counter=0)),
                           {"source": "loop", "step": 0, "parents": {}}, {"messages": "control-cp", "counter": "control-cp"})

        async def write(values):
            if asynchronous:
                await saver.aput_writes(config, values, "approval-task", "approval-node")
            else:
                saver.put_writes(config, values, "approval-task", "approval-node")

        await write([("__interrupt__", (Interrupt(value={"question": "确认?"}, id="approval-id"),))])
        await write([("__resume__", [{"decisions": [{"type": "approve"}]}])])
        await write([("__resume__", [{"decisions": [{"type": "reject"}]}])])
        await write([("counter", 1)])
        await write([("counter", 1)])
        with pytest.raises(RuntimeError, match="pending_writes 重试内容不一致"):
            await write([("counter", 2)])
        with saver._storage.storage.read_connection(SESSION_ID, thread) as connection:
            rows = dict(connection.execute("SELECT channel,write_index FROM pending_writes WHERE checkpoint_id='control-cp'"))
        assert rows == {"__interrupt__": WRITES_IDX_MAP["__interrupt__"], "__resume__": WRITES_IDX_MAP["__resume__"], "counter": 0}
        original = saver.get_tuple(config)
        assert original is not None
        with RolloutCheckpointSaver(sessions) as cold:
            restored = await cold.aget_tuple(config) if asynchronous else cold.get_tuple(config)
            assert restored is not None and restored.pending_writes == original.pending_writes
            assert restored.pending_writes is not None
            values = {channel: value for _, channel, value in restored.pending_writes}
            assert values["counter"] == 1
            assert values["__resume__"] == [{"decisions": [{"type": "reject"}]}]
            assert values["__interrupt__"] == [Interrupt(value={"question": "确认?"}, id="approval-id")]
