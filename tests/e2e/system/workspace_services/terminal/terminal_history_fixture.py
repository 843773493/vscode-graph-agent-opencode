"""持久终端历史回显场景专用的 checkpoint 输入构造。"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from langchain_core.messages import HumanMessage, ToolMessage

from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.core.identifier import create_prefixed_id
from app.core.path_utils import get_session_path_resolver
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)


def seed_historical_terminal_checkpoint(
    *,
    workspace_root: str,
    session_id: str,
    terminal_id: str,
) -> None:
    saver = RolloutCheckpointSaver(
        sessions_dir=Path(workspace_root) / ".boxteam" / "sessions"
    )
    terminal_payload = {
        "chunk_id": terminal_id,
        "session_id": terminal_id,
        "wall_time_seconds": 1.0,
        "output": "HISTORICAL_TERMINAL",
    }
    thread_id = get_session_path_resolver(Path(workspace_root) / ".boxteam/sessions").main_thread_id(session_id)
    message_id = create_prefixed_id("msg")
    messages_version = saver.get_next_version(None, None)
    checkpoint = {
        "id": str(uuid.uuid4()),
        "channel_values": {
            "messages": [
                HumanMessage(content="请启动一个历史终端", id=message_id),
                ToolMessage(
                    content=(
                        "Tool call exec_command with id call_cancelled "
                        "was cancelled - another message came in before it could be completed."
                    ),
                    tool_call_id="call_cancelled",
                    name="exec_command",
                ),
                ToolMessage(
                    content=json.dumps(terminal_payload),
                    tool_call_id="call_historical_terminal",
                    name="exec_command",
                ),
            ]
        },
        "channel_versions": {"messages": messages_version},
        "updated_channels": ["messages"],
    }
    with session_thread_owner_scope(MutationIntentOwner(session_id, thread_id), langgraph_thread_id=thread_id):
        accepted = saver.accept_turn(session_id, thread_id=thread_id, accepted_ingress_id=message_id, acceptance_idempotency_key=f"terminal-history:{message_id}", job_id=create_prefixed_id("job"), admission_idempotency_key=create_prefixed_id("adm"), payload="请启动一个历史终端", payload_kind="text", turn_id=create_prefixed_id("turn"), root_item_id=f"item-{message_id}")
        admission = saver.execution_admission(session_id, thread_id=thread_id, execution_id=accepted["execution_id"])
        with execution_admission_scope(admission, langgraph_thread_id=thread_id):
            saver.put(build_checkpoint_config(thread_id), checkpoint, metadata={"source": "e2e_seed", "step": -1, "writes": {}}, new_versions={"messages": messages_version})
