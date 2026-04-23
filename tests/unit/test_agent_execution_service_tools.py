from __future__ import annotations

from pathlib import Path
from datetime import datetime, timedelta

import pytest

from app.core.background_message_bus import BackgroundMessageBus
from app.core.background_task_registry import BackgroundTaskRegistry
from app.core.job_event_bus import EventType, JobEventBus
from app.schemas.job import EventDTO
from app.agents.agent_tools import (
    create_system_time_emitter_tool,
    create_monitor_session_agent_end_tool,
    create_send_message_to_session_tool,
)
from app.services.agent_execution_service import AgentExecutionService


class _DummyConfigService:
    def get_llm_providers(self):
        return [
            {
                "id": "primary",
                "model": "dummy-model",
                "api_key": "dummy-key",
                "endpoint": "http://localhost:1234",
                "interface": "chat.completion",
            }
        ]

    def get_default_agent_runtime_config(self):
        return {
            "system_prompt": "dummy system prompt",
            "providers": self.get_llm_providers(),
            "temperature": 0.2,
            "top_p": 1,
            "max_output_tokens": 4000,
        }

    def get_default_agent_id(self):
        return "default"

    def resolve_agent_id(self, agent_id):
        return agent_id or "default"

    def get_agent_runtime_config(self, agent_id=None):
        return self.get_default_agent_runtime_config()

    def get_agent_tool_config(self, agent_id=None):
        return {
            "denylist": [],
            "confirmation_required": [],
        }


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
        "app.agents.agent_factory.ConfigService.get_instance",
        lambda: _DummyConfigService(),
    )

    captured = {}

    def fake_create_agent(*args, **kwargs):
        captured.update(kwargs)
        return _DummyAgent()

    monkeypatch.setattr("app.agents.agent_factory.create_agent", fake_create_agent)

    service = AgentExecutionService.get_instance()
    service._get_or_create_agent("session_test")

    tool_names = [tool.name for tool in captured["tools"]]
    assert "python_exec" in tool_names
    assert "emit_system_time_messages" in tool_names
    assert "monitor_session_agent_end" in tool_names
    assert "collect_background_messages" in tool_names
    assert "send_message_to_session" in tool_names
    assert captured["system_prompt"].startswith("dummy system prompt")


@pytest.mark.asyncio
async def test_agent_tool_denylist_filters_direct_and_middleware_tools(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(AgentExecutionService, "_instance", None)
    monkeypatch.setattr(BackgroundMessageBus, "_instance", None)

    class _DenylistConfigService(_DummyConfigService):
        def get_agent_tool_config(self, agent_id=None):
            return {
                "denylist": ["send_message_to_session", "edit_file"],
                "confirmation_required": [],
            }

    monkeypatch.setattr(
        "app.agents.agent_factory.ConfigService.get_instance",
        lambda: _DenylistConfigService(),
    )

    captured = {}

    def fake_create_agent(*args, **kwargs):
        captured.update(kwargs)
        return _DummyAgent()

    monkeypatch.setattr("app.agents.agent_factory.create_agent", fake_create_agent)

    service = AgentExecutionService.get_instance()
    service._get_or_create_agent("session_denylist")

    direct_tool_names = [tool.name for tool in captured["tools"]]
    assert "send_message_to_session" not in direct_tool_names
    assert "python_exec" in direct_tool_names

    middleware_tool_names = []
    for middleware in captured["middleware"]:
        tools = getattr(middleware, "tools", None)
        if not tools:
            continue
        middleware_tool_names.extend(getattr(tool, "name", "") for tool in tools)

    assert "edit_file" not in middleware_tool_names
    assert "send_message_to_session" not in middleware_tool_names


@pytest.mark.asyncio
async def test_emit_system_time_messages_tool_emits_periodic_messages(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(AgentExecutionService, "_instance", None)
    monkeypatch.setattr(BackgroundMessageBus, "_instance", None)
    monkeypatch.setattr(
        "app.agents.agent_factory.ConfigService.get_instance",
        lambda: _DummyConfigService(),
    )

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr("app.agents.agent_tools.asyncio.sleep", fake_sleep)

    tool = create_system_time_emitter_tool("session_test")

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
        "app.agents.agent_factory.ConfigService.get_instance",
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
        job_id="job_target_1",
        step_id=None,
        type=EventType.AGENT_END,
        agent_id="deep_agent",
        payload={"final_text": "橙子"},
        timestamp=datetime.now() + timedelta(days=1),
    )

    class _FakeJobEventBus:
        async def list_events(self, job_id, after=None, limit=100):
            assert job_id == "job_target_1"
            return [future_event]

    class _FakeTargetJob:
        job_id = "job_target_1"
        created_at = datetime.now()

    class _FakeJobService:
        async def list(self, session_id=None):
            assert session_id == "target_session"
            return [_FakeTargetJob()]

    monkeypatch.setattr(
        "app.agents.agent_tools.BackgroundMessageBus.get_instance",
        lambda: _FakeBackgroundMessageBus(),
    )
    monkeypatch.setattr(
        "app.agents.agent_tools.JobEventBus.get_instance",
        lambda: _FakeJobEventBus(),
    )
    monkeypatch.setattr(
        "app.services.job_service.JobService.get_instance",
        lambda: _FakeJobService(),
    )

    tool = create_monitor_session_agent_end_tool("monitor_session")

    result = await tool.ainvoke(
        {
            "target_session_id": "target_session",
            "timeout_seconds": 1,
            "poll_interval_seconds": 0.01,
            "max_events": 1,
        }
    )

    assert result["status"] == "pending"
    assert result["task_name"] == "monitor_session_agent_end"
    assert result["metadata"]["target_session_id"] == "target_session"
    assert result["metadata"]["max_events"] == 1
    assert result["metadata"]["source_id"].startswith("monitor:target_session:")

    task = BackgroundTaskRegistry.get_instance().get_task("monitor_session", result["task_id"])
    assert task is not None

    await task

    assert emitted_messages, "监控工具没有写入任何后台消息"
    assert emitted_messages[0]["kind"].value == "interrupt"
    assert emitted_messages[0]["content"] == "橙子"
    assert emitted_messages[0]["payload"]["final_text"] == "橙子"


@pytest.mark.asyncio
async def test_send_message_to_session_tool_creates_job(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(AgentExecutionService, "_instance", None)
    monkeypatch.setattr(BackgroundMessageBus, "_instance", None)
    monkeypatch.setattr(
        "app.agents.agent_factory.ConfigService.get_instance",
        lambda: _DummyConfigService(),
    )

    captured = {}

    class _FakeResult:
        def model_dump(self, mode="json"):
            return {
                "message_id": "msg_test",
                "job_id": "job_test",
                "status": "accepted",
            }

    class _FakeMessageService:
        async def create_and_run(self, session_id, run_request):
            captured["session_id"] = session_id
            captured["content"] = run_request.message.content
            captured["role"] = run_request.message.role
            captured["agent_id"] = run_request.run.agent_id
            return _FakeResult()

    monkeypatch.setattr(
        "app.agents.agent_tools.MessageService.get_instance",
        lambda: _FakeMessageService(),
    )

    tool = create_send_message_to_session_tool()

    result = await tool.ainvoke({"target_session_id": "ses_target", "content": "请再次只重复前面的话"})

    assert captured["session_id"] == "ses_target"
    assert captured["content"] == "请再次只重复前面的话"
    assert captured["role"] == "user"
    assert captured["agent_id"] == "deep_agent"
    assert result["job_id"] == "job_test"
