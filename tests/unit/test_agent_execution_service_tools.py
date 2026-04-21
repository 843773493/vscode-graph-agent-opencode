from __future__ import annotations

from pathlib import Path
from datetime import datetime, timedelta

import pytest

from app.core.background_message_bus import BackgroundMessageBus
from app.core.background_task_registry import BackgroundTaskRegistry
from app.core.job_event_bus import EventType, JobEventBus
from app.schemas.job import EventDTO
from app.services.agent_execution_service import AgentExecutionService


class _DummyConfigService:
    def get_llm_providers(self):
        return [
            {
                "model": "dummy-model",
                "api_key": "dummy-key",
                "endpoint": "http://localhost:1234",
            }
        ]


class _DummyAgent:
    def get_graph(self):
        class _DummyGraph:
            nodes = {}

        return _DummyGraph()


@pytest.mark.asyncio
async def test_agent_includes_background_message_collection_tool(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(AgentExecutionService, "_instance", None)
    monkeypatch.setattr(BackgroundMessageBus, "_instance", None)
    monkeypatch.setattr(
        "app.services.agent_execution_service.ConfigService.get_instance",
        lambda: _DummyConfigService(),
    )

    captured = {}

    def fake_create_deep_agent(**kwargs):
        captured.update(kwargs)
        return _DummyAgent()

    monkeypatch.setattr("app.services.agent_execution_service.create_deep_agent", fake_create_deep_agent)

    service = AgentExecutionService.get_instance()
    service._get_or_create_agent("session_test")

    tool_names = [tool.name for tool in captured["tools"]]
    assert "python_exec" in tool_names
    assert "emit_system_time_messages" in tool_names
    assert "monitor_session_agent_end" in tool_names
    assert "collect_background_messages" in tool_names


@pytest.mark.asyncio
async def test_emit_system_time_messages_tool_emits_periodic_messages(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(AgentExecutionService, "_instance", None)
    monkeypatch.setattr(BackgroundMessageBus, "_instance", None)
    monkeypatch.setattr(
        "app.services.agent_execution_service.ConfigService.get_instance",
        lambda: _DummyConfigService(),
    )

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr("app.services.agent_execution_service.asyncio.sleep", fake_sleep)

    service = AgentExecutionService.get_instance()
    tool = service._create_system_time_emitter_tool("session_test")

    result = await tool.ainvoke({"interval_seconds": 0.01, "message_count": 3, "source_id": "clock-stream"})

    assert result["message_count"] == 3
    assert result["interval_seconds"] == 0.01
    assert result["source_id"] == "clock-stream"

    messages = await BackgroundMessageBus.get_instance().list_messages(
        "session_test",
        "deep_agent",
        source_id="clock-stream",
    )
    assert len(messages) == 3
    assert all(message.content for message in messages)


@pytest.mark.asyncio
async def test_monitor_session_agent_end_tool_emits_interrupt_message(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(AgentExecutionService, "_instance", None)
    monkeypatch.setattr(BackgroundMessageBus, "_instance", None)
    monkeypatch.setattr(JobEventBus, "_instance", None)
    monkeypatch.setattr(
        "app.services.agent_execution_service.ConfigService.get_instance",
        lambda: _DummyConfigService(),
    )
    monkeypatch.setattr(BackgroundTaskRegistry, "_instance", None)

    emitted_messages = []

    class _FakeBackgroundMessageBus:
        def emit(self, session_id, agent_id, content, *, kind, source_id=None, payload=None, message_id=None):
            emitted_messages.append(
                {
                    "session_id": session_id,
                    "agent_id": agent_id,
                    "content": content,
                    "kind": kind,
                    "source_id": source_id,
                    "payload": payload,
                }
            )

            class _FakeMessage:
                def model_dump(self, mode="json"):
                    return emitted_messages[-1]

            return _FakeMessage()

    future_event = EventDTO(
        event_id="evt_future_1",
        job_id="target_session",
        step_id=None,
        type=EventType.AGENT_END,
        agent_id="deep_agent",
        payload={"final_text": "橙子"},
        timestamp=datetime.now() + timedelta(days=1),
    )

    class _FakeJobEventBus:
        async def list_events(self, job_id, after=None, limit=100):
            assert job_id == "target_session"
            return [future_event]

    monkeypatch.setattr(
        "app.services.agent_execution_service.BackgroundMessageBus.get_instance",
        lambda: _FakeBackgroundMessageBus(),
    )
    monkeypatch.setattr(
        "app.services.agent_execution_service.JobEventBus.get_instance",
        lambda: _FakeJobEventBus(),
    )

    service = AgentExecutionService.get_instance()
    tool = service._create_monitor_session_agent_end_tool("monitor_session")

    result = await tool.ainvoke({"target_session_id": "target_session", "timeout_seconds": 1, "poll_interval_seconds": 0.01})

    assert result["status"] == "pending"
    assert result["task_name"] == "monitor_session_agent_end"
    assert result["metadata"]["target_session_id"] == "target_session"

    task = BackgroundTaskRegistry.get_instance().get_task("monitor_session", result["task_id"])
    assert task is not None

    await task

    assert emitted_messages, "监控工具没有写入任何后台消息"
    assert emitted_messages[0]["kind"].value == "interrupt"
    assert emitted_messages[0]["content"] == "橙子"
    assert emitted_messages[0]["payload"]["final_text"] == "橙子"
