from __future__ import annotations

import asyncio
from uuid import UUID

import pytest

from app.agents.tool_invocation_context import (
    ThreadRuntimeBinding,
    ToolInvocationContext,
)
from app.agents.tools.background import create_system_time_emitter_tool
from app.core.identifier import create_uuid_hex
from app.core.path_utils import get_session_path_resolver
from app.domain.itemized.identity.item_projection import ItemProjectionIdentity
from app.services.infrastructure.background_work_store import BackgroundWorkStore
from app.services.orchestration.background_work.mailbox import BackgroundMailbox
from tests.support.background_work import build_background_registry


@pytest.mark.asyncio
async def test_emit_system_time_messages_tool_emits_periodic_messages(
    monkeypatch,
    empty_workspace_scope,
    session_bundle_factory,
):
    workspace_root = empty_workspace_scope[1]
    sessions_dir = workspace_root / ".boxteam" / "sessions"
    session_bundle_factory(sessions_dir, "ses_019bca51019973818b63e9d44c0e94f5")
    background_mailbox = BackgroundMailbox(store=BackgroundWorkStore(sessions_dir=sessions_dir))
    background_task_registry = build_background_registry(sessions_dir)
    thread_id = get_session_path_resolver(sessions_dir).main_thread_id("ses_019bca51019973818b63e9d44c0e94f5")
    invocation = ToolInvocationContext(thread_binding=ThreadRuntimeBinding("ses_019bca51019973818b63e9d44c0e94f5", thread_id))
    invocation.set_tool_call_id("clock_call", projection_identity=ItemProjectionIdentity(
        projection_message_id="clock-assistant", model_call_id=str(UUID(hex=create_uuid_hex())),
    ))

    allow_emission = asyncio.Event()

    async def fake_sleep(_seconds):
        await allow_emission.wait()

    monkeypatch.setattr("app.services.orchestration.background_work.system_time_emitter.asyncio.sleep", fake_sleep)

    tool = create_system_time_emitter_tool(
        "ses_019bca51019973818b63e9d44c0e94f5",
        background_task_registry=background_task_registry,
        invocation_context=invocation,
    )

    result = await tool.ainvoke({"interval_seconds": 0.01, "message_count": 3, "source_id": "clock-stream"})

    assert result["task_name"] == "emit_system_time_messages"
    task = background_task_registry.get_task("ses_019bca51019973818b63e9d44c0e94f5", result["task_id"])
    assert task is not None
    allow_emission.set()
    await task

    handle = background_task_registry.get_handle("ses_019bca51019973818b63e9d44c0e94f5", result["task_id"])
    assert handle is not None
    assert handle.status == "completed"
    assert handle.metadata["message_count"] == 3
    assert handle.metadata["source_id"] == "clock-stream"
    assert isinstance(handle.metadata["result"], dict)

    messages = await background_mailbox.list_messages("ses_019bca51019973818b63e9d44c0e94f5", thread_id, "default")
    assert len(messages) == 3
    assert all(message.content for message in messages)
