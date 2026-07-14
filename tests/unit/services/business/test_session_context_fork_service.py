from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.core.checkpoint_config import build_checkpoint_config
from app.core.checkpoint_saver import FileSystemCheckpointSaver
from app.schemas.public_v2.session import SessionCreateRequest
from app.services.business.session_context_fork_service import (
    SessionContextForkService,
)
from app.services.business.session_service import SessionService
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.trace_event_store import TraceEventStore


@dataclass(frozen=True, slots=True)
class ForkServices:
    session_service: SessionService
    fork_service: SessionContextForkService
    checkpointer: FileSystemCheckpointSaver


@pytest.fixture
def fork_services(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ForkServices:
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    logs_dir = tmp_path / ".boxteam" / "logs"
    checkpointer = FileSystemCheckpointSaver(
        base_dir=tmp_path / ".boxteam" / "checkpoints"
    )
    session_service = SessionService(
        config_service=ConfigService(workspace_root=tmp_path),
        trace_event_store=TraceEventStore(logs_dir=logs_dir),
    )
    return ForkServices(
        session_service=session_service,
        fork_service=SessionContextForkService(
            session_service=session_service,
            checkpointer=checkpointer,
        ),
        checkpointer=checkpointer,
    )


@pytest.mark.asyncio
async def test_fork_copies_agent_state_into_independent_child(
    fork_services: ForkServices,
) -> None:
    source = await fork_services.session_service.create(
        SessionCreateRequest(title="源会话")
    )
    checkpoint = {
        "v": 1,
        "id": "checkpoint-source",
        "ts": "2026-07-13T00:00:00+00:00",
        "channel_values": {
            "messages": [
                HumanMessage(content="记住代号 alpha"),
                AIMessage(content="已记住"),
            ],
            "scratchpad": {"current_task": "继续验证 alpha"},
        },
        "channel_versions": {"messages": 1, "scratchpad": 1},
        "versions_seen": {},
        "pending_sends": [],
        "updated_channels": ["messages", "scratchpad"],
    }
    await fork_services.checkpointer.aput(
        build_checkpoint_config(source.session_id),
        checkpoint,
        {"source": "loop", "step": 2, "parents": {}},
        {"messages": 1, "scratchpad": 1},
    )

    child = await fork_services.fork_service.fork(source.session_id)

    assert child.parent_session_id == source.session_id
    assert child.current_agent_id == source.current_agent_id
    assert child.title == "源会话（上下文副本）"
    assert child.title_source == "auto"

    source_tuple = await fork_services.checkpointer.aget_tuple(
        build_checkpoint_config(source.session_id)
    )
    child_tuple = await fork_services.checkpointer.aget_tuple(
        build_checkpoint_config(child.session_id)
    )
    assert source_tuple is not None
    assert child_tuple is not None
    assert child_tuple.checkpoint["id"] != source_tuple.checkpoint["id"]
    source_values = source_tuple.checkpoint["channel_values"]
    child_values = child_tuple.checkpoint["channel_values"]
    assert child_values["scratchpad"] == source_values["scratchpad"]
    assert [message.content for message in child_values["messages"]] == [
        message.content for message in source_values["messages"]
    ]
    assert all(
        message.response_metadata["context_fork_source_session_id"]
        == source.session_id
        for message in child_values["messages"]
    )
    assert child_tuple.checkpoint.get("pending_sends") == []
    assert child_tuple.metadata["source"] == "fork"


@pytest.mark.asyncio
async def test_fork_empty_context_still_creates_bound_child(
    fork_services: ForkServices,
) -> None:
    source = await fork_services.session_service.create(
        SessionCreateRequest(title="空上下文")
    )

    child = await fork_services.fork_service.fork(source.session_id)

    assert child.parent_session_id == source.session_id
    assert await fork_services.checkpointer.aget_tuple(
        build_checkpoint_config(child.session_id)
    ) is None
